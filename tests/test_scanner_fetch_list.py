"""The per-cycle Kalshi fetch list, and the cap on its `priority` half.

`rest` was always capped at 200. `priority` -- every "KX*15M*" ticker -- was
not, so it grew with the number of distinct 15M tickers ever seen. Measured
2026-08-10: ~14 live at any moment across 8 series (KXBTC15M, KXGOLD15M,
KXDOGE15M, KXWTI15M, KXHYPE15M, KXNEAR15M, KXSILVER15M, ...), rolling every
15 minutes, so a 31h process accumulated on the order of a thousand -- about
12 batch fetches per 5s cycle instead of the healthy 2.

Since the live figure is 14, the cap is a safety net and must not bind in
normal operation. What it must never do is drop a market that is still open:
/api/crypto/signal reads those, and losing one is a feed_down for the swing
bot. So open markets are kept unconditionally and the cap bounds only the
stale remainder.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scanner import MarketSnapshot, Scanner


def _scanner():
    return Scanner(api=None)


def _traded(s, ticker, age_s=0.0, volume=0.0):
    s._ticker_stats[ticker]["last_ts"] = time.time() - age_s
    s._ticker_stats[ticker]["volume"] = volume


def _open_market(s, ticker, closes_in_s=600):
    s.market_snapshots[ticker] = MarketSnapshot(
        ticker=ticker, close_ts=time.time() + closes_in_s)


def test_normal_operation_is_unchanged():
    """14 live 15M tickers is the real number; the cap must not bind."""
    s = _scanner()
    for i in range(14):
        _traded(s, f"KXBTC15M-T{i}")
    assert set(s.fetch_list()) == {f"KXBTC15M-T{i}" for i in range(14)}


def test_priority_is_capped():
    s = _scanner()
    for i in range(500):
        _traded(s, f"KXBTC15M-T{i}", age_s=i)
    assert len(s.fetch_list(priority_cap=150)) == 150


def test_cap_keeps_the_freshest_and_drops_the_stalest():
    """An open market trades continuously; a rolled one stops. Recency of the
    last trade is therefore the signal for which 15M tickers still matter."""
    s = _scanner()
    _traded(s, "KXBTC15M-FRESH", age_s=1)
    _traded(s, "KXBTC15M-STALE", age_s=3000)
    assert s.fetch_list(priority_cap=1) == ["KXBTC15M-FRESH"]


def test_an_open_market_is_never_dropped_to_satisfy_the_cap():
    """The property that protects /api/crypto/signal. A quiet-but-open market
    outranks the cap however many fresher tickers compete with it."""
    s = _scanner()
    _traded(s, "KXBTC15M-QUIET-OPEN", age_s=9999)
    _open_market(s, "KXBTC15M-QUIET-OPEN")
    for i in range(300):
        _traded(s, f"KXBTC15M-NOISE{i}", age_s=1)
    assert "KXBTC15M-QUIET-OPEN" in s.fetch_list(priority_cap=10)


def test_snapshots_with_unknown_close_time_count_as_open():
    """The batch-fetch fallback builds snapshots without close_ts. prune_closed
    already refuses to guess those dead; the cap must not either."""
    s = _scanner()
    _traded(s, "KXBTC15M-UNKNOWN", age_s=9999)
    s.market_snapshots["KXBTC15M-UNKNOWN"] = MarketSnapshot(
        ticker="KXBTC15M-UNKNOWN", close_ts=None)
    for i in range(50):
        _traded(s, f"KXBTC15M-NOISE{i}", age_s=1)
    assert "KXBTC15M-UNKNOWN" in s.fetch_list(priority_cap=5)


def test_a_closed_market_does_not_get_the_open_exemption():
    s = _scanner()
    _traded(s, "KXBTC15M-CLOSED", age_s=9999)
    s.market_snapshots["KXBTC15M-CLOSED"] = MarketSnapshot(
        ticker="KXBTC15M-CLOSED", close_ts=time.time() - 600)
    for i in range(50):
        _traded(s, f"KXBTC15M-NOISE{i}", age_s=1)
    assert "KXBTC15M-CLOSED" not in s.fetch_list(priority_cap=5)


def test_rest_is_still_capped_and_ranked_by_volume():
    s = _scanner()
    for i in range(400):
        _traded(s, f"OTHER-{i}", volume=float(i))
    out = s.fetch_list()
    assert len(out) == 200
    assert "OTHER-399" in out and "OTHER-0" not in out


def test_rest_never_duplicates_priority():
    s = _scanner()
    _traded(s, "KXBTC15M-A", volume=999.0)
    _traded(s, "OTHER-B", volume=1.0)
    out = s.fetch_list()
    assert len(out) == len(set(out)) == 2


def test_default_cap_does_not_bind_at_realistic_scale():
    """Production calls fetch_list() with no arguments. Pin that the shipped
    default leaves real traffic untouched -- a cap that silently truncates
    live markets would be worse than no cap."""
    s = _scanner()
    for i in range(60):                      # >4x the measured 14
        _traded(s, f"KXBTC15M-T{i}", age_s=i)
    assert len(s.fetch_list()) == 60
