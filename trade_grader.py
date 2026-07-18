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
    if exit_reason in ("target", "target_half", "stretch"):
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


def post_exit_stats(ticks, side, exit_ts, expiry):
    """Best held-side price between exit (inclusive) and expiry, or None.

    For stops this answers 'did price rebound after we bailed' — a
    good_stop that was also recoverable means a patient exit could have
    left at breakeven-or-better even though settlement went against."""
    prices = [t["price"] for t in ticks
              if t.get("price") is not None and t.get("ts") is not None
              and exit_ts <= t["ts"] <= expiry]
    if not prices:
        return None
    if side == "NO":
        prices = [1.0 - p for p in prices]
    return round(max(prices), 4)


def trade_key(row):
    # exit_ts distinguishes scale-out legs that share ticker+entry_ts;
    # existing grade rows carry exit_ts too, so dedup stays backward-compatible
    return f"{row['ticker']}|{row['entry_ts']}|{row.get('exit_ts')}"


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
        "post_exit_high": (peh := post_exit_stats(
            ticks, side, trade["exit_ts"], expiry_of(trade))),
        "recoverable": ((peh is not None and peh > entry)
                        if trade.get("exit_reason") == "stop" else None),
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

    def refresh(self, now=None, keep_from=None):
        """keep_from: keep ticks at/after this ts even if older than KEEP_S
        (backfill grading of old trades needs their ticks protected)."""
        now = time.time() if now is None else now
        cutoff = now - KEEP_S
        if keep_from is not None:
            cutoff = min(cutoff, keep_from)
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size < self.pos:
            self.pos = 0
            self.by_ticker = {}
        if size == self.pos:
            self._prune(cutoff)
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
        self._prune(cutoff)

    def _prune(self, cutoff):
        for ticker in list(self.by_ticker):
            ticks = self.by_ticker[ticker]
            newest = ticks[-1]["ts"] if ticks and ticks[-1]["ts"] else None
            if newest is None or newest < cutoff:
                del self.by_ticker[ticker]

    def ticks(self, ticker):
        return self.by_ticker.get(ticker, [])


def read_jsonl(path):
    rows = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def append_jsonl(path, row):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(row) + "\n")


def archive_features(now):
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    out = Path(ARCHIVE_DIR) / day / "signal_feature_log.jsonl.gz"
    if out.exists() or not Path(FEATURES_PATH).exists():
        return False
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".gz.tmp")
    with open(FEATURES_PATH, "rb") as src, gzip.open(tmp, "wb") as dst:
        while chunk := src.read(1 << 20):
            dst.write(chunk)
    tmp.rename(out)
    return True


def run_cycle(idx, now=None):
    now = time.time() if now is None else now
    try:
        archive_features(now)
    except OSError as e:
        print(f"archive error: {e}", file=sys.stderr)
    graded = {trade_key(g) for g in read_jsonl(GRADES_PATH)}
    pending = []
    for t in read_jsonl(TRADES_PATH):
        if t.get("status") != "closed":
            continue
        try:
            if trade_key(t) in graded or now < expiry_of(t) + GRADE_DELAY_S:
                continue
        except (KeyError, TypeError):
            continue
        pending.append(t)
    if not pending:
        return 0
    oldest = min(t["entry_ts"] for t in pending)
    idx.refresh(now=now, keep_from=oldest - 3600)
    thesis = read_jsonl(THESIS_PATH)
    regime = read_jsonl(REGIME_PATH)
    n = 0
    for t in pending:
        try:
            row = grade_trade(t, idx.ticks(t["ticker"]), thesis, regime)
            append_jsonl(GRADES_PATH, row)
            n += 1
        except Exception as e:
            print(f"grade error {t.get('ticker')}: {e}", file=sys.stderr)
    return n


def main():
    once = "--once" in sys.argv
    idx = FeatureIndex(FEATURES_PATH)
    while True:
        try:
            n = run_cycle(idx)
            if n:
                print(f"graded {n} trade(s)", flush=True)
        except Exception as e:
            print(f"cycle error: {e}", file=sys.stderr)
        if once:
            break
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
