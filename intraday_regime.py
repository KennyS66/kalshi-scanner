#!/usr/bin/env python3
"""
Intraday regime journal — a session-level overlay on the immutable daily thesis.

The daily thesis (daily_thesis.py) is recorded once pre-market and graded
against next-day spot; it must never be rewritten intraday or the grading
experiment breaks. But after a price invalidation (or a regime shift the
morning research couldn't see), the loop is left with WAIT — an absence of a
read. This journal fills that hole: the market-analysis loop appends a regime
entry at session boundaries, on thesis invalidation, or when the live range
shifts, and day_plan.py overlays the latest same-day entry onto the watch plan.

Bias still comes ONLY from the graded daily call. The regime layer adapts the
watch plan (live levels, range vs trend, session context) — it never asserts
a directional bias of its own.

record:  python3 intraday_regime.py record REGIME --lo L --hi H [--note "..."]
           REGIME = range | trend_up | trend_down | breakout_watch
show:    python3 intraday_regime.py show
"""
import argparse
import json
import time
from datetime import datetime, timezone

from backtest_gate import DATA

LOG = DATA / "intraday_regime.jsonl"
REGIMES = ("range", "trend_up", "trend_down", "breakout_watch")

# Rough UTC session map (crypto trades 24/7; these mirror the session
# conventions already used in the loop notes: US open 13:00 UTC etc.).
SESSIONS = (
    (0, 7, "asia"),
    (7, 13, "europe"),
    (13, 21, "us"),
    (21, 24, "late"),
)


def current_session(dt=None) -> str:
    h = (dt or datetime.now(timezone.utc)).hour
    for lo, hi, name in SESSIONS:
        if lo <= h < hi:
            return name
    return "late"


def record(regime, lo, hi, note):
    regime = regime.lower()
    if regime not in REGIMES:
        raise SystemExit(f"REGIME must be one of {', '.join(REGIMES)}")
    now = datetime.now(timezone.utc)
    rec = {
        "ts": time.time(),
        "date": now.strftime("%Y-%m-%d"),
        "session": current_session(now),
        "regime": regime,
        "range_lo": float(lo) if lo else None,
        "range_hi": float(hi) if hi else None,
        "note": note or "",
    }
    DATA.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"Regime {now.strftime('%H:%M')}Z [{rec['session']}]: {regime}"
          + (f" {rec['range_lo']:,.0f}-{rec['range_hi']:,.0f}" if rec["range_lo"] and rec["range_hi"] else "")
          + (f" | {note}" if note else ""))


def latest_today():
    """Most recent regime entry for today (UTC), or None."""
    if not LOG.exists():
        return None
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    last = None
    for line in LOG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if row.get("date") == today:
            last = row
    return last


def show():
    r = latest_today()
    if not r:
        print("No regime entry for today (UTC).")
        return
    t = datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%H:%M")
    rng = (f" {r['range_lo']:,.0f}-{r['range_hi']:,.0f}"
           if r.get("range_lo") and r.get("range_hi") else "")
    print(f"{t}Z [{r.get('session','?')}] {r.get('regime','?')}{rng}"
          + (f" | {r.get('note','')}" if r.get("note") else ""))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    pr = sub.add_parser("record")
    pr.add_argument("regime")
    pr.add_argument("--lo", default="")
    pr.add_argument("--hi", default="")
    pr.add_argument("--note", default="")
    sub.add_parser("show")
    args = ap.parse_args()
    if args.cmd == "record":
        record(args.regime, args.lo, args.hi, args.note)
    elif args.cmd == "show":
        show()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
