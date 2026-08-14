# Websocket Measurement Rig Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture Coinbase spot and Kalshi book data at full websocket resolution for 5-7 days, then measure whether the momentum edge survives at a 1-second action latency.

**Architecture:** A standalone `wsrig/` package with its own systemd unit, writing gzipped tapes to `data/wsrig/`. It reads nothing from and writes nothing to the running scanner. Two websocket feeds and two REST pollers write to one append-only tape; an offline analyser replays the tape.

**Tech Stack:** Python 3, `asyncio`, `websockets` (to be installed), `cryptography` (present, 48.0.0), `pytest`. Reference implementations adapted from `/home/kenny/bots/daedalus-mm`.

## Global Constraints

- **Measurement only.** No order placement, no live money, no changes to the running scanner or its config. If a task seems to require touching `scanner.py`, `web.py`, `swing_bot.py` or `data/bot/`, stop and ask.
- **New code lives in `wsrig/`**, tests in `tests/wsrig/`. The repo root is already 30+ modules; a package keeps this isolated, which is the whole point of the design.
- **Every record carries three timestamps**: `tw` (wall, `time.time()`), `tm` (monotonic, `time.monotonic()`), `tx` (exchange, from the message; `null` if absent). Latency is the entire question — conflating these invalidates the measurement.
- **Bounded memory.** Every queue and buffer has an explicit maximum. This repo lost two days to an unbounded buffer in the week this was written.
- **The acceptance bar is fixed and lives in the spec.** Do not modify it while implementing. At δ=1s, Arm A, net of `0.07*p*(1-p)`: n≥30 per window across 3 disjoint windows, positive in all 3, |t| ≥ 2.64, magnitude ≥ +0.02/contract, survives drop-two-best-days.
- Run tests with `.venv/bin/python -m pytest`. The full suite currently passes at 386 tests; it must still pass after every task.
- Kalshi production WS: `wss://api.elections.kalshi.com/trade-api/ws/v2`, path `/trade-api/ws/v2`. Coinbase: `wss://ws-feed.exchange.coinbase.com`. Both are verified live in Task 8 before the long capture.
- **Arm B is deliberately out of scope for this plan.** The spec defines it as exploratory and it cannot decide Phase 2. It gets its own plan only if Arm A passes — building it now would create a second number to be tempted by when Arm A disappoints.

---

### Task 1: Tape writer and reader

**Files:**
- Create: `wsrig/__init__.py`
- Create: `wsrig/tape.py`
- Test: `tests/wsrig/test_tape.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Tape(dir: Path, hour_fmt: str = "%Y%m%d-%H")` with `write(rec: dict) -> None`, `close() -> None`, and a module function `read_tape(dir: Path) -> Iterator[dict]` yielding records in file-then-line order. `Tape.write` stamps `tw` and `tm` if absent and never overwrites a caller-supplied value.

- [ ] **Step 1: Write the failing test**

```python
# tests/wsrig/test_tape.py
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from wsrig.tape import Tape, read_tape


def test_write_then_read_roundtrips(tmp_path):
    t = Tape(tmp_path)
    t.write({"k": "spot", "p": 63416.5})
    t.close()
    got = list(read_tape(tmp_path))
    assert len(got) == 1
    assert got[0]["k"] == "spot"
    assert got[0]["p"] == 63416.5


def test_write_stamps_both_clocks(tmp_path):
    t = Tape(tmp_path)
    t.write({"k": "spot", "p": 1.0})
    t.close()
    rec = next(iter(read_tape(tmp_path)))
    assert isinstance(rec["tw"], float) and rec["tw"] > 1_700_000_000
    assert isinstance(rec["tm"], float)


def test_write_does_not_overwrite_caller_timestamps(tmp_path):
    """A replayed or back-dated record must keep its own clocks."""
    t = Tape(tmp_path)
    t.write({"k": "spot", "p": 1.0, "tw": 123.0, "tm": 456.0})
    t.close()
    rec = next(iter(read_tape(tmp_path)))
    assert rec["tw"] == 123.0 and rec["tm"] == 456.0


def test_rotates_by_hour(tmp_path):
    t = Tape(tmp_path)
    t._hour_key = lambda: "A"
    t.write({"k": "x", "n": 1})
    t._hour_key = lambda: "B"
    t.write({"k": "x", "n": 2})
    t.close()
    files = sorted(p.name for p in tmp_path.glob("*.jsonl.gz"))
    assert len(files) == 2, files
    assert [r["n"] for r in read_tape(tmp_path)] == [1, 2]


def test_files_are_valid_gzip_after_close(tmp_path):
    """A truncated gzip member loses the whole hour. Close must finalise."""
    t = Tape(tmp_path)
    for i in range(100):
        t.write({"k": "x", "n": i})
    t.close()
    f = next(tmp_path.glob("*.jsonl.gz"))
    with gzip.open(f, "rt") as fh:
        assert len([json.loads(l) for l in fh if l.strip()]) == 100


def test_read_tape_orders_files_chronologically(tmp_path):
    for name, n in (("tape-20260813-09.jsonl.gz", 2),
                    ("tape-20260813-08.jsonl.gz", 1)):
        with gzip.open(tmp_path / name, "wt") as fh:
            fh.write(json.dumps({"n": n}) + "\n")
    assert [r["n"] for r in read_tape(tmp_path)] == [1, 2]


def test_read_tape_skips_a_corrupt_trailing_line(tmp_path):
    """A crash mid-write leaves a partial line; it must not kill the read."""
    with gzip.open(tmp_path / "tape-20260813-08.jsonl.gz", "wt") as fh:
        fh.write(json.dumps({"n": 1}) + "\n")
        fh.write('{"n": 2')          # truncated
    assert [r["n"] for r in read_tape(tmp_path)] == [1]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/wsrig/test_tape.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'wsrig'`

- [ ] **Step 3: Write the implementation**

```python
# wsrig/__init__.py
"""Websocket measurement rig — capture only, no trading.

See docs/superpowers/specs/2026-08-13-websocket-measurement-rig-design.md
"""
```

```python
# wsrig/tape.py
"""Append-only gzipped record tape, rotated hourly.

Every record carries three clocks, because the entire question this rig
exists to answer is a latency question:

    tw  wall clock   (time.time())     -- joins across processes and to settlement
    tm  monotonic    (time.monotonic()) -- immune to NTP steps; the basis for deltas
    tx  exchange ts  (from the message) -- reveals feed-side lag

Reading is deliberately forgiving: a crash leaves a partial final line, and
losing an hour of capture to one truncated record would be absurd.
"""
from __future__ import annotations

import gzip
import json
import time
from collections.abc import Iterator
from pathlib import Path

FLUSH_EVERY = 200          # records; bounds loss on a hard kill


class Tape:
    def __init__(self, dir: Path, hour_fmt: str = "%Y%m%d-%H"):
        self.dir = Path(dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._hour_fmt = hour_fmt
        self._fh = None
        self._open_key = None
        self._since_flush = 0

    def _hour_key(self) -> str:
        return time.strftime(self._hour_fmt, time.gmtime())

    def _ensure_open(self) -> None:
        key = self._hour_key()
        if key != self._open_key:
            self.close()
            path = self.dir / f"tape-{key}.jsonl.gz"
            self._fh = gzip.open(path, "at", compresslevel=6)
            self._open_key = key

    def write(self, rec: dict) -> None:
        rec.setdefault("tw", time.time())
        rec.setdefault("tm", time.monotonic())
        self._ensure_open()
        self._fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        self._since_flush += 1
        if self._since_flush >= FLUSH_EVERY:
            self._fh.flush()
            self._since_flush = 0

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
            self._open_key = None
            self._since_flush = 0


def read_tape(dir: Path) -> Iterator[dict]:
    """Yield every record, files in chronological name order."""
    for path in sorted(Path(dir).glob("tape-*.jsonl.gz")):
        try:
            with gzip.open(path, "rt") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue          # partial final line after a crash
        except (OSError, EOFError):
            continue                      # truncated gzip member
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/wsrig/test_tape.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add wsrig/__init__.py wsrig/tape.py tests/wsrig/test_tape.py
git commit -m "wsrig: hourly-rotated gzipped tape with three clocks per record"
```

---

### Task 2: Capture-integrity verifier

This is the task that makes the measurement trustworthy. A rig that silently drops half an hour still produces a plausible-looking edge number.

**Files:**
- Create: `wsrig/verify_tape.py`
- Test: `tests/wsrig/test_verify_tape.py`

**Interfaces:**
- Consumes: `wsrig.tape.read_tape`.
- Produces: `verify(records: list[dict], expected_spot_rate_hz: float = 1.0) -> dict` returning
  `{"ok": bool, "checks": [{"name": str, "ok": bool, "detail": str}, ...]}`. Also a `main()` CLI printing the same.

- [ ] **Step 1: Write the failing test**

```python
# tests/wsrig/test_verify_tape.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from wsrig.verify_tape import verify


def _spot(tm, tw=None, p=63000.0):
    return {"k": "spot", "tm": tm, "tw": tw if tw is not None else 1_780_000_000.0 + tm, "p": p}


def _named(result, name):
    return next(c for c in result["checks"] if c["name"] == name)


def test_clean_tape_passes():
    recs = [_spot(t) for t in range(0, 600)]
    assert verify(recs, expected_spot_rate_hz=1.0)["ok"] is True


def test_detects_monotonic_clock_going_backwards():
    recs = [_spot(0), _spot(5), _spot(3)]
    assert _named(verify(recs), "monotonic_ordering")["ok"] is False


def test_detects_a_wall_clock_step():
    """NTP stepping the wall clock breaks any tw-based join to settlement."""
    recs = [_spot(0, tw=1_780_000_000.0), _spot(1, tw=1_780_000_001.0),
            _spot(2, tw=1_780_000_060.0)]          # +59s of wall for 1s of mono
    assert _named(verify(recs), "clock_drift")["ok"] is False


def test_detects_a_spot_feed_silence_gap():
    recs = [_spot(t) for t in range(0, 60)] + [_spot(t) for t in range(400, 460)]
    assert _named(verify(recs), "spot_continuity")["ok"] is False


def test_reports_sequence_gap_records():
    recs = [_spot(0), {"k": "gap", "tm": 1.0, "sid": 1, "expected": 5, "got": 9}, _spot(2)]
    c = _named(verify(recs), "sequence_gaps")
    assert c["ok"] is False and "1" in c["detail"]


def test_empty_tape_fails_rather_than_vacuously_passing():
    """The worst outcome is a rig that captured nothing and reported OK."""
    assert verify([])["ok"] is False


def test_book_coverage_flags_a_settled_market_with_no_quotes():
    recs = [_spot(0), {"k": "settle", "tm": 1.0, "t": "KXBTC15M-A", "result": "yes"}]
    assert _named(verify(recs), "book_coverage")["ok"] is False


def test_book_coverage_passes_when_the_market_has_quotes():
    recs = [_spot(0),
            {"k": "book", "tm": 0.5, "t": "KXBTC15M-A", "ya": 0.4, "na": 0.6},
            {"k": "settle", "tm": 1.0, "t": "KXBTC15M-A", "result": "yes"}]
    assert _named(verify(recs), "book_coverage")["ok"] is True
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/wsrig/test_verify_tape.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'wsrig.verify_tape'`

- [ ] **Step 3: Write the implementation**

```python
# wsrig/verify_tape.py
"""Is this tape trustworthy enough to draw a conclusion from?

A capture rig that silently dies for six hours still yields a tidy-looking
edge number. Every check here exists to make that failure loud instead.

Run before ANY analysis:  .venv/bin/python -m wsrig.verify_tape
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

from wsrig.tape import read_tape

MAX_SPOT_SILENCE_S = 120.0     # BTC never goes 2 minutes without a trade
MAX_CLOCK_DRIFT_S = 2.0        # |Δwall − Δmono| tolerated across the capture


def _check(name, ok, detail):
    return {"name": name, "ok": bool(ok), "detail": detail}


def verify(records: list[dict], expected_spot_rate_hz: float = 1.0) -> dict:
    checks = []

    if not records:
        return {"ok": False,
                "checks": [_check("non_empty", False, "tape is empty")]}
    checks.append(_check("non_empty", True, f"{len(records)} records"))

    mono = [r["tm"] for r in records if "tm" in r]
    backwards = sum(1 for a, b in zip(mono, mono[1:]) if b < a)
    checks.append(_check("monotonic_ordering", backwards == 0,
                         f"{backwards} records out of monotonic order"))

    # Wall and monotonic must advance together. A divergence means the wall
    # clock was stepped, which would corrupt any tw-based join.
    paired = [(r["tm"], r["tw"]) for r in records if "tm" in r and "tw" in r]
    worst = 0.0
    for (m0, w0), (m1, w1) in zip(paired, paired[1:]):
        worst = max(worst, abs((w1 - w0) - (m1 - m0)))
    checks.append(_check("clock_drift", worst <= MAX_CLOCK_DRIFT_S,
                         f"worst wall-vs-monotonic step {worst:.3f}s "
                         f"(limit {MAX_CLOCK_DRIFT_S}s)"))

    spot = [r["tm"] for r in records if r.get("k") == "spot"]
    if len(spot) < 2:
        checks.append(_check("spot_continuity", False,
                             f"only {len(spot)} spot ticks"))
    else:
        gaps = [(b - a) for a, b in zip(spot, spot[1:]) if b - a > MAX_SPOT_SILENCE_S]
        span_h = (spot[-1] - spot[0]) / 3600.0
        rate = len(spot) / max(spot[-1] - spot[0], 1e-9)
        checks.append(_check("spot_continuity", not gaps,
                             f"{len(gaps)} silences >{MAX_SPOT_SILENCE_S:.0f}s "
                             f"over {span_h:.1f}h; rate {rate:.2f}/s "
                             f"(expected ~{expected_spot_rate_hz:.2f}/s)"))

    gapsr = [r for r in records if r.get("k") == "gap"]
    by_sid = defaultdict(int)
    for g in gapsr:
        by_sid[g.get("sid")] += 1
    checks.append(_check("sequence_gaps", not gapsr,
                         f"{len(gapsr)} sequence gaps"
                         + (f" on sids {sorted(by_sid)}" if by_sid else "")))

    quoted = {r.get("t") for r in records if r.get("k") == "book"}
    settled = {r.get("t") for r in records if r.get("k") == "settle"}
    missing = sorted(settled - quoted)
    checks.append(_check("book_coverage", not missing,
                         f"{len(missing)} settled markets with no quotes"
                         + (f": {missing[:3]}" if missing else "")))

    return {"ok": all(c["ok"] for c in checks), "checks": checks}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="data/wsrig")
    args = ap.parse_args()
    res = verify(list(read_tape(Path(args.dir))))
    for c in res["checks"]:
        print(f"  {'PASS' if c['ok'] else 'FAIL'}  {c['name']:20} {c['detail']}")
    print(f"\n  TAPE: {'USABLE' if res['ok'] else 'NOT TRUSTWORTHY'}")
    raise SystemExit(0 if res["ok"] else 1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/wsrig/test_verify_tape.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add wsrig/verify_tape.py tests/wsrig/test_verify_tape.py
git commit -m "wsrig: tape integrity verifier — empty, gaps, clock steps, coverage"
```

---

### Task 3: Coinbase spot feed

**Files:**
- Create: `wsrig/ws_spot.py`
- Test: `tests/wsrig/test_ws_spot.py`

**Interfaces:**
- Consumes: `wsrig.tape.Tape`.
- Produces: `parse_ticker(msg: dict) -> dict | None` returning `{"k": "spot", "tx": float|None, "p": float}` or `None` for non-ticker/malformed messages; and `async run_spot_feed(tape, symbols: list[str], stop: asyncio.Event) -> None`.

Adapted from `/home/kenny/bots/daedalus-mm/daedalus/data/external/coinbase_ws.py`. The parsing is split out as a pure function so it can be tested without a socket.

- [ ] **Step 1: Write the failing test**

```python
# tests/wsrig/test_ws_spot.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from wsrig.ws_spot import parse_ticker


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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/wsrig/test_ws_spot.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'wsrig.ws_spot'`

- [ ] **Step 3: Write the implementation**

```python
# wsrig/ws_spot.py
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/wsrig/test_ws_spot.py -v`
Expected: 5 passed

- [ ] **Step 5: Install the dependency and confirm the import works**

```bash
.venv/bin/pip install 'websockets>=12'
.venv/bin/python -c "import wsrig.ws_spot; print('ok')"
```

- [ ] **Step 6: Commit**

```bash
git add wsrig/ws_spot.py tests/wsrig/test_ws_spot.py
git commit -m "wsrig: Coinbase spot WS feed with pure, testable parsing"
```

---

### Task 4: Kalshi book feed

**Files:**
- Create: `wsrig/ws_kalshi.py`
- Test: `tests/wsrig/test_ws_kalshi.py`

**Interfaces:**
- Consumes: `api.KalshiAPI` (for `_sign_request`), `wsrig.tape.Tape`.
- Produces: `SeqTracker` with `check(sid: int, seq: int) -> dict | None` (returns a `gap` record when a sequence number is skipped, else `None`); `parse_book(msg: dict) -> dict | None`; and `async run_kalshi_feed(tape, tickers_source, stop) -> None`.

Adapt `/home/kenny/bots/daedalus-mm/daedalus/venue/kalshi_ws.py`, keeping its connection, backoff, watchdog and sequence-gap logic. Strip: `structlog` (use `logging`), `daedalus.venue.models` (`WSEnvelope`/`WSFill`/`FeedGapEvent`/`TimerTick` — use plain dicts), and `daedalus.venue.auth.sign_request` (use `KalshiAPI._sign_request("GET", "/trade-api/ws/v2")`, which produces the same three headers).

- [ ] **Step 1: Write the failing test**

```python
# tests/wsrig/test_ws_kalshi.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from wsrig.ws_kalshi import SeqTracker, parse_book


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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/wsrig/test_ws_kalshi.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'wsrig.ws_kalshi'`

- [ ] **Step 3: Write the parsing half**

Write `SeqTracker` and `parse_book` exactly as below. The connection loop is added in Step 3b.

```python
# wsrig/ws_kalshi.py  (parsing half — port the connection loop per the notes above)
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
```

- [ ] **Step 3b: Add the connection loop**

Append this to `wsrig/ws_kalshi.py`. It is the daedalus loop reduced to what a
capture rig needs: no `messages()` iterator, no `TimerTick`, no pydantic
envelopes — records go straight to the tape.

```python
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
    api = KalshiAPI(api_key=os.environ.get("KALSHI_API_KEY"),
                    private_key_path=os.environ.get("KALSHI_PRIVATE_KEY_PATH"))
    return api._sign_request("GET", WS_PATH)


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
```

Note `additional_headers` — `websockets` ≥ 14 renamed it from `extra_headers`.
If the installed version rejects it, use `extra_headers`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/wsrig/test_ws_kalshi.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add wsrig/ws_kalshi.py tests/wsrig/test_ws_kalshi.py
git commit -m "wsrig: Kalshi book feed with per-sid sequence-gap detection"
```

---

### Task 5: Active-market tracker

**Files:**
- Create: `wsrig/market_tracker.py`
- Test: `tests/wsrig/test_market_tracker.py`

**Interfaces:**
- Consumes: `api.KalshiAPI.get_markets`.
- Produces: `active_btc15m(markets: list[dict], now: float, lookahead_s: float = 1200.0) -> list[str]` — tickers of KXBTC15M markets currently open or opening within `lookahead_s`; and `async run_tracker(api, on_change, stop, interval_s=60.0)` calling `on_change(tickers)` only when the set changes.

- [ ] **Step 1: Write the failing test**

```python
# tests/wsrig/test_market_tracker.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from wsrig.market_tracker import active_btc15m

NOW = 1_786_000_000.0


def _m(ticker, close_offset, status="active"):
    return {"ticker": ticker, "status": status, "close_ts": NOW + close_offset}


def test_selects_open_btc15m_markets():
    got = active_btc15m([_m("KXBTC15M-A", 600)], NOW)
    assert got == ["KXBTC15M-A"]


def test_excludes_other_series():
    ms = [_m("KXETH15M-A", 600), _m("KXBTC-DAILY", 600), _m("KXBTC15M-A", 600)]
    assert active_btc15m(ms, NOW) == ["KXBTC15M-A"]


def test_excludes_already_closed_markets():
    assert active_btc15m([_m("KXBTC15M-OLD", -60)], NOW) == []


def test_includes_the_next_market_before_it_opens():
    """Subscribing only at open would miss the first quotes of every market,
    which is a non-random slice of exactly the window we measure."""
    assert active_btc15m([_m("KXBTC15M-SOON", 1500)], NOW, lookahead_s=1800) == \
        ["KXBTC15M-SOON"]


def test_excludes_markets_far_in_the_future():
    assert active_btc15m([_m("KXBTC15M-LATER", 99_999)], NOW, lookahead_s=1200) == []


def test_substring_match_cannot_catch_unrelated_series():
    """`"15M" in ticker` matches KXUFCFIGHT-26AUG15MAKMGI-MGI. Match the series
    prefix instead — this bug is live in scanner.py:enrich_markets."""
    assert active_btc15m([_m("KXUFCFIGHT-26AUG15MAKMGI-MGI", 600)], NOW) == []


def test_result_is_sorted_for_stable_comparison():
    ms = [_m("KXBTC15M-B", 600), _m("KXBTC15M-A", 600)]
    assert active_btc15m(ms, NOW) == ["KXBTC15M-A", "KXBTC15M-B"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/wsrig/test_market_tracker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'wsrig.market_tracker'`

- [ ] **Step 3: Write the implementation**

```python
# wsrig/market_tracker.py
"""Which KXBTC15M markets should we be subscribed to right now?

Markets roll every 15 minutes. Subscribing only once a market is open would
miss its opening quotes, and the trigger window (mins_left 5-11) sits early in
a market's life -- so a late subscribe removes a non-random slice of exactly
what is being measured. Hence the lookahead.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("wsrig.tracker")

SERIES = "KXBTC15M"
POLL_INTERVAL_S = 60.0
LOOKAHEAD_S = 1200.0


def active_btc15m(markets: list[dict], now: float,
                  lookahead_s: float = LOOKAHEAD_S) -> list[str]:
    out = []
    for m in markets:
        ticker = m.get("ticker") or ""
        # Series-prefix match, not a substring test: `"15M" in ticker` also
        # catches events dated the 15th, e.g. KXUFCFIGHT-26AUG15MAKMGI-MGI.
        if ticker.split("-")[0] != SERIES:
            continue
        close_ts = m.get("close_ts")
        if close_ts is None or close_ts <= now:
            continue
        if close_ts - now > lookahead_s:
            continue
        out.append(ticker)
    return sorted(out)


async def run_tracker(api, on_change, stop: asyncio.Event,
                      interval_s: float = POLL_INTERVAL_S) -> None:
    """Poll REST for the active set; call on_change(tickers) when it changes."""
    import time
    current: list[str] = []
    while not stop.is_set():
        try:
            data = await asyncio.get_running_loop().run_in_executor(
                None, lambda: api.get_markets(status="open", limit=200))
            tickers = active_btc15m(data.get("markets", []), time.time())
            if tickers != current:
                log.info("active markets: %s -> %s", current, tickers)
                current = tickers
                await on_change(tickers)
        except Exception as exc:                      # never kill the capture
            log.warning("tracker poll failed: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/wsrig/test_market_tracker.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add wsrig/market_tracker.py tests/wsrig/test_market_tracker.py
git commit -m "wsrig: active KXBTC15M tracker with lookahead and prefix matching"
```

---

### Task 6: Settlement poller

**Files:**
- Create: `wsrig/settlement.py`
- Test: `tests/wsrig/test_settlement.py`

**Interfaces:**
- Consumes: `api.KalshiAPI.get_markets_by_tickers`, `wsrig.tape.Tape`.
- Produces: `settle_record(market: dict) -> dict | None` returning `{"k": "settle", "t": ticker, "result": "yes"|"no"}` only for finalised markets; and `async run_settlement(api, pending: set[str], tape, stop, interval_s=300.0)`.

The isolated rig cannot use the scanner's feature log, so settlement comes from Kalshi's authoritative `result` field rather than being inferred from `sign(distance)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/wsrig/test_settlement.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from wsrig.settlement import settle_record


def test_records_a_settled_yes():
    r = settle_record({"ticker": "KXBTC15M-A", "status": "settled", "result": "yes"})
    assert r == {"k": "settle", "t": "KXBTC15M-A", "result": "yes"}


def test_records_a_settled_no():
    assert settle_record({"ticker": "KXBTC15M-A", "status": "settled",
                          "result": "no"})["result"] == "no"


def test_open_markets_are_not_settled():
    assert settle_record({"ticker": "KXBTC15M-A", "status": "active"}) is None


def test_closed_but_unresolved_is_not_settled():
    """A market closes before it settles. Recording it early would invent an
    outcome, and a wrong outcome silently flips the sign of an edge."""
    assert settle_record({"ticker": "KXBTC15M-A", "status": "closed",
                          "result": ""}) is None


def test_an_unexpected_result_value_is_rejected():
    assert settle_record({"ticker": "KXBTC15M-A", "status": "settled",
                          "result": "void"}) is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/wsrig/test_settlement.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'wsrig.settlement'`

- [ ] **Step 3: Write the implementation**

```python
# wsrig/settlement.py
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/wsrig/test_settlement.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add wsrig/settlement.py tests/wsrig/test_settlement.py
git commit -m "wsrig: authoritative settlement from Kalshi's result field"
```

---

### Task 7: Supervisor, systemd unit, health check

**Files:**
- Create: `wsrig/main.py`
- Create: `deploy/kalshi-wsrig.service`
- Modify: `health_check.py` — add a tape-freshness check (no process check; see Step 3)
- Test: `tests/wsrig/test_main.py`, and extend `tests/test_health_check.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `python -m wsrig.main --dir data/wsrig` running until SIGTERM; `health_check.check_tape_age(age_s, limit)` returning the standard check dict.

- [ ] **Step 1: Write the failing test**

```python
# tests/wsrig/test_main.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from wsrig.main import pending_after_roll


def test_a_market_leaving_the_active_set_becomes_pending_settlement():
    assert pending_after_roll({"A", "B"}, ["B", "C"]) == {"A"}


def test_nothing_pending_when_the_set_only_grows():
    assert pending_after_roll({"A"}, ["A", "B"]) == set()


def test_all_previous_markets_pend_when_the_set_empties():
    assert pending_after_roll({"A", "B"}, []) == {"A", "B"}
```

Add to `tests/test_health_check.py`:

```python
def test_tape_age_fails_when_the_rig_stops_writing():
    """A dead rig mid-capture yields a partial tape nobody notices for days."""
    from health_check import check_tape_age
    assert check_tape_age(4000.0, limit=600.0)["ok"] is False


def test_tape_age_passes_when_fresh():
    from health_check import check_tape_age
    assert check_tape_age(30.0, limit=600.0)["ok"] is True


def test_tape_age_absent_is_a_failure_not_a_pass():
    from health_check import check_tape_age
    assert check_tape_age(None, limit=600.0)["ok"] is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/wsrig/test_main.py tests/test_health_check.py -v`
Expected: FAIL — no module `wsrig.main`; `ImportError: cannot import name 'check_tape_age'`

- [ ] **Step 3: Write the implementation**

```python
# wsrig/main.py
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

    api = KalshiAPI(api_key=os.environ.get("KALSHI_API_KEY"),
                    private_key_path=os.environ.get("KALSHI_PRIVATE_KEY_PATH"))
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
```

Add to `health_check.py`, next to `check_age`:

```python
def check_tape_age(age_s, limit):
    """Freshness of the wsrig capture tape.

    A capture that dies mid-run leaves a partial tape that still analyses
    cleanly and yields a confident, wrong number. Absent counts as failure.
    """
    if age_s is None:
        return {"name": "age:wsrig_tape", "ok": False,
                "detail": "no tape written"}
    ok = age_s <= limit
    return {"name": "age:wsrig_tape", "ok": ok,
            "detail": f"{age_s:.0f}s old (limit {limit:.0f}s)"}
```

Then register it inside the existing `collect()` in `health_check.py`. Insert
the marked lines immediately before `checks.append(check_latency(...))`:

```python
def collect():
    checks = [check_process(n, _count(p)) for n, p in COLLECTORS.items()]
    checks.append(check_age("swing_bot_heartbeat",
                            _state_heartbeat_age(BASE / "data/bot/bot_state.json"),
                            limit=120))
    checks.append(check_age("signal_feature_log",
                            _age_of(BASE / "data/whales/signal_feature_log.jsonl"),
                            limit=600))
    # --- ADDED: only checked once the rig is actually capturing, so this
    # --- stays silent on boxes where wsrig was never installed.
    tape_dir = BASE / "data/wsrig"
    tapes = sorted(tape_dir.glob("tape-*.jsonl.gz")) if tape_dir.exists() else []
    if tapes:
        checks.append(check_tape_age(time.time() - tapes[-1].stat().st_mtime,
                                     limit=600))
    # --- END ADDED
    checks.append(check_latency(_sample_latency(), FETCH_TIMEOUT))
    checks.append(check_footprint(*_scanner_footprint()))
    return checks
```

Do **not** add `wsrig` to `COLLECTORS`: that dict demands exactly one instance
of each process and fails when absent, which would make `health_check.py`
report DEGRADED on every box before the rig is installed. The tape-age check
above is the liveness signal, and it is self-disabling.

```ini
# deploy/kalshi-wsrig.service
[Unit]
Description=Kalshi websocket measurement rig (capture only, no trading)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory=/home/kenny/bots/kalshi-scanner
ExecStart=/home/kenny/bots/kalshi-scanner/.venv/bin/python -m wsrig.main --dir data/wsrig
Restart=always
RestartSec=15
StandardOutput=append:/home/kenny/bots/kalshi-scanner/logs/wsrig.log
StandardError=append:/home/kenny/bots/kalshi-scanner/logs/wsrig.log

[Install]
WantedBy=default.target
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/wsrig/ tests/test_health_check.py -v`
Expected: all pass

- [ ] **Step 5: Run the full suite — nothing else may break**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 386 + new tests, 0 failures

- [ ] **Step 6: Commit**

```bash
git add wsrig/main.py deploy/kalshi-wsrig.service health_check.py tests/wsrig/test_main.py tests/test_health_check.py
git commit -m "wsrig: supervisor, systemd unit, and tape-freshness health check"
```

---

### Task 8: Phase 0 smoke capture — verify assumptions before committing a week

**Files:**
- Modify: `docs/superpowers/specs/2026-08-13-websocket-measurement-rig-design.md` (append `## Phase 0 results`)
- No new module and no test file. This task's deliverable is a verified factual
  report about the live feeds, recorded in the spec. The scripts below are
  one-off probes, deliberately not committed as code.

**Interfaces:**
- Consumes: everything above.
- Produces: a printed report answering the spec's three open questions, plus a `data/wsrig-smoke/` tape.

**Do not proceed to Task 9 until every check below passes.** Each answers an
assumption that, if wrong, wastes the entire capture week.

- [ ] **Step 1: Run a one-hour capture into a separate directory**

```bash
cd /home/kenny/bots/kalshi-scanner
timeout 3600 .venv/bin/python -m wsrig.main --dir data/wsrig-smoke
```

- [ ] **Step 2: Verify tape integrity**

```bash
.venv/bin/python -m wsrig.verify_tape --dir data/wsrig-smoke
```

Expected: `TAPE: USABLE`. If `book_coverage` fails, the tracker is subscribing
too late — raise `LOOKAHEAD_S`. If `sequence_gaps` fails, record how many and
on which sid before continuing.

- [ ] **Step 3: Answer the spec's three open questions**

```bash
.venv/bin/python - <<'PY'
import collections
from pathlib import Path
from wsrig.tape import read_tape

recs = list(read_tape(Path("data/wsrig-smoke")))
spot = [r for r in recs if r["k"] == "spot"]
book = [r for r in recs if r["k"] == "book"]

span = max(r["tm"] for r in recs) - min(r["tm"] for r in recs)
print(f"span {span/60:.1f} min, {len(recs)} records")
print(f"Q2 message rate: spot {len(spot)/span:.2f}/s, book {len(book)/span:.2f}/s")
print(f"   projected 7-day tape: {len(recs)/span*86400*7/1e6:.1f}M records")

both = sum(1 for r in book if r["ya"] is not None and r["na"] is not None)
print(f"Q1 book records carrying BOTH asks: {both}/{len(book)} "
      f"({100*both/max(len(book),1):.1f}%)")
print(f"   message types seen: {collections.Counter(r['mtype'] for r in book)}")

lags = [r["tw"] - r["tx"] for r in spot if r.get("tx")]
lags.sort()
if lags:
    print(f"Q3 spot feed lag (tw - tx): p50 {lags[len(lags)//2]*1000:.0f}ms, "
          f"p95 {lags[int(len(lags)*0.95)]*1000:.0f}ms")
print(f"   distinct markets seen: {len({r['t'] for r in book})}")
PY
```

- [ ] **Step 3b: Confirm the WS spot series matches the REST series the edge was measured on**

The original momentum study used the REST `price` field. If the WS feed
reports a materially different series, the trigger will not reproduce.

```bash
.venv/bin/python - <<'PY'
import json, urllib.request, statistics
from pathlib import Path
from wsrig.tape import read_tape

with urllib.request.urlopen(
        "https://api.exchange.coinbase.com/products/BTC-USD/ticker", timeout=5) as r:
    rest = float(json.loads(r.read())["price"])
ws = [r["p"] for r in read_tape(Path("data/wsrig-smoke")) if r["k"] == "spot"][-20:]
print(f"REST price {rest:.2f}   WS last-20 median {statistics.median(ws):.2f}")
print(f"difference {abs(rest - statistics.median(ws)):.2f} "
      f"-- must be within normal tick noise (a few dollars), not a systematic offset")
PY
```

- [ ] **Step 4: Record the answers in the spec**

Append a `## Phase 0 results` section to
`docs/superpowers/specs/2026-08-13-websocket-measurement-rig-design.md` with
the measured message rate, projected tape size, whether both asks are present,
observed feed lag, and the REST-vs-WS comparison.

- [ ] **Step 5: Commit**

```bash
rm -rf data/wsrig-smoke
git add docs/superpowers/specs/2026-08-13-websocket-measurement-rig-design.md
git commit -m "wsrig: Phase 0 smoke results — feed rates, payload shape, spot-series match"
```

**Gate:** if `both asks present` is under 100%, the `ticker` channel is
insufficient and `orderbook_delta` must be reconstructed into top-of-book
before Task 10 can price anything. Resolve that here, not after the capture.

---

### Task 9: Start the capture

**Files:**
- No code. This task installs and starts the rig, and its deliverable is a running capture.

- [ ] **Step 1: Install and start the unit**

```bash
cp deploy/kalshi-wsrig.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now kalshi-wsrig
systemctl --user is-active kalshi-wsrig
```

- [ ] **Step 2: Confirm it is capturing and the scanner is unharmed**

```bash
sleep 120
.venv/bin/python -m wsrig.verify_tape --dir data/wsrig
.venv/bin/python health_check.py
```

Expected: tape USABLE, and health `OVERALL: HEALTHY` with every pre-existing
check still passing.

- [ ] **Step 3: Check again after 24 hours before trusting the week**

```bash
.venv/bin/python -m wsrig.verify_tape --dir data/wsrig
du -sh data/wsrig
```

- [ ] **Step 4: Let it run 5-7 days.** Do not analyse early; peeking at partial
data and then continuing is how a pre-registered bar becomes a negotiable one.

---

### Task 10: Arm A decay analysis and the go/no-go

**Files:**
- Create: `wsrig/decay.py`
- Test: `tests/wsrig/test_decay.py`

**Interfaces:**
- Consumes: `wsrig.tape.read_tape`.
- Produces: `resample_5s(spot: list[dict]) -> list[tuple[float, float]]`; `momentum_at(series, t, window_s=90.0) -> float | None` reproducing `web.py:827-833` exactly; `ask_at(book: list[dict], ticker: str, t: float, side: str) -> float | None`; `edge(side, ask, result) -> float`; and `run(records, deltas) -> dict`.

- [ ] **Step 1: Write the failing test**

```python
# tests/wsrig/test_decay.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from wsrig.decay import ask_at, edge, momentum_at, resample_5s


def test_resample_takes_the_last_tick_in_each_5s_bucket():
    spot = [{"tm": 0.0, "p": 100.0}, {"tm": 1.0, "p": 101.0}, {"tm": 6.0, "p": 110.0}]
    assert resample_5s(spot) == [(0.0, 101.0), (5.0, 110.0)]


def test_momentum_reproduces_the_production_endpoint_slope():
    """web.py:827-833 uses ONLY the first and last sample in the window:
    (p_last - p_first) / (t_last - t_first) * 60. Not a regression."""
    series = [(0.0, 100.0), (30.0, 999.0), (60.0, 130.0)]   # midpoint ignored
    assert momentum_at(series, 60.0, window_s=90.0) == 30.0


def test_momentum_needs_a_span_over_one_second():
    assert momentum_at([(0.0, 100.0), (0.5, 200.0)], 0.5) is None


def test_momentum_is_none_with_fewer_than_two_samples():
    assert momentum_at([(0.0, 100.0)], 0.0) is None


def test_momentum_only_uses_samples_inside_the_window():
    series = [(0.0, 100.0), (500.0, 100.0), (560.0, 130.0)]
    assert momentum_at(series, 560.0, window_s=90.0) == 30.0


def test_ask_at_uses_the_last_quote_at_or_before_the_instant():
    book = [{"tm": 0.0, "t": "A", "ya": 0.40, "na": 0.60},
            {"tm": 5.0, "t": "A", "ya": 0.45, "na": 0.55}]
    assert ask_at(book, "A", 4.9, "YES") == 0.40
    assert ask_at(book, "A", 5.1, "YES") == 0.45
    assert ask_at(book, "A", 5.1, "NO") == 0.55


def test_ask_at_never_looks_ahead():
    """Using a quote from after the action instant is lookahead bias and would
    manufacture an edge."""
    book = [{"tm": 10.0, "t": "A", "ya": 0.40, "na": 0.60}]
    assert ask_at(book, "A", 5.0, "YES") is None


def test_ask_at_ignores_other_tickers():
    book = [{"tm": 0.0, "t": "OTHER", "ya": 0.10, "na": 0.90}]
    assert ask_at(book, "A", 1.0, "YES") is None


def test_edge_pays_one_minus_ask_on_a_win_and_minus_ask_on_a_loss():
    assert edge("YES", 0.40, "yes") == 0.60
    assert edge("YES", 0.40, "no") == -0.40
    assert edge("NO", 0.55, "no") == 0.45
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/wsrig/test_decay.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'wsrig.decay'`

- [ ] **Step 3: Write the implementation**

```python
# wsrig/decay.py
"""Arm A: does the momentum edge survive a realistic action latency?

Reproduces the PRODUCTION estimator exactly (web.py:827-833) on a 5s-resampled
series, so the only difference from the original study is that the fill price
at delta is observed rather than extrapolated. Using a better estimator here
would confound the estimator with the latency, which is the one thing this must
not do -- see Arm B in the spec.
"""
from __future__ import annotations

import bisect


def resample_5s(spot: list[dict]) -> list[tuple[float, float]]:
    """Last tick in each 5s bucket, mimicking a 5s poller's view."""
    buckets: dict[float, float] = {}
    for r in spot:
        buckets[(r["tm"] // 5.0) * 5.0] = r["p"]
    return [(k, buckets[k]) for k in sorted(buckets)]


def momentum_at(series: list[tuple[float, float]], t: float,
                window_s: float = 90.0) -> float | None:
    """$/min over the trailing window, endpoint slope — production's formula."""
    recent = [(ts, p) for ts, p in series if 0 <= t - ts < window_s]
    if len(recent) < 2:
        return None
    dt_span = recent[-1][0] - recent[0][0]
    dp_span = recent[-1][1] - recent[0][1]
    if dt_span <= 1:
        return None
    return round((dp_span / dt_span) * 60, 2)


def ask_at(book: list[dict], ticker: str, t: float, side: str) -> float | None:
    """Best ask for `side` at instant `t`. Never looks ahead of `t`."""
    key = "ya" if side == "YES" else "na"
    rows = [r for r in book if r.get("t") == ticker and r.get(key) is not None]
    times = [r["tm"] for r in rows]
    i = bisect.bisect_right(times, t) - 1
    return rows[i][key] if i >= 0 else None


def edge(side: str, ask: float, result: str) -> float:
    """Payout minus cost, per contract, before fees."""
    won = (side == "YES" and result == "yes") or (side == "NO" and result == "no")
    return round((1.0 - ask) if won else -ask, 6)


def taker_fee(p: float) -> float:
    return 0.07 * p * (1 - p)
```

- [ ] **Step 3b: Add the driver**

```python
MOM_THRESHOLD = 30.0
MINS_LEFT_LO, MINS_LEFT_HI = 5.0, 11.0
DELTAS = (0.0, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0)


def run(records: list[dict], deltas=DELTAS) -> dict:
    """One event per market: first trigger instant, edge at each delta.

    Counting every trigger TICK instead would overstate n by ~15x -- 491
    trigger-ticks/day against 33.5 distinct markets/day.
    """
    spot = [r for r in records if r.get("k") == "spot"]
    book = [r for r in records if r.get("k") == "book"]
    settled = {r["t"]: r["result"] for r in records if r.get("k") == "settle"}
    close_tm = {}
    for r in book:                       # last quote seen ~ market close
        close_tm[r["t"]] = max(close_tm.get(r["t"], r["tm"]), r["tm"])

    series = resample_5s(spot)
    events = []
    for ticker, result in sorted(settled.items()):
        if ticker not in close_tm:
            continue                     # no quotes: excluded, not guessed
        quotes = [r for r in book if r["t"] == ticker]
        fired = None
        for ts, _ in series:
            mins_left = (close_tm[ticker] - ts) / 60.0
            if not (MINS_LEFT_LO <= mins_left <= MINS_LEFT_HI):
                continue
            mom = momentum_at(series, ts)
            if mom is not None and abs(mom) >= MOM_THRESHOLD:
                fired = (ts, "YES" if mom > 0 else "NO")
                break                    # first trigger only
        if fired is None:
            continue
        t0, side = fired
        row = {"ticker": ticker, "t0": t0, "side": side, "result": result}
        for d in deltas:
            ask = ask_at(quotes, ticker, t0 + d, side)
            row[f"d{d}"] = (None if ask is None
                            else round(edge(side, ask, result) - taker_fee(ask), 6))
        events.append(row)

    out = {"n": len(events), "events": events, "by_delta": {}}
    for d in deltas:
        vals = [e[f"d{d}"] for e in events if e.get(f"d{d}") is not None]
        if vals:
            m = sum(vals) / len(vals)
            var = sum((v - m) ** 2 for v in vals) / len(vals)
            se = (var / len(vals)) ** 0.5
            out["by_delta"][d] = {"n": len(vals), "mean": round(m, 5),
                                  "se": round(se, 5),
                                  "t": round(m / se, 2) if se else None}
    return out
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/wsrig/test_decay.py -v`
Expected: 9 passed

- [ ] **Step 5: Verify the tape before analysing it**

```bash
.venv/bin/python -m wsrig.verify_tape --dir data/wsrig
```

If this reports `NOT TRUSTWORTHY`, fix and recapture. Do not analyse a tape
that failed verification.

- [ ] **Step 6: Register the hypothesis, then run the analysis**

Register in `hypothesis_gate.py` BEFORE looking at any output, then run. Apply
the spec's bar at δ=1s: n≥30 per window across 3 disjoint windows, positive in
all 3, |t| ≥ 2.64, magnitude ≥ +0.02/contract, survives drop-two-best-days.

- [ ] **Step 7: Write the verdict up either way**

Record the result in the spec under `## Outcome`, and update the
`swing-bot-no-replay-edge-yet` memory. **If the bar is not met the idea is
dead** — no re-slicing, no relaxing the threshold, no promoting Arm B.

- [ ] **Step 8: Commit**

```bash
git add wsrig/decay.py tests/wsrig/test_decay.py docs/superpowers/specs/2026-08-13-websocket-measurement-rig-design.md
git commit -m "wsrig: Arm A decay analysis and go/no-go verdict"
```
