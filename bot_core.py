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
    "ev_gate": True,           # skip entry buckets with proven-negative EV
    "ev_gate_min_samples": 12, # bucket sample floor before the gate may skip
    "loop_deadman_mins": 45,   # pause if the marketloop heartbeat is staler
                               # than this (0 = never); auto-resumes when back
    "stop_loss_frac": 0.5,     # cut a play when its sell value falls this
                               # fraction below entry (0 = no stop): fires
                               # while the market is live, never rides a
                               # loser to settlement / a stale rolled exit
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


def should_stop_exit(play: dict, sig: dict, cfg: dict) -> bool:
    """Stop-loss: sell value fell stop_loss_frac below entry — cut it now."""
    frac = cfg.get("stop_loss_frac") or 0.0
    if not frac:
        return False
    px = sell_price_c(sig, play["side"])
    if px is None:
        return False
    return px <= play["entry"]["price"] * 100 * (1 - frac)


# ── EV gate: learned per-bucket skip from the closed-trade journal ────
# Buckets are deliberately coarse (side x entry-price band x time-left) so
# they accumulate samples in days, not months. A bucket only ever blocks
# entries once it holds >= ev_gate_min_samples closed trades AND its net
# average is negative — small samples and profitable buckets never gate.

def entry_bucket(side: str, sig: dict) -> str:
    m = sig.get("mins_left") or 0.0
    mb = "4-7m" if m < 7 else "7-11m" if m < 11 else "11m+"
    ask = sig.get("yes_ask") if side == "YES" else sig.get("no_ask")
    c = (ask or 0.0) * 100
    pb = "cheap" if c < 35 else "mid" if c <= 65 else "rich"
    return f"{side}|{pb}|{mb}"


def update_bucket_stats(stats: dict, side: str, entry_sig: dict,
                        net_pnl: float) -> None:
    """Fold one closed trade into stats in place (incremental, O(1))."""
    b = entry_bucket(side, entry_sig or {})
    d = stats.setdefault(b, {"n": 0, "wins": 0, "net": 0.0})
    d["n"] += 1
    d["wins"] += 1 if net_pnl > 0 else 0
    d["net"] = round(d["net"] + net_pnl, 4)
    d["net_avg"] = round(d["net"] / d["n"], 4)
    d["win_pct"] = round(100.0 * d["wins"] / d["n"], 1)


def bucket_stats(trades: list) -> dict:
    """Aggregate closed trades (with entry_sig snapshots) into buckets."""
    stats = {}
    for t in trades:
        if t.get("status") != "closed" or t.get("net_pnl") is None:
            continue
        update_bucket_stats(stats, t.get("side", "?"),
                            t.get("entry_sig") or {}, t["net_pnl"])
    return stats


def ev_gate_blocker(side: str, sig: dict, stats: dict, cfg: dict):
    """Reason to skip this entry per learned bucket EV, or None."""
    if not cfg.get("ev_gate", True):
        return None
    b = entry_bucket(side, sig)
    d = (stats or {}).get(b)
    floor = cfg.get("ev_gate_min_samples", 12)
    if d and d["n"] >= floor and d["net_avg"] < 0:
        return (f"ev_gate: bucket {b} net avg {d['net_avg']:+.2f} "
                f"over {d['n']} trades")
    return None
