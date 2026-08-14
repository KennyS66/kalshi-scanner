import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from wsrig.market_tracker import active_btc15m

NOW = 1_786_000_000.0


def _m(ticker, close_offset, status="active"):
    return {"ticker": ticker, "status": status, "close_ts": NOW + close_offset}


def test_selects_open_btc15m_markets():
    got = active_btc15m([_m("KXBTC15M-A", 600)], NOW)
    assert got == ["KXBTC15M-A"]


def test_excludes_other_series():
    ms = [_m("KXETH15M-A", 600), _m("KXBTC-DAILY", 600), _m("KXBTC15M-A", 600)]
    assert active_btc15m(ms, NOW) == ["KXBTC15M-A"]


def test_excludes_already_closed_markets():
    assert active_btc15m([_m("KXBTC15M-OLD", -60)], NOW) == []


def test_includes_the_next_market_before_it_opens():
    """Subscribing only at open would miss the first quotes of every market,
    which is a non-random slice of exactly the window we measure."""
    assert active_btc15m([_m("KXBTC15M-SOON", 1500)], NOW, lookahead_s=1800) == \
        ["KXBTC15M-SOON"]


def test_excludes_markets_far_in_the_future():
    assert active_btc15m([_m("KXBTC15M-LATER", 99_999)], NOW, lookahead_s=1200) == []


def test_substring_match_cannot_catch_unrelated_series():
    """`"15M" in ticker` matches KXUFCFIGHT-26AUG15MAKMGI-MGI. Match the series
    prefix instead — this bug is live in scanner.py:enrich_markets."""
    assert active_btc15m([_m("KXUFCFIGHT-26AUG15MAKMGI-MGI", 600)], NOW) == []


def test_result_is_sorted_for_stable_comparison():
    ms = [_m("KXBTC15M-B", 600), _m("KXBTC15M-A", 600)]
    assert active_btc15m(ms, NOW) == ["KXBTC15M-A", "KXBTC15M-B"]
