"""Which KXBTC15M markets should we be subscribed to right now?

Markets roll every 15 minutes. Subscribing only once a market is open would
miss its opening quotes, and the trigger window (mins_left 5-11) sits early in
a market's life -- so a late subscribe removes a non-random slice of exactly
what is being measured. Hence the lookahead.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("wsrig.tracker")

SERIES = "KXBTC15M"
POLL_INTERVAL_S = 60.0
LOOKAHEAD_S = 1200.0


def active_btc15m(markets: list[dict], now: float,
                  lookahead_s: float = LOOKAHEAD_S) -> list[str]:
    out = []
    for m in markets:
        ticker = m.get("ticker") or ""
        # Series-prefix match, not a substring test: `"15M" in ticker` also
        # catches events dated the 15th, e.g. KXUFCFIGHT-26AUG15MAKMGI-MGI.
        if ticker.split("-")[0] != SERIES:
            continue
        close_ts = m.get("close_ts")
        if close_ts is None or close_ts <= now:
            continue
        if close_ts - now > lookahead_s:
            continue
        out.append(ticker)
    return sorted(out)


async def run_tracker(api, on_change, stop: asyncio.Event,
                      interval_s: float = POLL_INTERVAL_S) -> None:
    """Poll REST for the active set; call on_change(tickers) when it changes."""
    import time
    current: list[str] = []
    while not stop.is_set():
        try:
            data = await asyncio.get_running_loop().run_in_executor(
                None, lambda: api.get_markets(status="open", limit=200))
            tickers = active_btc15m(data.get("markets", []), time.time())
            if tickers != current:
                log.info("active markets: %s -> %s", current, tickers)
                current = tickers
                await on_change(tickers)
        except Exception as exc:                      # never kill the capture
            log.warning("tracker poll failed: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass
