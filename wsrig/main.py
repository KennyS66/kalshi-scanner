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
from wsrig.ws_kalshi import SubState, run_kalshi_feed
from wsrig.ws_spot import run_spot_feed

log = logging.getLogger("wsrig")


def pending_after_roll(previous: set[str], current: list[str]) -> set[str]:
    """Markets that just left the active set — these need settlement."""
    return set(previous) - set(current)


async def supervise(tasks: list[asyncio.Task], stop: asyncio.Event) -> int:
    """Wait for shutdown, or for any task to die. Returns the process exit code.

    None of these tasks may finish while `stop` is clear — each one loops until
    told otherwise. So a completion is always a failure, and an *invisible*
    failure is the worst one available here: the other tasks keep the tape's
    mtime fresh, so `health_check.check_tape_age` stays green while (say) every
    Kalshi quote is missing. Exit non-zero instead and let systemd's
    Restart=always recycle a process that is still capturing everything.
    """
    stopper = asyncio.create_task(stop.wait(), name="stop")
    try:
        done, _ = await asyncio.wait([*tasks, stopper],
                                     return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopper.cancel()

    dead = [t for t in tasks if t in done]
    if not dead or stop.is_set():
        return 0
    for t in dead:
        exc = None if t.cancelled() else t.exception()
        if exc is not None:
            log.error("wsrig task %s died: %r", t.get_name(), exc, exc_info=exc)
        else:
            log.error("wsrig task %s exited on its own (returned %r) — it should "
                      "have run until shutdown", t.get_name(),
                      None if t.cancelled() else t.result())
    return 1


async def amain(dir: str) -> int:
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
    # Latest-wins, never blocking: a bounded queue would stall the tracker (and
    # therefore settlement) whenever the Kalshi socket stayed down long enough
    # to fill it. See wsrig.ws_kalshi.SubState.
    subs = SubState()

    async def on_change(tickers: list[str]) -> None:
        nonlocal active
        pending.update(pending_after_roll(active, tickers))
        active = set(tickers)
        subs.set(tickers)

    tasks = [
        asyncio.create_task(run_spot_feed(tape, ["BTC-USD"], stop), name="spot"),
        asyncio.create_task(run_kalshi_feed(tape, subs, stop), name="kalshi"),
        asyncio.create_task(run_tracker(api, on_change, stop), name="tracker"),
        asyncio.create_task(run_settlement(api, pending, tape, stop), name="settle"),
    ]
    code = 1
    try:
        code = await supervise(tasks, stop)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        tape.close()
        log.info("wsrig stopped (exit %d)", code)
    return code


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="data/wsrig")
    raise SystemExit(asyncio.run(amain(ap.parse_args().dir)))


if __name__ == "__main__":
    main()
