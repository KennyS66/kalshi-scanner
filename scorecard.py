"""Live scorecard for the /trade signal call, in cents per contract.

The old /trade pill scored the first-poll direction by hit rate, which is
near a coin flip by construction and hides price: a 36% hit rate on 33c
contracts breaks even. This scores the call the way it would be traded --
take the called side's ask at the decision point (first logged row with
<= DECISION_MINS left), pay the taker fee, settle on Kalshi's official
result -- and reports mean cents/contract with a t-stat over 7 and 30
days. Measured Jun-Oct 2026 it sits at ~0 (+0.06c, t=0.2); the point of a
live number is to show if that ever changes.
"""
import json
import math
import statistics
import time
from collections import defaultdict
from pathlib import Path

from settlement import official_results

FEATURE_LOG = Path(__file__).resolve().parent / "data" / "whales" / "signal_feature_log.jsonl"
DECISION_MINS = 10.0
MIN_MINS = 8.0          # a market first seen later than this has no decision row
WINDOWS = (("7d", 7), ("30d", 30))


def taker_fee(p: float) -> float:
    """Kalshi taker fee for one contract: 0.07*p*(1-p), rounded up to 1c."""
    return math.ceil(round(0.07 * p * (1 - p) * 100, 6)) / 100


def recent_rows(path=FEATURE_LOG, since: float = 0.0) -> list:
    """BTC15M rows with ts >= since. The log is append-only and chronological,
    so binary-search the byte offset instead of parsing ~500MB."""
    path = Path(path)
    size = path.stat().st_size

    def ts_at(off):
        f.seek(off)
        if off:
            f.readline()                      # skip the partial line
        while True:
            line = f.readline()
            if not line:
                return math.inf
            try:
                return json.loads(line).get("ts") or 0.0
            except ValueError:
                continue                      # torn lines exist

    with open(path, "rb") as f:
        lo, hi = 0, size
        while hi - lo > 1 << 16:
            mid = (lo + hi) // 2
            if ts_at(mid) < since:
                lo = mid
            else:
                hi = mid
        f.seek(lo)
        if lo:
            f.readline()
        rows = []
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if (r.get("ts") or 0) >= since and "BTC15M" in (r.get("ticker") or ""):
                rows.append(r)
    return rows


def samples(by_ticker: dict, results: dict) -> list:
    """One taker trade per settled market at the decision point."""
    out = []
    for ticker, rows in by_ticker.items():
        res = results.get(ticker)
        if res is None:
            continue
        for r in sorted(rows, key=lambda r: r.get("ts") or 0):
            m = r.get("mins_left")
            if m is None or m > DECISION_MINS:
                continue
            if m < MIN_MINS:
                break
            side = r.get("direction")
            px = r.get("yes_ask") if side == "YES" else r.get("no_ask")
            if side not in ("YES", "NO") or px is None or not 0.02 <= px <= 0.98:
                break
            won = side == res
            out.append({"ticker": ticker, "ts": r["ts"], "px": px, "won": won,
                        "pnl": round((1.0 if won else 0.0) - px - taker_fee(px), 4)})
            break
    out.sort(key=lambda s: s["ts"])
    return out


def summarise(s: list, now: float) -> dict:
    out = {}
    for name, days in WINDOWS:
        w = [x for x in s if x["ts"] >= now - days * 86400]
        n = len(w)
        if not n:
            out[name] = {"n": 0, "cents": None, "t": None,
                         "right_pct": None, "avg_price_c": None}
            continue
        p = [x["pnl"] for x in w]
        mean = statistics.mean(p)
        sd = statistics.stdev(p) if n > 1 else 0.0
        out[name] = {
            "n": n, "cents": round(100 * mean, 2),
            "t": round(mean / (sd / math.sqrt(n)), 2) if sd else None,
            "right_pct": round(100 * sum(x["won"] for x in w) / n, 1),
            "avg_price_c": round(100 * statistics.mean(x["px"] for x in w), 1)}
    return out


def build(path=FEATURE_LOG, now=None) -> dict:
    now = time.time() if now is None else now
    by = defaultdict(list)
    for r in recent_rows(path, since=now - max(d for _, d in WINDOWS) * 86400):
        by[r["ticker"]].append(r)
    s = samples(by, official_results(list(by)))
    return {"computed_ts": now, "decision_mins": DECISION_MINS,
            "windows": summarise(s, now)}


if __name__ == "__main__":
    print(json.dumps(build(), indent=1))
