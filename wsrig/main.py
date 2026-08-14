"""Supervisor: wires the two feeds and two pollers onto one tape.

Capture only. This process places no orders and touches nothing the scanner
owns; its sole side effect is writing to --dir.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path

from api import KalshiAPI
from wsrig.market_tracker import run_tracker
from wsrig.settlement import run_settlement
from wsrig.tape import Tape
from wsrig.ws_kalshi import run_kalshi_feed
from wsrig.ws_spot import run_spot_feed

log = logging.getLogger("wsrig")


def pending_after_roll(previous: set[str], current: list[str]) -> set[str]:
    """Markets that just left the active set — these need settlement."""
    return set(previous) - set(current)


async def amain(dir: str) -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    tape = Tape(Path(dir))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    from wsrig.creds import load_creds
    key_id, key_path = load_creds()
    api = KalshiAPI(api_key=key_id, private_key_path=key_path)
    active: set[str] = set()
    pending: set[str] = set()
    subscribe_q: asyncio.Queue = asyncio.Queue(maxsize=64)

    async def on_change(tickers: list[str]) -> None:
        nonlocal active
        pending.update(pending_after_roll(active, tickers))
        active = set(tickers)
        await subscribe_q.put(tickers)

    tasks = [
        asyncio.create_task(run_spot_feed(tape, ["BTC-USD"], stop)),
        asyncio.create_task(run_kalshi_feed(tape, subscribe_q, stop)),
        asyncio.create_task(run_tracker(api, on_change, stop)),
        asyncio.create_task(run_settlement(api, pending, tape, stop)),
    ]
    try:
        await stop.wait()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        tape.close()
        log.info("wsrig stopped cleanly")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="data/wsrig")
    asyncio.run(amain(ap.parse_args().dir))


if __name__ == "__main__":
    main()
