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


def _add_stats_only(s, ticker, last_seen_offset_s):
    """A ticker seen in a TRADE but with no market snapshot.

    scan_trades does `self._ticker_stats[ticker]` on a defaultdict, so every
    ticker that ever prints a trade gets an entry whether or not a snapshot
    is ever built for it.
    """
    s._ticker_stats[ticker]["count"] = 1
    s._ticker_stats[ticker]["last_ts"] = time.time() + last_seen_offset_s


def test_forgets_stats_for_tickers_that_never_had_a_snapshot():
    """The half of the 2026-08-09 leak the first fix missed.

    prune_closed derived its dead list from market_snapshots alone, so a
    _ticker_stats entry with no snapshot was unreachable and immortal. Live
    counts on 2026-08-10 showed 2669 stats against 288 snapshots. Those
    entries are not just memory: enrich_markets turns every one of them
    into an uncapped Kalshi fetch on every 5s cycle.
    """
    s = _scanner()
    _add_stats_only(s, "KXBTC15M-GHOST", -7 * 3600)
    s.prune_closed(grace_s=900, stats_ttl_s=3600)
    assert "KXBTC15M-GHOST" not in s._ticker_stats


def test_default_call_signature_expires_stale_stats():
    """enrich_markets calls `self.prune_closed()` with NO arguments.

    Every other test here passes stats_ttl_s explicitly, so all of them
    would keep passing if the default were changed back to infinity or the
    stats branch were made opt-in — while production leaked again. This is
    the only test that exercises the call production actually makes.
    """
    s = _scanner()
    _add_stats_only(s, "KXNOSNAP-STALE", -7200)      # 2h since its last trade
    s.prune_closed()
    assert "KXNOSNAP-STALE" not in s._ticker_stats


def test_keeps_stats_for_recently_traded_tickers():
    """A live market that simply has no snapshot yet must survive, or the
    scanner would forget the market it is about to enrich."""
    s = _scanner()
    _add_stats_only(s, "KXBTC15M-FRESH", -60)
    s.prune_closed(grace_s=900, stats_ttl_s=3600)
    assert "KXBTC15M-FRESH" in s._ticker_stats


def test_keeps_stats_for_an_open_market_that_has_gone_quiet():
    """No trades for hours is not death when the snapshot says it is open.
    Never let the trade-staleness rule override a live snapshot."""
    s = _scanner()
    _add(s, "QUIET", +600)                       # open, closes in 10 minutes
    s._ticker_stats["QUIET"]["last_ts"] = time.time() - 7 * 3600
    s.prune_closed(grace_s=900, stats_ttl_s=3600)
    assert "QUIET" in s._ticker_stats


def test_stats_entries_carry_a_last_seen_timestamp_by_default():
    """Pruning by age needs the field to exist on every entry."""
    s = _scanner()
    assert "last_ts" in s._ticker_stats["ANY"]


def test_prunes_many_and_reports_the_count():
    s = _scanner()
    for i in range(50):
        _add(s, f"OLD{i}", -3600)
    for i in range(5):
        _add(s, f"LIVE{i}", +600)
    assert s.prune_closed(grace_s=900) == 50
    assert len(s.market_snapshots) == 5
