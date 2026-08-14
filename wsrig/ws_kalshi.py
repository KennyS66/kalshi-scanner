"""Kalshi WS book feed -> tape.

Connection, backoff, watchdog and sequence-gap detection are ported from
daedalus/venue/kalshi_ws.py. Sequence gaps matter more here than they do
there: a silent gap would remove quotes from the middle of a trigger window
and quietly bias the decay curve, so every gap is written to the tape and the
affected window is excluded at analysis time.

Kalshi quotes in CENTS; the tape stores dollars to match the rest of the repo.
"""
from __future__ import annotations

import logging

log = logging.getLogger("wsrig.kalshi")

WS_PATH = "/trade-api/ws/v2"
WS_URL = "wss://api.elections.kalshi.com" + WS_PATH


class SeqTracker:
    """Per-subscription sequence continuity. Kalshi numbers per sid."""

    def __init__(self):
        self._last: dict[int, int] = {}

    def check(self, sid: int, seq: int) -> dict | None:
        last = self._last.get(sid)
        self._last[sid] = seq
        if last is None or seq <= last:
            return None                      # first message, or a replay
        if seq == last + 1:
            return None
        return {"k": "gap", "sid": sid, "expected": last + 1, "got": seq}


def _cents(v):
    return None if v is None else round(float(v) / 100.0, 4)


def parse_book(msg: dict) -> dict | None:
    """Kalshi ticker/orderbook message -> tape record, or None."""
    if msg.get("type") not in ("ticker", "orderbook_snapshot", "orderbook_delta"):
        return None
    body = msg.get("msg") or {}
    ticker = body.get("market_ticker")
    if not ticker:
        return None
    return {
        "k": "book",
        "t": ticker,
        "yb": _cents(body.get("yes_bid")),
        "ya": _cents(body.get("yes_ask")),
        "nb": _cents(body.get("no_bid")),
        "na": _cents(body.get("no_ask")),
        "sid": msg.get("sid"),
        "seq": msg.get("seq"),
        "tx": body.get("ts"),
        "mtype": msg.get("type"),
    }


import asyncio
import json
import os

import websockets
import websockets.exceptions

from api import KalshiAPI

BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 60.0
SILENCE_MAX_S = 30.0        # Kalshi is quiet between trades; watchdog only on hard stalls


def _auth_headers() -> dict:
    """Same RSA-PSS scheme as the REST client — reuse it rather than re-derive."""
    from wsrig.creds import load_creds
    key_id, key_path = load_creds()
    api = KalshiAPI(api_key=key_id, private_key_path=key_path)
    headers = api._sign_request("GET", WS_PATH)
    if not headers:
        raise RuntimeError(
            "Kalshi WS auth headers are empty — check ~/.kalshi/trading.env. "
            "Connecting unauthenticated would fail silently and cost the capture.")
    return headers


async def run_kalshi_feed(tape, subscribe_q: asyncio.Queue, stop: asyncio.Event) -> None:
    """Maintain the WS connection, re-subscribing on reconnect and on roll."""
    delay = BACKOFF_BASE_S
    tickers: list[str] = []
    cmd_id = 0
    while not stop.is_set():
        try:
            async with websockets.connect(WS_URL, additional_headers=_auth_headers(),
                                          open_timeout=15, ping_interval=20) as ws:
                log.info("kalshi feed connected")
                delay = BACKOFF_BASE_S
                seq = SeqTracker()

                async def send_sub(ts: list[str]) -> None:
                    nonlocal cmd_id
                    if not ts:
                        return
                    cmd_id += 1
                    await ws.send(json.dumps({
                        "id": cmd_id, "cmd": "subscribe",
                        "params": {"channels": ["ticker", "orderbook_delta"],
                                   "market_tickers": ts}}))

                await send_sub(tickers)

                async def pump_subs() -> None:
                    nonlocal tickers
                    while not stop.is_set():
                        tickers = await subscribe_q.get()
                        await send_sub(tickers)

                pump = asyncio.create_task(pump_subs())
                try:
                    while not stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=SILENCE_MAX_S)
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        if msg.get("seq") is not None and msg.get("sid") is not None:
                            gap = seq.check(msg["sid"], msg["seq"])
                            if gap:
                                tape.write(gap)
                        rec = parse_book(msg)
                        if rec is not None:
                            tape.write(rec)
                finally:
                    pump.cancel()

        except asyncio.CancelledError:
            break
        except asyncio.TimeoutError:
            tape.write({"k": "feed_stall", "src": "kalshi", "after_s": SILENCE_MAX_S})
        except (websockets.exceptions.WebSocketException, OSError) as exc:
            if stop.is_set():
                break
            log.warning("kalshi feed dropped: %s — reconnect in %.1fs", exc, delay)
            tape.write({"k": "feed_drop", "src": "kalshi", "err": str(exc)[:200]})
            await asyncio.sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX_S)
