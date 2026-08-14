"""Coinbase public WS ticker feed -> tape.

Adapted from daedalus/data/external/coinbase_ws.py. Differences: parsing is a
pure function so it is testable without a socket, exchange time is recorded as
an epoch float (never defaulted to local time), and output goes to the tape
rather than an aggregator.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging

import websockets
import websockets.exceptions

log = logging.getLogger("wsrig.spot")

URL = "wss://ws-feed.exchange.coinbase.com"
RECONNECT_DELAY_S = 2.0
MAX_RECONNECT_DELAY_S = 60.0


def parse_ticker(msg: dict) -> dict | None:
    """Coinbase ticker message -> tape record, or None if not usable."""
    if msg.get("type") != "ticker":
        return None
    sym = msg.get("product_id")
    raw = msg.get("price")
    if not sym or raw is None:
        return None
    try:
        price = float(raw)
    except (TypeError, ValueError):
        return None

    tx = None
    ts_str = msg.get("time")
    if ts_str:
        try:
            tx = dt.datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
        except ValueError:
            tx = None          # never fall back to local time; see docstring
    return {"k": "spot", "sym": sym, "p": price, "tx": tx}


async def run_spot_feed(tape, symbols: list[str], stop: asyncio.Event) -> None:
    delay = RECONNECT_DELAY_S
    while not stop.is_set():
        try:
            async with websockets.connect(URL, open_timeout=15, ping_interval=20) as ws:
                await ws.send(json.dumps({"type": "subscribe",
                                          "product_ids": symbols,
                                          "channels": ["ticker"]}))
                log.info("spot feed connected: %s", symbols)
                delay = RECONNECT_DELAY_S
                async for raw in ws:
                    if stop.is_set():
                        break
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    rec = parse_ticker(msg)
                    if rec is not None:
                        tape.write(rec)
        except asyncio.CancelledError:
            break
        except (websockets.exceptions.WebSocketException, OSError, asyncio.TimeoutError) as exc:
            if stop.is_set():
                break
            log.warning("spot feed dropped: %s — reconnect in %.1fs", exc, delay)
            tape.write({"k": "feed_drop", "src": "spot", "err": str(exc)[:200]})
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_RECONNECT_DELAY_S)
