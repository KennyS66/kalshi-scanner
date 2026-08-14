import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import wsrig.ws_spot as wss
from wsrig.ws_spot import parse_ticker, run_spot_feed


def test_parses_a_ticker_message():
    r = parse_ticker({"type": "ticker", "product_id": "BTC-USD",
                      "price": "63416.52", "time": "2026-08-13T22:00:00.123456Z"})
    assert r["k"] == "spot"
    assert r["p"] == 63416.52
    assert isinstance(r["tx"], float)
    assert r["tx"] > 1_700_000_000        # parsed to an epoch, not left as a string


def test_ignores_non_ticker_messages():
    assert parse_ticker({"type": "subscriptions", "channels": []}) is None
    assert parse_ticker({"type": "heartbeat"}) is None


def test_ignores_a_malformed_price():
    assert parse_ticker({"type": "ticker", "product_id": "BTC-USD",
                         "price": "not-a-number"}) is None
    assert parse_ticker({"type": "ticker", "product_id": "BTC-USD"}) is None


def test_missing_exchange_time_yields_null_not_a_guess():
    """Never substitute local time for exchange time — that would hide feed lag,
    which is precisely what this rig measures."""
    r = parse_ticker({"type": "ticker", "product_id": "BTC-USD", "price": "1.0"})
    assert r["tx"] is None


def test_records_the_product_so_eth_cannot_be_mistaken_for_btc():
    r = parse_ticker({"type": "ticker", "product_id": "ETH-USD", "price": "1884.0"})
    assert r["sym"] == "ETH-USD"


class _Tape:
    def __init__(self, stop, stop_after):
        self.recs = []
        self._stop = stop
        self._stop_after = stop_after

    def write(self, rec):
        self.recs.append(rec)
        if len(self.recs) >= self._stop_after:
            self._stop.set()


def test_an_unexpected_exception_type_does_not_kill_the_spot_feed(monkeypatch):
    """EOFError is not a WebSocketException and not an OSError. Before the fix it
    ended the task, and nothing noticed because Kalshi kept writing."""
    monkeypatch.setattr(wss, "RECONNECT_DELAY_S", 0.001)

    def connect(*a, **k):
        raise EOFError("socket layer")

    monkeypatch.setattr(wss.websockets, "connect", connect)

    async def go():
        stop = asyncio.Event()
        tape = _Tape(stop, stop_after=2)
        await asyncio.wait_for(run_spot_feed(tape, ["BTC-USD"], stop), timeout=2.0)
        assert [r["k"] for r in tape.recs] == ["feed_drop", "feed_drop"]
        assert tape.recs[0]["etype"] == "EOFError"

    asyncio.run(go())


def test_the_spot_feed_gives_up_after_persistent_failure(monkeypatch):
    monkeypatch.setattr(wss, "RECONNECT_DELAY_S", 0.001)
    monkeypatch.setattr(wss, "MAX_CONSECUTIVE_FAILURES", 3)
    monkeypatch.setattr(wss.websockets, "connect",
                        lambda *a, **k: (_ for _ in ()).throw(EOFError("down")))

    async def go():
        stop = asyncio.Event()
        tape = _Tape(stop, stop_after=99)      # never trips; the feed must exit itself
        await asyncio.wait_for(run_spot_feed(tape, ["BTC-USD"], stop), timeout=2.0)
        assert [r["k"] for r in tape.recs][-1] == "feed_error"

    asyncio.run(go())
