import time

from scanner import MarketSnapshot, Scanner


def _scanner():
    return Scanner(api=None)


def _add(s, ticker, close_offset_s):
    """Register a snapshot closing `close_offset_s` from now (negative = past)."""
    s.market_snapshots[ticker] = MarketSnapshot(
        ticker=ticker,
        close_ts=None if close_offset_s is None else time.time() + close_offset_s)
    s._ticker_stats[ticker]["count"] = 1


def test_prunes_markets_closed_beyond_the_grace_window():
    """The 2026-08-09 leak: 15M markets roll every 15 minutes and every ticker
    ever seen was retained AND re-fetched from the API every 5s cycle."""
    s = _scanner()
    _add(s, "OLD", -3600)          # closed an hour ago
    assert s.prune_closed(grace_s=900) == 1
    assert "OLD" not in s.market_snapshots


def test_keeps_open_markets():
    s = _scanner()
    _add(s, "OPEN", +600)          # closes in 10 minutes
    assert s.prune_closed(grace_s=900) == 0
    assert "OPEN" in s.market_snapshots


def test_keeps_recently_closed_markets_inside_the_grace_window():
    """/api/crypto/signal has its own 120s grace for just-expired markets, so
    pruning must not race it."""
    s = _scanner()
    _add(s, "JUSTCLOSED", -60)
    assert s.prune_closed(grace_s=900) == 0
    assert "JUSTCLOSED" in s.market_snapshots


def test_keeps_snapshots_with_unknown_close_time():
    """The batch-fetch fallback path builds snapshots without close_ts. They
    get a real one on the next successful scan; never guess them dead."""
    s = _scanner()
    _add(s, "UNKNOWN", None)
    assert s.prune_closed(grace_s=900) == 0
    assert "UNKNOWN" in s.market_snapshots


def test_also_forgets_the_tickers_trade_stats():
    """_ticker_stats is what enrich_markets iterates to decide what to fetch.
    Pruning only the snapshot would leave the API fetch unbounded."""
    s = _scanner()
    _add(s, "OLD", -3600)
    s.prune_closed(grace_s=900)
    assert "OLD" not in s._ticker_stats


def test_prunes_many_and_reports_the_count():
    s = _scanner()
    for i in range(50):
        _add(s, f"OLD{i}", -3600)
    for i in range(5):
        _add(s, f"LIVE{i}", +600)
    assert s.prune_closed(grace_s=900) == 50
    assert len(s.market_snapshots) == 5
