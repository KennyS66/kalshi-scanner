#!/usr/bin/env python3
"""
Beat-the-mid gate for the live entry signals.

Joins data/whales/signal_feature_log.jsonl (full signal snapshots, written by
web.api_crypto_signal) to settled outcomes in data/whales/picks_outcomes.json,
reconstructs the ACTUAL exit_watcher entry conditions, and checks whether each
entry type is profitable after approximate Kalshi fees.

This is the gate that must pass before any capital is risked. Until an entry
type has enough samples AND positive net EV, the verdict is FAIL / INSUFFICIENT.

Usage:  python3 backtest_gate.py [--min-n N]
Final line is machine-readable: "GATE_RESULT: <PASS|FAIL|INSUFFICIENT> ..."
"""
import argparse
import datetime as dt
import json
import math
from pathlib import Path

DATA = Path(__file__).parent / "data" / "whales"
FEATURE_LOG = DATA / "signal_feature_log.jsonl"
OUTCOMES = DATA / "picks_outcomes.json"


def fee(price: float) -> float:
    """Approximate Kalshi taker fee per contract. VERIFY vs current schedule."""
    return math.ceil(0.07 * price * (1 - price) * 100) / 100


def maker_fee(price: float) -> float:
    """Kalshi maker fee per contract on these markets: ZERO.

    CONFIRMED 2026-08-03 against Kenny's own live account fills -- the
    primary source the previous 25%-of-taker estimate was missing. 86
    genuine resting fills across 36 KXBTC15M tickers, 501.8 contracts,
    prices 0.07-0.87, every one with fee_cost exactly 0.00; the same
    volume taken would have cost $9.32. The taker formula above is
    confirmed by the same data (implied rate median 0.0701 over 1851
    taker fills vs the modeled 0.07).

    Applies only to fills that genuinely rested and waited, not an order
    that filled the instant it was placed -- that is a taker fill by
    definition regardless of order type (see bot_broker.PaperBroker.fill).
    """
    return 0.0


def load_rows():
    if not FEATURE_LOG.exists():
        return {}
    y = {t: (1 if v == "YES" else 0) for t, v in json.load(open(OUTCOMES)).items()} if OUTCOMES.exists() else {}
    by_ticker = {}
    with open(FEATURE_LOG) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            t = r.get("ticker")
            if not t or "BTC15M" not in t:
                continue
            by_ticker.setdefault(t, []).append(r)
    for t in by_ticker:
        by_ticker[t].sort(key=lambda r: r.get("ts", 0))
    return by_ticker, y


def first_entry(rows, predicate):
    for r in rows:
        if predicate(r):
            return r
    return None


def flush_entry(r):
    return (r.get("flush_score", 0) >= 90
            and r.get("buy_pressure", 0) >= 30000
            and 0.20 <= (r.get("price") or 0) <= 0.45)


def no_entry(r):
    ts = r.get("ts", 0)
    hour = dt.datetime.utcfromtimestamp(ts).hour if ts else 0
    dist_limit = -60 if hour >= 18 else -30
    d = r.get("distance")
    return (r.get("sig_combined", 0) <= -20
            and r.get("whale_trend", 0) < 0
            and d is not None and d <= dist_limit
            and r.get("whale_count", 0) >= 50
            and (r.get("mins_left") or 0) > 3)


def evaluate(name, by_ticker, y, predicate, side, min_n):
    """side: 'YES' (long YES, e.g. flush bounce) or 'NO' (long NO)."""
    pnls, resolved, fired = [], 0, 0
    for t, rows in by_ticker.items():
        e = first_entry(rows, predicate)
        if e is None:
            continue
        fired += 1
        if t not in y:
            continue
        resolved += 1
        price = e.get("price") or 0
        cost = price if side == "YES" else (1 - price)
        won = (y[t] == 1) if side == "YES" else (y[t] == 0)
        pnls.append((1.0 if won else 0.0) - cost - fee(cost))
    n = len(pnls)
    if n == 0:
        print(f"  [{name}] fired={fired} resolved=0 — no settled entries yet")
        return name, "INSUFFICIENT", n
    ev = sum(pnls) / n
    win = sum(1 for p in pnls if p > 0) / n
    if n < min_n:
        verdict = "INSUFFICIENT"
    elif ev > 0:
        verdict = "PASS"
    else:
        verdict = "FAIL"
    print(f"  [{name}] fired={fired} resolved={n} win={100*win:.0f}% "
          f"EV_net=${ev:+.3f}/contract  ->  {verdict} (need n>={min_n}, EV>0)")
    return name, verdict, n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-n", type=int, default=20,
                    help="minimum settled entries before a PASS/FAIL is trusted")
    args = ap.parse_args()

    loaded = load_rows()
    if not loaded:
        print("No signal_feature_log.jsonl yet.")
        print("GATE_RESULT: INSUFFICIENT no_data")
        return
    by_ticker, y = loaded
    print(f"15m markets in feature log: {len(by_ticker)} | settled outcomes known: "
          f"{sum(1 for t in by_ticker if t in y)}")

    results = [
        evaluate("FLUSH_BOUNCE (long YES)", by_ticker, y, flush_entry, "YES", args.min_n),
        evaluate("NO_ENTRY (long NO)", by_ticker, y, no_entry, "NO", args.min_n),
    ]

    passed = [name for name, v, _ in results if v == "PASS"]
    if passed:
        print(f"GATE_RESULT: PASS {','.join(passed)}")
    elif any(v == "FAIL" for _, v, _ in results):
        print("GATE_RESULT: FAIL no_entry_type_is_+EV")
    else:
        print("GATE_RESULT: INSUFFICIENT need_more_settled_events")


if __name__ == "__main__":
    main()
