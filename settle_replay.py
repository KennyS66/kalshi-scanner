#!/usr/bin/env python3
"""Replay settle_bot's entry rule over the historical feature log.

Acceptance gate from the spec: edge must land in +0.02..+0.05 per contract
and be positive in all three time windows. It will NOT reproduce +0.0406
exactly -- that came from one observation per market at ~10 minutes, while
the live rule takes the first qualifying tick anywhere in [5, 11]. A result
outside the band means the implementation does not match the rule that was
measured, and is a bug rather than a new finding.

Every qualifying market is booked at the resting limit price assuming a
100% fill. A resting bid only fills when price actually trades down to it,
typically after adverse movement, so this number is an upper bound on the
edge, not an expected one -- the same adverse-selection mechanism cost a
sibling market-making bot in this workspace -0.25/contract live.

Usage: .venv/bin/python settle_replay.py
"""
import datetime as dt
import json
import math
from collections import defaultdict

from settle_bot import DEFAULT_CONFIG, entry_decision, limit_price, settle_side

LOG = "data/whales/signal_feature_log.jsonl"
CUT = dt.datetime(2026, 6, 30, tzinfo=dt.timezone.utc).timestamp()


def load():
    by = defaultdict(list)
    with open(LOG) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if "BTC15M" not in (r.get("ticker") or ""):
                continue
            if (r.get("ts") or 0) < CUT or r.get("yes_ask") is None:
                continue
            r.setdefault("status", "ok")
            by[r["ticker"]].append(r)
    for rs in by.values():
        rs.sort(key=lambda r: r["ts"])
    return by


def replay(by, cfg):
    pnls = []
    for ticker, rs in by.items():
        settled = settle_side(rs[-1])
        for r in rs:                       # first qualifying tick wins
            side = entry_decision(r, cfg)
            if not side:
                continue
            px = limit_price(r, side)
            if px is None:
                break
            won = (side == settled)
            pnls.append((1.0 - px) if won else -px)
            break
    n = len(pnls)
    edge = sum(pnls) / n if n else 0.0
    return {"n": n, "edge": edge,
            "wins": sum(1 for p in pnls if p > 0)}


def main():
    cfg = dict(DEFAULT_CONFIG)
    by = load()
    tickers = sorted(by, key=lambda t: by[t][0]["ts"])
    third = len(tickers) // 3
    windows = [("W1", tickers[:third]), ("W2", tickers[third:2 * third]),
               ("W3", tickers[2 * third:])]
    allpos = True
    for name, ts in windows:
        r = replay({t: by[t] for t in ts}, cfg)
        allpos &= r["edge"] > 0
        print(f"{name}: n={r['n']:4} edge={r['edge']:+.4f}")
    total = replay(by, cfg)
    print(f"ALL: n={total['n']:4} edge={total['edge']:+.4f}")
    ok = allpos and 0.02 <= total["edge"] <= 0.05
    print(f"GATE: {'PASS' if ok else 'FAIL'} "
          f"(need +0.02..+0.05 overall and positive in 3/3; "
          f"assumes 100% fill, so this is an upper bound, not expected edge)")


if __name__ == "__main__":
    main()
