"""Pure strategy logic for the swing bot — no I/O except config file read.

Flip rule (spec v1): whale_trend changes sign vs. the last CONFIRMED sign,
new |whale_trend| >= flip_threshold, and momentum sign agrees with the new
direction. A zero whale_trend has no sign — it reseeds instead of flipping.
A sub-threshold or momentum-disagreeing opposite-sign sample does not
advance the confirmed sign, so it can't silently consume a later flip.
"""
import json
import math
import time
from collections import deque
from pathlib import Path

from backtest_gate import fee  # noqa: F401 — also used by entry_blockers

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
    "weekend_curfew": True,    # no entries Sat/Sun 00Z-13Z (thin-tape bleed)
    "overnight_curfew": True,  # no entries 00Z-13Z any day until replay beats it
    "min_edge_c": 2.0,         # min NET cents/contract at win target; None=off
    "scale_out": True,         # bank half at the win line, runner rides to stretch
    "profit_arm_usd": 15.0,    # day P&L peak that arms the profit lock; 0=off
    "profit_keep_frac": 0.5,   # halt the day if P&L gives back to this frac of peak
    "profit_size_frac": 0.5,   # entry-budget multiplier while the lock is armed
    "ev_gate_min_samples": 12, # bucket sample floor before the gate may skip
    "loop_deadman_mins": 45,   # pause if the marketloop heartbeat is staler
                               # than this (0 = never); auto-resumes when back
    "stop_loss_frac": 0.5,     # cut a play when its sell value falls this
                               # fraction below entry (0 = no stop): fires
                               # while the market is live, never rides a
                               # loser to settlement / a stale rolled exit
    "max_loss_usd": 100.0,     # hard cap on TOTAL net loss (0 = off): at the
                               # cap the bot flattens and blocks all entries
    "trade_risk_frac": 0.10,   # per-trade cost budget as a fraction of the
                               # REMAINING max-loss headroom (sizes shrink as
                               # losses consume the budget)
    "flip_exit": True,         # exit an open play on the opposite flip; off =
                               # let target/stop/time resolve it (whipsaw fix)
    "max_entry_momentum": 0.0, # skip flips with |momentum| above this — late,
                               # chase-y entries (0 = off)
    "max_entries_per_market": 0,  # cap re-entries per 15m market (0 = off)
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


def should_stretch_exit(play: dict, sig: dict) -> bool:
    """After a scale-out, the runner exits at the stretch line (sell_high)."""
    r = play.get("ranges")
    if not r:
        return False
    px = sell_price_c(sig, play["side"])
    return px is not None and px >= r["sell_high"]


def size_for_budget(budget: float, price: float) -> int:
    """Contracts so that qty * (price + fee) <= budget. 0 = can't afford."""
    cost = price + fee(price)
    if cost <= 0:
        return 0
    return max(0, math.floor(budget / cost))


def size_contracts(bankroll: float, price: float, risk_pct: float) -> int:
    """Contracts so that qty * (price + fee) <= bankroll * risk_pct. 0 = can't afford."""
    return size_for_budget(bankroll * risk_pct, price)


def loss_headroom(total_pnl: float, cfg: dict) -> float:
    """Dollars of max-loss budget left; profits never expand it past the cap.

    loss_cap_baseline (optional) re-anchors the cap: losses count only from
    that P&L level (set it to the current total to grant a fresh budget
    without touching history)."""
    cap = cfg.get("max_loss_usd") or 0.0
    if cap <= 0:
        return float("inf")
    baseline = cfg.get("loss_cap_baseline") or 0.0
    return max(0.0, cap + min(0.0, total_pnl - baseline))


def trade_budget(bankroll: float, total_pnl: float, cfg: dict) -> float:
    """Per-trade cost budget MATCHED to the remaining max-loss headroom:
    the smaller of the classic bankroll fraction and trade_risk_frac of
    what's left before the cap. Worst case (settle to 0) a trade burns
    only that slice, so the cap can't be blown through in one move."""
    base = bankroll * cfg["risk_pct"]
    head = loss_headroom(total_pnl, cfg)
    if head == float("inf"):
        return base
    return min(base, head * cfg.get("trade_risk_frac", 0.10))


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
    mom_cap = cfg.get("max_entry_momentum") or 0.0
    if mom_cap and abs(sig.get("momentum") or 0.0) > mom_cap:
        blockers.append(f"momentum {abs(sig.get('momentum') or 0.0):.0f} > "
                        f"{mom_cap:.0f} — late entry")
    if ranges is not None:
        ask = sig["yes_ask"] if ranges["side"] == "YES" else sig["no_ask"]
        ask_c = ask * 100
        if ask_c > ranges["buy_high"]:
            blockers.append(f"ask {ask_c:.1f}c above buy range "
                            f"{ranges['buy_low']:.1f}-{ranges['buy_high']:.1f}c")
        elif ask_c < ranges["buy_low"]:
            blockers.append(f"ask {ask_c:.1f}c below buy range "
                            f"{ranges['buy_low']:.1f}-{ranges['buy_high']:.1f}c")
        # thin-edge gate: projected NET cents/contract at the win target must
        # clear a floor — 3-5c gross margins lose to double fees (17 of the
        # first 60 target "wins" netted < $0.30; several were net negative)
        min_edge = cfg.get("min_edge_c")
        if min_edge is not None:
            edge_c = (ranges["sell_low"] - ask_c
                      - 100 * (fee(ask) + fee(ranges["sell_low"] / 100)))
            if edge_c < min_edge:
                blockers.append(f"thin_edge: {edge_c:.1f}c net at win target "
                                f"< {min_edge:.1f}c floor")
    return blockers


class RegimeTracker:
    """Classifies the live tape as 'trend' or 'range' from a rolling window
    of (ts, spot) samples — Kaufman's efficiency ratio: net directional
    travel over the window divided by the window's total PATH LENGTH (the
    sum of |move| between consecutive samples, not just hi-lo range — range
    alone is sensitive to where the window happens to start/end mid-cycle,
    so a truncated chop window can spuriously read as high-efficiency).
    High efficiency (most of the path length was net progress) reads as
    trend; low efficiency (lots of back-and-forth covering little net
    ground) reads as range/chop. Pure and replay-safe: driven only by the
    same (ts, spot) stream both the live tick loop and bot_replay.py already
    iterate over, so live and replay classify identically given the same
    price history.
    """

    def __init__(self, window_secs: float = 2700.0, min_samples: int = 10,
                 trend_threshold: float = 0.35):
        self.window_secs = window_secs
        self.min_samples = min_samples
        self.trend_threshold = trend_threshold
        self.samples: deque = deque()

    def update(self, ts, spot) -> None:
        if ts is None or spot is None:
            return
        self.samples.append((ts, spot))
        cutoff = ts - self.window_secs
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()

    def classify(self) -> str:
        """Returns 'trend' or 'range'. Defaults to 'range' (the tighter,
        more conservative exit fuse) whenever there isn't enough history
        to classify confidently."""
        if len(self.samples) < self.min_samples:
            return "range"
        prices = [p for _, p in self.samples]
        path_length = sum(abs(prices[i] - prices[i - 1])
                          for i in range(1, len(prices)))
        if path_length <= 0:
            return "range"
        net_move = abs(prices[-1] - prices[0])
        efficiency = net_move / path_length
        return "trend" if efficiency >= self.trend_threshold else "range"


def should_time_exit(sig: dict, cfg: dict, regime: str = None) -> bool:
    """Force-close once mins_left drops to the fuse for the current regime.

    regime=None (or an unrecognized value) falls back to the flat
    cfg['exit_mins'] — unchanged behavior for any caller that doesn't pass
    a regime, and for a bot with no exit_mins_trend/exit_mins_range set.
    """
    exit_mins = cfg.get("exit_mins")
    if regime == "trend" and cfg.get("exit_mins_trend") is not None:
        exit_mins = cfg["exit_mins_trend"]
    elif regime == "range" and cfg.get("exit_mins_range") is not None:
        exit_mins = cfg["exit_mins_range"]
    return (sig.get("mins_left") or 0.0) <= exit_mins


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

MOM_ALIGN_STRONG = 8.0   # aligned-momentum floor for the "strong" band
CURFEW_END_HOUR = 13     # weekend overnight curfew covers 00Z-13Z Sat/Sun


def weekend_curfew_blocker(now_ts: float, cfg: dict):
    """Reason to skip entries in overnight tape, or None.

    The bot runs 24/7 but only enters in sessions it hasn't measured as
    losing. Overnight 00-13Z measured -$1.10/trade avg (2026-07-18, n=57);
    the reopen path is replay/tuner evidence for those hours, then flipping
    the config. Exits are never curfewed - open plays manage themselves.
    overnight_curfew: all days 00-13Z; weekend_curfew: Sat/Sun only."""
    g = time.gmtime(now_ts)
    if g.tm_hour >= CURFEW_END_HOUR:
        return None
    if cfg.get("overnight_curfew", True):
        return f"overnight_curfew: no entries 00-{CURFEW_END_HOUR}Z"
    if cfg.get("weekend_curfew", True) and g.tm_wday >= 5:
        return f"weekend_curfew: no entries Sat/Sun 00-{CURFEW_END_HOUR}Z"
    return None


def session_tag(ts) -> str:
    """weekday_day / weekday_night / weekend_day / weekend_night, reusing
    the same weekday split as the [100 weekday + 100 weekend] live-unlock
    gate and the same day/night boundary as the overnight curfew
    (CURFEW_END_HOUR) — no new boundaries invented, just made explicit in
    the bucket key instead of handled by blunt inclusion/exclusion."""
    if not ts:
        return "unknown"
    g = time.gmtime(ts)
    wd = "weekend" if g.tm_wday >= 5 else "weekday"
    dn = "day" if g.tm_hour >= CURFEW_END_HOUR else "night"
    return f"{wd}_{dn}"


POOL_NAMES = ("weekday_day", "weekday_night", "weekend_day", "weekend_night")


def entry_bucket(side: str, sig: dict) -> str:
    m = sig.get("mins_left") or 0.0
    mb = "4-7m" if m < 7 else "7-11m" if m < 11 else "11m+"
    ask = sig.get("yes_ask") if side == "YES" else sig.get("no_ask")
    c = (ask or 0.0) * 100
    pb = "cheap" if c < 35 else "mid" if c <= 65 else "rich"
    # flow-conviction band: momentum signed toward the held side. Graded
    # stops cluster on weak-conviction entries (aligned but tepid), so the
    # gate buckets on strength and can learn that skip once a band shows
    # >= min_samples of negative EV. The signal layer already keeps raw
    # against-flow entries rare; they get their own band as a tripwire.
    am = (sig.get("momentum") or 0.0) * (1 if side == "YES" else -1)
    fl = ("against" if am < 0 else
          "weak" if am < MOM_ALIGN_STRONG else "strong")
    sess = session_tag(sig.get("ts"))
    return f"{side}|{pb}|{mb}|{fl}|{sess}"


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
    """Aggregate closed trades (with entry_sig snapshots) into buckets.

    entry_sig may predate the entry_sig snapshot carrying its own 'ts'
    (SIG_SNAPSHOT_KEYS grew that field later) — fall back to the trade's
    top-level entry_ts, always present, so session_tag works on the full
    trade history, not just trades closed after the snapshot fix."""
    stats = {}
    for t in trades:
        if t.get("status") != "closed" or t.get("net_pnl") is None:
            continue
        sig = dict(t.get("entry_sig") or {})
        sig.setdefault("ts", t.get("entry_ts"))
        update_bucket_stats(stats, t.get("side", "?"), sig, t["net_pnl"])
    return stats


# ── Flagged findings: the dashboard doing some of the noticing ────────────
# Each rule is a small, independent function of (trades, state, cfg,
# ev_buckets). One bad rule can't take down the others — compute_findings
# wraps each in its own try/except, same convention as trade_grader.py's
# per-record handling.

STUCK_EXIT_REASONS = ("time", "rolled")
STUCK_RATE_WINDOW = 20      # trades
STUCK_RATE_FLOOR = 0.40     # see spec: verified current-regime baseline is
                            # ~15.6%, not the ~6% full-history figure — 40%
                            # leaves real headroom in a 20-trade sample.


def _finding_bucket_bleeding(trades, state, cfg, ev_buckets):
    floor = cfg.get("ev_gate_min_samples", 12)
    out = []
    for bucket, d in (ev_buckets or {}).items():
        if d.get("n", 0) >= floor and d.get("net_avg", 0) < 0:
            out.append({"severity": "warn", "title": f"Bucket bleeding: {bucket}",
                        "detail": f"{d['n']} trades, net avg {d['net_avg']:+.2f} — "
                                  f"already auto-skipped by the EV gate"})
    return out


def _finding_day_giveback(trades, state, cfg, ev_buckets):
    out = []
    for pool in POOL_NAMES:
        ps = (state.get("pools") or {}).get(pool) or {}
        high, pnl = ps.get("day_high") or 0.0, ps.get("day_pnl") or 0.0
        if high > 0 and (high - pnl) > high * 0.5:
            out.append({"severity": "warn", "title": f"Day giving back gains: {pool}",
                        "detail": f"peaked at {high:+.2f}, now {pnl:+.2f} "
                                  f"({(high - pnl) / high * 100:.0f}% given back)"})
    return out


def _finding_bot_health(trades, state, cfg, ev_buckets):
    out = []
    if state.get("paused"):
        out.append({"severity": "warn", "title": "Bot paused", "detail": ""})
    halted_pools = [p for p in POOL_NAMES
                    if ((state.get("pools") or {}).get(p) or {}).get("halted")]
    if halted_pools:
        out.append({"severity": "warn", "title": "Bot halted",
                    "detail": ", ".join(halted_pools)})
    age = time.time() - (state.get("heartbeat") or 0)
    if age > 30:
        out.append({"severity": "warn", "title": "Feed stale",
                    "detail": f"no heartbeat for {age:.0f}s"})
    return out


def _finding_thin_session(trades, state, cfg, ev_buckets):
    floor = cfg.get("ev_gate_min_samples", 12)
    totals = {}
    for bucket, d in (ev_buckets or {}).items():
        sess = bucket.rsplit("|", 1)[-1]
        totals[sess] = totals.get(sess, 0) + d.get("n", 0)
    out = []
    for sess in POOL_NAMES:
        n = totals.get(sess, 0)
        if n < floor:
            out.append({"severity": "info", "title": f"Still learning {sess}",
                        "detail": f"{n}/{floor} trades toward the gate's sample floor"})
    return out


def _finding_stuck_rate(trades, state, cfg, ev_buckets):
    closed = [t for t in trades
             if t.get("status") == "closed" and t.get("net_pnl") is not None]
    closed.sort(key=lambda t: t.get("exit_ts") or 0)
    recent = closed[-STUCK_RATE_WINDOW:]
    if len(recent) < STUCK_RATE_WINDOW:
        return []
    stuck = sum(1 for t in recent if t.get("exit_reason") in STUCK_EXIT_REASONS)
    rate = stuck / len(recent)
    if rate > STUCK_RATE_FLOOR:
        return [{"severity": "warn", "title": "Stuck-position rate elevated",
                 "detail": f"{stuck}/{len(recent)} of the last trades exited via "
                           f"time/rolled ({rate * 100:.0f}%) — positions not "
                           f"resolving cleanly, forced closes near market expiry"}]
    return []


_FINDING_RULES = (_finding_bot_health, _finding_day_giveback,
                  _finding_bucket_bleeding, _finding_stuck_rate,
                  _finding_thin_session)


def compute_findings(trades: list, state: dict, cfg: dict, ev_buckets: dict) -> list:
    """Returns [{"severity": "warn"|"info", "title": ..., "detail": ...}, ...],
    most severe first (warn before info; stable within each). Empty list is a
    real, displayed state — "nothing flagged" — not the absence of a section."""
    out = []
    for rule in _FINDING_RULES:
        try:
            out.extend(rule(trades, state, cfg, ev_buckets))
        except Exception:
            continue
    out.sort(key=lambda f: 0 if f["severity"] == "warn" else 1)
    return out


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
