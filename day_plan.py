#!/usr/bin/env python3
"""
Day plan — turns the latest daily thesis (bias + key level) plus the current
BTC spot into a concrete watch plan the market-analysis loop configures itself
from. Makes the loop ADAPTIVE: a DOWN day watches support for breaks and favors
NO/short setups; an UP day watches resistance and favors YES/long; a WAIT/range
day just monitors the level and stays quiet.

Reads theses from data/whales/daily_thesis.jsonl (local) and
data/daily_theses.jsonl (written by the cloud pre-market routine), taking the
most recent by date. Fetches spot from the running scanner.

Usage:  python3 day_plan.py
Prints a human summary plus a machine line: "PLAN: bias=.. key=.. spot=.. pos=.."
"""
import json
import urllib.request
from pathlib import Path

from backtest_gate import DATA

THESIS_FILES = [DATA / "daily_thesis.jsonl", Path("data/daily_theses.jsonl")]
API = "http://localhost:9050/api/crypto/spot"
SIGNAL_API = "http://localhost:9050/api/crypto/signal"


def latest_thesis():
    rows = []
    for f in THESIS_FILES:
        if f.exists():
            for line in f.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    if not rows:
        return None
    rows.sort(key=lambda r: (r.get("date", ""), r.get("bias") != "WAIT"))  # prefer a directional call same-day
    return rows[-1]


def current_spot():
    for url, key in ((API, "btc"), (SIGNAL_API, "spot")):
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                d = json.loads(r.read())
                v = d.get(key)
                if v:
                    return float(v)
        except Exception:
            pass
    return None


def parse_level(s):
    digits = "".join(c for c in str(s) if c.isdigit() or c == ".")
    try:
        return float(digits)
    except Exception:
        return None


def main():
    t = latest_thesis()
    spot = current_spot()
    if not t:
        print("No daily thesis yet — run daily_thesis.py / wait for the 8am routine.")
        print("PLAN: bias=NONE key=0 spot={} pos=unknown".format(int(spot) if spot else 0))
        return

    bias = t.get("bias", "WAIT")
    key = parse_level(t.get("level")) or 0
    note = t.get("note", "")
    print(f"=== DAY PLAN  ({t.get('date','?')}) ===")
    print(f"Bias       : {bias}  (conviction {t.get('conviction','?')})")
    print(f"Thesis     : {note}")
    print(f"Key level  : ${key:,.0f}" if key else "Key level  : (none parsed)")
    if spot:
        print(f"Spot now   : ${spot:,.0f}")

    pos = "unknown"
    if spot and key:
        if spot > key:
            pos = "above"; gap = spot - key
            print(f"Position   : ${gap:,.0f} ABOVE the key level")
        elif spot < key:
            pos = "below"; gap = key - spot
            print(f"Position   : ${gap:,.0f} BELOW the key level")
        else:
            pos = "at"

    # Adaptive watch guidance
    print("\n-- watch plan --")
    if bias == "DOWN":
        print(f"Favor      : NO/short setups; trend-aligned with the down bias.")
        print(f"Watch      : key SUPPORT ${key:,.0f} — flag a TEST (~within $400) and a confirmed BREAK below.")
        print(f"Bounce risk: high at the level — bank short profit into it; a bounce = counter-trend long.")
    elif bias == "UP":
        print(f"Favor      : YES/long setups; trend-aligned with the up bias.")
        print(f"Watch      : key RESISTANCE ${key:,.0f} — flag a TEST and a confirmed BREAK above.")
        print(f"Reject risk: high at the level — bank long profit into it; a rejection = counter-trend short.")
    else:  # WAIT / range
        print(f"Favor      : nothing directional — WAIT/range day.")
        print(f"Watch      : key level ${key:,.0f} only; flag a confirmed break either way. Stay quiet in the range.")
    print("Cadence    : ~5min in chop; tighten near the key level; faster only on a confirmed break.")

    print(f"\nPLAN: bias={bias} key={int(key)} spot={int(spot) if spot else 0} pos={pos}")


if __name__ == "__main__":
    main()
