import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from wsrig.main import pending_after_roll, supervise


def test_a_market_leaving_the_active_set_becomes_pending_settlement():
    assert pending_after_roll({"A", "B"}, ["B", "C"]) == {"A"}


def test_nothing_pending_when_the_set_only_grows():
    assert pending_after_roll({"A"}, ["A", "B"]) == set()


def test_all_previous_markets_pend_when_the_set_empties():
    assert pending_after_roll({"A", "B"}, []) == {"A", "B"}


async def _forever():
    await asyncio.Event().wait()


def _drain(tasks):
    for t in tasks:
        t.cancel()


def test_supervise_exits_zero_when_shutdown_was_requested():
    async def go():
        stop = asyncio.Event()
        tasks = [asyncio.create_task(_forever(), name="spot"),
                 asyncio.create_task(_forever(), name="kalshi")]
        asyncio.get_running_loop().call_later(0.01, stop.set)
        code = await asyncio.wait_for(supervise(tasks, stop), timeout=1.0)
        _drain(tasks)
        await asyncio.gather(*tasks, return_exceptions=True)
        assert code == 0

    asyncio.run(go())


def test_supervise_exits_nonzero_when_a_feed_task_dies(caplog):
    """The failure this exists for: one feed dies, the others keep the tape's
    mtime fresh, and check_tape_age reports PASS while half the data is gone."""
    async def go():
        stop = asyncio.Event()

        async def boom():
            raise RuntimeError("kalshi auth headers are empty")

        tasks = [asyncio.create_task(_forever(), name="spot"),
                 asyncio.create_task(boom(), name="kalshi")]
        with caplog.at_level(logging.ERROR, logger="wsrig"):
            code = await asyncio.wait_for(supervise(tasks, stop), timeout=1.0)
        _drain(tasks)
        await asyncio.gather(*tasks, return_exceptions=True)
        assert code == 1
        assert any("kalshi" in r.message for r in caplog.records)

    asyncio.run(go())


def test_supervise_exits_nonzero_when_a_task_returns_early():
    """A feed that returns cleanly mid-capture is just as broken as one that
    raises — it stops capturing either way."""
    async def go():
        stop = asyncio.Event()

        async def quits():
            return "done"

        tasks = [asyncio.create_task(_forever(), name="spot"),
                 asyncio.create_task(quits(), name="settle")]
        code = await asyncio.wait_for(supervise(tasks, stop), timeout=1.0)
        _drain(tasks)
        await asyncio.gather(*tasks, return_exceptions=True)
        assert code == 1

    asyncio.run(go())
