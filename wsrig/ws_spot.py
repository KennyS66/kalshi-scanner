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

from wsrig.tape import safe_write

log = logging.getLogger("wsrig.spot")

URL = "wss://ws-feed.exchange.coinbase.com"
RECONNECT_DELAY_S = 2.0
MAX_RECONNECT_DELAY_S = 60.0
MAX_CONSECUTIVE_FAILURES = 10       # see ws_kalshi: past this, exit for the supervisor


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
    failures = 0
    while not stop.is_set():
        try:
            async with websockets.connect(URL, open_timeout=15, ping_interval=20) as ws:
                await ws.send(json.dumps({"type": "subscribe",
                                          "product_ids": symbols,
                                          "channels": ["ticker"]}))
                log.info("spot feed connected: %s", symbols)
                delay = RECONNECT_DELAY_S
                failures = 0
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
        except Exception as exc:              # noqa: BLE001 — deliberate catch-all
            # An unanticipated exception type (EOFError from the socket layer, a
            # parse bug) must not end the capture silently; back off and redial.
            if stop.is_set():
                break
            failures += 1
            log.warning("spot feed error #%d (%s): %s — reconnect in %.1fs",
                        failures, type(exc).__name__, exc, delay)
            safe_write(tape, {"k": "feed_drop", "src": "spot",
                              "etype": type(exc).__name__, "err": str(exc)[:200]})
            if failures >= MAX_CONSECUTIVE_FAILURES:
                log.error("spot feed failed %d times running (%s: %s) — exiting so "
                          "the supervisor can recycle the process",
                          failures, type(exc).__name__, exc)
                safe_write(tape, {"k": "feed_error", "src": "spot", "fatal": True,
                                  "etype": type(exc).__name__, "err": str(exc)[:200]})
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_RECONNECT_DELAY_S)
