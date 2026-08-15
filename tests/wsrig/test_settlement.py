import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from wsrig.settlement import settle_record

# A real settled KXBTC15M market, captured live 2026-08-15T04:08Z via the same
# /markets?tickers=... call run_settlement makes, trimmed to the relevant keys.
# Note what is NOT here: `settled_time` does not exist (the field is
# `settlement_ts`), and the status is "finalized" — never "settled".
REAL_FINALIZED = {
    "ticker": "KXBTC15M-26AUG150000-00",
    "status": "finalized",
    "result": "no",
    "close_time": "2026-08-15T04:00:00Z",
    "settlement_ts": 1786766418,
    "expiration_value": "63070.12",
    "last_price_dollars": "0.0010",
}


def test_records_a_real_finalized_market():
    """The venue's word for settled is "finalized". Requiring "settled" matched
    nothing, so every market sat in `pending` forever and the tape got no
    settlement records at all."""
    assert settle_record(REAL_FINALIZED) == \
        {"k": "settle", "t": "KXBTC15M-26AUG150000-00", "result": "no"}


def test_records_a_finalized_yes():
    r = settle_record({"ticker": "KXBTC15M-A", "status": "finalized", "result": "yes"})
    assert r == {"k": "settle", "t": "KXBTC15M-A", "result": "yes"}


def test_settled_is_still_accepted_in_case_the_vocabulary_varies():
    """Kept deliberately: the value differs between the query parameter and the
    object, so it may well differ between endpoints too."""
    assert settle_record({"ticker": "KXBTC15M-A", "status": "settled",
                          "result": "no"})["result"] == "no"


def test_open_markets_are_not_settled():
    assert settle_record({"ticker": "KXBTC15M-A", "status": "active",
                          "result": ""}) is None


def test_a_not_yet_open_market_is_not_settled():
    assert settle_record({"ticker": "KXBTC15M-A", "status": "initialized",
                          "result": ""}) is None


def test_closed_but_unresolved_is_not_settled():
    """A market closes before it settles. Recording it early would invent an
    outcome, and a wrong outcome silently flips the sign of an edge."""
    assert settle_record({"ticker": "KXBTC15M-A", "status": "closed",
                          "result": ""}) is None


def test_a_result_alone_never_settles_a_market_that_has_not_finalized():
    """The tempting way to fix the status bug is to trust `result` and drop the
    status check. Don't: the accepted statuses stay an explicit allowlist, and a
    populated `result` on a non-final market is not authority to record one."""
    assert settle_record({"ticker": "KXBTC15M-A", "status": "closed",
                          "result": "yes"}) is None
    assert settle_record({"ticker": "KXBTC15M-A", "status": "determined",
                          "result": "yes"}) is None


def test_an_unexpected_result_value_is_rejected():
    assert settle_record({"ticker": "KXBTC15M-A", "status": "finalized",
                          "result": "void"}) is None


def test_a_finalized_market_with_no_result_is_rejected():
    """Never infer the outcome — that is the whole reason this rig does not
    reuse the scanner's sign-of-distance heuristic."""
    assert settle_record({"ticker": "KXBTC15M-A", "status": "finalized",
                          "result": ""}) is None
    assert settle_record({"ticker": "KXBTC15M-A", "status": "finalized"}) is None


def test_a_record_without_a_ticker_is_rejected():
    assert settle_record({"status": "finalized", "result": "yes"}) is None
