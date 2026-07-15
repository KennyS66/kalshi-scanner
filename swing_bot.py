#!/usr/bin/env python3
"""Swing bot daemon — paper-first flow-flip scalper (spec 2026-07-15).

Polls /api/crypto/signal every poll_secs, enters on whale-flow sign flips,
exits on the opposite flip or at exit_mins to settlement. Paper fills only;
live is locked behind bot_broker.live_unlock_ok. All state under data/bot/.

Run:  python3 -u swing_bot.py &
"""
import datetime as dt
import json
import os
import time
from pathlib import Path

BOT_DIR = Path(__file__).parent / "data" / "bot"

STATE_FILE = "bot_state.json"
CONTROL_FILE = "control.json"
TRADES_FILE = "bot_trades.jsonl"
EVENTS_FILE = "bot_events.jsonl"
CONFIG_FILE = "config.json"


def _utc_day(ts: float) -> str:
    return dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")


def fresh_state() -> dict:
    return {"mode": "paper", "paused": False, "halted": False,
            "day": _utc_day(time.time()), "day_pnl": 0.0,
            "bankroll": 500.0, "bankroll_ts": 0.0,
            "open_plays": {}, "heartbeat": 0.0, "last_control_nonce": 0}


def load_state(bot_dir) -> dict:
    try:
        return json.loads((Path(bot_dir) / STATE_FILE).read_text())
    except Exception:
        return fresh_state()


def save_state(bot_dir, state: dict) -> None:
    bot_dir = Path(bot_dir)
    bot_dir.mkdir(parents=True, exist_ok=True)
    tmp = bot_dir / (STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, bot_dir / STATE_FILE)


def read_control(bot_dir, last_nonce: int):
    """Return (cmd, nonce). cmd is None unless a NEW nonce appeared."""
    try:
        c = json.loads((Path(bot_dir) / CONTROL_FILE).read_text())
        nonce = int(c.get("nonce", 0))
        if nonce > last_nonce and c.get("cmd") in ("pause", "resume", "flatten"):
            return c["cmd"], nonce
        return None, max(nonce, last_nonce)
    except Exception:
        return None, last_nonce


def roll_day_if_needed(state: dict, now_ts: float) -> bool:
    today = _utc_day(now_ts)
    if state.get("day") == today:
        return False
    state.update({"day": today, "day_pnl": 0.0, "halted": False})
    return True


def append_jsonl(path, row: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")
