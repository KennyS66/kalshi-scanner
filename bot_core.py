"""Pure strategy logic for the swing bot — no I/O except config file read.

Flip rule (spec v1): whale_trend changes sign, new |whale_trend| >=
flip_threshold, and momentum sign agrees with the new direction. A zero
whale_trend has no sign — it reseeds instead of flipping.
"""
import json
import math
from pathlib import Path

from backtest_gate import fee

DEFAULT_CONFIG = {
    "flip_threshold": 2.0,     # min |whale_trend| after the sign change
    "min_entry_mins": 4.0,     # no entries with less time than this
    "exit_mins": 2.0,          # always flat by here
    "risk_pct": 0.02,          # of bankroll per trade
    "day_stop_pct": 0.10,      # of bankroll; day halt threshold
    "max_open_plays": 3,
    "decided_lo": 0.05,        # market considered decided outside this band
    "decided_hi": 0.95,
    "poll_secs": 5,
    "mode": "paper",
    "live_requested": False,   # GUI toggle target; live also needs BOT_LIVE=1 + EV bar
}


def load_config(path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(Path(path).read_text()))
    except Exception:
        return dict(DEFAULT_CONFIG)
    return cfg


def _sign(x: float) -> int:
    return 1 if x > 0 else -1 if x < 0 else 0


class FlipDetector:
    """Per-ticker whale_trend sign-flip detector."""

    def __init__(self, flip_threshold: float):
        self.flip_threshold = flip_threshold
        self._prev = {}  # ticker -> last nonzero-signed whale_trend

    def update(self, ticker: str, whale_trend: float, momentum: float):
        prev = self._prev.get(ticker)
        cur_sign = _sign(whale_trend)
        if cur_sign == 0:
            self._prev.pop(ticker, None)
            return None
        self._prev[ticker] = whale_trend
        if prev is None or _sign(prev) == cur_sign:
            return None
        if abs(whale_trend) < self.flip_threshold:
            return None
        if _sign(momentum) != cur_sign:
            return None
        return "YES" if cur_sign > 0 else "NO"

    def forget(self, ticker: str):
        self._prev.pop(ticker, None)


def size_contracts(bankroll: float, price: float, risk_pct: float) -> int:
    """Contracts so that qty * (price + fee) <= bankroll * risk_pct. 0 = can't afford."""
    budget = bankroll * risk_pct
    cost = price + fee(price)
    if cost <= 0:
        return 0
    return max(0, math.floor(budget / cost))


def entry_blockers(sig: dict, cfg: dict, open_plays: dict,
                   halted: bool, paused: bool) -> list:
    """Reasons NOT to enter right now. Empty list means entry is allowed."""
    blockers = []
    if paused:
        blockers.append("paused")
    if halted:
        blockers.append("halted")
    if sig.get("status") != "ok":
        blockers.append("status_not_ok")
        return blockers
    if sig.get("yes_ask") is None or sig.get("no_ask") is None:
        blockers.append("no_quote (missing yes_ask/no_ask)")
        return blockers
    price = sig.get("price") or 0.0
    if price <= cfg["decided_lo"] or price >= cfg["decided_hi"]:
        blockers.append(f"decided price={price}")
    if (sig.get("mins_left") or 0.0) < cfg["min_entry_mins"]:
        blockers.append(f"mins_left {sig.get('mins_left')} < {cfg['min_entry_mins']}")
    if sig.get("ticker") in open_plays:
        blockers.append("already_open")
    elif len(open_plays) >= cfg["max_open_plays"]:
        blockers.append(f"max_open {len(open_plays)}")
    return blockers


def should_time_exit(sig: dict, cfg: dict) -> bool:
    return (sig.get("mins_left") or 0.0) <= cfg["exit_mins"]
