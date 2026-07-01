#!/usr/bin/env python3
"""
Daily BTC thesis journal — logs the pre-market directional read so we can grade
whether the daily research actually beats a coin flip. Paper only, no orders.

Grading is self-contained: each day's thesis records BTC spot at call time; a
thesis is scored against the NEXT day's recorded spot (UP wins if spot rose,
DOWN wins if it fell). WAIT theses are not scored (no directional bet).

record:  python3 daily_thesis.py record BIAS SPOT [--conviction 1-5] [--level L] [--note "..."]
           BIAS = UP | DOWN | WAIT
report:  python3 daily_thesis.py report
"""
import argparse
import json
from pathlib import Path

from backtest_gate import DATA

LOG = DATA / "daily_thesis.jsonl"


def record(bias, spot, conviction, level, note, date):
    bias = bias.upper()
    if bias not in ("UP", "DOWN", "WAIT"):
        raise SystemExit("BIAS must be UP, DOWN, or WAIT")
    rec = {
        "date": date,
        "bias": bias,
        "spot": float(spot),
        "conviction": int(conviction),
        "level": level or "",
        "note": note or "",
    }
    DATA.mkdir(parents=True, exist_ok=True)
    # Upsert: replace any existing entry for the same date, then append the new one.
    existing = []
    if LOG.exists():
        for line in LOG.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if row.get("date") != date:
                    existing.append(line)
            except Exception:
                existing.append(line)
    with LOG.open("w") as f:
        for line in existing:
            f.write(line + "\n")
        f.write(json.dumps(rec) + "\n")
    print(f"Logged {date}: {bias} (conv {conviction}) @ spot ${float(spot):,.0f}"
          f"{' | ' + note if note else ''}")


def report():
    if not LOG.exists():
        print("No theses logged yet.")
        return
    rows = [json.loads(l) for l in open(LOG) if l.strip()]
    rows.sort(key=lambda r: r.get("date", ""))
    print(f"=== DAILY BTC THESES ({len(rows)} logged) ===")
    print(f"{'date':12} {'bias':5} {'conv':>4} {'spot':>10} {'next':>10} {'result':>7}  note")
    graded = []
    for i, r in enumerate(rows):
        nxt = rows[i + 1]["spot"] if i + 1 < len(rows) else None
        result = "—"
        if r["bias"] in ("UP", "DOWN") and nxt is not None:
            won = (nxt > r["spot"]) if r["bias"] == "UP" else (nxt < r["spot"])
            result = "WIN" if won else "loss"
            graded.append(won)
        nxt_s = f"${nxt:,.0f}" if nxt is not None else "(pending)"
        print(f"{r['date']:12} {r['bias']:5} {r['conviction']:>4} "
              f"${r['spot']:>9,.0f} {nxt_s:>10} {result:>7}  {r['note'][:32]}")
    if graded:
        w = sum(graded)
        print(f"\nGRADED {len(graded)} directional theses | win rate {100*w/len(graded):.0f}% "
              f"(50% = coin flip; needs to clear 50% + costs to be worth real money)")
    else:
        print("\nNo gradable theses yet (need a directional call followed by a later day).")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    pr = sub.add_parser("record")
    pr.add_argument("bias")
    pr.add_argument("spot")
    pr.add_argument("--conviction", default=2)
    pr.add_argument("--level", default="")
    pr.add_argument("--note", default="")
    pr.add_argument("--date", default="", help="YYYY-MM-DD; pass the run date (Date.now unavailable in scripts)")
    sub.add_parser("report")
    args = ap.parse_args()
    if args.cmd == "record":
        if not args.date:
            raise SystemExit("--date YYYY-MM-DD is required")
        record(args.bias, args.spot, args.conviction, args.level, args.note, args.date)
    elif args.cmd == "report":
        report()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
