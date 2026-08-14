import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import asyncio

from wsrig.market_tracker import active_btc15m, run_tracker

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


def test_run_tracker_retries_on_change_if_it_fails():
    """If on_change raises, current should not be updated, so next poll retries."""
    import time

    async def async_test():
        call_count = 0
        calls = []
        poll_count = [0]  # Track how many times the loop executes

        async def failing_on_change(tickers):
            nonlocal call_count
            call_count += 1
            calls.append(tickers)
            if call_count == 1:
                raise ValueError("subscription failed")
            # Second call succeeds

        class MockAPI:
            def get_markets(self, status="open", limit=200):
                poll_count[0] += 1
                # Return a market that closes 600 seconds in the future
                now = time.time()
                return {
                    "markets": [
                        {
                            "ticker": "KXBTC15M-A",
                            "status": "active",
                            "close_ts": now + 600,
                        }
                    ]
                }

        stop = asyncio.Event()
        task = asyncio.create_task(run_tracker(MockAPI(), failing_on_change, stop, interval_s=0.01))

        # Let the task run for a bit to execute first poll
        await asyncio.sleep(0.15)

        # Debug: check if get_markets was even called
        assert poll_count[0] >= 1, f"get_markets should have been called, got {poll_count[0]} calls"
        assert call_count >= 1, f"on_change should have been called at least once, got {call_count} calls"

        # Wait for second poll
        await asyncio.sleep(0.05)
        assert call_count >= 2, f"on_change should have been called at least twice, got {call_count} calls"
        assert calls == [["KXBTC15M-A"], ["KXBTC15M-A"]], "Both calls should have same tickers"

        stop.set()
        try:
            await asyncio.wait_for(task, timeout=0.5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    asyncio.run(async_test())
