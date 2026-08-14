import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

import wsrig.verify_tape as vt
from wsrig.verify_tape import verify


def _spot(tm, tw=None, p=63000.0):
    return {"k": "spot", "tm": tm, "tw": tw if tw is not None else 1_780_000_000.0 + tm, "p": p}


def _named(result, name):
    return next(c for c in result["checks"] if c["name"] == name)


def test_clean_tape_passes():
    recs = [_spot(t) for t in range(0, 600)]
    assert verify(recs, expected_spot_rate_hz=1.0)["ok"] is True


def test_detects_monotonic_clock_going_backwards():
    recs = [_spot(0), _spot(5), _spot(3)]
    assert _named(verify(recs), "monotonic_ordering")["ok"] is False


def test_detects_a_wall_clock_step():
    """NTP stepping the wall clock breaks any tw-based join to settlement."""
    recs = [_spot(0, tw=1_780_000_000.0), _spot(1, tw=1_780_000_001.0),
            _spot(2, tw=1_780_000_060.0)]          # +59s of wall for 1s of mono
    assert _named(verify(recs), "clock_drift")["ok"] is False


def test_detects_a_spot_feed_silence_gap():
    recs = [_spot(t) for t in range(0, 60)] + [_spot(t) for t in range(400, 460)]
    assert _named(verify(recs), "spot_continuity")["ok"] is False


def test_detects_degraded_spot_feed_rate():
    """A feed can degrade without hitting a 120s gap. Verify() must catch the rate shortfall."""
    # 100 ticks spaced 10 seconds apart = span of 990s, rate ≈ 0.101/s
    # This is well below expected 1.0/s, but has no individual gap > 120s
    recs = [_spot(t * 10) for t in range(0, 100)]
    c = _named(verify(recs, expected_spot_rate_hz=1.0), "spot_continuity")
    assert c["ok"] is False and "RATE SHORTFALL" in c["detail"]


def test_reports_sequence_gap_records():
    recs = [_spot(0), {"k": "gap", "tm": 1.0, "sid": 1, "expected": 5, "got": 9}, _spot(2)]
    c = _named(verify(recs), "sequence_gaps")
    assert c["ok"] is False and "1" in c["detail"]


def test_empty_tape_fails_rather_than_vacuously_passing():
    """The worst outcome is a rig that captured nothing and reported OK."""
    assert verify([])["ok"] is False


def test_book_coverage_flags_a_settled_market_with_no_quotes():
    recs = [_spot(0), {"k": "settle", "tm": 1.0, "t": "KXBTC15M-A", "result": "yes"}]
    assert _named(verify(recs), "book_coverage")["ok"] is False


def test_book_coverage_passes_when_the_market_has_quotes():
    recs = [_spot(0),
            {"k": "book", "tm": 0.5, "t": "KXBTC15M-A", "ya": 0.4, "na": 0.6},
            {"k": "settle", "tm": 1.0, "t": "KXBTC15M-A", "result": "yes"}]
    assert _named(verify(recs), "book_coverage")["ok"] is True


# ---------------------------------------------------------------- feed health

def _outage(tm, kind="feed_drop", src="kalshi"):
    return {"k": kind, "tm": tm, "tw": 1_780_000_000.0 + tm, "src": src}


def test_feed_health_passes_on_a_quiet_capture():
    recs = [_spot(t) for t in range(0, 3600)] + [_outage(10), _outage(20)]
    assert _named(verify(recs), "feed_health")["ok"] is True


def test_feed_health_flags_a_tape_full_of_reconnects():
    """A capture that spent the day reconnecting still produces a tidy-looking
    edge number. These records exist to make that loud."""
    recs = [_spot(t) for t in range(0, 3600)]
    recs += [_outage(t, kind="feed_stall") for t in range(0, 60)]
    c = _named(verify(recs), "feed_health")
    assert c["ok"] is False and "feed_stall" in c["detail"]


def test_feed_health_counts_a_fatal_feed_error():
    recs = [_spot(t) for t in range(0, 600)] + [_outage(t, kind="feed_error")
                                                for t in range(0, 10)]
    assert _named(verify(recs), "feed_health")["ok"] is False


# --------------------------------------------------------- settlement coverage

def _book(tw, ticker="KXBTC15M-A"):
    return {"k": "book", "tm": tw - 1_780_000_000.0, "tw": tw, "t": ticker, "ya": 0.4}


def test_settlement_coverage_flags_a_quoted_market_that_never_settled():
    """A stalled settlement poller leaves `pending` full and writes nothing —
    invisible to every other check."""
    recs = [_book(1_780_000_000.0), _spot(0, tw=1_780_010_000.0)]   # ~2.8h later
    c = _named(verify(recs), "settlement_coverage")
    assert c["ok"] is False and "KXBTC15M-A" in c["detail"]


def test_settlement_coverage_passes_once_the_market_settled():
    recs = [_book(1_780_000_000.0),
            {"k": "settle", "tm": 1.0, "tw": 1_780_000_600.0,
             "t": "KXBTC15M-A", "result": "yes"},
            _spot(0, tw=1_780_010_000.0)]
    assert _named(verify(recs), "settlement_coverage")["ok"] is True


def test_settlement_coverage_ignores_a_market_still_being_quoted():
    """No complaint about a market that simply has not closed yet."""
    recs = [_book(1_780_000_000.0), _spot(0, tw=1_780_000_300.0)]
    assert _named(verify(recs), "settlement_coverage")["ok"] is True


# ------------------------------------------------------------------------- CLI

def test_the_cli_passes_the_expected_rate_through(monkeypatch):
    """The rate check was dead code from the CLI: main() always used the 1.0
    default, which passes almost any degraded feed."""
    seen = {}

    def fake_verify(records, expected_spot_rate_hz=None):
        seen["rate"] = expected_spot_rate_hz
        return {"ok": True, "checks": []}

    monkeypatch.setattr(vt, "read_tape", lambda d: iter([]))
    monkeypatch.setattr(vt, "verify", fake_verify)
    monkeypatch.setattr(sys, "argv",
                        ["verify_tape", "--dir", "x", "--expected-spot-rate-hz", "7.5"])
    with pytest.raises(SystemExit):
        vt.main()
    assert seen["rate"] == 7.5


def test_the_cli_default_rate_is_not_the_placeholder_one_per_second(monkeypatch):
    seen = {}

    def fake_verify(records, expected_spot_rate_hz=None):
        seen["rate"] = expected_spot_rate_hz
        return {"ok": True, "checks": []}

    monkeypatch.setattr(vt, "read_tape", lambda d: iter([]))
    monkeypatch.setattr(vt, "verify", fake_verify)
    monkeypatch.setattr(sys, "argv", ["verify_tape"])
    with pytest.raises(SystemExit):
        vt.main()
    assert seen["rate"] == vt.DEFAULT_SPOT_RATE_HZ > 1.0
