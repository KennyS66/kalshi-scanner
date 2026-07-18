#!/usr/bin/env python3
"""Trade grader - post-settlement loss forensics for the swing paper bot.

Sidecar daemon (started by start.sh). Grades every closed trade in
data/bot/bot_trades.jsonl once its market has settled: settlement side,
counterfactual P&L vs holding, max favorable/adverse excursion during the
hold, a verdict (good_stop / whipsaw_stop / clean_win / lucky_exit /
good_exit / left_money), and the day's bias/regime context. Appends one
row per trade to data/bot/bot_trade_grades.jsonl. Never touches trading
code or rewrites existing files. Also snapshots signal_feature_log.jsonl
to data/whales/archive/<date>/ once per UTC day.

Spec: docs/superpowers/specs/2026-07-17-trade-grader-design.md
"""
import gzip
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).parent
BOT_DIR = BASE / "data" / "bot"
WHALES_DIR = BASE / "data" / "whales"
TRADES_PATH = BOT_DIR / "bot_trades.jsonl"
GRADES_PATH = BOT_DIR / "bot_trade_grades.jsonl"
FEATURES_PATH = WHALES_DIR / "signal_feature_log.jsonl"
THESIS_PATH = WHALES_DIR / "daily_thesis.jsonl"
REGIME_PATH = WHALES_DIR / "intraday_regime.jsonl"
ARCHIVE_DIR = WHALES_DIR / "archive"

POLL_SEC = 60
SETTLE_WINDOW_S = 120   # a tick this close to expiry supports strike-basis settlement
GRADE_DELAY_S = 90      # wait this long past expiry so final ticks are on disk
PRICE_DECIDED_HI = 0.95
PRICE_DECIDED_LO = 0.05


def expiry_of(trade):
    return trade["entry_ts"] + trade["entry_sig"]["mins_left"] * 60.0


def verdict_for(exit_reason, side, settled):
    if settled == "unknown":
        return "ungraded"
    favorable = settled == side
    if exit_reason == "stop":
        return "whipsaw_stop" if favorable else "good_stop"
    if exit_reason == "target":
        return "clean_win" if favorable else "lucky_exit"
    return "left_money" if favorable else "good_exit"


def infer_settlement(ticks, expiry):
    before = [t for t in ticks if t.get("ts") is not None and t["ts"] <= expiry]
    if not before:
        return "unknown", "none"
    last = before[-1]
    spot, strike = last.get("spot"), last.get("floor_strike")
    if last["ts"] >= expiry - SETTLE_WINDOW_S and spot is not None and strike is not None:
        return ("YES" if spot >= strike else "NO"), "strike"
    price = last.get("price")
    if price is not None:
        if price > PRICE_DECIDED_HI:
            return "YES", "price"
        if price < PRICE_DECIDED_LO:
            return "NO", "price"
    return "unknown", "none"


def hold_path_stats(ticks, side, entry_price, entry_ts, exit_ts):
    prices = [t["price"] for t in ticks
              if t.get("price") is not None
              and t.get("ts") is not None and entry_ts <= t["ts"] <= exit_ts]
    if not prices:
        return None, None
    if side == "NO":
        prices = [1.0 - p for p in prices]
    mfe = round(max(prices) - entry_price, 4)
    mae = round(entry_price - min(prices), 4)
    return mfe, mae


def trade_key(row):
    return f"{row['ticker']}|{row['entry_ts']}"


def day_context(entry_ts, thesis_rows, regime_rows):
    date = time.strftime("%Y-%m-%d", time.gmtime(entry_ts))
    ctx = {"day_bias": None, "day_key": None, "day_conviction": None,
           "regime": "none", "regime_lo": None, "regime_hi": None}
    for row in thesis_rows:
        if row.get("date") == date:
            ctx["day_bias"] = row.get("bias")
            try:
                ctx["day_key"] = float(row.get("level"))
            except (TypeError, ValueError):
                ctx["day_key"] = None
            ctx["day_conviction"] = row.get("conviction")
    latest = None
    for row in regime_rows:
        ts = row.get("ts")
        if ts is not None and ts <= entry_ts and (latest is None or ts > latest["ts"]):
            latest = row
    if latest is not None:
        ctx["regime"] = latest.get("regime", "none")
        ctx["regime_lo"] = latest.get("range_lo")
        ctx["regime_hi"] = latest.get("range_hi")
    return ctx


def grade_trade(trade, ticks, thesis_rows, regime_rows):
    side, qty = trade["side"], trade["qty"]
    entry, exit_ = trade["entry_price"], trade["exit_price"]
    settled, basis = infer_settlement(ticks, expiry_of(trade))
    mfe, mae = hold_path_stats(ticks, side, entry,
                               trade["entry_ts"], trade["exit_ts"])
    if settled == "unknown":
        held = delta = None
    else:
        payout = 1.0 if settled == side else 0.0
        held = round(qty * (payout - entry), 2)
        delta = round(qty * (exit_ - entry) - held, 2)
    ctx = day_context(trade["entry_ts"], thesis_rows, regime_rows)
    if ctx["day_bias"] in ("UP", "DOWN"):
        aligned = (side == "YES") == (ctx["day_bias"] == "UP")
    else:
        aligned = None
    return {
        "ticker": trade["ticker"], "entry_ts": trade["entry_ts"],
        "exit_ts": trade["exit_ts"], "side": side, "qty": qty,
        "entry_price": entry, "exit_price": exit_,
        "net_pnl": trade.get("net_pnl"), "exit_reason": trade.get("exit_reason"),
        "settled": settled, "settle_basis": basis,
        "held_pnl_gross": held, "delta_vs_held": delta,
        "mfe": mfe, "mae": mae,
        "verdict": verdict_for(trade.get("exit_reason"), side, settled),
        **ctx, "aligned": aligned,
        "data_gap": settled == "unknown" or mfe is None,
        "graded_ts": time.time(),
    }


KEEP_S = 48 * 3600


class FeatureIndex:
    """Incremental per-ticker view of signal_feature_log.jsonl.

    Full read on first refresh (backfill needs history); afterwards reads
    only newly appended bytes. If the file shrinks (fresh-start reset),
    starts over from byte 0.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.pos = 0
        self.by_ticker = {}

    def refresh(self, now=None):
        now = time.time() if now is None else now
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size < self.pos:
            self.pos = 0
            self.by_ticker = {}
        if size == self.pos:
            self._prune(now)
            return
        with open(self.path) as f:
            f.seek(self.pos)
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                ticker = row.get("ticker")
                if not ticker:
                    continue
                self.by_ticker.setdefault(ticker, []).append(
                    {"ts": row.get("ts"), "spot": row.get("spot"),
                     "floor_strike": row.get("floor_strike"),
                     "price": row.get("price")})
            self.pos = f.tell()
        self._prune(now)

    def _prune(self, now):
        for ticker in list(self.by_ticker):
            ticks = self.by_ticker[ticker]
            newest = ticks[-1]["ts"] if ticks and ticks[-1]["ts"] else None
            if newest is None or newest < now - KEEP_S:
                del self.by_ticker[ticker]

    def ticks(self, ticker):
        return self.by_ticker.get(ticker, [])
