"""Authoritative settlement, straight from Kalshi.

The scanner infers outcomes from the sign of `distance` at the last observed
tick. This rig is isolated from that log, and an inferred outcome that is wrong
flips the sign of the edge it feeds -- so use the venue's own `result`.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("wsrig.settle")

VALID = ("yes", "no")
POLL_INTERVAL_S = 300.0


def settle_record(market: dict) -> dict | None:
    if market.get("status") != "settled":
        return None
    result = (market.get("result") or "").lower()
    if result not in VALID:
        return None                     # closed-but-unresolved, or void
    ticker = market.get("ticker")
    if not ticker:
        return None
    return {"k": "settle", "t": ticker, "result": result}


async def run_settlement(api, pending: set[str], tape, stop: asyncio.Event,
                         interval_s: float = POLL_INTERVAL_S) -> None:
    """Resolve `pending` tickers, writing each once and dropping it."""
    while not stop.is_set():
        batch = sorted(pending)[:100]
        if batch:
            try:
                data = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: api.get_markets_by_tickers(batch))
                for m in data.get("markets", []):
                    rec = settle_record(m)
                    if rec:
                        tape.write(rec)
                        pending.discard(rec["t"])
            except Exception as exc:
                log.warning("settlement poll failed: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass
