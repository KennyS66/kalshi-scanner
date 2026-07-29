# Per-Session Live-Trading Toggle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let each of the 4 trading sessions (weekday_day/weekday_night/weekend_day/weekend_night) unlock live trading independently once it clears its own 100-trade/positive-net-avg bar, controlled by a 3-state dashboard button per session instead of the current all-4-sessions-required gate and hand-edited config.

**Architecture:** Split `bot_broker.live_unlock_ok` into a coarse, trades-free construction-time check (`live_capability_ok`) and a per-entry, trades-free check (`live_unlock_ok(cfg, env, session)`) — both driven purely by a new `live_sessions_requested` config list, never re-deriving trade history after the moment a session is enabled. The 100-trade/net-avg gate check happens exactly once, server-side, in a new `POST /api/bot/live_session` endpoint at enable-time.

**Tech Stack:** Python 3.11, pytest, FastAPI (existing), vanilla JS (existing dashboard, no new frontend dependencies).

## Global Constraints

- `live_unlock_ok` and `live_capability_ok` never take a `trades` parameter. The 100-trade/positive-net-avg check (`session_gate_stats`) happens in exactly one place: the `POST /api/bot/live_session` "enable" handler.
- A session that regresses after being toggled live (net avg drops below zero from a losing stretch) **stays live** until manually disabled or until it hits its own hard/daily-soft stop — never silently re-locks itself. This is the confirmed, deliberate behavior from planning; a test must guard it explicitly.
- `broker_mode` (manual/auto) stays one global config key — no per-session mode in this pass.
- `BOT_LIVE=1` (environment variable) is completely unchanged — still required on top of everything else.
- Enabling a session server-side always re-verifies that session's own gate from live trade history — never trust the dashboard button's client-side state alone. Disabling never needs a gate check.
- Toggling a session live requires a confirmation step in the dashboard UI before the request is sent. Toggling off does not.

---

### Task 1: Split `live_unlock_ok` into `live_capability_ok` + per-session `live_unlock_ok`

**Files:**
- Modify: `bot_broker.py:94-138` (`live_unlock_ok` function and `LiveBroker.__init__`)
- Test: `tests/test_bot_broker.py`

**Interfaces:**
- Produces: `live_capability_ok(cfg: dict, env: dict) -> (bool, str)` — new function. `live_unlock_ok(cfg: dict, env: dict, session: str) -> (bool, str)` — same name, new signature (no `trades`, `session` now required). `LiveBroker.__init__(self, cfg: dict, env: dict = None, bot_dir=None)` — drops the `trades` parameter entirely. Consumed by Task 2 (swing_bot.py wiring) and Task 4 (the enable endpoint, which computes `session_gate_stats` itself rather than calling either of these).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_bot_broker.py` (the existing `_all_sessions_trades`/`_session_trades` helpers near the bottom of the file are for the OLD `trades`-based tests and are no longer relevant to these new functions — don't reuse them here, these tests only need `cfg`/`env` dicts):

```python
def test_live_capability_ok_requires_nonempty_sessions_and_bot_live():
    from bot_broker import live_capability_ok
    ok, reason = live_capability_ok({"live_sessions_requested": ["weekday_night"]}, {"BOT_LIVE": "1"})
    assert ok
    assert not live_capability_ok({"live_sessions_requested": []}, {"BOT_LIVE": "1"})[0]
    assert not live_capability_ok({}, {"BOT_LIVE": "1"})[0]  # key absent entirely
    assert not live_capability_ok({"live_sessions_requested": ["weekday_night"]}, {})[0]
    assert not live_capability_ok({"live_sessions_requested": ["weekday_night"]}, {"BOT_LIVE": "0"})[0]


def test_live_unlock_ok_checks_only_the_named_session():
    from bot_broker import live_unlock_ok
    cfg = {"live_sessions_requested": ["weekday_night"]}
    env = {"BOT_LIVE": "1"}
    assert live_unlock_ok(cfg, env, "weekday_night")[0]
    ok, reason = live_unlock_ok(cfg, env, "weekend_day")
    assert not ok
    assert "weekend_day" in reason
    # other sessions' presence/absence never affects this session's check
    cfg2 = {"live_sessions_requested": ["weekday_night", "weekend_day", "weekend_night"]}
    assert not live_unlock_ok(cfg2, env, "weekday_day")[0]   # still absent, still locked
    assert live_unlock_ok(cfg2, env, "weekend_night")[0]      # present, still unlocked


def test_live_unlock_ok_requires_bot_live_even_if_session_requested():
    from bot_broker import live_unlock_ok
    cfg = {"live_sessions_requested": ["weekday_night"]}
    assert not live_unlock_ok(cfg, {}, "weekday_night")[0]
    assert not live_unlock_ok(cfg, {"BOT_LIVE": "0"}, "weekday_night")[0]


def test_live_unlock_ok_does_not_recheck_trade_history():
    """The no-auto-disable-on-regression guarantee: once a session is in
    live_sessions_requested, it stays unlocked regardless of what its
    trade history would show if recomputed -- because these functions
    never look at trade history at all. This test's real assertion is
    the function signature itself: live_unlock_ok takes no trades
    argument, so there is nothing for a regression to be recomputed
    FROM. Calling it with only cfg/env (no trades anywhere in scope)
    and getting an unlock proves the guarantee structurally, not just
    by omission."""
    from bot_broker import live_unlock_ok
    cfg = {"live_sessions_requested": ["weekday_night"]}
    env = {"BOT_LIVE": "1"}
    # no trades variable exists anywhere in this test -- if live_unlock_ok
    # required one, this test would fail to even call it correctly.
    ok, reason = live_unlock_ok(cfg, env, "weekday_night")
    assert ok and reason == "unlocked"


def test_live_broker_constructs_with_no_trades_argument(tmp_path):
    from bot_broker import LiveBroker
    cfg = {"live_sessions_requested": ["weekday_night"], "broker_mode": "manual"}
    env = {"BOT_LIVE": "1"}
    b = LiveBroker(cfg, env, bot_dir=tmp_path)
    assert b.mode == "live"
    assert b.broker_mode == "manual"


def test_live_broker_construction_fails_when_no_session_ever_requested(tmp_path):
    from bot_broker import LiveBroker
    import pytest
    with pytest.raises(RuntimeError, match="live trading locked"):
        LiveBroker({"live_sessions_requested": []}, {"BOT_LIVE": "1"}, bot_dir=tmp_path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_bot_broker.py -q -k "live_capability_ok or live_unlock_ok_checks or live_unlock_ok_requires or live_unlock_ok_does_not or live_broker_constructs or live_broker_construction_fails" -v`
Expected: FAIL — `live_capability_ok` doesn't exist yet, and `live_unlock_ok`/`LiveBroker.__init__` still require a `trades` positional argument.

- [ ] **Step 3: Implement the split**

In `bot_broker.py`, replace the existing `live_unlock_ok` function (currently lines 94-121) with:

```python
def live_capability_ok(cfg: dict, env: dict):
    """Coarse, cheap, construction-time check: is live trading available
    AT ALL right now, for at least one session? Never looks at trade
    history -- that only matters once, at the moment a session is
    toggled live via POST /api/bot/live_session (see web.py). Returns
    (ok, reason)."""
    if not cfg.get("live_sessions_requested"):
        return False, "no session has been toggled live"
    if env.get("BOT_LIVE") != "1":
        return False, "BOT_LIVE=1 not set in environment"
    return True, "unlocked"


def live_unlock_ok(cfg: dict, env: dict, session: str):
    """Per-entry check: is THIS specific session unlocked for live
    trading? Per Kenny 2026-07-29: once a session is toggled live it
    stays live regardless of later performance (no auto-disable on
    regression) -- so this never re-derives session_gate_stats from
    trade history, it only checks live_sessions_requested + BOT_LIVE.
    The 100-trade/positive-net-avg bar is checked exactly once, at
    enable-time, server-side in web.py's live_session endpoint. Returns
    (ok, reason)."""
    if session not in (cfg.get("live_sessions_requested") or []):
        return False, f"{session} not toggled live"
    if env.get("BOT_LIVE") != "1":
        return False, "BOT_LIVE=1 not set in environment"
    return True, "unlocked"
```

Then update `LiveBroker.__init__` (currently lines 132-138):

```python
    def __init__(self, cfg: dict, env: dict = None, bot_dir=None):
        ok, reason = live_capability_ok(cfg, env if env is not None else dict(os.environ))
        if not ok:
            raise RuntimeError(f"live trading locked: {reason}")
        self.cfg = cfg
        self.env = env if env is not None else dict(os.environ)
        self.bot_dir = bot_dir
        self.broker_mode = cfg.get("broker_mode", "manual")
```

(Storing `self.env` is new — Task 2 needs it for the per-call `live_unlock_ok(self.cfg, self.env, session)` checks inside `buy`/`sell`/`fill`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_bot_broker.py -q -k "live_capability_ok or live_unlock_ok_checks or live_unlock_ok_requires or live_unlock_ok_does_not or live_broker_constructs or live_broker_construction_fails" -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Update the now-broken existing tests**

The existing tests `test_live_unlock_requires_all_four_conditions`,
`test_live_unlock_requires_100_and_positive_avg_in_every_session`, and
`test_live_broker_still_locked_when_unlock_fails` (search
`tests/test_bot_broker.py` for these names) call the OLD `live_unlock_ok(trades, cfg, env)` /
`LiveBroker(trades, cfg, env)` signatures and assert the OLD all-4-sessions
behavior — this is expected, since this task deliberately removes that
behavior. Delete these three tests entirely (the new per-session gate is
now enforced server-side in Task 4's endpoint tests, not here) along with
the `_all_sessions_trades`/`_session_trades`/`_SESSION_TS` helpers that
existed only to support them, IF nothing else in the file still uses those
helpers (grep to confirm: `grep -n "_all_sessions_trades\|_session_trades\|_SESSION_TS" tests/test_bot_broker.py`
— if any other test still references them, keep the helpers, only delete
the three obsolete test functions).

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass except `tests/test_swing_bot.py` and any other file
constructing `LiveBroker` with the old `(trades, cfg, ...)` signature —
those are fixed in Task 2. Note which tests fail here so Task 2 addresses
them; do not attempt to fix swing_bot.py in this task.

- [ ] **Step 7: Commit**

```bash
git add bot_broker.py tests/test_bot_broker.py
git commit -m "Split live_unlock_ok into construction-time + per-session checks, drop trades param"
```

---

### Task 2: Wire per-entry session checks into `LiveBroker.buy`/`sell`/`fill` and fix `Bot.__init__`

**Files:**
- Modify: `bot_broker.py` (`LiveBroker.buy`/`sell`/`fill`, `_signal_fill`, `_auto_fill`)
- Modify: `swing_bot.py:192-196` (`Bot.__init__`'s broker construction)
- Test: `tests/test_bot_broker.py`, `tests/test_swing_bot.py`

**Interfaces:**
- Consumes: `live_unlock_ok(cfg, env, session)` (Task 1).
- Produces: every `LiveBroker.buy`/`sell`/`fill` call now checks the specific entry's session before doing anything else, raising `RuntimeError("live trading locked: ...")` for an unlocked session on an otherwise-successfully-constructed broker.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_bot_broker.py`:

```python
def test_live_broker_buy_checks_the_entrys_own_session(tmp_path):
    from bot_broker import LiveBroker
    import pytest
    cfg = {"live_sessions_requested": ["weekday_night"], "broker_mode": "manual"}
    env = {"BOT_LIVE": "1"}
    b = LiveBroker(cfg, env, bot_dir=tmp_path)
    # weekday_night entry (ts falls in weekday_night per bot_core.session_tag) succeeds
    fill = b.buy("YES", 1, {"yes_ask": 0.31, "no_ask": 0.71, "ts": 1784592000.0,  # Tue 00:00Z
                            "ticker": "T1", "mins_left": 10.0})
    assert fill["price"] == 0.31
    # a weekday_day entry (same day, hour 14 -> day session) on the SAME already-constructed
    # broker instance is rejected -- proving the check is per-call, not per-broker
    with pytest.raises(RuntimeError, match="live trading locked"):
        b.buy("YES", 1, {"yes_ask": 0.31, "no_ask": 0.71, "ts": 1784592000.0 + 14 * 3600,
                         "ticker": "T2", "mins_left": 10.0})


def test_live_broker_sell_and_fill_also_check_session(tmp_path):
    from bot_broker import LiveBroker
    import pytest
    cfg = {"live_sessions_requested": ["weekday_night"], "broker_mode": "manual"}
    env = {"BOT_LIVE": "1"}
    b = LiveBroker(cfg, env, bot_dir=tmp_path)
    weekend_ts = 1784419200.0  # Sun 00:00Z -> weekend_night, not requested
    with pytest.raises(RuntimeError, match="live trading locked"):
        b.sell("YES", 1, {"yes_ask": 0.50, "no_ask": 0.50, "spread": 0.02,
                          "ts": weekend_ts, "ticker": "T3"})
    with pytest.raises(RuntimeError, match="live trading locked"):
        b.fill(0.50, 1, weekend_ts, maker=True, sig={"ticker": "T3", "side": "YES", "ts": weekend_ts})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_bot_broker.py -q -k "checks_the_entrys_own_session or sell_and_fill_also_check" -v`
Expected: FAIL — `buy`/`sell`/`fill` don't check session at all yet, so both calls in each test succeed instead of raising.

- [ ] **Step 3: Implement the per-call session check**

In `bot_broker.py`, add a small helper right after `LiveBroker.__init__` (before `_pool_of`):

```python
    def _check_session(self, sig: dict) -> str:
        """Resolve this entry's session and raise if it isn't unlocked.
        Returns the session name (callers that already need it, like
        _signal_fill/_auto_fill via _pool_of, can reuse the return value
        instead of re-deriving it)."""
        session = self._pool_of(sig)
        ok, reason = live_unlock_ok(self.cfg, self.env, session)
        if not ok:
            raise RuntimeError(f"live trading locked: {reason}")
        return session
```

Then add a call to `self._check_session(sig)` as the FIRST line of `buy`, `sell`, and `fill` (before any other logic in each):

```python
    def buy(self, side: str, qty: int, sig: dict) -> dict:
        self._check_session(sig)
        price = sig["yes_ask"] if side == "YES" else sig["no_ask"]
        if self.broker_mode == "manual":
            return self._signal_fill(side, qty, price, sig)
        return self._auto_fill(side, "buy", qty, price, sig, "market", "market")

    def sell(self, side: str, qty: int, sig: dict) -> dict:
        self._check_session(sig)
        ask = sig["yes_ask"] if side == "YES" else sig["no_ask"]
        spread = max(0.0, sig.get("spread") or 0.0)
        price = max(0.01, round(ask - spread, 4))
        if self.broker_mode == "manual":
            return self._signal_fill(side, qty, price, sig)
        return self._auto_fill(side, "sell", qty, price, sig, "market", "market")

    def fill(self, price: float, qty: int, ts: float, maker: bool = False,
             sig: dict = None, order_id: str = None) -> dict:
        sig = sig or {}
        self._check_session(sig)
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

(Only the added `self._check_session(sig)` lines and `fill`'s `sig = sig or {}` moving one line earlier are new — the rest of each method's body is unchanged from before this task; `fill`'s docstring from before this task stays as-is, just shown abbreviated here for the diff.)

- [ ] **Step 4: Run the new tests**

Run: `.venv/bin/python -m pytest tests/test_bot_broker.py -q -k "checks_the_entrys_own_session or sell_and_fill_also_check" -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Fix `Bot.__init__`'s now-broken construction call**

In `swing_bot.py`, replace (currently lines 192-196):
```python
        trades_for_gate = self._read_trades() if self.cfg.get("mode") == "live" else None
        if self.cfg.get("mode") == "live":
            self.broker = LiveBroker(trades_for_gate, self.cfg, bot_dir=self.dir)
        else:
            self.broker = PaperBroker()
```
with:
```python
        if self.cfg.get("mode") == "live":
            self.broker = LiveBroker(self.cfg, bot_dir=self.dir)
        else:
            self.broker = PaperBroker()
```
(`trades_for_gate` is dead code once `LiveBroker` no longer needs trades at
construction — deleted, not just unused.)

- [ ] **Step 6: Fix the swing_bot.py tests broken by this signature change**

Run: `.venv/bin/python -m pytest tests/test_swing_bot.py -q -k "live" -v` and read the failures. Every `Bot(...)`-in-live-mode test that seeds `bot_trades.jsonl` with 100-trades-per-pool boilerplate specifically to satisfy the OLD all-4-sessions `live_unlock_ok` gate (search for the pattern `ts_by_pool = {"weekday_day": ...` across the file — this exact block appears in several tests added earlier tonight) no longer needs that trade-seeding at all. Replace each occurrence's config with `live_sessions_requested` instead:

Old pattern (repeated in several tests, e.g. `test_bot_constructs_live_broker_when_mode_is_live_and_unlocked`):
```python
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
        {"mode": "live", "live_requested": True, "broker_mode": "manual", ...}))
```
New pattern:
```python
    (tmp_path / "config.json").write_text(_json.dumps(
        {"mode": "live", "live_sessions_requested": ["weekday_night"],
         "broker_mode": "manual", ...}))
```
(drop the `bot_trades.jsonl` seeding and the `ts_by_pool`/`trades` setup
entirely — `live_capability_ok`/`live_unlock_ok` never read that file
anymore; keep any OTHER config keys each test already had, just replace
`"live_requested": True` with `"live_sessions_requested": [<whichever
session that specific test's sig timestamps land in>]`). Apply this same
replacement to every test in `tests/test_swing_bot.py` that shows this
pattern — there are several (the live+auto construction test, the sizing
test, the polling test, the live-stop tests, the halt-on-error tests, the
7b backoff tests). For each one, keep the test's OWN sig timestamps
unchanged and only replace how "this session is live-eligible" gets
expressed in the seeded config.

For tests that deliberately test the LOCKED case (e.g.
`test_bot_raises_loudly_when_mode_live_but_gate_not_unlocked`), change the
config to have an EMPTY `live_sessions_requested: []` (or omit the key
entirely) instead of omitting `live_requested`/`BOT_LIVE` — same intent
(nothing unlocked), new mechanism.

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass. This is the task where every pre-existing live-mode
test across the whole 9-task feature gets touched — go carefully, and if
any single test's intent becomes unclear while converting it, re-read
what that specific test was originally proving (via its docstring/name)
before changing its config, rather than mechanically search-and-replacing.

- [ ] **Step 8: Commit**

```bash
git add bot_broker.py swing_bot.py tests/test_bot_broker.py tests/test_swing_bot.py
git commit -m "Wire per-entry session checks into LiveBroker; migrate existing live-mode tests off the old all-4-sessions gate"
```

---

### Task 3: `live_sessions_requested` config key + `_gate_split` cleanup

**Files:**
- Modify: `bot_core.py` (`DEFAULT_CONFIG`)
- Modify: `web.py:1359-1386` (`bot_status_payload`'s `unlock` field)
- Test: `tests/test_bot_core.py`, `tests/test_bot_web.py`

**Interfaces:**
- Produces: `DEFAULT_CONFIG["live_sessions_requested"] = []` (replaces `live_requested`). `bot_status_payload`'s `"unlock"` field now reflects `live_capability_ok`, not the old `live_unlock_ok(trades, cfg, env)`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_bot_core.py`:

```python
def test_default_config_has_live_sessions_requested_not_live_requested():
    assert DEFAULT_CONFIG["live_sessions_requested"] == []
    assert "live_requested" not in DEFAULT_CONFIG
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_bot_core.py -q -k live_sessions_requested_not -v`
Expected: FAIL — `live_requested` (the old bool) is still present, `live_sessions_requested` isn't.

- [ ] **Step 3: Update `DEFAULT_CONFIG`**

In `bot_core.py`, find `"live_requested": False,   # GUI toggle target; live also needs BOT_LIVE=1 + EV bar`
in `DEFAULT_CONFIG` and replace it with:
```python
    "live_sessions_requested": [],  # session names (subset of POOL_NAMES)
                               # toggled live via the /bot dashboard. A
                               # session's presence here + BOT_LIVE=1 in
                               # the environment together unlock live
                               # trading for that session -- checked once
                               # at toggle-time (POST /api/bot/live_session
                               # in web.py), never re-derived from trade
                               # history afterward (Kenny 2026-07-29: no
                               # auto-disable if a live session's
                               # performance later regresses).
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_bot_core.py -q -k live_sessions_requested_not -v`
Expected: PASS

- [ ] **Step 5: Write the failing test for `bot_status_payload`'s unlock field**

Add to `tests/test_bot_web.py` (check the file's existing imports/helpers first — it already has a `_seed` helper and tests bot_status_payload elsewhere in the file, follow that pattern):

```python
def test_status_payload_unlock_reflects_live_capability(tmp_path, monkeypatch):
    _seed(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps(
        {"live_sessions_requested": ["weekday_night"]}))
    monkeypatch.setenv("BOT_LIVE", "1")
    p = bot_status_payload(tmp_path)
    assert p["unlock"]["ok"] is True

    (tmp_path / "config.json").write_text(json.dumps({"live_sessions_requested": []}))
    p2 = bot_status_payload(tmp_path)
    assert p2["unlock"]["ok"] is False
```

- [ ] **Step 6: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_bot_web.py -q -k unlock_reflects_live_capability -v`
Expected: FAIL — `bot_status_payload` still calls the old `live_unlock_ok(trades, cfg, env)` 3-arg form, which no longer exists after Task 1 (this will actually fail with a `TypeError`, not just a wrong assertion, since Task 1 already changed the function signature — confirming this call site is broken is itself useful signal).

- [ ] **Step 7: Fix `bot_status_payload`**

In `web.py`, replace (currently around line 1366-1369):
```python
    from bot_broker import live_unlock_ok
    from bot_core import load_config
    cfg = load_config(d / "config.json")
    ok, reason = live_unlock_ok(trades, cfg, dict(os.environ))
```
with:
```python
    from bot_broker import live_capability_ok
    from bot_core import load_config
    cfg = load_config(d / "config.json")
    ok, reason = live_capability_ok(cfg, dict(os.environ))
```

- [ ] **Step 8: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_bot_web.py -q -k unlock_reflects_live_capability -v`
Expected: PASS

- [ ] **Step 9: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 10: Commit**

```bash
git add bot_core.py web.py tests/test_bot_core.py tests/test_bot_web.py
git commit -m "Replace live_requested bool with live_sessions_requested list in config"
```

---

### Task 4: `POST /api/bot/live_session` endpoint

**Files:**
- Modify: `web.py` (add near `bot_control_write`/`api_bot_control`, after `_gate_split`)
- Test: `tests/test_bot_web.py`

**Interfaces:**
- Consumes: `bot_core.session_gate_stats` (existing), `bot_core.POOL_NAMES` (existing), `bot_core.load_config` (existing).
- Produces: `bot_live_session_write(bot_dir, session: str, action: str, trades: list) -> dict` (returns `{"ok": bool, "reason": str}`), and the `POST /api/bot/live_session` route. Consumed by Task 5 (frontend calls this route).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_bot_web.py`:

```python
def test_bot_live_session_write_enable_requires_passing_gate(tmp_path):
    from web import bot_live_session_write
    # 50 trades, net avg positive -- fails the 100-trade floor
    trades = [{"status": "closed", "net_pnl": 0.1, "entry_ts": 1784592000.0,
              "entry_sig": {"ts": 1784592000.0}} for _ in range(50)]
    (tmp_path / "config.json").write_text(json.dumps({"live_sessions_requested": []}))
    result = bot_live_session_write(tmp_path, "weekday_night", "enable", trades)
    assert result["ok"] is False
    cfg = json.loads((tmp_path / "config.json").read_text())
    assert "weekday_night" not in cfg.get("live_sessions_requested", [])


def test_bot_live_session_write_enable_succeeds_when_gate_passes(tmp_path):
    from web import bot_live_session_write
    trades = [{"status": "closed", "net_pnl": 0.1, "entry_ts": 1784592000.0,
              "entry_sig": {"ts": 1784592000.0}} for _ in range(100)]
    (tmp_path / "config.json").write_text(json.dumps({"live_sessions_requested": []}))
    result = bot_live_session_write(tmp_path, "weekday_night", "enable", trades)
    assert result["ok"] is True
    cfg = json.loads((tmp_path / "config.json").read_text())
    assert cfg["live_sessions_requested"] == ["weekday_night"]


def test_bot_live_session_write_enable_is_idempotent(tmp_path):
    from web import bot_live_session_write
    trades = [{"status": "closed", "net_pnl": 0.1, "entry_ts": 1784592000.0,
              "entry_sig": {"ts": 1784592000.0}} for _ in range(100)]
    (tmp_path / "config.json").write_text(json.dumps(
        {"live_sessions_requested": ["weekday_night"]}))
    result = bot_live_session_write(tmp_path, "weekday_night", "enable", trades)
    assert result["ok"] is True
    cfg = json.loads((tmp_path / "config.json").read_text())
    assert cfg["live_sessions_requested"] == ["weekday_night"]   # no duplicate


def test_bot_live_session_write_disable_never_needs_gate(tmp_path):
    from web import bot_live_session_write
    (tmp_path / "config.json").write_text(json.dumps(
        {"live_sessions_requested": ["weekday_night", "weekend_day"]}))
    result = bot_live_session_write(tmp_path, "weekday_night", "disable", trades=[])
    assert result["ok"] is True
    cfg = json.loads((tmp_path / "config.json").read_text())
    assert cfg["live_sessions_requested"] == ["weekend_day"]


def test_bot_live_session_write_rejects_unknown_session(tmp_path):
    from web import bot_live_session_write
    (tmp_path / "config.json").write_text(json.dumps({"live_sessions_requested": []}))
    result = bot_live_session_write(tmp_path, "not_a_real_session", "enable", trades=[])
    assert result["ok"] is False


def test_api_live_session_endpoint_enable_and_disable(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import web
    monkeypatch.setattr(web, "_BOT_DIR", tmp_path)
    trades = [{"status": "closed", "net_pnl": 0.1, "entry_ts": 1784592000.0,
              "entry_sig": {"ts": 1784592000.0}} for _ in range(100)]
    (tmp_path / "bot_trades.jsonl").write_text(
        "\n".join(json.dumps(t) for t in trades))
    (tmp_path / "config.json").write_text(json.dumps({"live_sessions_requested": []}))
    client = TestClient(web.app)
    r = client.post("/api/bot/live_session", json={"session": "weekday_night", "action": "enable"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    cfg = json.loads((tmp_path / "config.json").read_text())
    assert cfg["live_sessions_requested"] == ["weekday_night"]
    r2 = client.post("/api/bot/live_session", json={"session": "weekday_night", "action": "disable"})
    assert r2.status_code == 200
    cfg2 = json.loads((tmp_path / "config.json").read_text())
    assert cfg2["live_sessions_requested"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_bot_web.py -q -k "bot_live_session_write or api_live_session_endpoint" -v`
Expected: FAIL — `bot_live_session_write` and the route don't exist yet.

- [ ] **Step 3: Implement `bot_live_session_write`**

In `web.py`, add after `_gate_split` (currently ending around line 1444), before `bot_control_write`:

```python
def bot_live_session_write(bot_dir, session: str, action: str, trades: list) -> dict:
    """Enable/disable one session in config.json's live_sessions_requested
    list. "enable" re-verifies that session's own 100-trade/positive-net-avg
    gate from live trade history -- the ONE place this feature ever
    computes session_gate_stats for the purpose of unlocking. "disable"
    never needs the gate (turning trading off is always safe). Returns
    {"ok": bool, "reason": str}."""
    from bot_core import POOL_NAMES, session_gate_stats, load_config
    if session not in POOL_NAMES:
        return {"ok": False, "reason": f"unknown session: {session}"}
    d = Path(bot_dir) if bot_dir else _BOT_DIR
    cfg = load_config(d / "config.json")
    requested = list(cfg.get("live_sessions_requested") or [])
    if action == "enable":
        stats = session_gate_stats(trades)[session]
        if stats["n"] < 100:
            return {"ok": False, "reason": f"{session} {stats['n']}/100 settled"}
        if stats["net_avg"] <= 0:
            return {"ok": False, "reason": f"{session} net avg {stats['net_avg']:+.4f} <= 0"}
        if session not in requested:
            requested.append(session)
    elif action == "disable":
        if session in requested:
            requested.remove(session)
    else:
        return {"ok": False, "reason": f"unknown action: {action}"}
    cfg["live_sessions_requested"] = requested
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "config.json.tmp"
    tmp.write_text(json.dumps(cfg))
    os.replace(tmp, d / "config.json")
    return {"ok": True, "reason": "updated"}
```

Then add the route after `api_bot_control` (currently ending around line 1479):

```python
@app.post("/api/bot/live_session")
async def api_live_session(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "reason": "bad json"}, status_code=400)
    session = body.get("session", "")
    action = body.get("action", "")
    trades = _read_jsonl_tail(_BOT_DIR / "bot_trades.jsonl", 100000)
    result = bot_live_session_write(_BOT_DIR, session, action, trades)
    status = 200 if result["ok"] else 400
    return JSONResponse(result, status_code=status)
```

Confirmed: `_read_jsonl_tail(path, limit=50)` (`web.py:1287-1298`) does
`Path(path).read_text().splitlines()[-limit:]` — a plain Python list slice,
which is safe and returns the whole file when `limit` exceeds the actual
line count. Passing `100000` as shown above is correct and reads the FULL
trade history (not a display-sized tail like the `1000`-row call
`bot_status_payload` uses for its own, different purpose) — no further
check needed, use the call exactly as written in Step 3.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_bot_web.py -q -k "bot_live_session_write or api_live_session_endpoint" -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add web.py tests/test_bot_web.py
git commit -m "Add POST /api/bot/live_session endpoint: server-side gate check on enable"
```

---

### Task 5: Dashboard buttons (3-state, per session, with confirmation)

**Files:**
- Modify: `bot_page.py` (near `unlockProgTiles`'s HTML and its rendering JS)
- No new pytest file — this is a display-and-interaction layer over already-tested backend endpoints (Task 4's tests already cover the write path directly); verify live in the browser instead, per this session's established practice for GUI changes.

**Interfaces:**
- Consumes: `POST /api/bot/live_session` (Task 4), the existing `unlockProgTiles`/`gateSessions` rendering (`bot_page.py`, already showing `n/100`, `net_avg`, and a ✓ per session), the existing `d.config` field already present in every `/api/bot/status` response (contains `live_sessions_requested` once Task 3 lands).

- [ ] **Step 1: Add a confirm-then-POST helper and per-session button markup**

In `bot_page.py`, find the existing per-session tile rendering inside `pollBot()` (search for `const unlockEl = $('unlockProgTiles')`). Extend the tile template to include a button, and add the click handler as a new top-level function:

```javascript
async function toggleLiveSession(session, curLive) {
  if (!curLive) {
    const mode = (lastBotCfg && lastBotCfg.broker_mode) || 'manual';
    const verb = mode === 'auto'
      ? 'places real orders automatically'
      : 'flags entries for you to place manually';
    if (!confirm(`Go live on ${session}? This ${verb}.`)) return;
  }
  const r = await fetch('/api/bot/live_session', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({session, action: curLive ? 'disable' : 'enable'})
  });
  const d = await r.json();
  if (!d.ok) { alert(`Failed: ${d.reason}`); return; }
  pollBot();
}
```

Then update the tile template inside `unlockEl.innerHTML = SESSIONS.map(s => { ... })` (currently building each session's gate tile) to add a button beneath the existing content. Read the current exact template first (`grep -n "unlockEl.innerHTML" -A 12 bot_page.py`) and append a button line inside the same template literal, driven by `st.ok` (from `gateSessions`) and whether `s` is in `(lastBotCfg && lastBotCfg.live_sessions_requested) || []`:

```javascript
      const isLive = ((lastBotCfg && lastBotCfg.live_sessions_requested) || []).includes(s);
      const btnCls = !st.ok ? 'live-btn locked' : isLive ? 'live-btn live' : 'live-btn available';
      const btnLabel = !st.ok ? 'locked' : isLive ? 'LIVE' : 'go live';
      const btnDisabled = !st.ok ? 'disabled' : '';
```
(computed as local variables inside the same `.map(s => { ... })` callback, alongside the existing `st`/`pct` locals, then interpolate `<button class="${btnCls}" ${btnDisabled} onclick="toggleLiveSession('${s}', ${isLive})">${btnLabel}</button>` into the returned template string, after the existing `gatebar` div and before the tile's closing `</div>`).

- [ ] **Step 2: Add CSS for the three button states**

In `bot_page.py`'s `<style>` block, find the existing `.gatebar` rule (used by the tiles this button lives inside) and add nearby:

```css
.live-btn { margin-top:6px; width:100%; padding:6px 0; border-radius:6px; font-size:11px;
            font-weight:900; text-transform:uppercase; letter-spacing:.5px; cursor:pointer;
            border:1px solid var(--border); background:transparent; color:var(--mute); }
.live-btn.locked { opacity:.4; cursor:not-allowed; }
.live-btn.available { color:var(--green); border-color:var(--green-bd); background:var(--green-bg); }
.live-btn.available:hover { background:var(--green); color:#06110c; }
.live-btn.live { color:#06110c; background:var(--green); border-color:var(--green);
                  box-shadow:0 0 14px rgba(63,214,140,.45); }
```
Confirmed: `--border:#263044`, `--green:#3fd68c`, `--green-bg:rgba(63,214,140,.08)`,
`--green-bd:rgba(63,214,140,.38)` are all already defined in this file's
`:root` block from earlier tonight's work — use them exactly as named
above, no new color values needed.

- [ ] **Step 3: Restart the web process and verify manually**

This step has no pytest coverage by design (per this task's Interfaces note) — verify directly:

```bash
cd /home/kenny/bots/kalshi-scanner
ss -ltnp | grep 9050   # find current PID
kill <PID>
source ~/.kalshi/trading.env
setsid nohup .venv/bin/python main.py --api-key "$KALSHI_API_KEY" --key-file "$KALSHI_PRIVATE_KEY_PATH" --web --web-port 9050 > /tmp/web_live_toggle.log 2>&1 < /dev/null &
disown
sleep 2
grep -ci error /tmp/web_live_toggle.log   # expect 0
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:9050/bot   # expect 200
```

Then navigate to `/bot` in a browser, confirm zero console errors on a fresh reload, and confirm: weekday_night's button shows "go live" in green-outline (its gate passes, per the last known live numbers — n≥100, net_avg>0 — but it hasn't been toggled in `config.json` yet), the other 3 sessions show "locked" greyed out and unclickable. Do NOT actually click "go live" during this verification pass — clicking it would genuinely add weekday_night to the real `data/bot/config.json`'s `live_sessions_requested`, which is a real state change on the live-running paper daemon (recall `mode` is still `"paper"` so no order-placement risk, but it's still a real config change) — that's Kenny's decision to make deliberately, not something to trigger while verifying the button renders.

- [ ] **Step 4: Commit**

```bash
git add bot_page.py
git commit -m "Add 3-state per-session live-trading toggle buttons to /bot dashboard"
```

---

## Final integration check (not a task — run after Task 5)

- [ ] Run the full suite one more time: `.venv/bin/python -m pytest tests/ -q` — expect all tests passing.
- [ ] Confirm `data/bot/config.json` still has `mode: "paper"` and an empty (or absent) `live_sessions_requested` — this plan changes the MECHANISM for going live, it does not itself put any session live. Nothing should have changed in the real running config as a side effect of implementing this plan.
- [ ] Confirm the three standing processes (`swing_bot.py`, `trade_grader.py`, `bot_tuner.py`) are still healthy, and that `swing_bot.py` specifically did NOT need a restart during this plan (it wasn't touched until Task 2's `swing_bot.py` edit — restart it after Task 2 lands, then it doesn't need touching again for Tasks 3-5, which only touch `bot_core.py`/`web.py`/`bot_page.py`).
- [ ] Grep the whole repo one final time for any remaining reference to the old `live_requested` key or the old 3-arg `live_unlock_ok(trades, cfg, env)` call shape, to confirm nothing was missed: `grep -rn "live_requested\b" --include="*.py" . | grep -v .venv` should return nothing outside historical git log / old comments in unrelated files (e.g. the manual/auto broker mode spec doc itself, which is a historical record and should NOT be edited).
