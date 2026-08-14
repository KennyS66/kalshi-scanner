import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from wsrig.settlement import settle_record


def test_records_a_settled_yes():
    r = settle_record({"ticker": "KXBTC15M-A", "status": "settled", "result": "yes"})
    assert r == {"k": "settle", "t": "KXBTC15M-A", "result": "yes"}


def test_records_a_settled_no():
    assert settle_record({"ticker": "KXBTC15M-A", "status": "settled",
                          "result": "no"})["result"] == "no"


def test_open_markets_are_not_settled():
    assert settle_record({"ticker": "KXBTC15M-A", "status": "active"}) is None


def test_closed_but_unresolved_is_not_settled():
    """A market closes before it settles. Recording it early would invent an
    outcome, and a wrong outcome silently flips the sign of an edge."""
    assert settle_record({"ticker": "KXBTC15M-A", "status": "closed",
                          "result": ""}) is None


def test_an_unexpected_result_value_is_rejected():
    assert settle_record({"ticker": "KXBTC15M-A", "status": "settled",
                          "result": "void"}) is None
