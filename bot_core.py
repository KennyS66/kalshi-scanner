"""Pure strategy logic for the swing bot — no I/O except config file read.

Flip rule (spec v1): whale_trend changes sign vs. the last CONFIRMED sign,
new |whale_trend| >= flip_threshold, and momentum sign agrees with the new
direction. A zero whale_trend has no sign — it reseeds instead of flipping.
A sub-threshold or momentum-disagreeing opposite-sign sample does not
advance the confirmed sign, so it can't silently consume a later flip.
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
    "paper_bankroll": 500.0,   # paper-mode stake; 0/absent = use real balance
    "live_requested": False,   # GUI toggle target; live also needs BOT_LIVE=1 + EV bar
    "use_ranges": True,        # gate entries/exits on the calibrated buy/sell ranges
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
    """Per-ticker whale_trend sign-flip detector.

    self._prev holds the last CONFIRMED signed whale_trend per ticker — it
    only advances on (a) initial seed, (b) a fired flip, or (c) a same-sign
    sample (which just refreshes magnitude). A sub-threshold or
    momentum-disagreeing opposite-sign sample must NOT advance it, or that
    sample would silently consume the flip for a later, valid opposite-sign
    sample.
    """

    def __init__(self, flip_threshold: float):
        self.flip_threshold = flip_threshold
        self._prev = {}  # ticker -> last confirmed nonzero-signed whale_trend

    def update(self, ticker: str, whale_trend: float, momentum: float):
        prev = self._prev.get(ticker)
        cur_sign = _sign(whale_trend)
        if cur_sign == 0:
            self._prev.pop(ticker, None)
            return None
        if prev is None:
            self._prev[ticker] = whale_trend
            return None
        if _sign(prev) == cur_sign:
            self._prev[ticker] = whale_trend  # same side: refresh magnitude
            return None
        if abs(whale_trend) < self.flip_threshold:
            return None  # sub-threshold opposite-sign sample: doesn't confirm
        if _sign(momentum) != cur_sign:
            return None  # momentum disagrees: doesn't confirm
        self._prev[ticker] = whale_trend  # confirmed flip
        return "YES" if cur_sign > 0 else "NO"

    def forget(self, ticker: str):
        self._prev.pop(ticker, None)


# ── buy/sell target ranges ────────────────────────────────────────────
# Same formula target_grader.py grades against (banner_targets.jsonl) and
# the /trade banner displays; the per-side offsets are what the grader
# tunes from that history (90% sell-low / 60% sell-high hit-rate goals).

def load_offsets(path) -> dict:
    """banner_offsets.json → {"yes": {...}, "no": {...}}; {} if unreadable."""
    try:
        d = json.loads(Path(path).read_text())
        return d if "yes" in d and "no" in d else {}
    except Exception:
        return {}


def compute_side_ranges(price: float, yes_pct: float, side: str,
                        side_offsets: dict) -> dict:
    """Buy/sell range in cents for `side`, calibrated by that side's offsets."""
    up = side == "YES"
    buy_c = (price if up else 1 - price) * 100
    flow_fair_c = yes_pct if up else 100 - yes_pct
    buy_low = max(1.0, buy_c - 3.0)
    buy_high = min(95.0, buy_c + 2.0)
    sell_low = max(
        buy_high + 2.0,   # floor: sell target must always be above buy range
        min(95.0, buy_high + 10.0 - (side_offsets.get("sell_low_offset_c") or 0.0)),
    )
    sell_high = max(
        sell_low + 2.0,
        min(95.0, flow_fair_c - (side_offsets.get("sell_high_offset_c") or 0.0)),
    )
    return {"side": side, "buy_low": round(buy_low, 1), "buy_high": round(buy_high, 1),
            "sell_low": round(sell_low, 1), "sell_high": round(sell_high, 1)}


def sell_price_c(sig: dict, side: str):
    """Achievable sell price in cents (mirrors PaperBroker.sell), or None."""
    ask = sig.get("yes_ask") if side == "YES" else sig.get("no_ask")
    if ask is None:
        return None
    spread = max(0.0, sig.get("spread") or 0.0)
    return max(0.01, ask - spread) * 100


def should_target_exit(play: dict, sig: dict) -> bool:
    """Take profit once the achievable sell reaches the entry-time sell_low —
    the calibrated 90%-hit WIN line the range history is graded against."""
    r = play.get("ranges")
    if not r:
        return False
    px = sell_price_c(sig, play["side"])
    return px is not None and px >= r["sell_low"]


def size_contracts(bankroll: float, price: float, risk_pct: float) -> int:
    """Contracts so that qty * (price + fee) <= bankroll * risk_pct. 0 = can't afford."""
    budget = bankroll * risk_pct
    cost = price + fee(price)
    if cost <= 0:
        return 0
    return max(0, math.floor(budget / cost))


def entry_blockers(sig: dict, cfg: dict, open_plays: dict,
                   halted: bool, paused: bool, ranges: dict = None) -> list:
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
    if ranges is not None:
        ask = sig["yes_ask"] if ranges["side"] == "YES" else sig["no_ask"]
        ask_c = ask * 100
        if ask_c > ranges["buy_high"]:
            blockers.append(f"ask {ask_c:.1f}c above buy range "
                            f"{ranges['buy_low']:.1f}-{ranges['buy_high']:.1f}c")
        elif ask_c < ranges["buy_low"]:
            blockers.append(f"ask {ask_c:.1f}c below buy range "
                            f"{ranges['buy_low']:.1f}-{ranges['buy_high']:.1f}c")
    return blockers


def should_time_exit(sig: dict, cfg: dict) -> bool:
    return (sig.get("mins_left") or 0.0) <= cfg["exit_mins"]
