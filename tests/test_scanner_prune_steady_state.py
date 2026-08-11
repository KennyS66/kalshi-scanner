"""Does _ticker_stats actually stay bounded under the MEASURED arrival rate?

The unit tests in test_scanner_prune.py prove prune_closed removes the right
single entry. They do not prove the thing that actually broke: that the
container stops growing when tickers arrive continuously for a day.

Rate comes from the live process on 2026-08-10, sampled via
/api/debug/memory at two uptimes:

    0:30   _ticker_stats 2669   market_snapshots 288
    4:12   _ticker_stats 5296   market_snapshots 404

~710 new tickers/minute, ~42,600/hour, none of which ever gets a snapshot
(only crypto 15M tickers and the top 200 by volume are enriched). Left
unbounded that is ~1M dicts over a day -- the 31h run that ended at 1858MB.

Simulated against a fake clock so a full day runs in a few seconds. The
`ttl_disabled` control exists because a bounds test that passes with AND
without the fix proves nothing; it pins that this simulation really does
reproduce the unbounded growth.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scanner as scanner_mod
from scanner import MarketSnapshot, Scanner

ARRIVALS_PER_MIN = 710          # measured
HOURS = 24
CONTROL_HOURS = 3               # enough to pass the TTL and show growth
TTL_S = 3600.0
UNBOUNDED = ARRIVALS_PER_MIN * 60 * HOURS      # ~1.02M with no expiry
STEADY = ARRIVALS_PER_MIN * 60                 # one TTL of arrivals


class FakeClock:
    def __init__(self, start=1_754_800_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _run_day(ttl_s, hours=HOURS):
    """Feed arrivals for HOURS, pruning each minute. Returns the size trace.

    Restores time.time itself rather than using monkeypatch so the trace can
    be computed once for the whole module -- a day of arrivals is ~1M dict
    creations and is not worth repeating per assertion.
    """
    clock = FakeClock()
    real_time = scanner_mod.time.time
    scanner_mod.time.time = clock
    try:
        s = Scanner(api=None)
        trace = []
        for minute in range(hours * 60):
            for i in range(ARRIVALS_PER_MIN):
                # A ticker that never gets a snapshot, exactly like the
                # non-crypto markets that make up the bulk of arrivals.
                s._ticker_stats[f"KXNOSNAP-{minute}-{i}"]["last_ts"] = clock.now
            s.prune_closed(stats_ttl_s=ttl_s)
            clock.advance(60)
            trace.append(len(s._ticker_stats))
        return trace
    finally:
        scanner_mod.time.time = real_time


@pytest.fixture(scope="module")
def trace():
    return _run_day(TTL_S)


@pytest.fixture(scope="module")
def trace_ttl_disabled():
    """Control: the same simulation with expiry effectively off.

    Deliberately short. Proving the simulation reproduces unbounded growth
    needs only enough horizon to pass the TTL; running the full day here
    would retain ~1M dicts (~500MB) to demonstrate nothing extra.
    """
    return _run_day(ttl_s=10 ** 12, hours=CONTROL_HOURS)


def test_control_reproduces_the_unbounded_growth(trace_ttl_disabled):
    """Without expiry the simulation must actually blow up, or every other
    assertion in this file is vacuous.

    Nothing is expired at all: every arrival is still resident.
    """
    assert trace_ttl_disabled[-1] == ARRIVALS_PER_MIN * 60 * CONTROL_HOURS
    # And it is still climbing at the end, not plateauing.
    assert trace_ttl_disabled[-1] > trace_ttl_disabled[len(trace_ttl_disabled) // 2]


def test_ticker_stats_plateaus_instead_of_growing_all_day(trace):
    assert trace[-1] < UNBOUNDED / 20, (
        f"still growing: ended at {trace[-1]} of an unbounded {UNBOUNDED}")

    # Flat across the back half, not merely slower: hour 12 vs hour 24.
    assert abs(trace[-1] - trace[len(trace) // 2]) < ARRIVALS_PER_MIN * 60, (
        f"not a plateau: {trace[len(trace)//2]} at 12h -> {trace[-1]} at 24h")


def test_steady_state_is_about_one_ttl_of_arrivals(trace):
    """Sanity on the bound itself: ~1h of arrivals, not 10x that."""
    assert STEADY * 0.8 <= trace[-1] <= STEADY * 1.5, (
        f"steady state {trace[-1]} is not ~{STEADY}")


def test_open_markets_are_never_dropped_however_long_it_runs(monkeypatch):
    """A live snapshot must outrank trade staleness for the whole day, or the
    scanner forgets the market it is meant to be watching."""
    clock = FakeClock()
    monkeypatch.setattr(scanner_mod.time, "time", clock)
    s = Scanner(api=None)
    s.market_snapshots["KXBTC15M-LIVE"] = MarketSnapshot(
        ticker="KXBTC15M-LIVE", close_ts=clock.now + HOURS * 3600 + 600)
    s._ticker_stats["KXBTC15M-LIVE"]["last_ts"] = clock.now   # never traded again

    for _ in range(HOURS * 60):
        s.prune_closed(stats_ttl_s=TTL_S)
        clock.advance(60)

    assert "KXBTC15M-LIVE" in s._ticker_stats
    assert "KXBTC15M-LIVE" in s.market_snapshots
