# Live Auto/Manual Broker Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the swing bot two symmetric live-trading sub-modes — `manual` (formalizes tonight's ad hoc signal-flagging script into real, tested code) and `auto` (places real orders) — both gated by the existing, unchanged 4-way `live_unlock_ok` check, with auto mode shipping fully wired but inert (no session currently clears the gate except weekday_night, and the gate requires all 4).

**Architecture:** A new `broker_mode` config key (`"manual"` | `"auto"`, default `"manual"`) sits alongside the existing `mode` (`"paper"` | `"live"`). `LiveBroker` (currently a stub that always raises) gains real `buy`/`sell`/`fill` implementations that route to either `emit_live_signal()` (manual — writes a structured event, no API call) or real signed Kalshi order calls (auto, via a new `live_broker.py`). A new per-tick `Bot._check_live_stop()` enforces a hard/daily-soft dollar stop against the *real* account balance before any auto entry, reusing the existing per-pool `halted` flag and `entry_blockers()`'s existing `halted` check — no changes needed to that pure function's signature.

**Tech Stack:** Python 3.11, pytest, `requests` (already a dependency), `cryptography` (already a dependency, used by `account.py`'s PSS-SHA256 signing).

## Global Constraints

- The 4-way `live_unlock_ok` gate (`bot_broker.py`) is **not modified** by this plan. No task changes its logic, its 100-trade floor, or its per-session independence.
- `bot_core.py` is documented as "no I/O except config file read" (its own module docstring) — nothing added to that file may fetch a live balance or make a network call. The live-balance stop check belongs in `swing_bot.py` (which already does I/O), not `bot_core.py`.
- Sizing for `auto` mode never uses `bot_core.trade_budget`'s %-of-pool formula. It reads flat config values (`live_qty`, `live_cap_usd`) instead — that formula was proven this session to produce unreasonable size (up to 33 contracts) on a small real account.
- Any real order failure, rejection, or partial fill: log with an `error` field, skip the entry, halt new **auto** entries for that specific session pool only (reuse the existing `ps["halted"]` flag), never retry.
- No task in this plan enables auto mode. `data/bot/config.json`'s `mode` stays `"paper"` throughout — every new code path is exercised only by tests (mocked HTTP, mocked balance) until Kenny deliberately flips config in a later, separate decision.
- Every new test file follows the existing mocking convention already used throughout the suite: `monkeypatch.setattr(bot_broker, "_balance_dollars", lambda: ...)` for balance, and the equivalent pattern (mocking `requests.post`/`requests.get`/`requests.delete` at the call site) for order-placement HTTP — never a real network call in a test.

---

### Task 1: Config keys for live sizing and stops

**Files:**
- Modify: `bot_core.py:17-62` (`DEFAULT_CONFIG` dict)
- Test: `tests/test_bot_core.py`

**Interfaces:**
- Produces: five new `DEFAULT_CONFIG` keys — `broker_mode` (str, default `"manual"`), `live_qty` (int, default `1`), `live_cap_usd` (float, default `20.0`), `live_hard_stop_usd` (float, default `-8.0`), `live_daily_soft_stop_usd` (float, default `-3.0`) — consumed by Tasks 4 and 6.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_bot_core.py`:

```python
from bot_core import DEFAULT_CONFIG


def test_default_config_has_live_broker_mode_keys():
    assert DEFAULT_CONFIG["broker_mode"] == "manual"
    assert DEFAULT_CONFIG["live_qty"] == 1
    assert DEFAULT_CONFIG["live_cap_usd"] == 20.0
    assert DEFAULT_CONFIG["live_hard_stop_usd"] == -8.0
    assert DEFAULT_CONFIG["live_daily_soft_stop_usd"] == -3.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_bot_core.py -q -k live_broker_mode_keys -v`
Expected: FAIL with `KeyError: 'broker_mode'`

- [ ] **Step 3: Add the config keys**

In `bot_core.py`, add to the end of `DEFAULT_CONFIG` (right before the closing `}` on line 62, after `"limit_fill_timeout_secs"`):

```python
    "limit_fill_timeout_secs": 30,  # how long a resting entry waits before
                                     # chasing to a market (taker) fill
    "broker_mode": "manual",   # "manual": mode=live entries write a
                                # live_signal event for a human to place by
                                # hand (no order API call). "auto": places
                                # real orders. Independent of `mode` --
                                # only meaningful when mode=="live".
    "live_qty": 1,              # flat contract count per auto/manual live
                                 # entry -- NOT bot_core.trade_budget's
                                 # %-of-pool formula, which was proven this
                                 # session to size unreasonably large (up
                                 # to 33 contracts) on a small real account
    "live_cap_usd": 20.0,       # informational total-stake cap Kenny is
                                 # running live with; not itself enforced
                                 # here (live_hard_stop_usd is the actual
                                 # enforced limit) -- kept alongside it so
                                 # the two numbers can't drift apart when
                                 # Kenny raises one
    "live_hard_stop_usd": -8.0,        # real account PnL-since-test-start
                                        # floor; auto entries blocked at or
                                        # below this (see Bot._check_live_stop)
    "live_daily_soft_stop_usd": -3.0,  # same, but resets daily (per-pool
                                        # day_pnl-style), pauses new auto
                                        # entries for the rest of that day
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_bot_core.py -q -k live_broker_mode_keys -v`
Expected: PASS

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all tests pass (155 + 1 new = 156)

- [ ] **Step 6: Commit**

```bash
git add bot_core.py tests/test_bot_core.py
git commit -m "Add broker_mode/live sizing/live stop config keys (inert, unused yet)"
```

---

### Task 2: `emit_live_signal` — the manual-mode event writer

**Files:**
- Modify: `bot_broker.py` (add near the top, after imports, before `PaperBroker`)
- Test: `tests/test_bot_broker.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `emit_live_signal(bot_dir, ticker: str, side: str, qty: int, price: float, tier: str, pool: str, error: str = None) -> None` — appends one JSON row to `<bot_dir>/live_signals.jsonl`. Consumed by Task 4 (`LiveBroker.buy`/`sell`/`fill` in manual mode) and Task 7 (auto-mode order-error logging).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_bot_broker.py` (new section at the end):

```python
import json as _json


def test_emit_live_signal_appends_one_row(tmp_path):
    from bot_broker import emit_live_signal
    emit_live_signal(tmp_path, "KXBTC15M-26JUL290300-00", "YES", 1, 0.31,
                     "patient", "weekday_night")
    rows = [_json.loads(l) for l in
            (tmp_path / "live_signals.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    r = rows[0]
    assert r["ticker"] == "KXBTC15M-26JUL290300-00"
    assert r["side"] == "YES"
    assert r["qty"] == 1
    assert r["price"] == 0.31
    assert r["tier"] == "patient"
    assert r["pool"] == "weekday_night"
    assert r["error"] is None
    assert isinstance(r["ts"], float)


def test_emit_live_signal_records_error(tmp_path):
    from bot_broker import emit_live_signal
    emit_live_signal(tmp_path, "KXBTC15M-26JUL290300-00", "YES", 1, 0.31,
                     "market", "weekday_night", error="order rejected: insufficient funds")
    rows = [_json.loads(l) for l in
            (tmp_path / "live_signals.jsonl").read_text().splitlines()]
    assert rows[0]["error"] == "order rejected: insufficient funds"


def test_emit_live_signal_appends_multiple_rows(tmp_path):
    from bot_broker import emit_live_signal
    emit_live_signal(tmp_path, "T1", "YES", 1, 0.50, "aggressive", "weekday_night")
    emit_live_signal(tmp_path, "T2", "NO", 2, 0.40, "patient", "weekday_night")
    rows = [_json.loads(l) for l in
            (tmp_path / "live_signals.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["ticker"] == "T1" and rows[1]["ticker"] == "T2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_bot_broker.py -q -k emit_live_signal -v`
Expected: FAIL with `ImportError: cannot import name 'emit_live_signal'`

- [ ] **Step 3: Implement `emit_live_signal`**

In `bot_broker.py`, add after the module docstring and imports (after `from backtest_gate import fee, maker_fee`), before `class PaperBroker:`:

```python
import json
import time
from pathlib import Path

LIVE_SIGNALS_FILE = "live_signals.jsonl"


def emit_live_signal(bot_dir, ticker: str, side: str, qty: int, price: float,
                     tier: str, pool: str, error: str = None) -> None:
    """Append one row to <bot_dir>/live_signals.jsonl -- the manual-mode
    broker path: instead of placing a real order, this is the signal a
    human reads and places by hand. Formalizes the ad hoc scratchpad
    watcher script used for the first night of the $20 live test into a
    real, tested code path (see the 2026-07-29 design spec)."""
    bot_dir = Path(bot_dir)
    bot_dir.mkdir(parents=True, exist_ok=True)
    row = {"ts": time.time(), "ticker": ticker, "side": side, "qty": qty,
           "price": price, "tier": tier, "pool": pool, "error": error}
    with (bot_dir / LIVE_SIGNALS_FILE).open("a") as f:
        f.write(json.dumps(row) + "\n")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_bot_broker.py -q -k emit_live_signal -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add bot_broker.py tests/test_bot_broker.py
git commit -m "Add emit_live_signal: formalized manual-mode broker event writer"
```

---

### Task 3: Signed POST/DELETE helpers for real order placement

**Files:**
- Create: `live_broker.py`
- Test: `tests/test_live_broker.py`

**Interfaces:**
- Consumes: `account._load_env()` (returns `(key_id, private_key_path)`), `account.HOST` (the Kalshi API base URL) — both already exist, unmodified.
- Produces: `place_order(side: str, action: str, ticker: str, qty: int, price: float, order_type: str) -> dict` (signed POST, returns the API's order object, including `order_id`), `get_order(order_id: str) -> dict` (signed GET), `cancel_order(order_id: str) -> dict` (signed DELETE). Consumed by Task 4.

`account.py`'s own docstring states "This script ONLY reads... It contains no order-placement code by design" — so the signing helpers for POST/DELETE live in this new file, not in `account.py`. Only credential *loading* (`_load_env`) is reused from there, exactly as the design spec requires ("no new credential path").

- [ ] **Step 1: Write the failing tests**

Create `tests/test_live_broker.py`:

```python
import pytest
from unittest import mock


def _fake_pk():
    """A real key isn't needed to test request construction/parsing --
    only that place_order/get_order/cancel_order build the right request
    and parse the response, so the actual .sign() call is mocked too."""
    pk = mock.Mock()
    pk.sign.return_value = b"fake-signature-bytes"
    return pk


def test_place_order_posts_signed_request_and_returns_order(monkeypatch):
    import live_broker
    monkeypatch.setattr(live_broker.account, "_load_env",
                        lambda: ("key-id-123", "/fake/path.pem"))
    monkeypatch.setattr(live_broker, "_load_private_key", lambda path: _fake_pk())

    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        r = mock.Mock()
        r.raise_for_status = lambda: None
        r.json = lambda: {"order": {"order_id": "abc-123", "status": "resting"}}
        return r

    monkeypatch.setattr(live_broker.requests, "post", fake_post)

    result = live_broker.place_order("yes", "buy", "KXBTC15M-26JUL290300-00",
                                     1, 0.31, "limit")
    assert result["order_id"] == "abc-123"
    assert captured["url"].endswith("/trade-api/v2/portfolio/orders")
    assert captured["headers"]["KALSHI-ACCESS-KEY"] == "key-id-123"
    assert "KALSHI-ACCESS-SIGNATURE" in captured["headers"]
    body = captured["json"]
    assert body["side"] == "yes"
    assert body["action"] == "buy"
    assert body["ticker"] == "KXBTC15M-26JUL290300-00"
    assert body["count"] == 1
    assert body["type"] == "limit"
    assert body["yes_price"] == 31   # cents, per Kalshi's integer-cents API


def test_get_order_returns_status(monkeypatch):
    import live_broker
    monkeypatch.setattr(live_broker.account, "_load_env",
                        lambda: ("key-id-123", "/fake/path.pem"))
    monkeypatch.setattr(live_broker, "_load_private_key", lambda path: _fake_pk())

    def fake_get(url, headers=None, timeout=None):
        r = mock.Mock()
        r.raise_for_status = lambda: None
        r.json = lambda: {"order": {"order_id": "abc-123", "status": "executed"}}
        return r

    monkeypatch.setattr(live_broker.requests, "get", fake_get)
    result = live_broker.get_order("abc-123")
    assert result["status"] == "executed"


def test_cancel_order_sends_delete(monkeypatch):
    import live_broker
    monkeypatch.setattr(live_broker.account, "_load_env",
                        lambda: ("key-id-123", "/fake/path.pem"))
    monkeypatch.setattr(live_broker, "_load_private_key", lambda path: _fake_pk())

    captured = {}

    def fake_delete(url, headers=None, timeout=None):
        captured["url"] = url
        r = mock.Mock()
        r.raise_for_status = lambda: None
        r.json = lambda: {"order": {"order_id": "abc-123", "status": "canceled"}}
        return r

    monkeypatch.setattr(live_broker.requests, "delete", fake_delete)
    result = live_broker.cancel_order("abc-123")
    assert result["status"] == "canceled"
    assert captured["url"].endswith("/trade-api/v2/portfolio/orders/abc-123")


def test_place_order_raises_on_http_error(monkeypatch):
    import live_broker
    monkeypatch.setattr(live_broker.account, "_load_env",
                        lambda: ("key-id-123", "/fake/path.pem"))
    monkeypatch.setattr(live_broker, "_load_private_key", lambda path: _fake_pk())

    def fake_post(url, headers=None, json=None, timeout=None):
        r = mock.Mock()
        r.raise_for_status = mock.Mock(
            side_effect=Exception("400 Bad Request: insufficient balance"))
        return r

    monkeypatch.setattr(live_broker.requests, "post", fake_post)
    with pytest.raises(Exception, match="insufficient balance"):
        live_broker.place_order("yes", "buy", "T", 1, 0.31, "limit")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_live_broker.py -q -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'live_broker'`

- [ ] **Step 3: Implement `live_broker.py`**

Create `live_broker.py`:

```python
#!/usr/bin/env python3
"""Real Kalshi order placement -- the `auto` broker_mode's execution layer.

Reuses account.py's credential loading (_load_env) but NOT account.py
itself for signing, since account.py is deliberately read-only by design
("no order-placement code" per its own docstring). This file is the one
place in the codebase that ever calls POST/DELETE on the Kalshi order
API, and only when bot_broker.LiveBroker is unlocked AND broker_mode is
"auto" (see bot_broker.py) -- manual mode never imports this module's
placement functions at all, only account._load_env indirectly isn't even
needed there since emit_live_signal makes no API call.
"""
import base64
import time

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import account

HOST = account.HOST
ORDERS_PATH = "/trade-api/v2/portfolio/orders"


def _load_private_key(path):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def _sign(pk, method: str, full_path: str):
    ts = str(int(time.time() * 1000))
    sig = pk.sign(f"{ts}{method}{full_path}".encode(),
                  padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                              salt_length=padding.PSS.MAX_LENGTH),
                  hashes.SHA256())
    key_id, kp_path = account._load_env()
    return key_id, {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
    }


def _headers(method: str, full_path: str):
    key_id, kp_path = account._load_env()
    pk = _load_private_key(kp_path)
    _, headers = _sign(pk, method, full_path)
    return headers


def place_order(side: str, action: str, ticker: str, qty: int,
                price: float, order_type: str) -> dict:
    """side: "yes"/"no". action: "buy"/"sell". price in dollars (converted
    to integer cents for the API here, matching Kalshi's *_price fields).
    order_type: "limit" or "market". Returns the API's order object."""
    headers = _headers("POST", ORDERS_PATH)
    price_key = "yes_price" if side == "yes" else "no_price"
    body = {"side": side, "action": action, "ticker": ticker, "count": qty,
            "type": order_type}
    if order_type == "limit":
        body[price_key] = round(price * 100)
    r = requests.post(HOST + ORDERS_PATH, headers=headers, json=body, timeout=15)
    r.raise_for_status()
    return r.json()["order"]


def get_order(order_id: str) -> dict:
    path = f"{ORDERS_PATH}/{order_id}"
    headers = _headers("GET", path)
    r = requests.get(HOST + path, headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()["order"]


def cancel_order(order_id: str) -> dict:
    path = f"{ORDERS_PATH}/{order_id}"
    headers = _headers("DELETE", path)
    r = requests.delete(HOST + path, headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()["order"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_live_broker.py -q -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add live_broker.py tests/test_live_broker.py
git commit -m "Add live_broker.py: signed order placement/status/cancel (unused, no caller yet)"
```

---

### Task 4: `LiveBroker.buy`/`sell`/`fill` — route to manual signal or auto order

**Files:**
- Modify: `bot_broker.py:97-111` (the `LiveBroker` class)
- Modify: `tests/test_bot_broker.py:124-127` (`test_live_broker_locked_raises` needs a config update, see Step 3)
- Test: `tests/test_bot_broker.py`

**Interfaces:**
- Consumes: `emit_live_signal` (Task 2), `live_broker.place_order`/`get_order`/`cancel_order` (Task 3), `live_unlock_ok` (existing, unmodified).
- Produces: `LiveBroker(trades, cfg, env=None)` — constructs successfully once unlocked (previously always raised even when unlocked); `.buy(side, qty, sig)`, `.sell(side, qty, sig)`, `.fill(price, qty, ts, maker=False)` — same three-method shape as `PaperBroker`, each returning a fill dict (or, in manual mode, a signal-only marker dict with no real fill) or raising with a captured error (logged via `emit_live_signal(..., error=...)` before re-raising, so Task 7's caller can catch it and halt the pool). Consumed by Task 5 (`Bot.__init__`) and Task 7 (error handling).

**Design note on the auto fill path:** matching the resting-limit/chase mechanics `PaperBroker.fill` already implements in simulation, `LiveBroker`'s auto `buy`/`sell` place a **market (IOC)** order directly — the resting-limit-then-chase *state machine* (place limit → poll → cancel-and-chase) is orchestrated by `swing_bot.py`'s existing `_place_entry`/`_process_pending`/`_fill_pending` loop (Task 5 wires this), which already tracks pending entries tick-by-tick; `LiveBroker.fill(price, qty, ts, maker=True)` is what gets called once that loop decides a resting order should be placed, and `LiveBroker.buy`/`sell` are what get called for an immediate/chased entry. `fill(..., maker=True)` places a **limit** order at `price` and returns immediately with the order's `order_id` tracked in the pending-entry state (added to `pend` dict) rather than blocking on fill confirmation in this call — confirmation happens on a later tick via a new poll step (Task 5, Step 3's `_process_pending` change).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_bot_broker.py`:

```python
def test_live_broker_manual_buy_emits_signal_not_order(tmp_path, monkeypatch):
    import bot_broker
    monkeypatch.setattr(bot_broker, "requests", None)  # would explode if called
    cfg = {"live_requested": True, "broker_mode": "manual"}
    env = {"BOT_LIVE": "1"}
    b = bot_broker.LiveBroker(_all_sessions_trades(), cfg, env, bot_dir=tmp_path)
    fill = b.buy("YES", 1, {"yes_ask": 0.31, "no_ask": 0.71, "ts": 1000.0,
                            "ticker": "T1", "mins_left": 10.0})
    assert fill["price"] == 0.31
    assert fill["qty"] == 1
    assert fill["fee_total"] == 0.0   # nothing was actually filled
    assert fill.get("signal_only") is True
    rows = [_json.loads(l) for l in (tmp_path / "live_signals.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["side"] == "YES" and rows[0]["price"] == 0.31


def test_live_broker_auto_buy_places_market_order(tmp_path, monkeypatch):
    import bot_broker
    import live_broker
    cfg = {"live_requested": True, "broker_mode": "auto"}
    env = {"BOT_LIVE": "1"}
    calls = []
    monkeypatch.setattr(live_broker, "place_order", lambda *a, **k: calls.append((a, k)) or
                        {"order_id": "abc", "status": "executed",
                         "yes_price": 31, "no_price": 69})
    b = bot_broker.LiveBroker(_all_sessions_trades(), cfg, env, bot_dir=tmp_path)
    fill = b.buy("YES", 1, {"yes_ask": 0.31, "no_ask": 0.71, "ts": 1000.0,
                            "ticker": "T1", "mins_left": 10.0})
    assert fill["price"] == 0.31
    assert fill["qty"] == 1
    assert not (tmp_path / "live_signals.jsonl").exists()  # no signal in auto mode
    assert calls[0][0] == ("yes", "buy", "T1", 1, 0.31, "market")


def test_live_broker_auto_buy_error_emits_signal_with_error_and_reraises(tmp_path, monkeypatch):
    import bot_broker
    import live_broker
    cfg = {"live_requested": True, "broker_mode": "auto"}
    env = {"BOT_LIVE": "1"}
    def _boom(*a, **k):
        raise RuntimeError("400 insufficient balance")
    monkeypatch.setattr(live_broker, "place_order", _boom)
    b = bot_broker.LiveBroker(_all_sessions_trades(), cfg, env, bot_dir=tmp_path)
    with pytest.raises(RuntimeError, match="insufficient balance"):
        b.buy("YES", 1, {"yes_ask": 0.31, "no_ask": 0.71, "ts": 1000.0,
                         "ticker": "T1", "mins_left": 10.0})
    rows = [_json.loads(l) for l in (tmp_path / "live_signals.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and "insufficient balance" in rows[0]["error"]


def test_live_broker_still_locked_when_unlock_fails():
    with pytest.raises(RuntimeError, match="live trading locked"):
        bot_broker.LiveBroker(_session_trades("weekday_day", 3, 0.01),
                              {"live_requested": True}, {})


def test_live_broker_fill_with_order_id_never_places_a_second_real_order(tmp_path, monkeypatch):
    """The bug this guards: swing_bot._place_entry places the REAL resting
    limit order up front to get an order_id to poll. Once _process_pending
    confirms it executed, it calls fill(..., maker=True) to finalize the
    accounting -- fill() must NOT place a second real order for a position
    that's already open. Passing order_id is exactly how the caller tells
    fill() "this already happened, just do the bookkeeping." """
    import bot_broker
    import live_broker
    cfg = {"live_requested": True, "broker_mode": "auto"}
    env = {"BOT_LIVE": "1"}
    monkeypatch.setattr(live_broker, "place_order", lambda *a, **k:
                        (_ for _ in ()).throw(
                            AssertionError("fill() must not place a new order "
                                           "when order_id is already known")))
    b = bot_broker.LiveBroker(_all_sessions_trades(), cfg, env, bot_dir=tmp_path)
    fill = b.fill(0.49, 3, 1000.0, maker=True,
                 sig={"ticker": "T1", "side": "YES"}, order_id="already-placed-123")
    assert fill["price"] == 0.49
    assert fill["qty"] == 3
    assert fill["order_id"] == "already-placed-123"
    assert fill["fee_total"] > 0   # maker fee still computed


def test_live_broker_fill_without_order_id_places_a_new_order(tmp_path, monkeypatch):
    """The complement of the guard above: the chase-to-market path cancels
    the original order first, so fill() is called with order_id=None and
    MUST place a genuinely new order."""
    import bot_broker
    import live_broker
    cfg = {"live_requested": True, "broker_mode": "auto"}
    env = {"BOT_LIVE": "1"}
    calls = []
    monkeypatch.setattr(live_broker, "place_order", lambda *a, **k:
                        calls.append(a) or {"order_id": "new-order-456",
                                            "yes_price": 55, "no_price": 45})
    b = bot_broker.LiveBroker(_all_sessions_trades(), cfg, env, bot_dir=tmp_path)
    fill = b.fill(0.55, 2, 1000.0, maker=False,
                 sig={"ticker": "T1", "side": "YES"}, order_id=None)
    assert len(calls) == 1
    assert fill["order_id"] == "new-order-456"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_bot_broker.py -q -k "live_broker_manual or live_broker_auto or live_broker_still_locked or live_broker_fill" -v`
Expected: FAIL (TypeError on unexpected `bot_dir` kwarg, the old unconditional second raise means every unlocked construction still fails, and `fill()` doesn't accept `order_id` yet)

- [ ] **Step 3: Update the now-obsolete unconditional-raise test**

The existing `test_live_broker_locked_raises` (currently `tests/test_bot_broker.py:124-127`) tests that even an *unlocked* `LiveBroker` raises, because construction always raised unconditionally. That's no longer true — replace it with the more precise `test_live_broker_still_locked_when_unlock_fails` already added above (which asserts the gate itself still blocks a locked construction), and delete the old test:

```python
def test_live_broker_locked_raises():
    import pytest
    with pytest.raises(RuntimeError, match="live trading locked"):
        LiveBroker(_session_trades("weekday_day", 3, 0.01), {"live_requested": True}, {})
```

Delete this whole function — `test_live_broker_still_locked_when_unlock_fails` (added in Step 1) supersedes it with the same assertion made explicit about *why* (unlock failure, not an unconditional stub).

- [ ] **Step 4: Implement the new `LiveBroker`**

Replace the entire `LiveBroker` class in `bot_broker.py` (currently lines 97-111):

```python
class LiveBroker:
    """Real order placement, gated by live_unlock_ok. broker_mode controls
    what buy/sell/fill actually do once unlocked:
      "manual" (default): emit a live_signal event, no order API call.
      "auto": place a real order via live_broker.py.
    See the 2026-07-29 design spec for the full mode matrix."""
    mode = "live"

    def __init__(self, trades: list, cfg: dict, env: dict = None, bot_dir=None):
        ok, reason = live_unlock_ok(trades, cfg, env if env is not None else dict(os.environ))
        if not ok:
            raise RuntimeError(f"live trading locked: {reason}")
        self.cfg = cfg
        self.bot_dir = bot_dir
        self.broker_mode = cfg.get("broker_mode", "manual")

    def _pool_of(self, sig: dict) -> str:
        from bot_core import session_tag
        return session_tag(sig.get("ts"))

    def _signal_fill(self, side, qty, price, sig, tier="market") -> dict:
        emit_live_signal(self.bot_dir, sig.get("ticker"), side, qty, price,
                         tier, self._pool_of(sig))
        return {"price": price, "qty": qty, "fee_total": 0.0,
                "ts": sig.get("ts") or 0.0, "maker": False, "signal_only": True}

    def _auto_fill(self, side, action, qty, price, sig, order_type, tier) -> dict:
        import live_broker
        kalshi_side = "yes" if side == "YES" else "no"
        try:
            order = live_broker.place_order(kalshi_side, action, sig.get("ticker"),
                                            qty, price, order_type)
        except Exception as e:
            emit_live_signal(self.bot_dir, sig.get("ticker"), side, qty, price,
                             tier, self._pool_of(sig), error=str(e))
            raise
        fee_price_key = "yes_price" if kalshi_side == "yes" else "no_price"
        fill_price = (order.get(fee_price_key) or round(price * 100)) / 100.0
        maker = order_type == "limit"
        from backtest_gate import fee as taker_fee, maker_fee
        f = maker_fee if maker else taker_fee
        return {"price": fill_price, "qty": qty,
                "fee_total": round(f(fill_price) * qty, 4),
                "ts": sig.get("ts") or 0.0, "maker": maker,
                "order_id": order.get("order_id")}

    def buy(self, side: str, qty: int, sig: dict) -> dict:
        price = sig["yes_ask"] if side == "YES" else sig["no_ask"]
        if self.broker_mode == "manual":
            return self._signal_fill(side, qty, price, sig)
        return self._auto_fill(side, "buy", qty, price, sig, "market", "market")

    def sell(self, side: str, qty: int, sig: dict) -> dict:
        ask = sig["yes_ask"] if side == "YES" else sig["no_ask"]
        spread = max(0.0, sig.get("spread") or 0.0)
        price = max(0.01, round(ask - spread, 4))
        if self.broker_mode == "manual":
            return self._signal_fill(side, qty, price, sig)
        return self._auto_fill(side, "sell", qty, price, sig, "market", "market")

    def fill(self, price: float, qty: int, ts: float, maker: bool = False,
             sig: dict = None, order_id: str = None) -> dict:
        """Explicit-price fill -- the resting-limit-order path
        (swing_bot._process_pending calls this the same way it calls
        PaperBroker.fill). sig is required here (unlike PaperBroker) to
        resolve the ticker/pool for emit_live_signal / place_order.

        order_id matters only in auto mode: swing_bot._place_entry already
        places the REAL resting limit order up front (to get an order_id to
        poll) -- by the time _process_pending confirms it executed and
        calls fill(..., maker=True) to finalize the accounting, the order
        has ALREADY happened. Without this parameter, fill() would call
        _auto_fill -> place_order again and place a SECOND real order for
        the same intended position. When order_id is provided, skip
        placement entirely and just build the fill dict from the already-
        known execution price. order_id is None for a genuinely new
        placement (the chase-to-market path, after the original resting
        order was cancelled, and plain buy()/sell() calls)."""
        sig = sig or {}
        side = sig.get("side", "YES")
        tier = "patient" if maker else "market"
        if self.broker_mode == "manual":
            return self._signal_fill(side, qty, price, sig, tier=tier)
        if order_id is not None:
            from backtest_gate import fee as taker_fee, maker_fee
            f = maker_fee if maker else taker_fee
            return {"price": price, "qty": qty,
                    "fee_total": round(f(price) * qty, 4),
                    "ts": ts, "maker": maker, "order_id": order_id}
        order_type = "limit" if maker else "market"
        return self._auto_fill(side, "buy", qty, price, sig, order_type, tier)
```

Also add the import at the top of `bot_broker.py` (after the existing `import os`):

```python
import os

import account
from backtest_gate import fee, maker_fee
```

(unchanged — `account` and `backtest_gate` imports already exist; `emit_live_signal` was added in Task 2 in the same file, no new import needed for it.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_bot_broker.py -q -v`
Expected: all `test_bot_broker.py` tests pass, including the 4 new ones and the replacement for the deleted one.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add bot_broker.py tests/test_bot_broker.py
git commit -m "Implement LiveBroker buy/sell/fill: manual emits signal, auto places real orders"
```

---

### Task 5: Wire `Bot` to construct `LiveBroker` and poll pending auto orders

**Files:**
- Modify: `swing_bot.py:190-192` (`self.broker = PaperBroker()`)
- Modify: `swing_bot.py:252-275` (`_enter`, to use flat `live_qty` sizing instead of `size_for_budget` in live mode)
- Modify: `swing_bot.py:322-348` (`_process_pending`, to poll a resting auto order's real status instead of comparing to the live quote when `mode=="live" and broker_mode=="auto"`)
- Modify: `swing_bot.py:277-305` (`_place_entry`, so the `pending_entries` row records the real order's `order_id` when auto mode placed a resting limit order, and so it also uses flat `live_qty` sizing)
- Test: `tests/test_swing_bot.py`

**Interfaces:**
- Consumes: `LiveBroker` (Task 4), `live_broker.get_order`/`cancel_order` (Task 3), the `live_qty` config key (Task 1).
- Produces: `Bot.broker` is a `LiveBroker` instance when `cfg["mode"] == "live"` (still `PaperBroker()` otherwise, unchanged default). `Bot._entry_qty(budget, price)` — the shared sizing choke point both `_enter` and `_place_entry` call, so sizing can't drift between the two entry paths (mirrors why `_pool_and_budget` itself is already shared for the same reason). Consumed by Task 6 (stop checks gate entries before this broker is ever called) and Task 7 (error handling around these same call sites).

**Design note on sizing:** per the design spec's Sizing section, live mode (both manual and auto — a human placing a manually-flagged trade should see the same flat sizing the bot itself would use, not a %-of-pool number that was never validated against a small real account) must NEVER use `bot_core.trade_budget`'s %-of-pool formula for the actual quantity, only `live_qty` flat sizing. `budget` (from `_pool_and_budget`) is still computed and still gates *whether* an entry is affordable at all (via the existing `qty < 1` skip check) but the qty itself comes from `live_qty` once affordability is confirmed.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_swing_bot.py`:

```python
def test_bot_constructs_live_broker_when_mode_is_live_and_unlocked(tmp_path, monkeypatch):
    import json as _json
    import bot_broker
    # seed a fully-unlocked trade history (reuse test_bot_broker's helper shape
    # inline here since swing_bot tests don't import test_bot_broker)
    from bot_core import POOL_NAMES, session_tag
    ts_by_pool = {"weekday_day": 1784592000.0 + 14 * 3600,
                  "weekday_night": 1784592000.0,
                  "weekend_day": 1784419200.0 + 14 * 3600,
                  "weekend_night": 1784419200.0}
    trades = []
    for pool, ts in ts_by_pool.items():
        for _ in range(100):
            trades.append({"status": "closed", "net_pnl": 0.01, "entry_ts": ts,
                           "entry_sig": {"ts": ts}})
    (tmp_path / "bot_trades.jsonl").write_text(
        "\n".join(_json.dumps(t) for t in trades))
    (tmp_path / "config.json").write_text(_json.dumps(
        {"mode": "live", "live_requested": True, "broker_mode": "manual",
         "overnight_curfew": False, "weekend_curfew": False}))
    monkeypatch.setenv("BOT_LIVE", "1")
    bot = Bot(tmp_path, fetch_fn=lambda: None,
              offsets_file=tmp_path / "banner_offsets.json",
              loop_log=tmp_path / "loop_log.jsonl")
    assert bot.broker.mode == "live"
    assert bot.broker.broker_mode == "manual"


def test_bot_falls_back_to_paper_broker_when_mode_paper(tmp_path, monkeypatch):
    bot = _mkbot(tmp_path, [_sig()], monkeypatch)
    assert bot.broker.mode == "paper"


def test_bot_raises_loudly_when_mode_live_but_gate_not_unlocked(tmp_path, monkeypatch):
    import json as _json
    (tmp_path / "config.json").write_text(_json.dumps(
        {"mode": "live", "overnight_curfew": False, "weekend_curfew": False}))
    with pytest.raises(RuntimeError, match="live trading locked"):
        Bot(tmp_path, fetch_fn=lambda: None,
            offsets_file=tmp_path / "banner_offsets.json",
            loop_log=tmp_path / "loop_log.jsonl")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_swing_bot.py -q -k "constructs_live_broker or falls_back_to_paper or raises_loudly_when_mode_live" -v`
Expected: FAIL — `bot.broker.mode` is always `"paper"` today regardless of config, and no `RuntimeError` is raised (the stub is never even reached because `PaperBroker()` is hardcoded).

- [ ] **Step 3: Wire broker construction**

In `swing_bot.py`, replace line 192:

```python
        self.broker = PaperBroker()   # LiveBroker only via unlock bar (not v1)
```

with:

```python
        trades_for_gate = self._read_trades() if self.cfg.get("mode") == "live" else None
        if self.cfg.get("mode") == "live":
            self.broker = LiveBroker(trades_for_gate, self.cfg, bot_dir=self.dir)
        else:
            self.broker = PaperBroker()
```

Note: `self._read_trades()` is defined just below `__init__` in the same class (line 210 today) and is safe to call here since it only reads `TRADES_FILE`, which doesn't depend on anything else set up later in `__init__`. `trades` (the variable already computed on line 203 for `bucket_stats`/`_migrate_pools`) is read again separately here rather than reused, since it's cheap (a single small file read) and keeps this change a minimal, isolated diff next to the one line it's replacing rather than reordering surrounding code.

Update the import at the top of `swing_bot.py` (line 98-99):

```python
from bot_broker import (PaperBroker, LiveBroker, round_trip_pnl, fetch_bankroll,
                        FALLBACK_BANKROLL)
```

- [ ] **Step 4: Run the construction tests**

Run: `.venv/bin/python -m pytest tests/test_swing_bot.py -q -k "constructs_live_broker or falls_back_to_paper or raises_loudly_when_mode_live" -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Write the failing test for flat `live_qty` sizing**

Add to `tests/test_swing_bot.py`:

```python
def test_live_mode_entries_use_flat_live_qty_not_pool_budget_formula(tmp_path, monkeypatch):
    import json as _json
    ts_by_pool = {"weekday_day": 1784592000.0 + 14 * 3600,
                  "weekday_night": 1784592000.0,
                  "weekend_day": 1784419200.0 + 14 * 3600,
                  "weekend_night": 1784419200.0}
    trades = []
    for pool, ts in ts_by_pool.items():
        for _ in range(100):
            trades.append({"status": "closed", "net_pnl": 0.01, "entry_ts": ts,
                           "entry_sig": {"ts": ts}})
    (tmp_path / "bot_trades.jsonl").write_text(
        "\n".join(_json.dumps(t) for t in trades))
    (tmp_path / "config.json").write_text(_json.dumps(
        {"mode": "live", "live_requested": True, "broker_mode": "manual",
         "overnight_curfew": False, "weekend_curfew": False,
         "limit_entries": False,   # exercise _enter's sizing directly
         "live_qty": 1, "paper_bankroll": 500.0}))
    monkeypatch.setenv("BOT_LIVE", "1")
    bot = Bot(tmp_path, fetch_fn=lambda: None,
              offsets_file=tmp_path / "banner_offsets.json",
              loop_log=tmp_path / "loop_log.jsonl")
    sig = _sig(whale_trend=3.0, momentum=30.0, ts=ts_by_pool["weekday_night"],
              mins_left=10.0, yes_ask=0.50)
    # paper_bankroll=500 -> pool bankroll 125 -> the OLD %-of-pool formula
    # would size this at several contracts (as it does in every existing
    # paper test using the same bankroll); live mode must use live_qty=1
    # regardless of that budget.
    bot._enter("YES", sig)
    assert bot.state["open_plays"]["M1"]["qty"] == 1


def test_paper_mode_entries_still_use_pool_budget_formula(tmp_path, monkeypatch):
    # regression guard: this task's sizing change must be live-mode-only --
    # paper's existing %-of-pool sizing (already covered extensively by
    # other tests in this file) must not change.
    sigs = [_sig(), _sig(whale_trend=3.0, momentum=30.0, ts=1005.0)]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert bot.state["open_plays"]["M1"]["qty"] > 1   # unchanged paper sizing
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_swing_bot.py -q -k "flat_live_qty or still_use_pool_budget" -v`
Expected: the first FAILS (sizing still comes from `size_for_budget`/`trade_budget`, producing more than 1 contract at this bankroll); the second already PASSES today (it's a regression guard for existing behavior, added now so a future change can't silently break it without a red test).

- [ ] **Step 7: Add `_entry_qty` and wire it into `_enter` and `_place_entry`**

In `swing_bot.py`, add a new method right after `_pool_and_budget` (currently ending at line 250, right before `def _enter`):

```python
    def _entry_qty(self, budget: float, price: float) -> int:
        """Live mode (manual or auto) always sizes flat at cfg['live_qty']
        once `budget` has already confirmed the entry is affordable at all
        -- paper's %-of-pool trade_budget formula was proven this session
        to size unreasonably large (up to 33 contracts) when transplanted
        onto a small real account, so live entries never use it for the
        actual quantity."""
        if self.broker.mode == "live":
            return self.cfg.get("live_qty", 1)
        return size_for_budget(budget, price)
```

Then in `_enter` (currently lines 252-275), replace:
```python
        qty = size_for_budget(budget, price)
```
with:
```python
        qty = self._entry_qty(budget, price)
```

And in `_place_entry` (currently lines 277-305, already being modified by this task's Step 3 for `pend`/`order_id`), replace:
```python
        qty = size_for_budget(budget, limit_price)
```
with:
```python
        qty = self._entry_qty(budget, limit_price)
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_swing_bot.py -q -k "flat_live_qty or still_use_pool_budget" -v`
Expected: PASS (both)

- [ ] **Step 9: Write the failing test for auto-mode pending-order polling**

Add to `tests/test_swing_bot.py`:

```python
def test_process_pending_polls_real_order_status_in_auto_mode(tmp_path, monkeypatch):
    import json as _json
    import live_broker
    from bot_core import POOL_NAMES
    ts_by_pool = {"weekday_day": 1784592000.0 + 14 * 3600,
                  "weekday_night": 1784592000.0,
                  "weekend_day": 1784419200.0 + 14 * 3600,
                  "weekend_night": 1784419200.0}
    trades = []
    for pool, ts in ts_by_pool.items():
        for _ in range(100):
            trades.append({"status": "closed", "net_pnl": 0.01, "entry_ts": ts,
                           "entry_sig": {"ts": ts}})
    (tmp_path / "bot_trades.jsonl").write_text(
        "\n".join(_json.dumps(t) for t in trades))
    (tmp_path / "config.json").write_text(_json.dumps(
        {"mode": "live", "live_requested": True, "broker_mode": "auto",
         "overnight_curfew": False, "weekend_curfew": False,
         "limit_entries": True}))
    monkeypatch.setenv("BOT_LIVE", "1")
    monkeypatch.setattr(live_broker, "place_order", lambda *a, **k:
                        {"order_id": "ord-1", "status": "resting",
                         "yes_price": 49, "no_price": 51})
    monkeypatch.setattr(live_broker, "get_order", lambda order_id:
                        {"order_id": order_id, "status": "executed",
                         "yes_price": 49, "no_price": 51})
    bot = Bot(tmp_path, fetch_fn=lambda: None,
              offsets_file=tmp_path / "banner_offsets.json",
              loop_log=tmp_path / "loop_log.jsonl")
    sig = _sig(whale_trend=3.0, momentum=30.0, ts=ts_by_pool["weekday_night"],
              mins_left=10.0, yes_ask=0.50)
    bot._place_entry("YES", sig)
    assert "M1" in bot.state["pending_entries"]
    assert bot.state["pending_entries"]["M1"]["order_id"] == "ord-1"
    next_sig = _sig(whale_trend=3.0, momentum=30.0,
                    ts=ts_by_pool["weekday_night"] + 5, mins_left=9.9, yes_ask=0.50)
    bot._process_pending(next_sig)
    assert "M1" not in bot.state["pending_entries"]
    assert "M1" in bot.state["open_plays"]
    assert bot.state["open_plays"]["M1"]["entry"]["price"] == 0.49
```

- [ ] **Step 10: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_swing_bot.py -q -k polls_real_order_status -v`
Expected: FAIL — `_place_entry` doesn't call the broker at all today (it only records a `pending_entries` row and never places anything until `_fill_pending` later), so no `order_id` is ever attached, and `_process_pending`'s fill condition still compares to the live *quote* rather than polling a real order.

- [ ] **Step 11: Wire order placement into `_place_entry` and polling into `_process_pending`**

In `swing_bot.py`, modify `_place_entry` (already touched by Step 7's `_entry_qty` change) — after computing `limit_price` and `qty`, before writing the `pending_entries` row, place the real resting order when in live+auto mode. The full function, showing both this step's and Step 7's changes together:

```python
    def _place_entry(self, side, sig, ranges=None):
        """Route to a resting limit order (aggressive/patient, same tiers
        /trade's panel shows) when there's enough time to wait for one;
        otherwise fall back to _enter's immediate market fill. A limit
        order never fills the instant it's placed by construction (its
        price is strictly below the current ask) -- see _process_pending
        for the fill/chase/cancel handling on later ticks."""
        ask = sig["yes_ask"] if side == "YES" else sig["no_ask"]
        tier = entry_tier(sig.get("mins_left"), ask) if self.cfg.get("limit_entries", True) else None
        if tier is None:
            self._enter(side, sig, ranges)
            return
        tier_name, offset = tier
        pb = self._pool_and_budget(sig)
        if pb is None:
            return
        pool, ps, budget = pb
        limit_price = max(0.01, round(ask - offset, 4))
        qty = self._entry_qty(budget, limit_price)
        if qty < 1:
            self._event("skip", f"budget too small for 1 contract at {limit_price}",
                        sig["ticker"], sig)
            return
        pend = {"side": side, "qty": qty, "limit_price": limit_price,
                "tier": tier_name, "placed_ts": sig.get("ts") or 0.0,
                "ranges": ranges, "entry_sig": _snap(sig), "pool": pool}
        if self.broker.mode == "live" and self.broker.broker_mode == "auto":
            import live_broker
            order = live_broker.place_order(
                "yes" if side == "YES" else "no", "buy", sig["ticker"], qty,
                limit_price, "limit")
            pend["order_id"] = order["order_id"]
        self.state["pending_entries"][sig["ticker"]] = pend
        self._event("place", f"{side} x{qty} limit @ {limit_price:.3f} ({tier_name})",
                    sig["ticker"], sig)
```

Now modify `_process_pending` (currently lines 322-348) to poll the real order when `order_id` is present, instead of comparing to the live quote:

```python
    def _process_pending(self, sig):
        """Advance every resting entry order by one tick: fill if the
        market has traded down to the limit, chase to market once the
        timeout elapses, or cancel (no cost -- nothing was ever risked) if
        its market rolled away before either happened. Roll detection
        mirrors the exit loop: only a genuine ticker mismatch (or a
        non-ok row) counts as rolled. A same-ticker row with no usable
        quote (routine in replay) just waits for the next tick instead of
        being cancelled -- folding the quote check into the roll condition
        was a bug that nuked resting orders on their own market's quiet
        ticks. In live+auto mode (order_id present), fill/no-fill is
        decided by polling the REAL order's status, not by comparing the
        limit price to the tick's quote -- the quote can lag or jitter
        around the exact touch price in ways paper's simulation doesn't
        need to worry about, but a real resting order's own status is
        authoritative."""
        ticker = sig.get("ticker")
        timeout = self.cfg.get("limit_fill_timeout_secs", 30)
        for t in list(self.state["pending_entries"]):
            pend = self.state["pending_entries"][t]
            if sig.get("status") != "ok" or t != ticker:
                if pend.get("order_id"):
                    import live_broker
                    live_broker.cancel_order(pend["order_id"])
                del self.state["pending_entries"][t]
                self._event("cancel", "rolled before limit filled or chased", t, sig)
                continue
            if pend.get("order_id"):
                import live_broker
                status = live_broker.get_order(pend["order_id"])
                if status.get("status") == "executed":
                    self._fill_pending(t, pend, sig, maker=True)
                elif (sig.get("ts") or 0.0) - pend["placed_ts"] >= timeout:
                    live_broker.cancel_order(pend["order_id"])
                    self._fill_pending(t, pend, sig, maker=False, chase=True)
                continue
            if sig.get("yes_ask") is None or sig.get("no_ask") is None:
                continue  # same market, no usable quote this tick -- wait
            ask = sig["yes_ask"] if pend["side"] == "YES" else sig["no_ask"]
            if ask <= pend["limit_price"]:
                self._fill_pending(t, pend, sig, maker=True)
            elif (sig.get("ts") or 0.0) - pend["placed_ts"] >= timeout:
                self._fill_pending(t, pend, sig, maker=False, chase=True)
            # else: still waiting, leave it pending
```

`_fill_pending` (unchanged so far in this task) currently calls `self.broker.fill(price, pend["qty"], sig.get("ts") or 0.0, maker=maker)`. This needs two changes: pass `sig` through (so `LiveBroker.fill` can resolve ticker/pool/side), and pass `order_id` **only when finalizing the original resting order that auto mode already placed for real in `_place_entry`** (`chase=False`) — never on the chase path, since chasing means the original order was just cancelled and a genuinely new market order must be placed (see Task 4's `LiveBroker.fill` docstring on this exact distinction, and its two regression tests guarding both directions).

Find in `_fill_pending` (currently around line 307-311):
```python
    def _fill_pending(self, ticker, pend, sig, maker, chase=False):
        del self.state["pending_entries"][ticker]
        price = (sig["yes_ask"] if pend["side"] == "YES" else sig["no_ask"]) \
                if chase else pend["limit_price"]
        fill = self.broker.fill(price, pend["qty"], sig.get("ts") or 0.0, maker=maker)
```
Replace with:
```python
    def _fill_pending(self, ticker, pend, sig, maker, chase=False):
        del self.state["pending_entries"][ticker]
        price = (sig["yes_ask"] if pend["side"] == "YES" else sig["no_ask"]) \
                if chase else pend["limit_price"]
        order_id = None if chase else pend.get("order_id")
        fill = self.broker.fill(price, pend["qty"], sig.get("ts") or 0.0, maker=maker,
                                sig={**sig, "side": pend["side"]}, order_id=order_id)
```

`PaperBroker.fill(self, price, qty, ts, maker=False)` doesn't accept `sig`/`order_id` today. Add both, ignored/unused, purely so both brokers share one call signature at this call site:

In `bot_broker.py`, update `PaperBroker.fill`:
```python
    def fill(self, price: float, qty: int, ts: float, maker: bool = False,
             sig: dict = None, order_id: str = None) -> dict:
        """A fill at an explicit price -- buy()/sell() are always taker
        (market-style, immediate); a resting limit order that actually
        waited to be touched calls this directly with maker=True. See
        backtest_gate.maker_fee for the maker-rate caveat. `sig`/`order_id`
        are unused here (PaperBroker doesn't need them) -- accepted only
        so callers can pass one shared signature to either PaperBroker or
        LiveBroker (see LiveBroker.fill's docstring for what order_id
        actually controls there)."""
        f = maker_fee if maker else fee
        return {"price": price, "qty": qty,
                "fee_total": round(f(price) * qty, 4),
                "ts": ts, "maker": maker}
```

- [ ] **Step 12: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_swing_bot.py -q -k polls_real_order_status -v`
Expected: PASS

- [ ] **Step 13: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass — pay particular attention to every existing `PaperBroker.fill`-touching test (the whole resting-limit-order test block added earlier this session) since the signature changed; they should be unaffected since `sig` is optional and unused by `PaperBroker`. Also confirm the two sizing tests from Step 6/8 still pass alongside everything else.

- [ ] **Step 14: Commit**

```bash
git add swing_bot.py bot_broker.py tests/test_swing_bot.py
git commit -m "Wire Bot to construct LiveBroker; auto mode places/polls real orders; flat live_qty sizing"
```

---

### Task 6: Code-enforced live hard/daily-soft stop

**Files:**
- Modify: `swing_bot.py` (add `Bot._check_live_stop()`, call it from `tick()`)
- Test: `tests/test_swing_bot.py`

**Interfaces:**
- Consumes: `bot_broker._balance_dollars()` (existing), the `live_hard_stop_usd`/`live_daily_soft_stop_usd` config keys (Task 1).
- Produces: `Bot._check_live_stop()` — halts new **auto** entries for every pool (this stop is account-wide, not per-pool, since it reads one real balance) once real PnL-since-day-start or PnL-since-a-recorded-baseline crosses the configured floor. Sets `ps["halted"] = True` on every pool the same way `_check_max_loss` does, so `entry_blockers`'s existing `halted` check (already wired, no signature change) blocks new entries automatically.

**Design note:** "PnL since test start" needs a baseline balance. Reuse the pattern `web.py`'s `/api/live_test` panel already established tonight (`LIVE_TEST_START_BALANCE`, a fixed number recorded at test-start time) — but as a **state** field (`self.state["live_baseline_balance"]`), set once on the first live+auto tick and never overwritten, so it survives restarts via the existing `bot_state.json` persistence rather than being hardcoded in a second place. Daily soft stop resets at UTC day roll, reusing the existing `roll_day_if_needed` hook.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_swing_bot.py`:

```python
def _live_auto_bot(tmp_path, monkeypatch, balance_sequence):
    import json as _json
    import bot_broker
    ts_by_pool = {"weekday_day": 1784592000.0 + 14 * 3600,
                  "weekday_night": 1784592000.0,
                  "weekend_day": 1784419200.0 + 14 * 3600,
                  "weekend_night": 1784419200.0}
    trades = []
    for pool, ts in ts_by_pool.items():
        for _ in range(100):
            trades.append({"status": "closed", "net_pnl": 0.01, "entry_ts": ts,
                           "entry_sig": {"ts": ts}})
    (tmp_path / "bot_trades.jsonl").write_text(
        "\n".join(_json.dumps(t) for t in trades))
    (tmp_path / "config.json").write_text(_json.dumps(
        {"mode": "live", "live_requested": True, "broker_mode": "auto",
         "overnight_curfew": False, "weekend_curfew": False,
         "live_hard_stop_usd": -8.0, "live_daily_soft_stop_usd": -3.0}))
    monkeypatch.setenv("BOT_LIVE", "1")
    it = iter(balance_sequence)
    monkeypatch.setattr(bot_broker, "_balance_dollars", lambda: next(it))
    return Bot(tmp_path, fetch_fn=lambda: None,
              offsets_file=tmp_path / "banner_offsets.json",
              loop_log=tmp_path / "loop_log.jsonl")


def test_live_stop_baseline_set_on_first_check_then_not_overwritten(tmp_path, monkeypatch):
    bot = _live_auto_bot(tmp_path, monkeypatch, [20.02, 19.50, 19.00])
    bot._check_live_stop()
    assert bot.state["live_baseline_balance"] == 20.02
    bot._check_live_stop()
    assert bot.state["live_baseline_balance"] == 20.02   # not re-baselined


def test_live_stop_halts_all_pools_at_hard_stop(tmp_path, monkeypatch):
    bot = _live_auto_bot(tmp_path, monkeypatch, [20.02, 12.00])
    bot._check_live_stop()   # baseline = 20.02
    bot._check_live_stop()   # 12.00 - 20.02 = -8.02 <= -8.0 hard stop
    for p in bot.state["pools"].values():
        assert p["halted"] is True
    events = _rows(tmp_path, EVENTS_FILE)
    assert any(e["action"] == "halt" and "live_hard_stop" in e["reason"] for e in events)


def test_live_stop_no_halt_above_the_line(tmp_path, monkeypatch):
    bot = _live_auto_bot(tmp_path, monkeypatch, [20.02, 17.50])
    bot._check_live_stop()
    bot._check_live_stop()   # 17.50 - 20.02 = -2.52, above both stops
    for p in bot.state["pools"].values():
        assert p["halted"] is False


def test_live_stop_inactive_in_manual_mode(tmp_path, monkeypatch):
    import json as _json
    import bot_broker
    ts = 1784592000.0
    trades = [{"status": "closed", "net_pnl": 0.01, "entry_ts": ts,
              "entry_sig": {"ts": ts}} for _ in range(100)]
    (tmp_path / "bot_trades.jsonl").write_text(
        "\n".join(_json.dumps(t) for t in trades * 4))  # not actually unlocked, doesn't matter here
    (tmp_path / "config.json").write_text(_json.dumps(
        {"mode": "paper", "overnight_curfew": False, "weekend_curfew": False}))
    monkeypatch.setattr(bot_broker, "_balance_dollars",
                        lambda: (_ for _ in ()).throw(AssertionError("should not be called")))
    bot = Bot(tmp_path, fetch_fn=lambda: None,
              offsets_file=tmp_path / "banner_offsets.json",
              loop_log=tmp_path / "loop_log.jsonl")
    bot._check_live_stop()   # paper mode -- must be a no-op, must not call balance
    assert "live_baseline_balance" not in bot.state
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_swing_bot.py -q -k live_stop -v`
Expected: FAIL with `AttributeError: 'Bot' object has no attribute '_check_live_stop'`

- [ ] **Step 3: Implement `_check_live_stop`**

In `swing_bot.py`, add a new method right after `_check_max_loss` (currently ending at line 534, right before the `# ── main tick ──` comment on line 536):

```python
    def _check_live_stop(self):
        """Code-enforced hard/daily-soft dollar stop against the REAL
        account balance -- only meaningful in live+auto mode, since manual
        mode always has a human reading the dashboard before placing
        anything, and paper mode has no real balance to check. Halts every
        pool (not just one) since this reads one account-wide balance, not
        a per-pool P&L -- unlike _check_day_stop/_check_max_loss, which are
        genuinely per-pool because paper's bankroll is split 4 ways."""
        if not (self.broker.mode == "live" and self.broker.broker_mode == "auto"):
            return
        from bot_broker import _balance_dollars
        try:
            balance = _balance_dollars()
        except BaseException:
            return   # transient API failure -- try again next tick, don't halt on a blip
        if "live_baseline_balance" not in self.state:
            self.state["live_baseline_balance"] = balance
            return
        pnl = round(balance - self.state["live_baseline_balance"], 4)
        hard = self.cfg.get("live_hard_stop_usd", -8.0)
        daily = self.cfg.get("live_daily_soft_stop_usd", -3.0)
        already_halted = all(p.get("halted") for p in self.state["pools"].values())
        if pnl <= hard and not already_halted:
            for p in self.state["pools"].values():
                p["halted"] = True
            self._flatten("live_hard_stop")
            self._event("halt", f"live_hard_stop: pnl {pnl:+.2f} <= {hard:.2f} "
                        f"vs baseline {self.state['live_baseline_balance']:.2f}")
        elif pnl <= daily and not already_halted:
            for p in self.state["pools"].values():
                p["halted"] = True
            self._flatten("live_daily_soft_stop")
            self._event("halt", f"live_daily_soft_stop: pnl {pnl:+.2f} <= {daily:.2f} "
                        f"vs baseline {self.state['live_baseline_balance']:.2f}")
```

Wire it into `tick()` — in `swing_bot.py`, find the existing block (currently lines 546-548):
```python
        self._check_day_stop()
        self._check_profit_lock()
        self._check_max_loss()
```
Replace with:
```python
        self._check_day_stop()
        self._check_profit_lock()
        self._check_max_loss()
        self._check_live_stop()
```

Daily-reset behavior: `roll_day_if_needed` (existing, called at the top of `tick()`) already resets every pool's `halted` flag to `False` at UTC day roll (see `swing_bot.py:71-80`) — the daily soft stop's halt is lifted by that same existing mechanism the next day, with no separate reset logic needed. The `live_baseline_balance` itself is **not** reset daily (it's the test's fixed start, matching `web.py`'s `LIVE_TEST_START_BALANCE` being fixed for the whole test) — only the hard stop should ever be considered a real end to the test; the daily soft stop is a same-day pause.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_swing_bot.py -q -k live_stop -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add swing_bot.py tests/test_swing_bot.py
git commit -m "Add code-enforced live hard/daily-soft stop for auto mode"
```

---

### Task 7: Halt-on-order-error for auto mode

**Files:**
- Modify: `swing_bot.py` (`_process_pending`, `_fill_pending`, `_enter` — wrap live+auto broker calls)
- Test: `tests/test_swing_bot.py`

**Interfaces:**
- Consumes: `LiveBroker`'s error-raising behavior (Task 4 — `_auto_fill` already calls `emit_live_signal(..., error=...)` before re-raising, so the *logging* half of this is already done; this task adds the *halt* half).
- Produces: any `Exception` raised by `self.broker.buy`/`sell`/`fill` in live+auto mode is caught at the call site, and halts that specific entry's session pool (reusing `ps["halted"]`) without crashing the tick loop.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_swing_bot.py`:

```python
def test_auto_order_error_halts_only_that_pool_not_the_whole_bot(tmp_path, monkeypatch):
    import json as _json
    import live_broker
    ts_by_pool = {"weekday_day": 1784592000.0 + 14 * 3600,
                  "weekday_night": 1784592000.0,
                  "weekend_day": 1784419200.0 + 14 * 3600,
                  "weekend_night": 1784419200.0}
    trades = []
    for pool, ts in ts_by_pool.items():
        for _ in range(100):
            trades.append({"status": "closed", "net_pnl": 0.01, "entry_ts": ts,
                           "entry_sig": {"ts": ts}})
    (tmp_path / "bot_trades.jsonl").write_text(
        "\n".join(_json.dumps(t) for t in trades))
    (tmp_path / "config.json").write_text(_json.dumps(
        {"mode": "live", "live_requested": True, "broker_mode": "auto",
         "overnight_curfew": False, "weekend_curfew": False,
         "limit_entries": True}))
    monkeypatch.setenv("BOT_LIVE", "1")
    def _boom(*a, **k):
        raise RuntimeError("400 insufficient balance")
    monkeypatch.setattr(live_broker, "place_order", _boom)
    bot = Bot(tmp_path, fetch_fn=lambda: None,
              offsets_file=tmp_path / "banner_offsets.json",
              loop_log=tmp_path / "loop_log.jsonl")
    sig = _sig(whale_trend=3.0, momentum=30.0, ts=ts_by_pool["weekday_night"],
              mins_left=10.0, yes_ask=0.50)
    bot._place_entry("YES", sig)   # must not raise out of the caller
    assert bot.state["pools"]["weekday_night"]["halted"] is True
    assert bot.state["pools"]["weekday_day"]["halted"] is False   # other pools unaffected
    assert "M1" not in bot.state["pending_entries"]
    events = _rows(tmp_path, EVENTS_FILE)
    assert any(e["action"] == "halt" and "order_error" in e["reason"] for e in events)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_swing_bot.py -q -k auto_order_error_halts -v`
Expected: FAIL — today `live_broker.place_order`'s exception propagates straight out of `_place_entry`, crashing the caller (the test would see the raw `RuntimeError` instead of a clean return).

- [ ] **Step 3: Wrap the auto-mode order call in `_place_entry`**

In `swing_bot.py`, in `_place_entry` (as modified by Task 5), wrap the order-placement block in a try/except:

```python
        pend = {"side": side, "qty": qty, "limit_price": limit_price,
                "tier": tier_name, "placed_ts": sig.get("ts") or 0.0,
                "ranges": ranges, "entry_sig": _snap(sig), "pool": pool}
        if self.broker.mode == "live" and self.broker.broker_mode == "auto":
            import live_broker
            try:
                order = live_broker.place_order(
                    "yes" if side == "YES" else "no", "buy", sig["ticker"], qty,
                    limit_price, "limit")
            except Exception as e:
                ps["halted"] = True
                self._event("halt", f"[{pool}] order_error: {e} -- auto entries "
                            f"blocked until manually resumed", sig["ticker"], sig)
                return
            pend["order_id"] = order["order_id"]
        self.state["pending_entries"][sig["ticker"]] = pend
        self._event("place", f"{side} x{qty} limit @ {limit_price:.3f} ({tier_name})",
                    sig["ticker"], sig)
```

(`emit_live_signal(..., error=...)` already fires inside `LiveBroker._auto_fill`/`place_order`'s caller from Task 4 — this task's job is only to catch the exception here so it doesn't propagate and crash the tick loop, and to set the pool's `halted` flag so `entry_blockers` stops offering new entries for that pool.)

Apply the same pattern to `_process_pending`'s two `live_broker.get_order`/`cancel_order` call sites and to `_fill_pending`'s `self.broker.fill(...)` call (all three added/touched in Task 5) — wrap each in the same try/except-then-halt-and-return shape, using the pool from `pend["pool"]` (available in `_process_pending`/`_fill_pending`, unlike `_place_entry` which has `pool` directly in scope already):

```python
            if pend.get("order_id"):
                import live_broker
                try:
                    status = live_broker.get_order(pend["order_id"])
                except Exception as e:
                    ps = self.state["pools"][pend["pool"]]
                    ps["halted"] = True
                    del self.state["pending_entries"][t]
                    self._event("halt", f"[{pend['pool']}] order_error: {e} -- "
                                f"auto entries blocked until manually resumed", t, sig)
                    continue
                if status.get("status") == "executed":
                    self._fill_pending(t, pend, sig, maker=True)
                elif (sig.get("ts") or 0.0) - pend["placed_ts"] >= timeout:
                    try:
                        live_broker.cancel_order(pend["order_id"])
                    except Exception as e:
                        ps = self.state["pools"][pend["pool"]]
                        ps["halted"] = True
                        del self.state["pending_entries"][t]
                        self._event("halt", f"[{pend['pool']}] order_error: {e} -- "
                                    f"auto entries blocked until manually resumed", t, sig)
                        continue
                    self._fill_pending(t, pend, sig, maker=False, chase=True)
                continue
```

This replaces the equivalent unguarded block Task 5 introduced in `_process_pending`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_swing_bot.py -q -k auto_order_error_halts -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add swing_bot.py tests/test_swing_bot.py
git commit -m "Halt only the affected pool on an auto-mode order error, don't crash the tick"
```

---

### Task 8: Dashboard display for `live_signals.jsonl`

**Files:**
- Modify: `web.py` (add `/api/live_signals` endpoint, right after `api_live_test` at web.py:1544-1581)
- Modify: `bot_page.py` (extend the `liveTestPanel` built in the earlier live-test-tracking work to also show manual-mode signals)
- Test: manual browser verification (per this session's established practice for any dashboard change) — no new Python test needed since this is a read-only display of a file Task 2 already covers with direct tests.

**Interfaces:**
- Consumes: `data/bot/live_signals.jsonl` (Task 2's `emit_live_signal` output).
- Produces: `GET /api/live_signals` — returns the most recent N rows.

- [ ] **Step 1: Add the endpoint**

In `web.py`, add right after `api_live_test` (after the closing `})` on line 1581, before `_TRADE_HTML = r"""`):

```python
@app.get("/api/live_signals")
async def api_live_signals() -> JSONResponse:
    path = _BOT_DIR / "live_signals.jsonl"
    if not path.exists():
        return JSONResponse({"signals": []})
    try:
        lines = path.read_text().splitlines()[-50:]
        signals = [json.loads(l) for l in lines if l.strip()]
    except Exception:
        signals = []
    return JSONResponse({"signals": signals})
```

Confirm `_BOT_DIR` is already defined at module scope in `web.py` (it's used by `bot_status_payload`/`bot_control_write` already — `grep -n "_BOT_DIR\s*=" web.py` to find its exact definition line and reuse it directly, no new constant needed).

- [ ] **Step 2: Verify the endpoint manually**

```bash
cd /home/kenny/bots/kalshi-scanner
.venv/bin/python -c "
import json
row = {'ts': 1234.0, 'ticker': 'T1', 'side': 'YES', 'qty': 1, 'price': 0.5,
       'tier': 'patient', 'pool': 'weekday_night', 'error': None}
import pathlib
p = pathlib.Path('/tmp/live_signals_smoke_test')
p.mkdir(exist_ok=True)
(p / 'live_signals.jsonl').write_text(json.dumps(row) + '\n')
"
```

Then restart the web process (following this session's established restart pattern — `kill` the current PID found via `ss -ltnp | grep 9050`, relaunch `main.py --web`) and check:

```bash
curl -s http://127.0.0.1:9050/api/live_signals | python3 -m json.tool
```

Expected: `{"signals": []}` initially (the real `data/bot/live_signals.jsonl` doesn't exist yet — Task 2's code creates it lazily on first real manual-mode entry, and this deployment hasn't turned on `mode: "live"` yet per this plan's Global Constraints).

- [ ] **Step 3: Extend the dashboard panel**

In `bot_page.py`, find the `pollLiveTest` function (added earlier this session) and its polling `setInterval`. Add a sibling `pollLiveSignals` function and a small table beneath the existing `liveTestPanel`'s fills table, following the exact same `fj`/`$`/`esc` helper pattern already used throughout that file:

```javascript
async function pollLiveSignals() {
  const d = await fj('/api/live_signals', null);
  const rows = (d && d.signals) || [];
  const el = $('liveSignalsTable');
  if (!el) return;
  el.tBodies[0].innerHTML = rows.slice().reverse().map(r => `<tr>
    <td>${esc(r.ticker)}</td>
    <td><span class="side-chip ${esc((r.side||'').toLowerCase())}">${esc(r.side)}</span></td>
    <td>${r.qty}</td>
    <td>${(r.price * 100).toFixed(1)}¢</td>
    <td>${esc(r.tier)}</td>
    <td>${r.error ? `<span class="neg">${esc(r.error)}</span>` : ''}</td></tr>`).join('');
}
setInterval(pollLiveSignals, 10000);
pollLiveSignals();
```

Add the corresponding `<table id="liveSignalsTable">` markup near the existing `liveTestPanel` HTML, matching the same structure as `ltFillsTable` (`<thead><tr><th>ticker</th><th>side</th><th>qty</th><th>price</th><th>tier</th><th>error</th></tr></thead><tbody></tbody>`), under a small `<h3>Manual/auto live signals</h3>` heading.

- [ ] **Step 4: Verify live in the browser**

Following this session's established practice for every dashboard change: navigate to `/bot`, take a screenshot, check the browser console for JS errors (a fresh page load, not a cached one — reload after arming console tracking), and confirm no errors before considering this task done. This panel will show empty (no signals yet, since `mode` stays `"paper"`) — the goal of this verification step is confirming the JS parses and polls cleanly, not that it displays real data yet.

- [ ] **Step 5: Commit**

```bash
git add web.py bot_page.py
git commit -m "Add /api/live_signals endpoint and dashboard table for manual-mode signals"
```

---

## Final integration check (not a task — run after Task 8)

- [ ] Run the full suite one more time: `.venv/bin/python -m pytest tests/ -q` — expect all tests passing (155 baseline + roughly 20 new across Tasks 1-7).
- [ ] Confirm `data/bot/config.json`'s `mode` is still `"paper"` (`cat data/bot/config.json | grep mode`) — this plan must not have changed the live deployment's actual running mode.
- [ ] Confirm the three standing processes (`swing_bot.py`, `trade_grader.py`, `bot_tuner.py`) and the web dashboard are still healthy after any restart performed during Task 8's manual verification (same health-check pattern used throughout this session: `pgrep -af`, `grep -ci error` on each daemon's log, `curl` the dashboard).
- [ ] The scratchpad `live_test_watch.py` script and its Monitor task stay running as-is — this plan doesn't retire them (that's an operational decision for whoever turns on `mode: "live", broker_mode: "manual"` later, not a step in this implementation plan).
