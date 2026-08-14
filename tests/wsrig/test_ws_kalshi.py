import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import wsrig.ws_kalshi as wsk
from wsrig.ws_kalshi import SeqTracker, SubState, control_record, parse_book, run_kalshi_feed


def test_first_sequence_is_never_a_gap():
    assert SeqTracker().check(1, 100) is None


def test_consecutive_sequences_are_not_gaps():
    t = SeqTracker()
    t.check(1, 100)
    assert t.check(1, 101) is None


def test_a_skipped_sequence_is_reported():
    t = SeqTracker()
    t.check(1, 100)
    g = t.check(1, 105)
    assert g["k"] == "gap" and g["sid"] == 1 and g["expected"] == 101 and g["got"] == 105


def test_sequences_are_tracked_per_subscription():
    """Kalshi seq is per-sid; sharing one counter would invent gaps."""
    t = SeqTracker()
    t.check(1, 100)
    t.check(2, 500)
    assert t.check(1, 101) is None
    assert t.check(2, 501) is None


def test_a_replayed_sequence_is_not_a_gap():
    t = SeqTracker()
    t.check(1, 100)
    assert t.check(1, 100) is None


def test_parse_book_extracts_both_sides():
    r = parse_book({"type": "ticker", "sid": 1, "seq": 7,
                    "msg": {"market_ticker": "KXBTC15M-A", "yes_bid": 38,
                            "yes_ask": 41, "no_bid": 59, "no_ask": 62, "ts": 1786000000}})
    assert r["k"] == "book" and r["t"] == "KXBTC15M-A"
    assert r["ya"] == 0.41 and r["na"] == 0.62      # cents -> dollars
    assert r["seq"] == 7 and r["sid"] == 1


def test_parse_book_ignores_unrelated_message_types():
    assert parse_book({"type": "subscribed", "sid": 1}) is None


# ---------------------------------------------------------------- control frames

def test_control_record_keeps_the_type_and_body():
    r = control_record({"type": "error", "id": 1,
                        "msg": {"code": 6, "msg": "invalid market ticker"}})
    assert r["k"] == "ctl" and r["type"] == "error"
    assert "invalid market ticker" in r["msg"]


def test_control_record_truncates_a_large_body():
    """If parse_book ever stops matching, every book frame lands here — a full
    orderbook per record would drown the tape."""
    r = control_record({"type": "orderbook_snapshot", "msg": {"yes": [[1, 2]] * 500}})
    assert len(r["msg"]) <= wsk.CTL_MSG_MAX


# ------------------------------------------------------- connection-loop harness

HANG = object()


class FakeTape:
    """Records what was written; optionally trips `stop` after N writes."""

    def __init__(self, stop=None, stop_after=None):
        self.recs = []
        self._stop = stop
        self._stop_after = stop_after

    def write(self, rec):
        self.recs.append(rec)
        if self._stop is not None and len(self.recs) >= self._stop_after:
            self._stop.set()

    @property
    def kinds(self):
        return [r.get("k") for r in self.recs]


class FakeWS:
    def __init__(self, script):
        self.sent = []
        self._script = list(script)

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if not self._script:
            raise OSError("connection closed")
        item = self._script.pop(0)
        if item is HANG:
            await asyncio.sleep(3600)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeConnect:
    def __init__(self, ws=None, raises=None):
        self._ws = ws
        self._raises = raises

    async def __aenter__(self):
        if self._raises is not None:
            raise self._raises
        return self._ws

    async def __aexit__(self, *exc):
        return False


def _fast(monkeypatch, silence=0.02, backoff=0.001):
    monkeypatch.setattr(wsk, "_auth_headers", lambda: {"KALSHI-ACCESS-KEY": "test"})
    monkeypatch.setattr(wsk, "SILENCE_MAX_S", silence)
    monkeypatch.setattr(wsk, "BACKOFF_BASE_S", backoff)


def _ticker(seq=1):
    return json.dumps({"type": "ticker", "sid": 1, "seq": seq,
                       "msg": {"market_ticker": "KXBTC15M-A", "yes_bid": 38,
                               "yes_ask": 41, "no_bid": 59, "no_ask": 62}})


# ------------------------------------------------------------ resilience of the loop

def test_a_runtime_error_from_auth_does_not_kill_the_feed(monkeypatch):
    """Empty credentials raise RuntimeError inside the try. Before the fix that
    escaped every except clause and silently ended the Kalshi capture."""
    _fast(monkeypatch)

    def boom():
        raise RuntimeError("Kalshi WS auth headers are empty")

    monkeypatch.setattr(wsk, "_auth_headers", boom)

    async def go():
        stop = asyncio.Event()
        tape = FakeTape(stop, stop_after=2)      # one retry, then shut down
        await asyncio.wait_for(run_kalshi_feed(tape, SubState(), stop), timeout=2.0)
        assert tape.kinds == ["feed_drop", "feed_drop"]
        assert tape.recs[0]["etype"] == "RuntimeError"

    asyncio.run(go())


def test_a_failing_tape_write_does_not_kill_the_feed(monkeypatch):
    """Disk-full raises OSError from inside the except block that is meant to be
    handling the error. It must not escape."""
    _fast(monkeypatch)
    monkeypatch.setattr(wsk, "_auth_headers",
                        lambda: (_ for _ in ()).throw(EOFError("socket layer")))

    class ExplodingTape:
        def __init__(self, stop):
            self.calls = 0
            self.stop = stop

        def write(self, rec):
            self.calls += 1
            if self.calls >= 3:
                self.stop.set()
            raise OSError("no space left on device")

    async def go():
        stop = asyncio.Event()
        tape = ExplodingTape(stop)
        await asyncio.wait_for(run_kalshi_feed(tape, SubState(), stop), timeout=2.0)
        assert tape.calls >= 3        # kept retrying instead of dying

    asyncio.run(go())


def test_the_feed_gives_up_after_persistent_failure_so_the_supervisor_sees_it(monkeypatch):
    """Retrying forever on bad credentials is the same silent degradation as
    dying: exit instead, so systemd recycles the process."""
    _fast(monkeypatch)
    monkeypatch.setattr(wsk, "MAX_CONSECUTIVE_FAILURES", 3)
    monkeypatch.setattr(wsk, "_auth_headers",
                        lambda: (_ for _ in ()).throw(RuntimeError("bad creds")))

    async def go():
        stop = asyncio.Event()
        tape = FakeTape()
        await asyncio.wait_for(run_kalshi_feed(tape, SubState(), stop), timeout=2.0)
        assert tape.kinds == ["feed_drop", "feed_drop", "feed_drop", "feed_error"]
        assert tape.recs[-1]["fatal"] is True
        assert not stop.is_set()      # exited on its own; that is the signal

    asyncio.run(go())


def test_a_connect_timeout_backs_off_instead_of_being_called_a_stall(monkeypatch):
    """websockets raises TimeoutError from connect() when open_timeout expires.
    Taking the watchdog path there means reconnecting with zero backoff."""
    _fast(monkeypatch, backoff=0.01)
    monkeypatch.setattr(wsk.websockets, "connect",
                        lambda *a, **k: FakeConnect(raises=asyncio.TimeoutError()))

    async def go():
        stop = asyncio.Event()
        tape = FakeTape(stop, stop_after=3)
        t0 = time.monotonic()
        await asyncio.wait_for(run_kalshi_feed(tape, SubState(), stop), timeout=2.0)
        elapsed = time.monotonic() - t0
        assert tape.kinds == ["feed_drop"] * 3        # not feed_stall
        assert tape.recs[0]["etype"] == "TimeoutError"
        assert elapsed >= 0.03      # slept 0.01 then 0.02 — the backoff grew

    asyncio.run(go())


def test_a_silent_socket_is_taped_as_a_stall_and_then_reconnected(monkeypatch):
    """A half-open socket must be dropped and redialled, not waited on forever."""
    _fast(monkeypatch)
    conns = []

    def connect(*a, **k):
        ws = FakeWS([_ticker(), HANG])
        conns.append(ws)
        return FakeConnect(ws)

    monkeypatch.setattr(wsk.websockets, "connect", connect)

    async def go():
        stop = asyncio.Event()
        tape = FakeTape(stop, stop_after=3)
        await asyncio.wait_for(run_kalshi_feed(tape, SubState(), stop), timeout=2.0)
        assert tape.kinds == ["book", "feed_stall", "book"]
        assert len(conns) == 2        # the stall produced a fresh connection

    asyncio.run(go())


def test_control_frames_are_taped_and_a_rejected_subscribe_is_logged(monkeypatch, caplog):
    """Zero book records has three explanations; only the tape can tell them apart."""
    _fast(monkeypatch)
    err = json.dumps({"type": "error", "id": 1,
                      "msg": {"code": 6, "msg": "invalid market ticker"}})
    monkeypatch.setattr(wsk.websockets, "connect",
                        lambda *a, **k: FakeConnect(FakeWS([err, HANG])))

    async def go():
        stop = asyncio.Event()
        tape = FakeTape(stop, stop_after=2)
        with caplog.at_level(logging.ERROR, logger="wsrig.kalshi"):
            await asyncio.wait_for(run_kalshi_feed(tape, SubState(), stop), timeout=2.0)
        assert tape.recs[0]["k"] == "ctl" and tape.recs[0]["type"] == "error"
        assert "invalid market ticker" in tape.recs[0]["msg"]
        assert any("rejected" in r.message for r in caplog.records)

    asyncio.run(go())


# --------------------------------------------------------------- latest-wins subs

def test_substate_never_blocks_and_keeps_only_the_latest():
    s = SubState()
    s.set(["A"])
    s.set(["B"])                      # a bounded queue would have to be drained
    assert s.get() == ["B"] and s.changed.is_set()
    got = s.get()
    got.append("C")
    assert s.get() == ["B"]           # callers cannot mutate the shared state


def test_reconnect_subscribes_once_to_the_current_markets(monkeypatch):
    """Rolls that happen while disconnected must collapse into one subscribe for
    the markets that are live now — not one per queued roll."""
    _fast(monkeypatch, silence=5.0)
    ws = FakeWS([HANG])
    monkeypatch.setattr(wsk.websockets, "connect", lambda *a, **k: FakeConnect(ws))

    async def go():
        stop = asyncio.Event()
        subs = SubState()
        subs.set(["KXBTC15M-A"])          # two rolls before the socket came up
        subs.set(["KXBTC15M-B"])
        task = asyncio.create_task(run_kalshi_feed(FakeTape(), subs, stop))
        await asyncio.sleep(0.05)
        subs.set(["KXBTC15M-C"])          # a roll while connected
        await asyncio.sleep(0.05)
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        sent = [m["params"]["market_tickers"] for m in ws.sent]
        assert sent == [["KXBTC15M-B"], ["KXBTC15M-C"]]

    asyncio.run(go())
