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


BOOK_TYPES = ("ticker", "orderbook_snapshot", "orderbook_delta")
CTL_MSG_MAX = 300          # error bodies are tiny; a mis-parsed book is not


def control_record(msg: dict) -> dict:
    """Whatever parse_book could not turn into a quote — taped, never dropped.

    If a capture yields zero book records, three hypotheses have to be told
    apart: the subscribe was rejected, the field names moved, or the market was
    quiet. Silently discarding `error`/`subscribed`/`ok` frames makes that
    impossible after the fact. The body is truncated because a book message
    that stopped parsing would otherwise dump a full orderbook per frame.
    """
    body = msg.get("msg")
    text = body if isinstance(body, str) else json.dumps(body, default=str)
    return {"k": "ctl", "type": msg.get("type"), "msg": text[:CTL_MSG_MAX]}


def parse_book(msg: dict) -> dict | None:
    """Kalshi ticker/orderbook message -> tape record, or None."""
    if msg.get("type") not in BOOK_TYPES:
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

from api import KalshiAPI
from wsrig.tape import safe_write

BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 60.0
SILENCE_MAX_S = 30.0        # Kalshi is quiet between trades; watchdog only on hard stalls
# ~5 minutes of capped backoff. Past this the fault is not transient (bad
# credentials, DNS, a moved endpoint) and retrying in-process forever would
# mean a live-looking rig capturing no Kalshi data at all. Exit instead and let
# the supervisor take the process down so systemd recycles it loudly.
MAX_CONSECUTIVE_FAILURES = 10


class SubState:
    """Latest-wins subscription target, shared between tracker and feed.

    A bounded Queue deadlocks: nothing drains it while the socket is down, so a
    long Kalshi outage eventually blocks the tracker's poll loop forever — and
    the tracker is also what feeds settlement. Setting an Event never blocks,
    and coalescing means a reconnect sends ONE subscribe for the markets that
    are live NOW instead of replaying every roll that happened while we were
    disconnected.
    """

    def __init__(self) -> None:
        self._tickers: list[str] = []
        self.changed = asyncio.Event()

    def set(self, tickers: list[str]) -> None:
        self._tickers = list(tickers)
        self.changed.set()

    def get(self) -> list[str]:
        return list(self._tickers)


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


async def run_kalshi_feed(tape, subs: SubState, stop: asyncio.Event) -> None:
    """Maintain the WS connection, re-subscribing on reconnect and on roll."""
    delay = BACKOFF_BASE_S
    failures = 0
    cmd_id = 0
    while not stop.is_set():
        try:
            # NB _auth_headers() runs here, inside the try: it raises RuntimeError
            # on empty credentials, and that must take the backoff path like any
            # other failure rather than killing this task.
            async with websockets.connect(WS_URL, additional_headers=_auth_headers(),
                                          open_timeout=15, ping_interval=20) as ws:
                log.info("kalshi feed connected")
                delay = BACKOFF_BASE_S
                failures = 0
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

                # Clear before reading, always read the CURRENT set: a roll that
                # lands mid-connect re-sets the Event and gets its own subscribe.
                subs.changed.clear()
                await send_sub(subs.get())

                async def pump_subs() -> None:
                    while not stop.is_set():
                        await subs.changed.wait()
                        subs.changed.clear()
                        try:
                            await send_sub(subs.get())
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:      # noqa: BLE001
                            # The socket is on its way out; recv() will raise and
                            # the reconnect re-sends the current set anyway. Just
                            # don't die with an unretrieved exception.
                            log.warning("subscribe failed: %s — resending on "
                                        "reconnect", exc)
                            return

                pump = asyncio.create_task(pump_subs())
                try:
                    while not stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(),
                                                         timeout=SILENCE_MAX_S)
                        except asyncio.TimeoutError:
                            # Watchdog only. Scoped to recv() so that a
                            # connect-handshake timeout — also a TimeoutError,
                            # and an OSError subclass — falls through to the
                            # backoff path below instead of hot-looping here.
                            log.warning("kalshi feed silent for %.0fs — reconnecting",
                                        SILENCE_MAX_S)
                            safe_write(tape, {"k": "feed_stall", "src": "kalshi",
                                              "after_s": SILENCE_MAX_S})
                            break        # drop the socket; the outer loop redials
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
                        else:
                            ctl = control_record(msg)
                            if ctl["type"] == "error":
                                log.error("kalshi rejected a command: %s", ctl["msg"])
                            tape.write(ctl)
                finally:
                    pump.cancel()

        except asyncio.CancelledError:
            break
        except Exception as exc:              # noqa: BLE001 — deliberate catch-all
            # Anything at all: a RuntimeError from _auth_headers, an EOFError from
            # the socket layer, a bug in parsing. None of them may end the feed.
            if stop.is_set():
                break
            failures += 1
            log.warning("kalshi feed error #%d (%s): %s — reconnect in %.1fs",
                        failures, type(exc).__name__, exc, delay)
            safe_write(tape, {"k": "feed_drop", "src": "kalshi",
                              "etype": type(exc).__name__, "err": str(exc)[:200]})
            if failures >= MAX_CONSECUTIVE_FAILURES:
                log.error("kalshi feed failed %d times running (%s: %s) — exiting so "
                          "the supervisor can recycle the process rather than "
                          "capturing spot-only data indefinitely",
                          failures, type(exc).__name__, exc)
                safe_write(tape, {"k": "feed_error", "src": "kalshi", "fatal": True,
                                  "etype": type(exc).__name__, "err": str(exc)[:200]})
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX_S)
