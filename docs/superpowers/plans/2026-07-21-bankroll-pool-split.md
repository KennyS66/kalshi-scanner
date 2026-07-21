# Bankroll Pool Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the swing bot's shared $500 paper bankroll and shared risk-halt state (day-stop, profit-lock, max-loss cap) into 4 independent pools, one per market session (`weekday_day`/`weekday_night`/`weekend_day`/`weekend_night`), so a bad session can't shrink another session's capital or trip a halt that stops its trading — plus the dashboard changes to actually see it.

**Architecture:** `bot_state.json` gains a `pools` dict (4 sub-dicts, each with its own bankroll/day_pnl/day_high/total_pnl/halted/loss_capped). Every write path that currently touches the top-level fields — entry sizing, exit P&L accumulation, the three risk checks, flatten, entry blocking, dashboard findings — gets resolved to the entry's pool via the existing `session_tag()` and routed there instead. A one-time migration backfills the 4 pools from trade history on first boot with the new schema.

**Tech Stack:** Python (bot_core.py, swing_bot.py, web.py), vanilla JS (bot_page.py), pytest.

## Global Constraints

- 4 pools, $500 split evenly ($125 each) — from the approved spec.
- Halts are fully isolated per pool; one pool halting never affects another.
- `day_stop_pct` (a %) scales naturally with each pool's smaller bankroll. `profit_arm_usd` and `max_loss_usd` (flat $ thresholds) stay flat and identical per pool — NOT divided by 4.
- `max_open_plays`/`max_entries_per_market` stay global (concurrency controls, not capital attribution) — untouched.
- The 100-weekday + 100-weekend live-unlock gate (`bot_broker.live_unlock_ok`) is untouched — different concept.
- Reuse `session_tag()` (already in `bot_core.py`) for all pool attribution — don't reinvent.
- Spec: `docs/superpowers/specs/2026-07-21-bankroll-pool-split-design.md`

---

## Task 0: Remove the dead `daytime_trades` test (pre-existing breakage, blocks test collection)

`daytime_trades()` was removed from `bot_core.py` earlier tonight (superseded by session-tagged buckets), but `tests/test_bot_core.py` still imports and tests it — this is a real regression from that earlier edit that was never caught, and it breaks `pytest`'s ability to even collect that file (ImportError at collection time), which later tasks in this plan need to run.

**Files:**
- Modify: `tests/test_bot_core.py:356-365`

- [ ] **Step 1: Confirm the current break**

Run: `.venv/bin/python -m pytest tests/test_bot_core.py -q`
Expected: `ImportError: cannot import name 'daytime_trades' from 'bot_core'`

- [ ] **Step 2: Delete the dead import and test**

Remove these exact lines from `tests/test_bot_core.py` (currently lines 356-365, a blank line before and after):

```python
from bot_core import daytime_trades

def test_daytime_trades_filters_curfew_hours():
    wd_13z = 1784293200.0    # 13:00Z
    wd_03z = 1784257200.0    # 03:00Z
    rows = [{"exit_ts": wd_13z, "status": "closed"},
            {"exit_ts": wd_03z, "status": "closed"},
            {"status": "closed"}]                     # no exit_ts: dropped
    out = daytime_trades(rows)
    assert out == [rows[0]]
```

- [ ] **Step 3: Confirm the file collects and passes**

Run: `.venv/bin/python -m pytest tests/test_bot_core.py -q`
Expected: PASS (no ImportError, all existing tests green)

- [ ] **Step 4: Commit**

```bash
git add tests/test_bot_core.py
git commit -m "tests: remove dead daytime_trades test (function removed earlier, import was never caught)"
```

---

## Task 1: Pool state schema + one-time migration

**Files:**
- Modify: `bot_core.py` (add `POOL_NAMES` constant)
- Modify: `swing_bot.py:30-34` (`fresh_state`), `swing_bot.py:64-70` (`roll_day_if_needed`), `swing_bot.py:80-86` (imports), `swing_bot.py:97` area (new `_fresh_pool`/`_migrate_pools` helpers), `swing_bot.py:136-142` (`Bot.__init__` boot seed)
- Test: `tests/test_swing_bot.py`

**Interfaces:**
- Produces: `bot_core.POOL_NAMES = ("weekday_day", "weekday_night", "weekend_day", "weekend_night")`; `swing_bot._fresh_pool(bankroll=125.0) -> dict`; `swing_bot._migrate_pools(trades, paper_bankroll, today) -> dict`
- Consumes: `bot_core.session_tag(ts) -> str` (already exists), `bot_core._utc_day` pattern already used elsewhere in this file for date grouping

### Step 1: Add `POOL_NAMES` to `bot_core.py`

Add right after `session_tag()` (currently ends around line 370), before `entry_bucket`:

```python
POOL_NAMES = ("weekday_day", "weekday_night", "weekend_day", "weekend_night")
```

Update `_finding_thin_session` to reuse it instead of its own literal tuple — find the line that currently reads:
```python
    for sess in ("weekday_day", "weekday_night", "weekend_day", "weekend_night"):
```
Replace with:
```python
    for sess in POOL_NAMES:
```

- [ ] **Step 1a: Make the edit above**
- [ ] **Step 1b: Sanity check**

Run: `.venv/bin/python -c "from bot_core import POOL_NAMES; print(POOL_NAMES)"`
Expected: `('weekday_day', 'weekday_night', 'weekend_day', 'weekend_night')`

### Step 2: Write the failing tests in `tests/test_swing_bot.py`

Replace `test_roll_day_resets_pnl_and_halt` (currently ~line 34):

```python
def test_roll_day_resets_pnl_and_halt():
    s = fresh_state()
    s["day"] = "2020-01-01"
    s["pools"]["weekday_night"]["day_pnl"] = -50.0
    s["pools"]["weekday_night"]["halted"] = True
    s["pools"]["weekend_day"]["day_pnl"] = -7.0     # every pool resets together
    s["pools"]["weekend_day"]["halted"] = True
    assert roll_day_if_needed(s, time.time()) is True
    for p in s["pools"].values():
        assert p["day_pnl"] == 0.0 and p["halted"] is False
    assert roll_day_if_needed(s, time.time()) is False  # same day now
```

Replace `test_state_roundtrip_atomic` (currently ~line 10):

```python
def test_state_roundtrip_atomic(tmp_path):
    s = fresh_state()
    s["pools"]["weekday_night"]["day_pnl"] = -3.21
    s["open_plays"]["T1"] = {"side": "YES", "qty": 5}
    save_state(tmp_path, s)
    assert load_state(tmp_path) == s
    assert not list(tmp_path.glob("*.tmp"))            # no temp litter
```

Leave `test_load_state_missing_gives_fresh` untouched — it's schema-agnostic.

Replace `test_profit_lock_not_armed_below_threshold` (currently ~line 604):

```python
def test_profit_lock_not_armed_below_threshold():
    from swing_bot import fresh_state, roll_day_if_needed
    s = fresh_state()
    s["day"] = "2020-01-01"
    s["pools"]["weekday_night"]["day_pnl"] = 5.0
    s["pools"]["weekday_night"]["day_high"] = 20.0
    roll_day_if_needed(s, time.time())
    assert s["pools"]["weekday_night"]["day_high"] == 0.0  # watermark resets each day
```

Add a new test for the migration helper (place it near the top, after the roll-day test):

```python
def test_migrate_pools_backfills_total_and_todays_day_pnl():
    from swing_bot import _migrate_pools
    trades = [
        {"status": "closed", "net_pnl": -10.0, "entry_ts": 1.0, "exit_ts": 100.0,
         "entry_sig": {}},                                     # weekday_night, today
        {"status": "closed", "net_pnl": 5.0, "entry_ts": 1.0, "exit_ts": 200.0,
         "entry_sig": {}},                                     # weekday_night, today
        {"status": "closed", "net_pnl": -50.0, "entry_ts": 1.0,
         "exit_ts": -86400.0, "entry_sig": {}},                 # weekday_night, NOT today
        {"status": "open"},                                     # ignored
    ]
    pools = _migrate_pools(trades, paper_bankroll=400.0, today="1970-01-01")
    assert pools["weekday_night"]["bankroll"] == 100.0          # 400/4
    assert pools["weekday_night"]["total_pnl"] == -55.0         # all 3 closed rows
    assert pools["weekday_night"]["day_pnl"] == -5.0            # only today's 2 rows
    assert pools["weekday_night"]["day_high"] == 0.0            # peaked at 0 (first leg -10, never positive)
    assert pools["weekday_day"]["total_pnl"] == 0.0
    assert pools["weekday_day"]["bankroll"] == 100.0
```

- [ ] **Step 2a: Write the tests above**
- [ ] **Step 2b: Run to confirm they fail**

Run: `.venv/bin/python -m pytest tests/test_swing_bot.py -k "roll_day or state_roundtrip or load_state_missing or profit_lock_not_armed or migrate_pools" -q`
Expected: FAIL (old schema has no `pools` key, `_migrate_pools` doesn't exist)

### Step 3: Implement the schema + migration

Replace `fresh_state()` in `swing_bot.py` (currently lines 30-34):

```python
def _fresh_pool(bankroll: float = 125.0) -> dict:
    return {"bankroll": bankroll, "day_pnl": 0.0, "day_high": 0.0,
            "total_pnl": 0.0, "halted": False, "loss_capped": False}


def fresh_state() -> dict:
    return {"mode": "paper", "paused": False,
            "day": _utc_day(0.0),
            "pools": {p: _fresh_pool() for p in POOL_NAMES},
            "bankroll_ts": 0.0,
            "open_plays": {}, "heartbeat": 0.0, "last_control_nonce": 0}
```

Replace `roll_day_if_needed()` (currently lines 64-70):

```python
def roll_day_if_needed(state: dict, now_ts: float) -> bool:
    today = _utc_day(now_ts)
    if state.get("day") == today:
        return False
    for ps in state.get("pools", {}).values():
        ps["day_pnl"] = 0.0
        ps["day_high"] = 0.0
        ps["halted"] = False
    state.update({"day": today, "market_entries": {}})
    return True
```

Add `session_tag` and `POOL_NAMES` to the existing `from bot_core import (...)` block (currently lines 80-86):

```python
from bot_core import (FlipDetector, RegimeTracker, load_config, entry_blockers,
                      should_time_exit, should_target_exit, should_stop_exit,
                      should_stretch_exit,
                      size_for_budget, trade_budget, loss_headroom,
                      compute_side_ranges, load_offsets,
                      bucket_stats, update_bucket_stats, ev_gate_blocker,
                      weekend_curfew_blocker, session_tag, POOL_NAMES)
```

Add the migration helper — place it after `_snap()` (currently ~line 97), before `fetch_signal()`:

```python
def _migrate_pools(trades: list, paper_bankroll: float, today: str) -> dict:
    """One-time backfill for the old single-bankroll schema: splits the
    configured paper_bankroll evenly across the 4 pools, then attributes
    every closed trade to a pool via session_tag(entry_ts) — same
    entry_ts-fallback bucket_stats() uses — to seed total_pnl (full
    history) and day_pnl/day_high (today's trades only, replayed in
    exit-ts order so day_high tracks the same running peak
    _check_profit_lock would have produced live)."""
    per_pool = (paper_bankroll or 500.0) / 4
    pools = {p: _fresh_pool(per_pool) for p in POOL_NAMES}
    todays_rows = {p: [] for p in POOL_NAMES}
    for t in trades:
        if t.get("status") != "closed" or t.get("net_pnl") is None:
            continue
        sig = dict(t.get("entry_sig") or {})
        sig.setdefault("ts", t.get("entry_ts"))
        pool = session_tag(sig.get("ts"))
        if pool not in pools:
            continue   # "unknown": entry_ts missing on very old rows
        pools[pool]["total_pnl"] = round(pools[pool]["total_pnl"] + t["net_pnl"], 4)
        if t.get("exit_ts") and _utc_day(t["exit_ts"]) == today:
            todays_rows[pool].append(t)
    for pool, rows in todays_rows.items():
        rows.sort(key=lambda t: t.get("exit_ts") or 0)
        running = peak = 0.0
        for t in rows:
            running = round(running + t["net_pnl"], 4)
            peak = max(peak, running)
        pools[pool]["day_pnl"] = running
        pools[pool]["day_high"] = peak
    return pools
```

Replace `Bot.__init__`'s boot seed (currently lines 136-142 — note the CURRENT code has no migration guard at all, it reseeds `total_pnl` unconditionally on every boot; this fixes that too):

```python
        # EV-gate stats + pool P&L: seeded from the closed-trade journal at
        # boot, then kept current incrementally in _enter/_scale_out/_exit.
        # Migration guard: only backfill pools from trade history if this is
        # an old-schema state file (no "pools" key yet) — once pools exist,
        # never re-derive them (day_pnl/total_pnl already accumulate
        # incrementally going forward; re-running this would double-count).
        trades = self._read_trades()
        self.ev_stats = bucket_stats(trades)
        if "pools" not in self.state:
            self.state["pools"] = _migrate_pools(
                trades, self.cfg.get("paper_bankroll") or 500.0,
                self.state.get("day"))
```

- [ ] **Step 3a: Make all four edits above**
- [ ] **Step 3b: Run to confirm the tests now pass**

Run: `.venv/bin/python -m pytest tests/test_swing_bot.py -k "roll_day or state_roundtrip or load_state_missing or profit_lock_not_armed or migrate_pools" -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add bot_core.py swing_bot.py tests/test_swing_bot.py
git commit -m "swing_bot: split state schema into per-pool bankroll/P&L/risk, one-time migration from old schema"
```

---

## Task 2: Entry/exit/sizing pool routing

**Files:**
- Modify: `swing_bot.py:168-190` (`_enter`), `swing_bot.py:192-222` (`_scale_out`), `swing_bot.py:224-252` (`_exit`), `swing_bot.py:260-275` (`_refresh_bankroll`), new `_play_pool` helper
- Test: `tests/test_swing_bot.py`

**Interfaces:**
- Consumes: `bot_core.session_tag(ts)`, `bot_core.POOL_NAMES`, `swing_bot._fresh_pool()` (Task 1)
- Produces: `swing_bot._play_pool(play) -> str`; every `open_plays[ticker]` dict now carries a `"pool"` key set at entry time

### Step 1: Write/update the failing tests

`test_full_round_trip_flip_entry_and_flip_exit` (currently ~line 89) — change only the assertion at line 106:

```python
    assert bot.state["pools"]["weekday_night"]["day_pnl"] == t["net_pnl"]
```

`test_paper_bankroll_override_sizes_trades_and_skips_balance_fetch` (currently ~line 203) — pool bankroll is now `400/4=100`, changing the sizing outcome. Replace the final assertions (currently ~lines 220-223):

```python
    assert calls == []                                   # no live balance fetch
    assert bot.state["pools"]["weekday_night"]["bankroll"] == 100.0  # 400/4, not 400
    # $100 pool * 2% = $2 budget; yes_ask 0.52 + fee 0.02 = 0.54 -> 3 contracts
    assert bot.state["open_plays"]["M1"]["qty"] == 3
```

`test_sizing_shrinks_with_consumed_loss_budget` (currently ~line 470) — with a $125 pool bankroll, the original `net_pnl=-50.0` fixture no longer makes the loss-budget constraint bind (verified: `trade_budget(125, -50, cfg)` doesn't drop below the bankroll-based budget). Replace the whole test body:

```python
def test_sizing_shrinks_with_consumed_loss_budget(tmp_path, monkeypatch):
    # $125 pool (500/4) -> base budget 125*.02=$2.50 -> 4 contracts w/ full headroom.
    # total_pnl -91 -> headroom 9 -> trade_risk_frac .10 -> $0.90 budget -> 1
    # contract: the loss-budget constraint now binds instead of the bankroll one.
    rows = [dict(_losing_trade_row(), net_pnl=-91.0)]
    (tmp_path / TRADES_FILE).write_text(json.dumps(rows[0]))
    sigs = [_sig(), _sig(whale_trend=3.0, momentum=30.0, ts=1005.0)]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    qty = bot.state["open_plays"]["M1"]["qty"]
    assert 0 < qty <= 1                             # vs 4 with full headroom
```

`test_scale_out_banks_half_then_runner_rides_to_stretch` (currently ~line 536) — entry qty drops from 18 to 4 at the new pool bankroll, so half/runner qty and P&L all change (verified via `trade_budget`/`size_for_budget` directly: entry qty 4, half 2, runner 2). Replace the entry-sig comment and assertions (currently ~lines 543, 550-560):

```python
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0),                 # enter x4 @ .52 ($125 pool * 2% = $2.50)
        _sig(whale_trend=3.5, momentum=20.0, yes_ask=0.66, ts=1010.0),   # sell 64c >= 62 -> scale half
        _sig(whale_trend=3.5, momentum=20.0, yes_ask=0.74, ts=1015.0),   # sell 72c >= 64 -> stretch
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    trades = _rows(tmp_path, TRADES_FILE)
    assert [t["exit_reason"] for t in trades] == ["target_half", "stretch"]
    half, runner = trades
    assert half["qty"] == 2 and runner["qty"] == 2
    assert half["exit_price"] == 0.64 and runner["exit_price"] == 0.72
    assert bot.state["open_plays"] == {}
    # both legs realized into the pool's day pnl; ev gate saw ONE combined sample
    assert bot.state["pools"]["weekday_night"]["day_pnl"] == pytest.approx(
        half["net_pnl"] + runner["net_pnl"])
    bucket = [v for v in bot.ev_stats.values()]
    assert len(bucket) == 1 and bucket[0]["n"] == 1
    assert bucket[0]["net"] == pytest.approx(half["net_pnl"] + runner["net_pnl"])
```

`test_scale_out_qty_one_exits_full_at_target` (currently ~line 563) — this test overrides `paper_bankroll: 30.0` to force exactly 1 contract; bump it 4x so the resulting *pool* bankroll (`120/4=30`) reproduces the original scenario unchanged:

```python
    (tmp_path / "config.json").write_text(_json.dumps(
        {"scale_out": True, "min_edge_c": None, "paper_bankroll": 120.0,
         "overnight_curfew": False, "weekend_curfew": False}))
```

(rest of that test body is unchanged — qty still 1.)

- [ ] **Step 1a: Write/update the tests above**
- [ ] **Step 1b: Run to confirm they fail against current (pre-Task-2) code**

Run: `.venv/bin/python -m pytest tests/test_swing_bot.py -k "full_round_trip or paper_bankroll_override or sizing_shrinks or scale_out" -q`
Expected: FAIL

### Step 2: Implement the routing

Replace `_enter` (currently lines 168-190):

```python
    def _enter(self, side, sig, ranges=None):
        pool = session_tag(sig.get("ts"))
        ps = self.state.get("pools", {}).get(pool)
        if ps is None:
            self._event("skip", f"unknown session pool for ts={sig.get('ts')}",
                        sig["ticker"], sig)
            return
        price = sig["yes_ask"] if side == "YES" else sig["no_ask"]
        budget = trade_budget(ps["bankroll"], ps.get("total_pnl", 0.0), self.cfg)
        arm = self.cfg.get("profit_arm_usd") or 0.0
        if arm > 0 and ps.get("day_high", 0.0) >= arm:
            # profit lock armed: green day banked — risk small from here
            budget *= self.cfg.get("profit_size_frac", 0.5)
        qty = size_for_budget(budget, price)
        if qty < 1:
            self._event("skip", f"budget too small for 1 contract at {price}",
                        sig["ticker"], sig)
            return
        fill = self.broker.buy(side, qty, sig)
        me = self.state.setdefault("market_entries", {})
        me[sig["ticker"]] = me.get(sig["ticker"], 0) + 1
        self.state["open_plays"][sig["ticker"]] = {
            "side": side, "qty": qty, "entry": fill, "ranges": ranges,
            "entry_sig": _snap(sig), "last_sig": dict(sig), "pool": pool}
        tgt = (f" target {ranges['sell_low']:.1f}c"
               f" (stretch {ranges['sell_high']:.1f}c)") if ranges else ""
        self._event("enter", f"{side} x{qty} @ {fill['price']}{tgt}",
                    sig["ticker"], sig)
```

`ps` is guaranteed present in normal operation because `_refresh_bankroll` always runs earlier in `tick()` and populates all 4 `POOL_NAMES` keys before `_manage()`/`_enter()` runs. The `ps is None` branch only guards a signal missing `ts` — shouldn't happen with the live feed, but must never crash the tick.

Add `_play_pool` right after `_migrate_pools` (Task 1):

```python
def _play_pool(play: dict) -> str:
    """Pool a play belongs to — stored at entry time (see _enter); falls
    back to re-deriving it from entry_sig for plays that predate this field
    (an open position carried over a live restart under the old schema)."""
    return play.get("pool") or session_tag((play.get("entry_sig") or {}).get("ts"))
```

Replace lines 205-207 inside `_scale_out`:

```python
        pool = _play_pool(play)
        ps = self.state["pools"].setdefault(pool, _fresh_pool())
        ps["day_pnl"] = round(ps["day_pnl"] + pnl, 4)
        ps["total_pnl"] = round(ps.get("total_pnl", 0.0) + pnl, 4)
```

Replace lines 235-237 inside `_exit` (same pattern):

```python
        pool = _play_pool(play)
        ps = self.state["pools"].setdefault(pool, _fresh_pool())
        ps["day_pnl"] = round(ps["day_pnl"] + pnl, 4)
        ps["total_pnl"] = round(ps.get("total_pnl", 0.0) + pnl, 4)
```

`.setdefault` here (not the `_enter`-style skip): an *exit* must never fail to record P&L or leave a position stuck open. This only matters for very old plays with neither a `"pool"` field nor a usable `entry_sig.ts`.

Replace `_refresh_bankroll` (currently lines 260-275):

```python
    def _refresh_bankroll(self, now_ts):
        pools = self.state.setdefault("pools", {})
        for p in POOL_NAMES:
            pools.setdefault(p, _fresh_pool())
        # Paper mode with a configured paper bankroll: fixed stake, no live
        # balance fetch. Live mode (future) always uses the real balance.
        pb = self.cfg.get("paper_bankroll") or 0
        if self.broker.mode == "paper" and pb > 0:
            for p in POOL_NAMES:
                pools[p]["bankroll"] = float(pb) / 4
            self.state["bankroll_ts"] = now_ts
            return
        if now_ts - self.state["bankroll_ts"] < BANKROLL_REFRESH_SECS:
            return
        bal = fetch_bankroll()
        if bal is not None:
            for p in POOL_NAMES:
                pools[p]["bankroll"] = bal / 4
        elif not any(pools[p]["bankroll"] for p in POOL_NAMES):
            for p in POOL_NAMES:
                pools[p]["bankroll"] = FALLBACK_BANKROLL / 4
        self.state["bankroll_ts"] = now_ts
```

- [ ] **Step 2a: Make all edits above**
- [ ] **Step 2b: Run to confirm the tests now pass**

Run: `.venv/bin/python -m pytest tests/test_swing_bot.py -k "full_round_trip or paper_bankroll_override or sizing_shrinks or scale_out" -q`
Expected: PASS

- [ ] **Step 2c: Regression-check Task 1's tests still pass**

Run: `.venv/bin/python -m pytest tests/test_swing_bot.py -q`
Expected: PASS on everything covered so far (some later tests — day_stop/max_loss/profit_lock — still fail until Task 3; that's expected at this point)

- [ ] **Step 3: Commit**

```bash
git add bot_core.py swing_bot.py tests/test_swing_bot.py
git commit -m "swing_bot: route entry sizing and exit P&L through the entry's session pool"
```

---

## Task 3: Isolate the three risk checks + flatten

**Files:**
- Modify: `swing_bot.py:254-257` (`_flatten`), `swing_bot.py:314-319` (`_check_day_stop`), `swing_bot.py:321-336` (`_check_profit_lock`), `swing_bot.py:338-353` (`_check_max_loss`)
- Test: `tests/test_swing_bot.py`

**Interfaces:**
- Consumes: `swing_bot._play_pool(play)` (Task 2), `bot_core.POOL_NAMES` (Task 1)
- Produces: `Bot._flatten(self, reason, pool=None)` — new optional `pool` kwarg; bot-wide callers (manual Flatten, deadman) keep calling with no `pool=`

### Step 1: Write the failing tests

`test_day_stop_halts_entries` (currently ~line 135) — change the setup and assertion:

```python
    bot.state["pools"]["weekday_night"]["day_pnl"] = -51.0   # beyond 10% of $125 pool
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert bot.state["pools"]["weekday_night"]["halted"] is True
```

`test_max_loss_cap_flattens_blocks_and_survives_day_roll` (currently ~line 438) — change lines 444, 447, 455:

```python
    assert bot.state["pools"]["weekday_night"]["total_pnl"] == -101.0
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert bot.state["pools"]["weekday_night"]["loss_capped"] is True
    ...
    bot.tick(now_ts=1000.0 + 86400 * 30)
    assert bot.state["pools"]["weekday_night"]["loss_capped"] is True
```

`test_max_loss_cap_releases_when_config_raised` (currently ~line 458) — change lines 464, 467:

```python
    assert bot.state["pools"]["weekday_night"]["loss_capped"] is True
    (tmp_path / "config.json").write_text(_json.dumps({"max_loss_usd": 200.0}))
    bot.tick(now_ts=1005.0)
    assert "loss_capped" not in bot.state["pools"]["weekday_night"]
```

`test_profit_lock_halts_on_giveback_and_halves_size` (currently ~line 581) — pool bankroll changes entry qty from 9 to 2 (verified: `size_for_budget(1.25, 0.54) == 2`). Replace lines 589-599:

```python
    bot.state["pools"]["weekday_night"]["day_pnl"] = 20.0
    bot.tick(now_ts=1000.0)                    # seeds detector, sets day_high
    assert bot.state["pools"]["weekday_night"]["day_high"] == 20.0
    bot.tick(now_ts=1000.0)                    # flip -> enter at half budget
    play = list(bot.state["open_plays"].values())[0]
    # $125 pool * 2% = $2.50 full budget -> half $1.25 -> qty 2 @ .52+.02 fee
    assert play["qty"] == 2
    # giveback: drop below 50% of the 20 peak -> halt, profit banked
    bot.state["pools"]["weekday_night"]["day_pnl"] = 9.5
    bot.tick(now_ts=1000.0)
    assert bot.state["pools"]["weekday_night"]["halted"] is True
```

Add two new isolation tests — the actual point of this whole feature. `_sig()` at `ts=1000.0` resolves to `weekday_night`; `ts=47800.0` (13:03Z, same Thursday) resolves to `weekday_day`:

```python
def test_pool_halt_blocks_only_that_pools_entries(tmp_path, monkeypatch):
    import json as _json
    (tmp_path / "config.json").write_text(_json.dumps(
        {"overnight_curfew": False, "weekend_curfew": False}))
    sigs = [
        _sig(ts=1000.0),                                              # seed weekday_night (M1)
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0),               # flip -> M1 blocked
        _sig(ticker="M2", ts=47800.0),                                 # seed weekday_day (M2, 13:03Z)
        _sig(ticker="M2", whale_trend=3.0, momentum=30.0, ts=47805.0), # flip -> M2 should enter
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    bot.state["pools"]["weekday_night"]["halted"] = True
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert "M1" not in bot.state["open_plays"]
    assert "M2" in bot.state["open_plays"]
    assert bot.state["pools"]["weekday_day"]["halted"] is False
    events = _rows(tmp_path, EVENTS_FILE)
    skips = [e for e in events if e["action"] == "skip" and e["ticker"] == "M1"]
    assert any("halted" in e["reason"] for e in skips)


def test_day_stop_halt_flattens_only_that_pools_plays(tmp_path, monkeypatch):
    import json as _json
    (tmp_path / "config.json").write_text(_json.dumps(
        {"overnight_curfew": False, "weekend_curfew": False}))
    sigs = [
        _sig(ts=1000.0),
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0),               # enter M1 (weekday_night)
        _sig(ticker="M2", ts=47800.0),
        _sig(ticker="M2", whale_trend=3.0, momentum=30.0, ts=47805.0), # enter M2 (weekday_day)
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert set(bot.state["open_plays"]) == {"M1", "M2"}
    bot.state["pools"]["weekday_night"]["day_pnl"] = -51.0   # trip only this pool's day stop
    bot.tick(now_ts=1000.0)                                  # feed exhausted; checks still run
    assert "M1" not in bot.state["open_plays"]                # flattened
    assert "M2" in bot.state["open_plays"]                     # untouched
    assert bot.state["pools"]["weekday_night"]["halted"] is True
    assert bot.state["pools"]["weekday_day"]["halted"] is False
```

- [ ] **Step 1a: Write/update all tests above**
- [ ] **Step 1b: Run to confirm they fail**

Run: `.venv/bin/python -m pytest tests/test_swing_bot.py -k "day_stop or max_loss_cap or profit_lock or pool_halt" -q`
Expected: FAIL

### Step 2: Implement isolated risk checks

Replace `_flatten` (currently lines 254-257):

```python
    def _flatten(self, reason, pool=None):
        for ticker in list(self.state["open_plays"]):
            play = self.state["open_plays"][ticker]
            if pool is not None and _play_pool(play) != pool:
                continue
            self._exit(ticker, play, play["last_sig"], reason)
```

`_handle_control`'s `flatten` command and any other bot-wide caller keep calling `self._flatten(reason)` with no `pool=` — unchanged behavior there.

Replace `_check_day_stop` (currently lines 314-319):

```python
    def _check_day_stop(self):
        for p in POOL_NAMES:
            ps = self.state["pools"][p]
            stop = self.cfg["day_stop_pct"] * ps["bankroll"]
            if not ps["halted"] and ps["day_pnl"] <= -stop:
                ps["halted"] = True
                self._flatten("halt", pool=p)
                self._event("halt", f"[{p}] day_pnl {ps['day_pnl']:+.2f} <= -{stop:.2f}")
```

Replace `_check_profit_lock` (currently lines 321-336):

```python
    def _check_profit_lock(self):
        """Trail each pool's own profit peak: once armed, halt that pool
        before a give-back erases it."""
        arm = self.cfg.get("profit_arm_usd") or 0.0
        if arm <= 0:
            return
        for p in POOL_NAMES:
            ps = self.state["pools"][p]
            hi = max(ps.get("day_high", 0.0), ps["day_pnl"])
            ps["day_high"] = hi
            if ps["halted"] or hi < arm:
                continue
            floor = hi * self.cfg.get("profit_keep_frac", 0.5)
            if ps["day_pnl"] <= floor:
                ps["halted"] = True
                self._flatten("halt", pool=p)
                self._event("halt", f"[{p}] profit_lock: day peaked {hi:+.2f}, "
                            f"banking {ps['day_pnl']:+.2f} (floor {floor:.2f})")
```

Replace `_check_max_loss` (currently lines 338-353):

```python
    def _check_max_loss(self):
        """Hard cap on each pool's own TOTAL loss. Flat max_loss_usd,
        identical per pool (not divided by 4) — see design spec."""
        for p in POOL_NAMES:
            ps = self.state["pools"][p]
            capped = loss_headroom(ps.get("total_pnl", 0.0), self.cfg) <= 0
            if capped and not ps.get("loss_capped"):
                ps["loss_capped"] = True
                self._flatten("max_loss", pool=p)
                self._event("halt", f"[{p}] MAX LOSS CAP: total_pnl "
                            f"{ps.get('total_pnl', 0.0):+.2f} <= "
                            f"-{self.cfg.get('max_loss_usd', 0):.0f} — trading "
                            f"blocked until max_loss_usd is raised")
            elif not capped and ps.get("loss_capped"):
                ps.pop("loss_capped", None)
                self._event("resume", f"[{p}] max-loss cap released (config raised)")
```

- [ ] **Step 2a: Make all four edits above**
- [ ] **Step 2b: Run to confirm the tests now pass**

Run: `.venv/bin/python -m pytest tests/test_swing_bot.py -k "day_stop or max_loss_cap or profit_lock or pool_halt" -q`
Expected: PASS

- [ ] **Step 2c: Full regression check**

Run: `.venv/bin/python -m pytest tests/test_swing_bot.py -q`
Expected: PASS on everything except tests that depend on Task 4 (entry_blockers pool-awareness) — see Task 4.

- [ ] **Step 3: Commit**

```bash
git add swing_bot.py tests/test_swing_bot.py
git commit -m "swing_bot: fully isolate day-stop/profit-lock/max-loss halts and flatten per pool"
```

---

## Task 4: `entry_blockers` call site + `compute_findings` pool-awareness

**Files:**
- Modify: `swing_bot.py:420-431` (`_manage`'s `entry_blockers` call site)
- Modify: `bot_core.py` (`_finding_day_giveback`, `_finding_bot_health`)
- Test: `tests/test_swing_bot.py`, `tests/test_bot_core.py`

**Interfaces:**
- Consumes: `bot_core.POOL_NAMES`, `bot_core.session_tag`

### Step 1: Write the failing tests

Add to `tests/test_swing_bot.py` (covers the `loss_capped` branch specifically, which Task 3's isolation tests don't exercise):

```python
def test_loss_capped_pool_blocks_only_that_pools_entries(tmp_path, monkeypatch):
    import json as _json
    (tmp_path / "config.json").write_text(_json.dumps(
        {"overnight_curfew": False, "weekend_curfew": False}))
    sigs = [
        _sig(ts=1000.0),
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0),               # M1, weekday_night
        _sig(ticker="M2", ts=47800.0),
        _sig(ticker="M2", whale_trend=3.0, momentum=30.0, ts=47805.0), # M2, weekday_day
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    bot.state["pools"]["weekday_night"]["loss_capped"] = True
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert "M1" not in bot.state["open_plays"]
    assert "M2" in bot.state["open_plays"]
```

Add to `tests/test_bot_core.py` (new import + fixture + 3 tests — `compute_findings` has no existing tests today):

```python
import time as _time
from bot_core import compute_findings, POOL_NAMES


def _state_with_pools(**pool_overrides):
    pools = {p: {"bankroll": 125.0, "day_pnl": 0.0, "day_high": 0.0,
                 "total_pnl": 0.0, "halted": False, "loss_capped": False}
             for p in POOL_NAMES}
    for pool, over in pool_overrides.items():
        pools[pool].update(over)
    return {"paused": False, "heartbeat": _time.time(), "pools": pools}


def test_finding_day_giveback_is_per_pool_and_names_it():
    state = _state_with_pools(weekend_night={"day_high": 20.0, "day_pnl": 5.0})
    out = compute_findings([], state, {}, {})
    titles = [f["title"] for f in out]
    assert any("weekend_night" in t for t in titles)
    assert not any("weekday_day" in t for t in titles)


def test_finding_bot_health_reports_only_halted_pools():
    state = _state_with_pools(weekday_night={"halted": True})
    out = compute_findings([], state, {}, {})
    hit = next(f for f in out if f["title"] == "Bot halted")
    assert hit["detail"] == "weekday_night"


def test_finding_bot_health_ignores_missing_pools_key():
    # legacy raw state dict (pre-migration, read straight off disk by web.py)
    out = compute_findings([], {"paused": False, "heartbeat": _time.time()}, {}, {})
    assert not any(f["title"] == "Bot halted" for f in out)
```

- [ ] **Step 1a: Write the tests above**
- [ ] **Step 1b: Run to confirm they fail**

Run: `.venv/bin/python -m pytest tests/test_swing_bot.py tests/test_bot_core.py -k "loss_capped_pool or finding_" -q`
Expected: FAIL

### Step 2: Implement

Replace the `entry_blockers` call site inside `_manage()` (currently lines 420-431):

```python
        ranges = self._ranges_for(flip, sig)
        pool = session_tag(sig.get("ts"))
        ps = self.state.get("pools", {}).get(pool, {})
        blockers = entry_blockers(sig, self.cfg, self.state["open_plays"],
                                  ps.get("halted", False), self.state["paused"],
                                  ranges)
        ev = ev_gate_blocker(flip, sig, self.ev_stats, self.cfg)
        if ev:
            blockers.append(ev)
        cur = weekend_curfew_blocker(sig.get("ts") or time.time(), self.cfg)
        if cur:
            blockers.append(cur)
        if ps.get("loss_capped"):
            blockers.append("max_loss_cap")
```

(everything after this in `_manage` — `max_entries_per_market` check, etc. — unchanged.)

Replace `_finding_day_giveback` in `bot_core.py`:

```python
def _finding_day_giveback(trades, state, cfg, ev_buckets):
    out = []
    for pool in POOL_NAMES:
        ps = (state.get("pools") or {}).get(pool) or {}
        high, pnl = ps.get("day_high") or 0.0, ps.get("day_pnl") or 0.0
        if high > 0 and (high - pnl) > high * 0.5:
            out.append({"severity": "warn", "title": f"Day giving back gains: {pool}",
                        "detail": f"peaked at {high:+.2f}, now {pnl:+.2f} "
                                  f"({(high - pnl) / high * 100:.0f}% given back)"})
    return out
```

Replace `_finding_bot_health` in `bot_core.py`:

```python
def _finding_bot_health(trades, state, cfg, ev_buckets):
    out = []
    if state.get("paused"):
        out.append({"severity": "warn", "title": "Bot paused", "detail": ""})
    halted_pools = [p for p in POOL_NAMES
                    if ((state.get("pools") or {}).get(p) or {}).get("halted")]
    if halted_pools:
        out.append({"severity": "warn", "title": "Bot halted",
                    "detail": ", ".join(halted_pools)})
    age = time.time() - (state.get("heartbeat") or 0)
    if age > 30:
        out.append({"severity": "warn", "title": "Feed stale",
                    "detail": f"no heartbeat for {age:.0f}s"})
    return out
```

`state.get("paused")` and the heartbeat-staleness check stay untouched — both remain global fields per the spec.

- [ ] **Step 2a: Make all three edits above**
- [ ] **Step 2b: Run to confirm the tests now pass**

Run: `.venv/bin/python -m pytest tests/test_swing_bot.py tests/test_bot_core.py -k "loss_capped_pool or finding_" -q`
Expected: PASS

- [ ] **Step 2c: Full regression check on both files**

Run: `.venv/bin/python -m pytest tests/test_swing_bot.py tests/test_bot_core.py -q`
Expected: PASS, everything green

- [ ] **Step 3: Commit**

```bash
git add bot_core.py swing_bot.py tests/test_swing_bot.py tests/test_bot_core.py
git commit -m "swing_bot/bot_core: pool-aware entry blocking and dashboard findings"
```

---

## Task 5: Dashboard backend — per-date-per-pool P&L

**Files:**
- Modify: `bot_core.py` (new `pool_by_date_stats`), `web.py:1359-1385` (`bot_status_payload`)
- Test: `tests/test_bot_core.py`, `tests/test_bot_web.py`

**Interfaces:**
- Produces: `bot_core.pool_by_date_stats(trades) -> dict` — `{utc_exit_date: {pool: net_pnl_sum}}`
- Consumes: `bot_core.session_tag`

### Step 1: Write the failing tests

Add to `tests/test_bot_core.py`:

```python
from bot_core import pool_by_date_stats

def test_pool_by_date_stats_groups_by_exit_date_and_entry_session():
    trades = [
        {"status": "closed", "net_pnl": 1.5, "exit_ts": 50000.0,   # 13:53Z Thu
         "entry_ts": 47000.0, "entry_sig": {}},                     # 13:03Z -> weekday_day
        {"status": "closed", "net_pnl": -0.5, "exit_ts": 50100.0,
         "entry_ts": 1000.0, "entry_sig": {"ts": 1000.0}},         # 00:16Z -> weekday_night
        {"status": "open"},                                         # ignored
        {"status": "closed", "net_pnl": 2.0},                       # no exit_ts -> ignored
    ]
    out = pool_by_date_stats(trades)
    day = "1970-01-01"
    assert out[day]["weekday_day"] == 1.5
    assert out[day]["weekday_night"] == -0.5
```

Add to `tests/test_bot_web.py`, next to the other `bot_status_payload` tests (check that file for its existing `_seed`-style fixture helper and match its convention — if none exists, write trades directly to `tmp_path / "bot_trades.jsonl"` the same way other tests in that file do):

```python
def test_status_payload_includes_pool_by_date(tmp_path):
    trades = [{"status": "closed", "net_pnl": 0.2, "entry_ts": 1.0, "exit_ts": 100.0,
              "entry_sig": {"ts": 1.0}}]
    (tmp_path / "bot_trades.jsonl").write_text(
        "\n".join(json.dumps(t) for t in trades))
    p = bot_status_payload(tmp_path)
    assert "1970-01-01" in p["pool_by_date"]
    assert p["pool_by_date"]["1970-01-01"]["weekday_night"] == 0.2
```

- [ ] **Step 1a: Write the tests above (check `tests/test_bot_web.py` first for its actual fixture pattern and adjust the trade-seeding to match — don't duplicate a helper that already exists)**
- [ ] **Step 1b: Run to confirm they fail**

Run: `.venv/bin/python -m pytest tests/test_bot_core.py tests/test_bot_web.py -k "pool_by_date" -q`
Expected: FAIL

### Step 2: Implement

Add to `bot_core.py`, near `bucket_stats` (after its closing line, before the findings section):

```python
def pool_by_date_stats(trades: list) -> dict:
    """{utc_exit_date: {pool: net_pnl_sum}} for closed trades — the entry's
    session_tag (same entry_ts fallback as bucket_stats) attributes each
    trade to a pool; the EXIT date buckets it by day for the Deep Dive
    per-date breakdown."""
    out = {}
    for t in trades:
        if t.get("status") != "closed" or t.get("net_pnl") is None or not t.get("exit_ts"):
            continue
        day = time.strftime("%Y-%m-%d", time.gmtime(t["exit_ts"]))
        sig = dict(t.get("entry_sig") or {})
        sig.setdefault("ts", t.get("entry_ts"))
        pool = session_tag(sig.get("ts"))
        d = out.setdefault(day, {})
        d[pool] = round(d.get(pool, 0.0) + t["net_pnl"], 4)
    return out
```

In `web.py`, update the local import inside `bot_status_payload` (currently line 1370):

```python
    from bot_core import bucket_stats, compute_findings, pool_by_date_stats
```

And add `"pool_by_date"` to the returned dict (currently lines 1378-1385):

```python
    return {"state": state, "stats": _trade_stats(trades),
            "trades": trades[-50:],
            "events": _read_jsonl_tail(d / "bot_events.jsonl", 50),
            "unlock": {"ok": ok, "reason": reason}, "config": cfg,
            "ev_buckets": ev_buckets, "tuner": tuner,
            "grades": grades[-200:], "grade_summary": _grade_summary(grades),
            "gate": _gate_split(trades),
            "pool_by_date": pool_by_date_stats(trades),
            "findings": compute_findings(trades, state, cfg, ev_buckets)}
```

- [ ] **Step 2a: Make both edits above**
- [ ] **Step 2b: Run to confirm the tests now pass**

Run: `.venv/bin/python -m pytest tests/test_bot_core.py tests/test_bot_web.py -k "pool_by_date" -q`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add bot_core.py web.py tests/test_bot_core.py tests/test_bot_web.py
git commit -m "bot_core/web: add pool_by_date_stats for the Deep Dive per-date pool P&L table"
```

---

## Task 6: Dashboard frontend — 4 pool cards + per-date table

**Files:**
- Modify: `bot_page.py` (hoist session helpers, Overview tiles, `pollBot()` badge logic, new `renderPoolTiles`/`renderPoolByDate`, Deep Dive table)

No pytest coverage exists for this file — verify manually per Task 7.

### Step 1: Hoist session helpers to module scope

`SESSIONS`, `sessCls`, `sessLabel` are currently declared *inside* `renderEvGate` — nothing else can reuse them. Move them to top-level scope, next to the existing `evFilter` declaration:

```js
const SESSIONS = ['weekday_day', 'weekday_night', 'weekend_day', 'weekend_night'];
const sessCls = s => ({weekday_day:'wd_day', weekday_night:'wd_night',
                       weekend_day:'we_day', weekend_night:'we_night'}[s] || '');
const sessLabel = s => ({weekday_day:'WD·day', weekday_night:'WD·night',
                         weekend_day:'WE·day', weekend_night:'WE·night'}[s] || (s || '?'));
let evFilter = null;      // null = all sessions, else 'weekday_day' etc.
```

Delete the now-duplicate `const SESSIONS = ...` / `const sessCls = ...` / `const sessLabel = ...` lines from inside `renderEvGate` — the function body keeps using them unchanged, just resolved from outer scope now.

- [ ] **Step 1a: Make the hoist edit above**
- [ ] **Step 1b: Sanity check nothing broke**

Run: `.venv/bin/python -c "from bot_page import BOT_HTML; print('SESSIONS' in BOT_HTML)"`
Expected: `True`

### Step 2: Overview — 4 pool cards replace the day-P&L/bankroll/loss-budget tiles

In the HTML, remove the `dayPnl`(hero)/`bankroll`/`lossBudget` tiles and insert a pool-tiles container above the remaining `.tiles` block:

```html
  <div class="ev-summary" id="poolTiles"></div>

  <div class="tiles">
    <div class="tile"><div class="k">win rate</div><div class="v" id="winRate">—</div>
      <div class="s" id="winRateSub"></div></div>
    <div class="tile"><div class="k">net avg / trade</div><div class="v" id="netAvg">—</div>
      <div class="s" id="nTrades"></div></div>
    <div class="tile"><div class="k">open plays</div><div class="v" id="nOpen">—</div>
      <div class="s" id="riskSub"></div></div>
    <div class="tile"><div class="k">range win line</div><div class="v" id="histWin">—</div>
      <div class="s" id="histWinSub"></div></div>
    <div class="tile"><div class="k">gate progress</div><div class="v" id="gateProg">—</div>
      <div class="s" id="gateWd">wd —</div><div class="gatebar"><i id="gateWdBar" style="width:0%"></i></div>
      <div class="s" id="gateWe">we —</div><div class="gatebar"><i id="gateWeBar" style="width:0%;background:var(--purple)"></i></div></div>
  </div>
```

- [ ] **Step 2: Make the HTML edit above**

### Step 3: `pollBot()` — pool-aware badge + `renderPoolTiles`

Replace the top-badge computation:

```js
  const fresh = s.heartbeat && (Date.now() / 1000 - s.heartbeat) < 30;
  const anyHalted = s.pools && Object.values(s.pools).some(p => p.halted);
  const run = !fresh ? ['BOT OFFLINE', 'offline'] : anyHalted ? ['HALTED', 'halted']
            : s.paused ? ['PAUSED', 'paused'] : ['RUNNING', 'running'];
  $('runBadge').textContent = run[0];
  $('runBadge').className = 'badge ' + run[1];
  $('logo').classList.toggle('off', !fresh);
```

Replace the old bankroll/dayPnl/lossBudget rendering block with a call to the new function:

```js
  renderPoolTiles(s);
```

Add `renderPoolTiles`, near `renderEvGate`:

```js
function renderPoolTiles(s) {
  const pools = s.pools || {};
  $('poolTiles').innerHTML = SESSIONS.map(p => {
    const ps = pools[p] || {};
    const dp = ps.day_pnl || 0;
    const bankroll = ps.bankroll || 0;
    const status = ps.halted ? '<span class="neg" style="font-weight:800">HALTED</span>'
                 : ps.loss_capped ? '<span class="neg" style="font-weight:800">LOSS CAP</span>'
                 : '<span class="dim">running</span>';
    return `<div class="tile">
        <div class="k"><span class="badge sess ${sessCls(p)}">${sessLabel(p)}</span></div>
        <div class="v ${dp >= 0 ? 'pos' : 'neg'}">${money(dp)}</div>
        <div class="s">bankroll $${bankroll.toFixed(2)} · ${status}</div>
      </div>`;
  }).join('');
}
```

- [ ] **Step 3: Make the JS edits above**

### Step 4: Deep Dive — pool-by-date table

Add a new panel inside `deepdiveTab`'s Performance grid, after the session-map panel:

```html
    <div class="panel wide">
      <h3>Pool P&amp;L by day <span class="dim" style="text-transform:none">(net per pool, UTC date rows)</span></h3>
      <div class="tbl-wrap"><table id="poolDateTable"><thead><tr></tr></thead><tbody></tbody></table></div>
      <div class="empty" id="poolDateEmpty" hidden>no closed trades yet</div>
    </div>
```

Add the render function:

```js
function renderPoolByDate(d) {
  const byDate = d.pool_by_date || {};
  const dates = Object.keys(byDate).sort().reverse().slice(0, 30);
  $('poolDateTable').tHead.rows[0].innerHTML =
    '<th>date</th>' + SESSIONS.map(p => `<th>${sessLabel(p)}</th>`).join('');
  $('poolDateTable').tBodies[0].innerHTML = dates.map(day => {
    const row = byDate[day] || {};
    return `<tr><td>${day}</td>` + SESSIONS.map(p => {
      const v = row[p];
      return v == null ? '<td class="dim">—</td>'
        : `<td class="${v >= 0 ? 'pos' : 'neg'}">${money(v)}</td>`;
    }).join('') + '</tr>';
  }).join('');
  $('poolDateEmpty').hidden = dates.length > 0;
}
```

Call it from `pollBot()`, near the existing `sessTable` rendering:

```js
  renderPoolByDate(d);
```

- [ ] **Step 4: Make the HTML + JS edits above**

### Step 5: Verify and commit

- [ ] **Step 5a: Static sanity check**

Run: `.venv/bin/python -c "from bot_page import BOT_HTML; assert 'poolTiles' in BOT_HTML and 'poolDateTable' in BOT_HTML; print('ok')"`
Expected: `ok`

- [ ] **Step 5b: Commit**

```bash
git add bot_page.py
git commit -m "bot_page: render 4 pool cards on Overview + a per-date pool P&L table on Deep Dive"
```

---

## Task 7: Full regression + live verification

**Files:** none (verification only)

- [ ] **Step 1: Full test suite**

Run: `.venv/bin/python -m pytest tests/test_swing_bot.py tests/test_bot_core.py tests/test_bot_web.py -q`
Expected: PASS, everything green (Task 0 already fixed the pre-existing collection error)

- [ ] **Step 2: Confirm the live migration runs correctly**

The current `data/bot/bot_state.json` is old-schema (verified: has top-level `bankroll`/`day_pnl`/`day_high`/`halted`/`total_pnl`, no `pools` key). Restart the bot process per this repo's normal flow (`./start.sh`, or however `swing_bot.py` is currently supervised — check for a running process first) and confirm:
- `data/bot/bot_state.json` now has a `pools` key with all 4 `POOL_NAMES`, each `bankroll ≈ 125.0`
- Each pool's `total_pnl` roughly matches `session_report.py`'s independently-computed per-session totals (run `python3 session_report.py` and eyeball the weekday_day/weekday_night/weekend_day/weekend_night live-trade numbers against the new pools)

- [ ] **Step 3: Confirm the migration doesn't re-run**

Restart the bot process a second time. Confirm `total_pnl` in each pool does NOT jump or double — the `"pools" not in self.state` guard means the second boot should leave existing pool totals untouched (they'll only change from real new trades between restarts, not from a re-backfill).

- [ ] **Step 4: Browser check**

Open `/bot` via `claude-in-chrome`:
- No console errors
- Overview shows 4 pool cards with real bankroll/day-P&L/status
- Deep Dive shows the new "Pool P&L by day" table with real dates and per-pool columns
- Click through Overview → Deep Dive → Overview to confirm nothing broke in the existing tab-switch behavior from the earlier dashboard remake

- [ ] **Step 5: Confirm isolation live (optional, the automated tests in Task 3 already prove this — treat this as a belt-and-suspenders spot check, not required)**

If doing it: make a throwaway copy of `config.json`, temporarily lower `day_stop_pct` to something that will trip on the next tick, point a **test** bot instance at it (not the live one), and confirm only the pool that actually breached its threshold halts/flattens while the others keep running. Skip this step if the automated isolation tests from Task 3 are considered sufficient proof.

---

## Notes on gaps found during planning (beyond what the spec called out)

1. `Bot.__init__`'s `total_pnl` seed had no migration guard at all before this plan — it silently reseeded from full trade history on *every* boot. Task 1 fixes this as a necessary side effect of adding the one-time-migration guard.
2. Because pool bankroll drops from $500 to $125, several existing tests' expected quantities change non-trivially (which constraint binds — bankroll vs. loss-budget — can flip). Every affected test above has its new numbers verified directly against `trade_budget`/`size_for_budget` rather than estimated.
3. Open plays that predate this migration (carried across a live restart under the old schema) won't have a `"pool"` field — `_play_pool` falls back to re-deriving it from `entry_sig.ts`, and `_exit`/`_scale_out` use `.setdefault` rather than a hard lookup so an exit can never fail to record P&L.
4. `SESSIONS`/`sessCls`/`sessLabel` were function-local to `renderEvGate` in `bot_page.py` — hoisting them to module scope avoids a third duplicate copy of the session badge mapping for the new pool tiles and date table.
