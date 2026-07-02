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
from datetime import datetime, timezone
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


def thesis_age_days(t):
    """Whole UTC days between the thesis date and today; None if unparseable."""
    d = str(t.get("date", "")).strip()
    try:
        td = datetime.strptime(d, "%Y-%m-%d").date()
    except Exception:
        return None
    return (datetime.now(timezone.utc).date() - td).days


def parse_level(s):
    digits = "".join(c for c in str(s) if c.isdigit() or c == ".")
    try:
        return float(digits)
    except Exception:
        return None


# A same-day thesis can still be broken by price action hours before the next
# research cycle. This is the noise floor for calling that a genuine break
# rather than a wick/test — matches the existing "~$400 test" convention used
# in the watch-plan text below.
PRICE_BREAK_THRESHOLD = 400.0


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

    # Staleness guard: a thesis only configures today's loop if it is today's
    # (UTC). An older directional call is degraded to WAIT so the loop keeps
    # watching the level but does NOT trade a stale bias. Key level is retained.
    age = thesis_age_days(t)
    stale = age is None or age >= 1
    orig_bias = bias
    if stale:
        bias = "WAIT"
        age_txt = "unknown age" if age is None else f"{age}d old"
        print("!" * 56)
        print(f"!! STALE THESIS ({age_txt}) — dated {t.get('date','?')}, today is "
              f"{datetime.now(timezone.utc).date()} (UTC).")
        print(f"!! Original bias {orig_bias} DEGRADED to WAIT. Record a fresh thesis:")
        print(f"!!   python3 daily_thesis.py record BIAS <spot> --conviction N --level {int(key) if key else 60000}")
        print("!" * 56)

    # Price-invalidation guard: independent of calendar staleness. If spot has
    # moved cleanly through the key level against a same-day bias, the bias no
    # longer reflects the market and should stop being favored until a fresh
    # thesis confirms a side — this is what makes the bias a live variable
    # instead of a value frozen at whatever daily_research.py last wrote.
    invalidated = False
    invalidation_msg = None
    gap_signed = (spot - key) if (spot and key) else None
    if not stale and bias in ("UP", "DOWN") and gap_signed is not None:
        if bias == "DOWN" and gap_signed > PRICE_BREAK_THRESHOLD:
            invalidated = True
            invalidation_msg = (f"spot ${gap_signed:,.0f} ABOVE key ${key:,.0f} — "
                                 f"a bounce broke through the DOWN thesis's level")
        elif bias == "UP" and -gap_signed > PRICE_BREAK_THRESHOLD:
            invalidated = True
            invalidation_msg = (f"spot ${-gap_signed:,.0f} BELOW key ${key:,.0f} — "
                                 f"a rejection broke through the UP thesis's level")

    if invalidated:
        bias = "WAIT"
        print("!" * 56)
        print(f"!! BIAS INVALIDATED BY PRICE ACTION — {invalidation_msg}.")
        print(f"!! Original bias {orig_bias} DEGRADED to WAIT for this session (counter-trend zone).")
        print(f"!! Not stale by date, but price has already done what the thesis warned about.")
        print("!" * 56)

    print(f"=== DAY PLAN  ({t.get('date','?')}) ===")
    if stale:
        conv = "n/a (stale→WAIT)"
    elif invalidated:
        conv = "n/a (price-invalidated→WAIT)"
    else:
        conv = t.get("conviction", "?")
    print(f"Bias       : {bias}  (conviction {conv})")
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
    if invalidated:
        print(f"Favor      : nothing directional — original {orig_bias} bias was broken by price; stand down.")
        print(f"Watch      : a reclaim of key ${key:,.0f} back on the {orig_bias.lower()} side re-confirms the "
              f"original thesis; continuation the other way sets up a fresh counter-trend read.")
        print(f"Note       : this is a live re-read of price vs. the level, not a new researched thesis — "
              f"treat conviction as low until the next research cycle.")
    elif bias == "DOWN":
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

    print(f"\nPLAN: bias={bias} key={int(key)} spot={int(spot) if spot else 0} pos={pos} "
          f"stale={'yes' if stale else 'no'} invalidated={'yes' if invalidated else 'no'}")


if __name__ == "__main__":
    main()
