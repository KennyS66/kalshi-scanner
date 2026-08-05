#!/usr/bin/env python3
"""Directional hold-to-settlement paper strategy.

Enters on the LEVEL of sig_combined, buys that side as a maker, holds to
settlement. No targets, no stops, no time exit.

Deliberately shares nothing with swing_bot but the :9050 signal feed and
pure helpers from bot_core. Its journal must never reach data/bot/ --
bot_core.session_gate_stats and bucket_stats read that directory to decide
whether the OTHER strategy may trade live.

Spec: docs/superpowers/specs/2026-08-04-settle-bot-design.md
Run:  python3 -u settle_bot.py
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).parent
SETTLE_DIR = BASE / "data" / "settle"
CONFIG_FILE = "settle_config.json"
STATE_FILE = "settle_state.json"
TRADES_FILE = "settle_trades.jsonl"
EVENTS_FILE = "settle_events.jsonl"

DEFAULT_CONFIG = {
    "entry_threshold": 10.0,   # |sig_combined| bar; 10 not 20 (20 broke in W3)
    "min_mins_left": 5.0,      # flat top of the edge curve, not its peak
    "max_mins_left": 11.0,
    "qty": 1,                  # flat, always
    "poll_secs": 5,
    "mode": "paper",
}


def load_config(path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(Path(path).read_text()))
    except Exception:
        pass
    return cfg


def _utc_day(ts=None) -> str:
    return dt.datetime.fromtimestamp(ts or time.time(),
                                     dt.timezone.utc).strftime("%Y-%m-%d")


def fresh_state() -> dict:
    return {"day": _utc_day(), "heartbeat": 0.0, "pending": {}, "open": {},
            "reconcile_baseline": 0, "unresolved": 0}


def load_state(bot_dir) -> dict:
    try:
        return json.loads((Path(bot_dir) / STATE_FILE).read_text())
    except Exception:
        return fresh_state()


def save_state(bot_dir, state: dict) -> None:
    d = Path(bot_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, d / STATE_FILE)


def append_jsonl(path, row: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")
