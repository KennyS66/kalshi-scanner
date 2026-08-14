import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

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
