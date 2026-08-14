import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from wsrig.ws_kalshi import SeqTracker, parse_book


def test_first_sequence_is_never_a_gap():
    assert SeqTracker().check(1, 100) is None


def test_consecutive_sequences_are_not_gaps():
    t = SeqTracker()
    t.check(1, 100)
    assert t.check(1, 101) is None


def test_a_skipped_sequence_is_reported():
    t = SeqTracker()
    t.check(1, 100)
    g = t.check(1, 105)
    assert g["k"] == "gap" and g["sid"] == 1 and g["expected"] == 101 and g["got"] == 105


def test_sequences_are_tracked_per_subscription():
    """Kalshi seq is per-sid; sharing one counter would invent gaps."""
    t = SeqTracker()
    t.check(1, 100)
    t.check(2, 500)
    assert t.check(1, 101) is None
    assert t.check(2, 501) is None


def test_a_replayed_sequence_is_not_a_gap():
    t = SeqTracker()
    t.check(1, 100)
    assert t.check(1, 100) is None


def test_parse_book_extracts_both_sides():
    r = parse_book({"type": "ticker", "sid": 1, "seq": 7,
                    "msg": {"market_ticker": "KXBTC15M-A", "yes_bid": 38,
                            "yes_ask": 41, "no_bid": 59, "no_ask": 62, "ts": 1786000000}})
    assert r["k"] == "book" and r["t"] == "KXBTC15M-A"
    assert r["ya"] == 0.41 and r["na"] == 0.62      # cents -> dollars
    assert r["seq"] == 7 and r["sid"] == 1


def test_parse_book_ignores_unrelated_message_types():
    assert parse_book({"type": "subscribed", "sid": 1}) is None
