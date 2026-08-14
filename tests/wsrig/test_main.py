import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from wsrig.main import pending_after_roll


def test_a_market_leaving_the_active_set_becomes_pending_settlement():
    assert pending_after_roll({"A", "B"}, ["B", "C"]) == {"A"}


def test_nothing_pending_when_the_set_only_grows():
    assert pending_after_roll({"A"}, ["A", "B"]) == set()


def test_all_previous_markets_pend_when_the_set_empties():
    assert pending_after_roll({"A", "B"}, []) == {"A", "B"}
