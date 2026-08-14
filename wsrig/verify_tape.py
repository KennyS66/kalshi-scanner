"""Is this tape trustworthy enough to draw a conclusion from?

A capture rig that silently dies for six hours still yields a tidy-looking
edge number. Every check here exists to make that failure loud instead.

Run before ANY analysis:  .venv/bin/python -m wsrig.verify_tape
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

from wsrig.tape import read_tape

MAX_SPOT_SILENCE_S = 120.0     # BTC never goes 2 minutes without a trade
MAX_CLOCK_DRIFT_S = 2.0        # |Δwall − Δmono| tolerated across the capture

# Placeholder. Coinbase's ticker channel prints on every match, which for BTC-USD
# is comfortably more than 1/s — 1.0 was low enough to pass a badly degraded
# feed. Retune this to the rate Task 8's smoke capture actually measures.
DEFAULT_SPOT_RATE_HZ = 2.0

# Records the rig writes when a feed is in trouble. They exist to be counted
# here; a tape full of reconnects is not a tape to draw a conclusion from.
OUTAGE_KINDS = ("feed_drop", "feed_stall", "feed_error")
# One outage per 10 minutes is already a degraded capture. The max(span, 1h)
# floor keeps a short tape from being graded leniently just for being short.
MAX_OUTAGES_PER_HOUR = 6.0
# KXBTC15M markets resolve within minutes of close. An hour past a market's last
# quote with no settle record means the settlement poller is stuck, not that
# Kalshi is slow — and pending/ never shrinking is invisible in the tape itself.
SETTLE_GRACE_S = 3600.0


def _check(name, ok, detail):
    return {"name": name, "ok": bool(ok), "detail": detail}


def verify(records: list[dict],
           expected_spot_rate_hz: float = DEFAULT_SPOT_RATE_HZ) -> dict:
    checks = []

    if not records:
        return {"ok": False,
                "checks": [_check("non_empty", False, "tape is empty")]}
    checks.append(_check("non_empty", True, f"{len(records)} records"))

    mono = [r["tm"] for r in records if "tm" in r]
    backwards = sum(1 for a, b in zip(mono, mono[1:]) if b < a)
    checks.append(_check("monotonic_ordering", backwards == 0,
                         f"{backwards} records out of monotonic order"))

    # Wall and monotonic must advance together. A divergence means the wall
    # clock was stepped, which would corrupt any tw-based join.
    paired = [(r["tm"], r["tw"]) for r in records if "tm" in r and "tw" in r]
    worst = 0.0
    for (m0, w0), (m1, w1) in zip(paired, paired[1:]):
        worst = max(worst, abs((w1 - w0) - (m1 - m0)))
    checks.append(_check("clock_drift", worst <= MAX_CLOCK_DRIFT_S,
                         f"worst wall-vs-monotonic step {worst:.3f}s "
                         f"(limit {MAX_CLOCK_DRIFT_S}s)"))

    spot = [r["tm"] for r in records if r.get("k") == "spot"]
    if len(spot) < 2:
        checks.append(_check("spot_continuity", False,
                             f"only {len(spot)} spot ticks"))
    else:
        gaps = [(b - a) for a, b in zip(spot, spot[1:]) if b - a > MAX_SPOT_SILENCE_S]
        span_h = (spot[-1] - spot[0]) / 3600.0
        rate = len(spot) / max(spot[-1] - spot[0], 1e-9)

        # Check both gap condition and rate condition
        min_acceptable_rate = expected_spot_rate_hz * 0.9  # 10% tolerance
        rate_ok = rate >= min_acceptable_rate
        gaps_ok = not gaps
        ok = rate_ok and gaps_ok

        detail = (f"{len(gaps)} silences >{MAX_SPOT_SILENCE_S:.0f}s "
                  f"over {span_h:.1f}h; rate {rate:.2f}/s "
                  f"(expected ~{expected_spot_rate_hz:.2f}/s)")
        if not rate_ok:
            detail += f"; RATE SHORTFALL: {rate:.3f}/s < {min_acceptable_rate:.3f}/s threshold"

        checks.append(_check("spot_continuity", ok, detail))

    gapsr = [r for r in records if r.get("k") == "gap"]
    by_sid = defaultdict(int)
    for g in gapsr:
        by_sid[g.get("sid")] += 1
    checks.append(_check("sequence_gaps", not gapsr,
                         f"{len(gapsr)} sequence gaps"
                         + (f" on sids {sorted(by_sid)}" if by_sid else "")))

    quoted = {r.get("t") for r in records if r.get("k") == "book"}
    settled = {r.get("t") for r in records if r.get("k") == "settle"}
    missing = sorted(settled - quoted)
    checks.append(_check("book_coverage", not missing,
                         f"{len(missing)} settled markets with no quotes"
                         + (f": {missing[:3]}" if missing else "")))

    # Durations below use tw, not tm: monotonic resets to ~0 whenever the process
    # restarts, and read_tape() concatenates every hourly file in the directory.
    wall = [r["tw"] for r in records if "tw" in r]
    span_h = (max(wall) - min(wall)) / 3600.0 if len(wall) >= 2 else 0.0

    outages = defaultdict(int)
    for r in records:
        if r.get("k") in OUTAGE_KINDS:
            outages[r["k"]] += 1
    n_out = sum(outages.values())
    allowed = MAX_OUTAGES_PER_HOUR * max(span_h, 1.0)
    checks.append(_check("feed_health", n_out <= allowed,
                         f"{n_out} feed outage records over {span_h:.1f}h "
                         f"(limit {allowed:.0f})"
                         + (f" {dict(sorted(outages.items()))}" if outages else "")))

    # The inverse of book_coverage: markets we quoted that never settled. A
    # settlement poller that always errors leaves `pending` full and writes
    # nothing, which no other check can see.
    last_quote: dict[str, float] = {}
    for r in records:
        if r.get("k") == "book" and r.get("t") and "tw" in r:
            t = r["t"]
            last_quote[t] = max(last_quote.get(t, r["tw"]), r["tw"])
    end_tw = max(wall) if wall else 0.0
    stale = sorted(t for t, tw in last_quote.items()
                   if t not in settled and end_tw - tw > SETTLE_GRACE_S)
    checks.append(_check("settlement_coverage", not stale,
                         f"{len(stale)} markets last quoted >"
                         f"{SETTLE_GRACE_S / 3600:.0f}h ago with no settle record"
                         + (f": {stale[:3]}" if stale else "")))

    return {"ok": all(c["ok"] for c in checks), "checks": checks}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="data/wsrig")
    ap.add_argument("--expected-spot-rate-hz", type=float,
                    default=DEFAULT_SPOT_RATE_HZ,
                    help="spot ticks/s the feed should sustain; the rate check "
                         "fails below 90%% of it (default: %(default)s)")
    args = ap.parse_args()
    res = verify(list(read_tape(Path(args.dir))),
                 expected_spot_rate_hz=args.expected_spot_rate_hz)
    for c in res["checks"]:
        print(f"  {'PASS' if c['ok'] else 'FAIL'}  {c['name']:20} {c['detail']}")
    print(f"\n  TAPE: {'USABLE' if res['ok'] else 'NOT TRUSTWORTHY'}")
    raise SystemExit(0 if res["ok"] else 1)


if __name__ == "__main__":
    main()
