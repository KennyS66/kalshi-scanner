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


# The Coinbase BTC-USD ticker channel was measured at 1.58 msg/s over a 45s
# live capture on 2026-08-15, during a quiet stretch.
MEASURED_SPOT_RATE_HZ = 1.58


def test_the_real_measured_spot_rate_passes_with_the_shipped_default():
    """The default used to be 2.0/s with a 0.9 tolerance — an effective floor of
    1.8/s, above the rate the feed actually runs at. Every verify() on a
    perfectly healthy tape would have reported NOT TRUSTWORTHY, which is worse
    than no check at all: it teaches you to ignore the one alarm that matters."""
    step = 1.0 / MEASURED_SPOT_RATE_HZ
    recs = [_spot(i * step) for i in range(int(3600 * MEASURED_SPOT_RATE_HZ))]
    c = _named(verify(recs), "spot_continuity")
    assert c["ok"] is True, c["detail"]


def test_the_default_rate_floor_still_catches_a_catastrophically_dead_feed():
    """The floor exists for "the feed died or is 10x down", not to police the
    normal variation that tracks BTC trading activity."""
    recs = [_spot(i * 20.0) for i in range(180)]        # 0.05/s, no >120s gap
    c = _named(verify(recs), "spot_continuity")
    assert c["ok"] is False and "RATE SHORTFALL" in c["detail"]


def test_reports_sequence_gap_records():
    recs = [_spot(0), {"k": "gap", "tm": 1.0, "sid": 1, "expected": 5, "got": 9}, _spot(2)]
    c = _named(verify(recs), "sequence_gaps")
    assert c["ok"] is False and "1" in c["detail"]
    assert c["applicable"] is True   # a gap record is proof tracking was live


# `seq` is present only on orderbook_delta/orderbook_snapshot messages, never
# on `ticker`. The shipped rig captures ticker-only (measured: orderbook_delta
# is 685 msg/s / 99.85% of traffic vs 1 msg/s for ticker), so on every real
# tape SeqTracker never fires and `gapsr` is permanently []. Reporting PASS in
# that case is a lie: the check never had a chance to fail. See the Phase 0
# smoke capture write-up for the measured 0/3575 book records carrying `seq`.

def test_sequence_gaps_reports_not_applicable_when_no_record_carries_seq():
    recs = [_spot(0), _book(1_780_000_000.0)]              # seq absent, ticker-only
    c = _named(verify(recs), "sequence_gaps")
    assert c["ok"] is True                     # N/A must not fail the tape
    assert c["applicable"] is False
    assert "seq" in c["detail"].lower()


def test_not_applicable_sequence_gaps_does_not_fail_the_overall_tape():
    recs = [_spot(t) for t in range(0, 600)] + [_book(1_780_000_600.0)]
    assert verify(recs, expected_spot_rate_hz=1.0)["ok"] is True


def test_sequence_gaps_still_applicable_and_passing_when_seq_is_present():
    """orderbook_delta/orderbook_snapshot captures do carry seq. If someone
    captures that mode later, the check must behave exactly as it always
    has: applicable, and PASS when there are no gaps."""
    recs = [_spot(0), {**_book(1_780_000_000.0), "seq": 5}]
    c = _named(verify(recs), "sequence_gaps")
    assert c["ok"] is True
    assert c["applicable"] is True


def test_sequence_gaps_still_fails_on_a_real_gap_when_seq_is_present():
    recs = [_spot(0), {**_book(1_780_000_000.0), "seq": 5},
            {"k": "gap", "tm": 1.0, "sid": 1, "expected": 6, "got": 9}]
    c = _named(verify(recs), "sequence_gaps")
    assert c["ok"] is False
    assert c["applicable"] is True


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


# ----------------------------------------------------------- book continuity
#
# The ticker-mode substitute for sequence-gap detection: with no `seq`, the
# only remaining way to notice a dropped quote is a wall-clock hole in one
# market's own quote stream. Scoped per-market — the silence between one
# market's last quote and the next market's first quote is normal (a market
# quotes for ~15min then goes silent at close) and must never be flagged.

BASE_TW = 1_780_000_000.0


def test_book_continuity_fires_on_a_real_hole_in_a_markets_quote_stream():
    recs = [_book(BASE_TW + i, ticker="KXBTC15M-A") for i in range(30)]
    recs.append(_book(BASE_TW + 30 + vt.BOOK_CONTINUITY_GAP_S + 30,
                       ticker="KXBTC15M-A"))
    c = _named(verify(recs), "book_continuity")
    assert c["ok"] is False
    assert "1 gaps" in c["detail"]


def test_book_continuity_ignores_the_gap_between_two_different_markets():
    """A market's quotes legitimately stop at close; the check must be scoped
    within a single market's own stream, not across markets."""
    recs = [_book(BASE_TW + i, ticker="KXBTC15M-A") for i in range(30)]
    # ~14 minutes of silence before the next market starts quoting
    recs += [_book(BASE_TW + 30 + 840 + i, ticker="KXBTC15M-B") for i in range(30)]
    c = _named(verify(recs), "book_continuity")
    assert c["ok"] is True


def test_book_continuity_does_not_flag_a_markets_final_quote_before_close():
    """Quotes legitimately stop when a market closes. There is no record
    after the last one, so nothing should be flagged for it."""
    recs = [_book(BASE_TW + i, ticker="KXBTC15M-A") for i in range(30)]
    c = _named(verify(recs), "book_continuity")
    assert c["ok"] is True


def test_book_continuity_passes_on_a_regular_one_per_second_quote_stream():
    """Matches the Phase 0 smoke capture's measured book rate: ~1/s per
    active market, worst observed within-market gap 4.0s."""
    recs = [_book(BASE_TW + i, ticker="KXBTC15M-A") for i in range(900)]
    c = _named(verify(recs), "book_continuity")
    assert c["ok"] is True


def test_book_continuity_reports_count_and_worst_gap_even_when_passing():
    recs = [_book(BASE_TW + i, ticker="KXBTC15M-A") for i in range(5)]
    c = _named(verify(recs), "book_continuity")
    assert c["ok"] is True
    assert "0 gaps" in c["detail"]
    assert "worst gap" in c["detail"].lower()


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


def test_the_cli_passes_the_module_default_rate_through(monkeypatch):
    """The rate check was inert once because the CLI never forwarded the value.
    (This assertion used to also require the default be >1.0/s, which encoded
    the guess that got the floor set above the feed's real rate.)"""
    seen = {}

    def fake_verify(records, expected_spot_rate_hz=None):
        seen["rate"] = expected_spot_rate_hz
        return {"ok": True, "checks": []}

    monkeypatch.setattr(vt, "read_tape", lambda d: iter([]))
    monkeypatch.setattr(vt, "verify", fake_verify)
    monkeypatch.setattr(sys, "argv", ["verify_tape"])
    with pytest.raises(SystemExit):
        vt.main()
    assert seen["rate"] == vt.DEFAULT_SPOT_RATE_HZ


def test_the_default_floor_sits_below_the_rate_the_feed_was_measured_at():
    """Pins the relationship, not the number: whatever the floor is retuned to,
    it must stay under the observed 1.58/s or verify() false-alarms again."""
    assert vt.DEFAULT_SPOT_RATE_HZ * 0.9 < MEASURED_SPOT_RATE_HZ


def test_the_cli_prints_na_not_pass_for_a_not_applicable_check(monkeypatch, capsys):
    """PASS and N/A must be visually distinguishable in the CLI output — a
    reader scanning for PASS/FAIL should not mistake a check that could not
    run for one that ran and succeeded."""
    fake_result = {"ok": True, "checks": [
        {"name": "sequence_gaps", "ok": True, "applicable": False,
         "detail": "N/A: ticker channel carries no seq"},
    ]}
    monkeypatch.setattr(vt, "read_tape", lambda d: iter([]))
    monkeypatch.setattr(vt, "verify", lambda records, expected_spot_rate_hz=None: fake_result)
    monkeypatch.setattr(sys, "argv", ["verify_tape"])
    with pytest.raises(SystemExit):
        vt.main()
    out = capsys.readouterr().out
    assert "N/A" in out
    assert "PASS" not in out
    assert "FAIL" not in out
