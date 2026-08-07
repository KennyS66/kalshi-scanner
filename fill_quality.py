#!/usr/bin/env python3
"""Adverse-selection diagnostic: did we fill on the markets that beat us?

The check every maker-entry backtest needs and none of them contain. A
backtest that assumes fills measures "would this rule have been right",
NOT "can I trade it". Those differed by 29 cents per contract for
settle_bot on 2026-08-07.

Compare the settlement outcome of markets where the resting order FILLED
against those where it was placed and cancelled unfilled. Same signal, same
limit price -- the only difference is whether the market came to us. If the
filled group performs materially worse, the entry mechanism is selecting
against you: a resting buy fills only when price falls to it, i.e. only when
the market is disagreeing with the signal, so you transact exclusively on
the subset where you are already being proven wrong.

settle_bot, 2026-08-07, ~24h of live paper:

    filled     n=49  edge -0.0382  win 46.9%
    unfilled   n=39  edge +0.2560  win 82.1%
    difference       -0.2942 +- 0.0973   t = -3.02

That killed the strategy. Note the unfilled group's +0.2560 is NOT an
opportunity -- those are precisely the markets where price moved away from
the bid, so it was never capturable at that price. It is the control group,
not a target.

Usage:  python3 fill_quality.py [--dir data/settle]
"""
import argparse
import datetime as dt
import json
import math
import re
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).parent
FEATURE_LOG = BASE / "data" / "whales" / "signal_feature_log.jsonl"

ADVERSE_T = -2.0     # difference this many SE below zero counts as selection


def _summary(rows):
    """n / mean edge / win% / standard error for one group."""
    edges = [(1.0 - r["limit"]) if r["settled"] == r["side"] else -r["limit"]
             for r in rows]
    n = len(edges)
    if not n:
        return {"n": 0, "edge": None, "win_pct": None, "se": None}
    mean = sum(edges) / n
    var = sum((e - mean) ** 2 for e in edges) / n
    return {"n": n,
            "edge": round(mean, 4),
            "win_pct": round(100.0 * sum(1 for e in edges if e > 0) / n, 1),
            "se": round(math.sqrt(var / n), 4)}


def fill_quality(filled, unfilled):
    """Compare filled vs unfilled outcomes.

    Each row needs `side` ("YES"/"NO"), `limit` (the resting price) and
    `settled` (which side settled in the money).

    Returns both group summaries, their difference, its t-statistic, and an
    `adverse` verdict. A zero-variance difference (every row identical) is
    treated as deterministic rather than infinitely significant.
    """
    f, u = _summary(filled), _summary(unfilled)
    if not f["n"] or not u["n"]:
        return {"filled": f, "unfilled": u, "difference": None,
                "t": None, "adverse": False}
    diff = round(f["edge"] - u["edge"], 4)
    se = math.sqrt(f["se"] ** 2 + u["se"] ** 2)
    t = round(diff / se, 2) if se else None
    adverse = diff < 0 if t is None else t <= ADVERSE_T
    return {"filled": f, "unfilled": u, "difference": diff,
            "t": t, "adverse": bool(adverse)}


# ── CLI: read a settle_bot-shaped journal and resolve settlement ──────────

def _load_events(bot_dir):
    """(placed, filled) keyed by ticker, parsed from settle_events.jsonl."""
    placed, filled = {}, set()
    path = Path(bot_dir) / "settle_events.jsonl"
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        if e["action"] == "place":
            m = re.match(r"(YES|NO) x\d+ limit ([\d.]+)", e.get("reason", ""))
            if m:
                placed[e["ticker"]] = {"ticker": e["ticker"], "side": m.group(1),
                                       "limit": float(m.group(2))}
        elif e["action"] == "enter":
            filled.add(e["ticker"])
    return placed, filled


def _settlement(tickers):
    """Which side settled, from the sign of `distance` at each market's last
    observed tick -- the same basis trade_grader uses (99.5% agreement)."""
    last = {}
    with open(FEATURE_LOG) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            t = r.get("ticker")
            if t in tickers and r.get("distance") is not None:
                if t not in last or r["ts"] > last[t]["ts"]:
                    last[t] = r
    return {t: ("YES" if (v.get("distance") or 0) > 0 else "NO")
            for t, v in last.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(BASE / "data" / "settle"))
    args = ap.parse_args()

    placed, filled_set = _load_events(args.dir)
    settled = _settlement(set(placed))
    rows = [dict(p, settled=settled[t]) for t, p in placed.items() if t in settled]
    f = [r for r in rows if r["ticker"] in filled_set]
    u = [r for r in rows if r["ticker"] not in filled_set]

    r = fill_quality(f, u)
    print(f"placed {len(placed)}  settlement resolved {len(rows)}")
    for k in ("filled", "unfilled"):
        g = r[k]
        if g["n"]:
            print(f"  {k:9} n={g['n']:4}  edge={g['edge']:+.4f}  "
                  f"win={g['win_pct']:5.1f}%  se={g['se']:.4f}")
    if r["difference"] is not None:
        print(f"  difference {r['difference']:+.4f}  t={r['t']}")
        print("  VERDICT: ADVERSE SELECTION — the entry mechanism is selecting "
              "against you" if r["adverse"] else
              "  VERDICT: no adverse selection detected")


if __name__ == "__main__":
    main()
