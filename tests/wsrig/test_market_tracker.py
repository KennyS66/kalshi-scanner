import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import asyncio
from datetime import datetime, timezone

from wsrig.market_tracker import active_btc15m, run_tracker

NOW = 1_786_000_000.0


def _iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _m(ticker, close_offset, status="active"):
    """A market in the shape the live API actually returns.

    There is no `close_ts` field anywhere in a real response — the field is
    `close_time`, an ISO8601 string. Reading `close_ts` made active_btc15m
    return [] unconditionally, which is a dead tracker that looks healthy.
    """
    close = NOW + close_offset
    return {"ticker": ticker, "status": status,
            "close_time": _iso(close), "open_time": _iso(close - 900)}


# A real KXBTC15M object, captured live 2026-08-15T04:08Z and trimmed to the
# keys the rig touches. Fixtures invented from the plan are what produced the
# bug this file now guards; this one came off the wire.
REAL_ACTIVE = {
    "ticker": "KXBTC15M-26AUG150015-15",
    "status": "active",
    "open_time": "2026-08-15T04:00:00Z",
    "close_time": "2026-08-15T04:15:00Z",
    "result": "",
    "yes_bid_dollars": "0.8000",
    "yes_ask_dollars": "0.8100",
    "last_price_dollars": "0.8000",
}
REAL_ACTIVE_NOW = 1786766907.0          # 2026-08-15T04:08:27Z, when it was captured


def test_selects_open_btc15m_markets():
    got = active_btc15m([_m("KXBTC15M-A", 600)], NOW)
    assert got == ["KXBTC15M-A"]


def test_a_real_captured_market_object_is_selected():
    """The live shape end to end: ISO8601 `close_time`, no `close_ts`."""
    assert active_btc15m([REAL_ACTIVE], REAL_ACTIVE_NOW) == ["KXBTC15M-26AUG150015-15"]


def test_excludes_other_series():
    ms = [_m("KXETH15M-A", 600), _m("KXBTC-DAILY", 600), _m("KXBTC15M-A", 600)]
    assert active_btc15m(ms, NOW) == ["KXBTC15M-A"]


def test_excludes_already_closed_markets():
    assert active_btc15m([_m("KXBTC15M-OLD", -60)], NOW) == []


def test_includes_the_next_market_before_it_opens():
    """Subscribing only at open would miss the first quotes of every market,
    which is a non-random slice of exactly the window we measure.

    A pre-open market reports status "initialized", not "active" — the selector
    must key on the close time, never on the status string.
    """
    ms = [_m("KXBTC15M-SOON", 1500, status="initialized")]
    assert active_btc15m(ms, NOW, lookahead_s=1800) == ["KXBTC15M-SOON"]


def test_excludes_markets_far_in_the_future():
    assert active_btc15m([_m("KXBTC15M-LATER", 99_999)], NOW, lookahead_s=1200) == []


def test_excludes_the_24h_of_initialized_markets_the_series_query_returns():
    """The series listing carries ~24h of not-yet-open markets, newest first.
    Without a working lookahead bound the rig would subscribe to all of them."""
    far = [_m(f"KXBTC15M-F{i}", 3600 * i, status="initialized") for i in range(1, 25)]
    near = _m("KXBTC15M-NOW", 600)
    assert active_btc15m(far + [near], NOW, lookahead_s=1200) == ["KXBTC15M-NOW"]


def test_substring_match_cannot_catch_unrelated_series():
    """`"15M" in ticker` matches KXUFCFIGHT-26AUG15MAKMGI-MGI. Match the series
    prefix instead — this bug is live in scanner.py:enrich_markets."""
    assert active_btc15m([_m("KXUFCFIGHT-26AUG15MAKMGI-MGI", 600)], NOW) == []


def test_result_is_sorted_for_stable_comparison():
    ms = [_m("KXBTC15M-B", 600), _m("KXBTC15M-A", 600)]
    assert active_btc15m(ms, NOW) == ["KXBTC15M-A", "KXBTC15M-B"]


# --------------------------------------------------------- close_time parsing

def test_a_numeric_close_ts_is_still_honoured_if_one_ever_appears():
    """Belt and braces: the field does not exist today, but accepting it costs
    nothing and a schema change should not blind the tracker a second time."""
    assert active_btc15m([{"ticker": "KXBTC15M-A", "close_ts": NOW + 600}], NOW) == \
        ["KXBTC15M-A"]


def test_a_naive_close_time_is_read_as_utc_not_local():
    """`.timestamp()` on a tz-naive datetime silently assumes the host's zone,
    which would shift every market by the local UTC offset."""
    naive = {"ticker": "KXBTC15M-A", "close_time": _iso(NOW + 600).rstrip("Z")}
    assert active_btc15m([naive], NOW) == ["KXBTC15M-A"]


def test_a_malformed_close_time_skips_the_market_instead_of_raising():
    """A raise inside the loop is caught by run_tracker's catch-all and turns
    one bad record into a permanently empty active set behind a log line."""
    ms = [{"ticker": "KXBTC15M-BAD", "close_time": "not a timestamp"},
          {"ticker": "KXBTC15M-NONE", "close_time": None},
          {"ticker": "KXBTC15M-MISSING"},
          _m("KXBTC15M-GOOD", 600)]
    assert active_btc15m(ms, NOW) == ["KXBTC15M-GOOD"]


# ------------------------------------------------------------- the REST query

class RecordingAPI:
    """Captures the kwargs run_tracker actually sends to get_markets."""

    def __init__(self, markets=None):
        self.calls = []
        self._markets = markets if markets is not None else []

    def get_markets(self, **kwargs):
        self.calls.append(kwargs)
        return {"markets": self._markets}


def _run_one_poll(api, on_change=None):
    async def go():
        seen = []

        async def default_on_change(tickers):
            seen.append(tickers)

        stop = asyncio.Event()
        task = asyncio.create_task(
            run_tracker(api, on_change or default_on_change, stop, interval_s=5.0))
        for _ in range(200):
            await asyncio.sleep(0.005)
            if api.calls:
                break
        await asyncio.sleep(0.02)
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return seen

    return asyncio.run(go())


def test_the_query_filters_by_series_because_the_plain_listing_never_has_them():
    """~12,000 markets across 12 pages of status="open" contain zero KXBTC15M.
    Only the series_ticker parameter surfaces the series at all."""
    api = RecordingAPI()
    _run_one_poll(api)
    assert api.calls, "run_tracker never called get_markets"
    assert api.calls[0]["series_ticker"] == "KXBTC15M"


def test_the_query_sends_no_status_filter_so_pre_open_markets_are_visible():
    """status="open" returns ONLY the single currently-trading market; pre-open
    markets are "initialized" and it excludes them, so the lookahead could never
    fire. The API also rejects a combined "open,unopened" filter outright."""
    api = RecordingAPI()
    _run_one_poll(api)
    assert api.calls[0].get("status") is None


def test_the_query_windows_on_close_time_because_the_listing_is_newest_first():
    """The series listing is close_time DESCENDING: page 1 of an unfiltered
    limit=200 starts ~24h in the future. Paging blindly and filtering locally
    puts the currently-trading market ~95 rows deep, one horizon change away
    from falling off the page and silently emptying the capture again."""
    import time

    api = RecordingAPI()
    t0 = time.time()
    _run_one_poll(api)
    call = api.calls[0]
    assert call["min_close_ts"] <= t0, "must not exclude a market closing right now"
    assert t0 - 300 <= call["min_close_ts"], "window reaches needlessly far back"
    assert t0 + 1100 <= call["max_close_ts"] <= t0 + 1300, "≈ the lookahead bound"


def test_a_market_20_hours_out_is_excluded_even_if_the_server_returns_it():
    """Server-side windowing is defence in depth, not a substitute for the
    local bound — active_btc15m stays authoritative."""
    import time

    now = time.time()
    api = RecordingAPI([
        {"ticker": "KXBTC15M-FAR", "status": "initialized",
         "close_time": _iso(now + 20 * 3600)},
        {"ticker": "KXBTC15M-NEAR", "status": "active",
         "close_time": _iso(now + 600)},
    ])
    assert _run_one_poll(api) == [["KXBTC15M-NEAR"]]


def test_run_tracker_retries_on_change_if_it_fails():
    """If on_change raises, current should not be updated, so next poll retries."""
    import time

    async def async_test():
        call_count = 0
        calls = []
        poll_count = [0]

        async def failing_on_change(tickers):
            nonlocal call_count
            call_count += 1
            calls.append(tickers)
            if call_count == 1:
                raise ValueError("subscription failed")

        class MockAPI:
            def get_markets(self, **kwargs):
                poll_count[0] += 1
                return {"markets": [{"ticker": "KXBTC15M-A", "status": "active",
                                     "close_time": _iso(time.time() + 600)}]}

        stop = asyncio.Event()
        task = asyncio.create_task(
            run_tracker(MockAPI(), failing_on_change, stop, interval_s=0.01))

        await asyncio.sleep(0.15)
        assert poll_count[0] >= 1, f"get_markets should have been called, got {poll_count[0]}"
        assert call_count >= 1, f"on_change should have been called once, got {call_count}"

        await asyncio.sleep(0.05)
        assert call_count >= 2, f"on_change should have been called twice, got {call_count}"
        assert calls == [["KXBTC15M-A"], ["KXBTC15M-A"]], "Both calls should have same tickers"

        stop.set()
        try:
            await asyncio.wait_for(task, timeout=0.5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    asyncio.run(async_test())
