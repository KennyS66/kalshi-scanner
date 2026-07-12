#!/usr/bin/env python3
"""
Daily BTC thesis journal — logs the pre-market directional read so we can grade
whether the daily research actually beats a coin flip. Paper only, no orders.

Grading: each day's thesis records BTC spot at call time and is scored against
that day's UTC close (CoinGecko; falls back to the next recorded spot when
offline). Days that move <0.25% grade flat and are not counted. WAIT theses are
not scored, but the report also tracks per-signal-class reliability (MA
structure, momentum, F&G, ETF flow) on every non-flat day.

record:  python3 daily_thesis.py record BIAS SPOT [--conviction 1-5] [--level L] [--note "..."]
           BIAS = UP | DOWN | WAIT
report:  python3 daily_thesis.py report
"""
import argparse
import json
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backtest_gate import DATA

LOG = DATA / "daily_thesis.jsonl"

# A day that moves less than this from the recorded spot grades as FLAT
# (neither win nor loss) — a directional call can't claim a 0.1% drift.
FLAT_PCT = 0.25


def _daily_closes(days=35):
    """date(YYYY-MM-DD) -> that day's UTC close, from CoinGecko daily points.

    CoinGecko's daily points are stamped 00:00 UTC, i.e. each one is the CLOSE
    of the previous UTC day. Returns {} offline (report falls back to grading
    against the next recorded spot).
    """
    url = ("https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
           f"?vs_currency=usd&days={days}&interval=daily")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (kalshi-scanner/1.0)"})
        with urllib.request.urlopen(req, timeout=12) as r:
            pts = json.loads(r.read())["prices"]
    except Exception:
        return {}
    closes = {}
    for ts_ms, price in pts:
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        if dt.hour == 0 and dt.minute == 0:
            closes[(dt - timedelta(days=1)).strftime("%Y-%m-%d")] = price
    return closes


def _signal_dirs(note):
    """Parse a thesis note into {signal_class: +1|-1} for every directional
    signal it recorded. Classes: ma_structure, momentum, fear_greed, etf_flow."""
    dirs = {}
    if "bullish structure" in note or re.search(r"above 50d[^;]*MA", note):
        dirs["ma_structure"] = +1
    elif "bearish structure" in note or re.search(r"below 50d[^;]*MA", note):
        dirs["ma_structure"] = -1
    m = re.search(r"14d ([+-][\d.]+)% — short-term trend (up|down)", note)
    if m:
        dirs["momentum"] = +1 if m.group(2) == "up" else -1
    m = re.search(r"F&G (\d+)", note)
    if m:
        fg = int(m.group(1))
        if fg <= 35:
            dirs["fear_greed"] = -1
        elif fg >= 65:
            dirs["fear_greed"] = +1
    m = re.search(r"ETF flow ([+-])\$", note)
    if m:
        dirs["etf_flow"] = +1 if m.group(1) == "+" else -1
    return dirs


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
    closes = _daily_closes()
    basis = "same-day UTC close" if closes else "next recorded spot (offline fallback)"
    print(f"=== DAILY BTC THESES ({len(rows)} logged, graded vs {basis}) ===")
    print(f"{'date':12} {'bias':5} {'conv':>4} {'spot':>10} {'close':>10} {'day%':>7} {'result':>7}  note")
    graded = []          # (won, conviction)
    sig_stats = {}       # class -> [n, wins]
    for i, r in enumerate(rows):
        close = closes.get(r["date"])
        if close is None and i + 1 < len(rows):
            close = rows[i + 1]["spot"]
        result, day_pct, actual = "—", None, 0
        if close is not None:
            day_pct = (close / r["spot"] - 1) * 100
            if abs(day_pct) < FLAT_PCT:
                result = "flat"
            else:
                actual = 1 if day_pct > 0 else -1
                if r["bias"] in ("UP", "DOWN"):
                    won = (actual > 0) if r["bias"] == "UP" else (actual < 0)
                    result = "WIN" if won else "loss"
                    graded.append((won, int(r.get("conviction", 2))))
                # Signal classes are scored on every non-flat day, WAIT included:
                # each signal made its own directional claim regardless of bias.
                for cls, d in _signal_dirs(r.get("note", "")).items():
                    n_w = sig_stats.setdefault(cls, [0, 0])
                    n_w[0] += 1
                    n_w[1] += 1 if d == actual else 0
        close_s = f"${close:,.0f}" if close is not None else "(pending)"
        pct_s = f"{day_pct:+.1f}%" if day_pct is not None else ""
        print(f"{r['date']:12} {r['bias']:5} {r['conviction']:>4} "
              f"${r['spot']:>9,.0f} {close_s:>10} {pct_s:>7} {result:>7}  {r['note'][:40]}")

    if graded:
        w = sum(1 for won, _ in graded if won)
        print(f"\nGRADED {len(graded)} directional theses | win rate {100*w/len(graded):.0f}% "
              f"(50% = coin flip; needs to clear 50% + costs to be worth real money)")
        hi = [(won, c) for won, c in graded if c >= 3]
        if hi:
            hw = sum(1 for won, _ in hi if won)
            print(f"  conviction ≥3: {hw}/{len(hi)} won")
    else:
        print("\nNo gradable theses yet (need a directional call followed by a later day).")

    if sig_stats:
        print("\n-- signal-class reliability (direction each signal implied vs actual day) --")
        for cls, (n, wins) in sorted(sig_stats.items(), key=lambda kv: -kv[1][0]):
            print(f"  {cls:14} {wins}/{n} right ({100*wins/n:.0f}%)")


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
