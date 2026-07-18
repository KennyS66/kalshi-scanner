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
