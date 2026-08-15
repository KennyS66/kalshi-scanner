"""Which KXBTC15M markets should we be subscribed to right now?

Markets roll every 15 minutes. Subscribing only once a market is open would
miss its opening quotes, and the trigger window (mins_left 5-11) sits early in
a market's life -- so a late subscribe removes a non-random slice of exactly
what is being measured. Hence the lookahead.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

log = logging.getLogger("wsrig.tracker")

SERIES = "KXBTC15M"
POLL_INTERVAL_S = 60.0
# A market opens 15 minutes before it closes, so a 20-minute close-time bound
# leaves only a ~5 minute pre-open margin. That is enough to be subscribed
# before the first quote, and it is deliberately tight: widening it pulls in
# markets whose books do not exist yet.
LOOKAHEAD_S = 1200.0
# Kalshi's clock decides close_time. Ask the server for a slightly wider window
# than we want and let active_btc15m make the exact cut, so a few seconds of
# skew can never drop the market that is trading right now.
CLOCK_SKEW_GUARD_S = 60.0


def close_epoch(market: dict) -> float | None:
    """Market close as epoch seconds, or None if there isn't a usable one.

    The live API has no `close_ts` field at all — it returns `close_time` as an
    ISO8601 string ("2026-08-15T04:15:00Z"). Reading the field that was assumed
    rather than the one that exists made this selector return [] on every poll,
    forever, while looking perfectly healthy.

    Never raises: run_tracker's catch-all would turn one malformed record into a
    permanently empty active set behind a single log line.
    """
    ts = market.get("close_ts")
    if isinstance(ts, (int, float)):
        return float(ts)
    raw = market.get("close_time")
    if not isinstance(raw, str) or not raw:
        return None
    if raw[-1] in ("Z", "z"):
        # Python <3.11 rejects the trailing Z. Normalise it rather than depend
        # on which interpreter the rig happens to run under.
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # Kalshi timestamps are UTC. `.timestamp()` on a naive datetime assumes
        # the host's zone and would shift every market by the local offset.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def active_btc15m(markets: list[dict], now: float,
                  lookahead_s: float = LOOKAHEAD_S) -> list[str]:
    out = []
    for m in markets:
        ticker = m.get("ticker") or ""
        # Series-prefix match, not a substring test: `"15M" in ticker` also
        # catches events dated the 15th, e.g. KXUFCFIGHT-26AUG15MAKMGI-MGI.
        if ticker.split("-")[0] != SERIES:
            continue
        close_ts = close_epoch(m)
        # Select on the close time, never on `status`: a market we want to be
        # subscribed to before it opens reports "initialized", not "active".
        if close_ts is None or close_ts <= now:
            continue
        if close_ts - now > lookahead_s:
            continue
        out.append(ticker)
    return sorted(out)


async def run_tracker(api, on_change, stop: asyncio.Event,
                      interval_s: float = POLL_INTERVAL_S,
                      lookahead_s: float = LOOKAHEAD_S) -> None:
    """Poll REST for the active set; call on_change(tickers) when it changes.

    Three properties of the live listing endpoint shape this query, each of
    which silently returned nothing when assumed away:

    * The unfiltered listing never contains KXBTC15M — ~12,000 markets over 12
      pages of `status="open"` yielded zero. Only `series_ticker` surfaces it.
    * `status="open"` is a filter keyword that matches exactly the ONE market
      currently trading; a market we want to subscribe to before it opens
      reports `initialized` and is excluded. Sending no status filter is what
      makes the lookahead able to fire at all, and the API rejects a combined
      `"open,unopened"` outright ("only one status filter may be supplied").
    * The series listing is close_time DESCENDING — page 1 of an unfiltered
      limit=200 starts ~24h out and buries the currently-trading market ~95
      rows deep. Windowing server-side on close time keeps that page-1 depth
      from ever mattering; active_btc15m stays the authoritative filter.
    """
    import time
    current: list[str] = []
    while not stop.is_set():
        try:
            now = time.time()
            data = await asyncio.get_running_loop().run_in_executor(
                None, lambda: api.get_markets(
                    status=None, limit=200, series_ticker=SERIES,
                    min_close_ts=int(now - CLOCK_SKEW_GUARD_S),
                    max_close_ts=int(now + lookahead_s)))
            tickers = active_btc15m(data.get("markets", []), now, lookahead_s)
            if tickers != current:
                log.info("active markets: %s -> %s", current, tickers)
                await on_change(tickers)
                current = tickers
        except Exception as exc:                      # never kill the capture
            log.warning("tracker poll failed: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass
