# Kalshi Scanner / Swing Bot — Code Quality & Architecture Review

Date: 2026-08-02
Scope: `bot_core.py`, `swing_bot.py`, `bot_broker.py`, `live_broker.py`, `web.py`,
`bot_replay.py`, `backtest_gate.py`, `trade_grader.py`, `target_grader.py`,
`data/bot/config.json`, `tests/`. Read-only review — no code changed.

Strategy/edge questions are explicitly out of scope. This is about correctness,
data management, architecture, and operational robustness of a bot about to
place small amounts of real money.

---

## Executive summary — top 5, ranked

1. **Manual-mode live entries/exits have no exception handling around the
   per-session live-unlock check, and that check is only ever performed
   inside the broker — never before it.** The moment `config.json["mode"]`
   is hand-edited to `"live"` with only one session in
   `live_sessions_requested` (exactly Kenny's stated plan), the first flip
   signal in any of the other three sessions throws an uncaught
   `RuntimeError` that aborts the tick before state is saved — and with
   `limit_entries: true` (the current setting), it lands in the *pending*
   path, so `_process_pending` re-raises on **every subsequent tick** until
   that market rolls away, a sustained multi-minute outage per occurrence,
   not a single dropped tick. No test exercises a partial (1-of-4) session
   unlock. **This is the single most important finding — it sits directly
   on the path to the real-money test.** See §1.1.

2. **`tick()` has no fault isolation.** Any single unhandled exception
   anywhere inside `_manage()` (finding #1, a bad config value, anything
   else) aborts the *entire* tick before `save_state()`/heartbeat update.
   The process keeps running and looks alive, but no entries, no exits, and
   no state persistence happen from that point until the underlying cause
   clears. This turns a narrow bug into an entire-bot outage with a
   deceptively healthy-looking process. See §1.2.

3. **In `broker_mode: "manual"` — the mode about to go live — the
   code-enforced dollar stops don't run, and the live PnL the dashboard
   shows in their place is a fee-free estimate, not a reconciled number.**
   `_check_live_stop` (`swing_bot.py:678-679`) is a deliberate no-op unless
   `broker_mode == "auto"`. `config.json`'s `live_hard_stop_usd: -4.0` and
   `live_daily_soft_stop_usd: -1.5` will not fire under manual mode. The
   design's stated justification is "a human is watching the dashboard" —
   but manual-mode fills are synthetic (`LiveBroker._signal_fill`,
   `bot_broker.py:155-159`: modeled price, `fee_total: 0.0`) and get booked
   straight into `total_pnl`/`day_pnl` and `bot_trades.jsonl` with no
   reconciliation against the real account, so the numbers that human is
   watching will systematically overstate real PnL by the true fees. See
   §1.6.

4. **The Kalshi fee formula that gates real money is explicitly
   self-flagged as unverified.** `backtest_gate.fee()`/`maker_fee()` feed
   every PnL number, the EV-bucket gate, and the 100-trade live-unlock gate
   itself (`bot_core.session_gate_stats`). If the real fee schedule differs
   even modestly, a session could be unlocked for live money on numbers
   that were never actually true. See §3.1.

5. **No idempotency protection on real order placement.** `live_broker.place_order`
   has no client-order-id / idempotency key. A network timeout after the
   order actually reached Kalshi, followed by any retry (manual or future
   automated), risks a duplicate real order. See §1.4.

Also flagged, not in the top 5 but worth a look: all state-mutating
`/api/bot/*` endpoints are unauthenticated and the server binds `0.0.0.0`
(§5.1) — `POST /api/bot/live_session` (unlocks real money) and
`POST /api/bot/control` (`flatten`) require no credential of any kind.

---

## 1. Correctness / bug risk

### 1.1 Manual live mode: the session-unlock check is unguarded (CRITICAL)

`bot_core.entry_blockers()` and `swing_bot.Bot._manage()` never check
`cfg["live_sessions_requested"]` or call `bot_broker.live_unlock_ok`. The
*only* place per-session live-unlock is enforced is deep inside
`LiveBroker._check_session()` (`bot_broker.py:140-149`), called from
`buy()`/`sell()`/`fill()` (`bot_broker.py:181-228`), which **raises**
`RuntimeError` if the session resolved from the *current* signal's `ts`
isn't in `live_sessions_requested`.

Every call site in `swing_bot.py` that invokes the broker wraps this in a
try/except **only for `broker_mode == "auto"`**; the `manual` branch (the
mode Kenny is actually running, per `data/bot/config.json:34`) calls the
broker directly, unguarded:

- `_enter` (`swing_bot.py:280-288`): `except Exception: ... if auto: halt+return; raise` — manual mode re-raises.
- `_fill_pending` (`swing_bot.py:346-356`): same pattern, manual mode re-raises.
- `_scale_out` (`swing_bot.py:449-459`): auto branch has try/except; the `else:` (manual) branch at line 458-459 calls `self.broker.sell(...)` unguarded.
- `_exit` (`swing_bot.py:499-508`): same — `else: fill = self.broker.sell(...)` at line 507-508 is unguarded.

**Concrete failure scenario A (near-certain on go-live):** `mode` is a
single bot-wide setting — there is no per-session mode, only
`live_sessions_requested` gates individual sessions (`bot_core.py:29-38`).
Kenny's plan (per memory) is to hand-edit `mode` to `"live"` and unlock one
session (e.g. `weekday_night`) via the `/bot` dashboard. The instant a flip
fires during `weekday_day`, `weekend_day`, or `weekend_night` (any of the
other three, still un-gated), `_place_entry`/`_enter`/`_fill_pending` calls
into `LiveBroker`, which raises `"weekday_day not toggled live"`. In manual
mode this propagates uncaught out of `_manage()` to `run()`'s top-level
catch (§1.2), aborting that tick. This will recur on essentially every
subsequent flip in a non-unlocked session — i.e., most of the bot's normal
operating time immediately after go-live.

**Latent, currently-unobserved variant:** `LiveBroker._check_session`
re-derives the session from `sig.get("ts")` **at sell time**, not from the
position's own entry-time pool (which *is* stored on the play as
`play["pool"]`, `swing_bot.py:293`, but never passed to the broker). In
principle a position whose exit tick lands in a different session than its
entry (a market straddling the 13:00Z or Fri/Sat 00:00Z session boundary)
would hit the same unguarded-raise problem on exit. I checked this against
the full trade history rather than asserting it: of 680 closed trades in
`data/bot/bot_trades.jsonl`, **zero** have `session_tag(entry_ts) !=
session_tag(exit_ts)` — the combination of `min_entry_mins`/`exit_mins`
bounding entry and forced-exit timing tightly enough against each market's
own 15-minute expiry appears to prevent it in practice, at least under the
current config. It remains a real architectural gap (the broker *should*
trust the position's own entry-time pool, not re-derive from wall clock)
and is worth fixing alongside scenario A, but treat it as a latent
correctness issue, not an observed or likely-imminent one — scenario A
above is the one that will actually bite.

**Test gap confirming this was never exercised:** every live-mode test in
`tests/test_swing_bot.py` that builds a `Bot` passes **all four** session
names in `live_sessions_requested`
(e.g. `tests/test_swing_bot.py:971-973, 1011-1013, 1057-1059, 1099-1101,
1165-1167, 1222-1224, 1265-1267`). Not one test constructs the realistic
partial-unlock configuration — the exact configuration the per-session
toggle feature exists for and the exact configuration about to go live.

**Suggested fix direction:** check `live_unlock_ok(cfg, env, session)` in
`entry_blockers`/`_manage` *before* ever calling the broker, using the
same session resolution the entry itself will use, so a not-yet-unlocked
session simply shows up as a normal blocker/skip event like any other gate.
Separately, for exits, resolve the session from `play["pool"]` (the
position's own entry-time session) rather than re-deriving it from the
current tick's `sig.ts` — a position that was legitimately entered live
should never become un-sellable because wall-clock time crossed a session
boundary while it was open. And regardless of both: wrap the manual-mode
branches in `_enter`/`_fill_pending`/`_scale_out`/`_exit` in the same
try/except discipline already applied to the auto-mode branches, so a
broker-layer failure degrades to a halted pool/skip event instead of an
uncaught exception.

### 1.2 `tick()` has no fault isolation (HIGH)

`Bot.run()` (`swing_bot.py:811-818`) wraps `self.tick()` in a blanket
`try/except Exception: self._event("error", repr(e))`. Inside `tick()`
(`swing_bot.py:706-730`), `save_state()` and the heartbeat update happen
**once, at the very end**, after `_manage(sig)` returns. `_manage` itself
loops over every open play, every pending entry, and then evaluates new
entries — with no per-play/per-pool exception isolation (contrast with
`bot_core.compute_findings`, which explicitly wraps each rule in its own
try/except for exactly this reason, `bot_core.py:657-661`).

Consequence: **any** single exception anywhere in that pipeline — the
live-broker session mismatch in §1.1, a config value of the wrong type
after a hand-edit typo (`config.json` is hand-edited routinely per git
history), a `None` where a float is expected — aborts the whole tick
before `save_state`/heartbeat write. The process keeps running (still
polling every `poll_secs`, still logging an `"error"` event each time), so
it does *not* look crashed, but no entries, no exits for *any* pool, and no
state persistence happen until the underlying condition clears. The only
visible symptom is a stale `heartbeat` in `bot_state.json`, which
`bot_core._finding_bot_health` (`bot_core.py:608-611`) does flag on the
dashboard after 30s — so it's not silent forever, but it is silent until
someone is looking at the dashboard.

**Suggested fix direction:** isolate the entry/exit/pending-processing
loops the same way `compute_findings` isolates its rules — one play's
failure shouldn't block every other play's exit or the next pool's entry
check in the same tick. At minimum, wrap `_manage()`'s per-ticker exit loop
and the final entry attempt in their own try/except so a failure in one
market doesn't prevent `save_state`/heartbeat from running for the tick.

### 1.3 `LiveBroker.fill()` hardcodes the order action to `"buy"`

`bot_broker.py:227-228`: the chase-to-market branch of `fill()` always
calls `self._auto_fill(side, "buy", qty, price, sig, order_type, tier)` —
the action is hard-coded, not derived from context. Today this is
harmless because `fill()` is only ever reached from the entry path
(`_fill_pending`, `swing_bot.py:347`); exits always call `sell()` directly.
But nothing in the type signature or a comment prevents a future caller
(e.g. a resting-limit *exit* order, a natural extension of the same
tiering `_place_entry` uses for entries) from calling `fill()` with
`side` meaning "sell" and silently placing a real **buy** instead. Worth a
guard or an explicit `action` parameter now, while the blast radius of
getting it wrong is still small.

### 1.4 No idempotency key on real order placement

`live_broker.place_order` (`live_broker.py:50-63`) POSTs with a 15s
timeout and no client-supplied idempotency key. If the request times out
*after* Kalshi already accepted it (a real possibility — the exception
raised on timeout looks identical to "never reached the server"), any
retry — including a human re-clicking or a future automated retry —
places a second real order for the same intended fill. There's currently
no automatic retry in the code (`_auto_fill` re-raises and the pool
halts, `bot_broker.py:161-179`), so today the exposure is bounded by
manual action, but this is worth fixing before `broker_mode` is ever
flipped to `"auto"` at any scale, since idempotency keys are cheap to add
now and expensive to retrofit after an incident.

### 1.5 Fee model is explicitly unverified (see §3.1 for the broader impact)

Correctness-specific note: `backtest_gate.fee()` (`backtest_gate.py:27-29`,
comment "VERIFY vs current schedule") and `maker_fee()`
(`backtest_gate.py:32-44`, "NOT verified against the primary source" —
Kalshi's fee-schedule PDF 429'd every fetch attempt) are used, unmodified,
in `bot_broker.PaperBroker.fill`, `LiveBroker._auto_fill`, and
`bot_core.entry_blockers`'s thin-edge gate. Every dollar figure the bot
reports — including the one gating live money — is only as correct as
this formula.

### 1.6 Manual mode: enforced dollar stops don't run, and the "human is
watching" fallback is watching a fee-free number (CRITICAL — this is the
mode about to go live)

`_check_live_stop` (`swing_bot.py:670-684`) is the code that enforces
`live_hard_stop_usd`/`live_daily_soft_stop_usd` against the *real* account
balance. Its very first line is:
```python
if not (self.broker.mode == "live" and self.broker.broker_mode == "auto"):
    return
```
This is deliberate (docstring: "only meaningful in live+auto mode, since
manual mode always has a human reading the dashboard before placing
anything") and it's tested that way
(`tests/test_swing_bot.py::test_live_stop_inactive_in_manual_mode`). But
`config.json`'s current `broker_mode` is `"manual"`
(`data/bot/config.json:34`) — the same file sets `live_hard_stop_usd: -4.0`
and `live_daily_soft_stop_usd: -1.5` (`config.json:37-38`) right next to
it, in a way that invites reading them as active protections. They are
not, under the configuration about to be used.

The design leans entirely on the human-watching-the-dashboard fallback for
manual mode — but the dashboard's live numbers are not real fills.
`LiveBroker._signal_fill` (`bot_broker.py:155-159`) is what manual-mode
`buy()`/`sell()`/`fill()` actually return: a synthetic fill dict at the
*modeled* price with `"fee_total": 0.0` — no fee is charged at all,
because manual mode never calls the exchange to find out what the real
fill/fee was. `_exit`/`_scale_out` book that synthetic fill's PnL straight
into `ps["total_pnl"]`/`ps["day_pnl"]` and append a `"mode": "live"` row to
`bot_trades.jsonl` (`swing_bot.py:466-467, 512-513, 516-524`) exactly like
a real fill would be booked. Nothing anywhere reconciles these rows against
the actual Kalshi account balance. The dashboard the human is supposed to
be watching in place of the automated stop will show a PnL number that
systematically overstates real results by the true (currently unverified,
§1.5/§3.1) per-contract fee on every round trip — and the code-enforced
safety net that would catch a human not watching closely enough is,
itself, switched off in exactly this mode.

**Suggested fix direction:** either make `_check_live_stop` mode-agnostic
(fetch the real balance regardless of `broker_mode`, since the balance
check itself doesn't require the auto-mode order-placement path) so the
dollar stops enforce in manual mode too, or — at minimum — surface the
manual-mode fee gap loudly on the `/bot` dashboard (e.g. label manual-mode
PnL as "estimated, pre-fee" rather than showing it identically to a real
reconciled number) and add a periodic real-balance-vs-ledger reconciliation
check that fires a `_finding`-style warning when they diverge past some
threshold.

---

## 2. Data management

### 2.1 `data/bot/*.jsonl` has no rotation; `data/whales/*` does

`trade_grader.py:246-263` (`archive_features`) gzips and archives
`data/whales/signal_feature_log.jsonl` once per UTC day into
`data/whales/archive/<date>/` — confirmed working (14 days of archives on
disk). **Nothing does this for `data/bot/bot_events.jsonl` (992K),
`data/bot/bot_trades.jsonl` (392K), or `data/bot/bot_trade_grades.jsonl`
(396K)** — the only archive entry for `data/bot/` on disk is a single
one-time snapshot from 2026-07-15. These three files grow forever.

### 2.2 `web._read_jsonl_tail` is not a real tail — it's a full read + slice

`web.py:1287-1298`:
```python
def _read_jsonl_tail(path, limit=50):
    lines = Path(path).read_text().splitlines()[-limit:]
```
This reads the **entire file** into memory and does a Python-level
`splitlines()[-limit:]` regardless of `limit`. `bot_status_payload`
(`web.py:1359-1386`), which backs `GET /api/bot/status` — almost certainly
the dashboard's most frequently polled endpoint — calls this three times
per request (trades, events, grades), i.e. O(total log size) work on every
poll. Combined with §2.1 (no rotation), this cost only grows. At current
sizes (~1.8MB combined) it's not urgent; it will be a real latency/CPU cost
within a few months of continuous operation at this write rate.

Separately, `bot_live_session_write` (`web.py:1552`) calls
`_read_jsonl_tail(_BOT_DIR / "bot_trades.jsonl", 100000)` — effectively
"read the whole trade history" on every live-session toggle. Low frequency
(a manual dashboard action), so lower priority than the status endpoint,
but same underlying inefficiency.

**Suggested fix direction:** seek from the end of the file in fixed-size
chunks until `limit` newlines are found (the pattern `web.candles_from_log`
already uses correctly at `web.py:1389-1424`, tailing the last 4MB via
`f.seek(0, 2)` / `f.seek(start)` — the fix already exists in the same
file, just not applied to `_read_jsonl_tail`). Add a daily/size-based
archive step for the three `data/bot/*.jsonl` files, mirroring
`trade_grader.archive_features`.

### 2.3 `config.json`: no schema/type validation, and any parse error silently reverts *every* key to default

`bot_core.load_config` (`bot_core.py:107-113`):
```python
def load_config(path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(Path(path).read_text()))
    except Exception:
        return dict(DEFAULT_CONFIG)
    return cfg
```
Two separate gaps: (a) no per-key type/range validation — a string where a
float is expected (e.g. `"live_hard_stop_usd": "-4"`) loads successfully
and only fails later, inside a comparison, as an uncaught exception that
triggers §1.2's whole-tick freeze; (b) any JSON parse error (a stray
comma, an unterminated string — realistic for a file that's hand-edited,
as `config.json`'s presence in `git status` at session start confirms it
is) silently discards **every** custom key, not just the malformed one,
and reverts to `DEFAULT_CONFIG` with **no log line, no event, nothing on
the dashboard** to say this happened. Since `tick()` reloads config every
iteration (`swing_bot.py:710`, intentional hot-reload), a bad edit takes
effect — or silently *fails to* take effect — within one poll interval,
and Kenny would have no way to know his tuned `stop_loss_frac`/
`live_hard_stop_usd`/etc. silently reverted to defaults unless he happens
to diff the dashboard's displayed config against what he intended.

Note the *direction* of the fallback is actually safety-conscious —
`DEFAULT_CONFIG`'s `mode` is `"paper"` and `live_sessions_requested` is
`[]` (`bot_core.py:27,29`), so a corrupt config always reverts to the
*safe* state, never an accidentally-more-permissive one. That part is
good design (noted in §6). The gap is purely the silence: no event log
entry, no dashboard indicator, when this fallback fires.

**Suggested fix direction:** log an `_event("config_error", ...)` (or
equivalent) whenever `load_config` hits the except branch, and consider
validating the loaded dict's types against `DEFAULT_CONFIG`'s types
key-by-key, dropping only the offending key rather than the whole file.

### 2.4 Paper and live trades share the same ledger with no mode filter in aggregation

Every closed-trade row carries `"mode": self.broker.mode`
(`swing_bot.py:469, 517`), but nothing downstream filters on it:
`bot_core.session_gate_stats`, `bucket_stats`, `pool_by_date_stats`, and
`web._trade_stats` all aggregate every row in `bot_trades.jsonl`
regardless of `mode`. Today this is harmless (everything is `"paper"`).
Once a session goes live, that session's pool `total_pnl`/`day_pnl`
(`swing_bot.py:466-467, 512-513`) will accumulate live and paper dollars
into the same running total with no way to see them separately without
re-deriving from the raw `mode` field by hand. The 100-trade live-unlock
gate itself (`session_gate_stats`) is safe (it only ever runs against
history that predates going live), but the *ongoing* dashboard PnL numbers
for an already-live session will blend real and simulated money.

**Suggested fix direction:** once any session is live, either split
`day_pnl`/`total_pnl` into `{paper: ..., live: ...}` per pool, or filter
`bot_status_payload`'s aggregation by `mode` explicitly.

---

## 3. Architecture

### 3.1 The fee model is a single unverified formula load-bearing across the entire system

Expanding on §1.5: `backtest_gate.fee()`/`maker_fee()` is the *one* place
Kalshi's fee schedule is encoded, and it feeds: every `PaperBroker`/
`LiveBroker` fill's `fee_total` (hence every trade's `net_pnl`), the
`min_edge_c` thin-edge entry gate (`bot_core.py:342-348`), the EV-bucket
gate that skips historically-losing buckets (`bot_core.ev_gate_blocker`),
and — critically — `bot_core.session_gate_stats`, the exact function
`web.bot_live_session_write` (`web.py:1461-1465`) calls to decide whether
a session has "100 trades, net-positive" and may be unlocked for real
money. If the true fee is even a little higher than modeled, a session
could clear the 100-trade gate on numbers that would not have cleared it
under the real cost structure. This isn't a coding bug so much as an
architectural single point of failure worth flagging: one under-verified
constant sits upstream of every dollar figure in the system, including
the literal gate for real-money risk.

### 3.2 `web.py` (3946 lines / 192K) mixes three concerns: API/business logic, and ~2000 lines of inlined HTML/CSS/JS for three separate dashboards

`grep -c '<html'` finds three full inline HTML documents starting at
`web.py:1628`, `web.py:2630`, `web.py:3689` (the `/whales`, `/crypto`, and
`/trade` screens respectively), each with its own `<script>` block
(`web.py:2097, 2963, 3803`). Route count is modest (29 `@app.get/post`
handlers), so the bulk of the file's size is business logic
(stats/grading aggregation) interleaved with page templates as Python
string literals, in the same module. `bot_page.py` (the `/bot` dashboard,
1215 lines) already demonstrates the pattern this codebase *should* use
for the other three screens — it's a separate file with its own inline
HTML — but `web.py` itself hasn't been split that way. This is exactly
the "smell" its size suggests: no real separation between the FastAPI
route layer, the aggregation/business logic, and the presentation layer,
all in one file that's hard to navigate, hard to review diffs on, and
where a change to one dashboard's JS can't be scoped or tested
independently of the stats functions living in the same file.

**Suggested fix direction:** extract each of the three inline dashboards
into its own module (mirroring `bot_page.py`), and separate the
stats/aggregation functions (`_trade_stats`, `_gate_split`,
`bot_status_payload`, etc.) into a `bot_stats.py`-style module that both
`web.py` and any future CLI/reporting tool can import without pulling in
FastAPI or the HTML.

### 3.3 `bot_replay.py` correctly reuses the live `Bot` class — no drift risk found

Positive finding, stated here because the task specifically asked about
drift risk: `bot_replay.replay()` (`bot_replay.py:36-81`) constructs a
real `swing_bot.Bot` instance and drives it tick-by-tick off historical
rows (`bot.tick(now_ts=r.get("ts", 0))`, `bot_replay.py:70-71`) — it is
*not* a separate reimplementation of entry/exit logic. This means replay
and live share the literal same code paths for flip detection, gating,
sizing, target/stop/time exits, and scale-out. There is no realistic way
for replay and live to silently diverge on strategy logic, which is
exactly what the memory note about a past "fill-realism" drift incident
would want fixed. The one intentional divergence (curfews forced off in
replay, `bot_replay.py:44-46, 54-55`) is deliberate and documented as
replay's job being "measuring the raw strategy," not simulating the live
overlay. See §6 for one minor, low-severity replay-fidelity gap
(`loop_deadman_mins` uses a real file mtime during replay, §6.2).

`trade_grader.py`, by contrast, does **not** import `bot_core` and
independently reimplements a counterfactual grading model
(`grade_trade`, `trade_grader.py:129-162`) — but this is by design: its
job is evaluating what *already happened* against an alternative
(hold-to-settlement) outcome, not re-simulating entry/exit decisions, so
it isn't subject to the same drift risk as a strategy reimplementation
would be. Worth knowing this distinction rather than re-auditing it later.

---

## 4. Testing

`tests/` is substantial (3191 lines across 8 files) and covers a lot of
the right things well: atomic state round-trips, day-roll/halt/profit-lock/
max-loss interactions, per-pool isolation, sell-error backoff, pending-entry
fill/chase/cancel/roll handling, and the live-stop hard/soft dollar floors
(`tests/test_swing_bot.py:1111-1206`). This is meaningfully more thorough
than a typical solo-dev trading bot.

The gap that matters most: **every live-mode test unlocks all four
sessions at once** (§1.1) — `tests/test_swing_bot.py:971-973, 1011-1013,
1057-1059, 1099-1101, 1165-1167, 1222-1224, 1265-1267` all pass
`live_sessions_requested: ["weekday_day", "weekday_night", "weekend_day",
"weekend_night"]`. There is no test for: a partial (subset) unlock and an
entry attempt landing in a non-unlocked session; an open position whose
exit-time session differs from its entry-time session; or a `config.json`
parse/type error surfacing mid-tick. All three are directly exercised by
§1.1/§1.2/§2.3 and none are covered.

`bot_broker.py`'s `live_unlock_ok`/`live_capability_ok` themselves are
well unit-tested in isolation (`tests/test_bot_broker.py:209-313`) — the
gap is specifically in *integration*: how `swing_bot.Bot` behaves when
that gate fails mid-operation, which is only tested for `broker_mode ==
"auto"` (e.g. `tests/test_swing_bot.py:1207-1303`), never `"manual"`.

**Suggested fix direction:** add a test that constructs a live+manual Bot
with exactly one session unlocked, feeds it a flip in a *different*
session, and asserts the tick completes (doesn't raise) and produces a
skip/halt event rather than an uncaught exception — this test should fail
against the current code and is the most direct regression guard for §1.1.

---

## 5. Operational robustness

### 5.1 No authentication on state-mutating endpoints; server binds all interfaces

`web.py:470`: `uvicorn.Config(app, host="0.0.0.0", port=port, ...)`.
`web.py` has no auth/session/API-key middleware anywhere (confirmed by
absence of any `Authorization`/`Depends(...)`/credential check on the
FastAPI routes). `POST /api/bot/live_session` (`web.py:1544-1555`, can
unlock a session for real-money trading) and `POST /api/bot/control`
(`web.py:1531-1541`, accepts `"flatten"`, which liquidates every open
position) are reachable by anyone who can reach `host:9050` — no
credential of any kind is checked. For a bot about to control real money,
this is worth explicit acknowledgment even if the current deployment is a
home machine not exposed to the internet: `0.0.0.0` binding means any
future port-forward, VPN misconfiguration, or shared-network exposure
turns this into a live financial control surface with no gate.

**Suggested fix direction:** at minimum, bind `127.0.0.1` unless remote
access is genuinely needed (and reach it over SSH tunnel/VPN instead), or
add a shared-secret header check on the mutating `/api/bot/*` endpoints.

### 5.2 Crash/restart recovery is solid — with one gap around the exact moment of a real fill

Positive finding: `save_state` (`swing_bot.py:51-56`) and every
`config.json`/`control.json` write in `web.py` use the tmp-file +
`os.replace` atomic-write pattern, so a crash mid-write can never produce
a torn/partially-written state file — a reader always sees either the old
or the new complete file. Boot correctly guards against replaying a stale
`control.json` command (`swing_bot.py:180-189`), and `open_plays` is
loaded straight from the last-saved `bot_state.json`, so a restart doesn't
lose track of a position *that was already persisted*.

The gap: `save_state` is called **once, at the end of `tick()`**
(`swing_bot.py:729-730`), after all of that tick's entries/exits have
already executed against the broker. If the process is killed (power
loss, OOM) between a successful `LiveBroker.buy()`/`fill()` and that
end-of-tick `save_state()` call, the resulting position exists but
`bot_state.json` never learns about it. **This applies specifically to
`broker_mode: "auto"`** — a real order that has already happened on
Kalshi. In the current `"manual"` configuration there is no exchange
order to lose: the "fill" is an append to `live_signals.jsonl`
(`emit_live_signal`, `bot_broker.py:21-33`), which is itself a durable
file write that survives the crash just as well as `bot_state.json`
would, and no position exists on the real account until the human reads
that signal and places the order by hand. So under today's config this
gap is dormant; it becomes live the moment `broker_mode` is switched to
`"auto"`. On restart with the gap triggered, the bot has no record of the
position: it won't manage it (no stop/target logic applied) and, if the
same flip condition recurs, `entry_blockers`'s `already_open` check
(`bot_core.py:322-323`) won't see it either, risking an unintended second
entry on the same market. Given `live_qty=1` and the current
`live_cap_usd` (§ config, $10-20), the blast radius of any single
occurrence would be small, but the mechanism is real and worth knowing
about before ever switching to `"auto"` or scaling size up.

**Suggested fix direction:** persist `open_plays` (or at least append a
one-line "pending fill" marker) immediately after a live fill succeeds,
before proceeding to the rest of the tick, rather than batching the save
to the end.

### 5.3 No process supervisor; consistent with existing memory, restated for completeness

`start.sh` launches `swing_bot.py` via `nohup ... &` with no systemd unit,
no `restart=always`, nothing (`start.sh`'s `start_bg` helper). This
matches the existing memory note (`reboot-restart-checklist.md`) — not a
new finding, but worth restating in this report's frame: given §5.2's
gap is bounded by Kalshi's markets self-settling within 15 minutes
regardless of bot activity (see §6.3), the actual risk from "no
supervisor" is more about *missed trading time* than *unbounded position
risk* — but it does mean a live position open at the exact moment of a
crash rides to settlement unmanaged (no stop-loss, no target) rather than
being actively closed, for whatever's left of that market's remaining
minutes.

---

## 6. Not concerning (checked, ruled out — saves re-investigating)

**6.1 `config.json` read/write races between the web dashboard and the
trading loop.** All writes (`swing_bot.save_state`, and every
`config.json`/`control.json` write in `web.py`) use tmp-file +
`os.replace`, which is atomic on POSIX — a reader (including
`swing_bot`'s hot-reload every tick, `swing_bot.py:710`) can never observe
a torn/partial write. Within `web.py` itself, the two config-mutating
handlers (`api_live_session`, `api_session_pause`, `web.py:1544-1567`)
`await request.json()` *before* their read-modify-write of `config.json`,
and the read-modify-write itself contains no `await` — under uvicorn's
single-process asyncio event loop (confirmed: `uvicorn.Server` at
`web.py:470-471`, no multi-worker config found), that section runs to
completion without yielding, so two concurrent HTTP requests to these
specific handlers can't interleave their read-modify-write. This was the
first thing worth checking given the "hot-edited while the loop runs"
framing, and it holds up.

**6.2 Replay/live drift.** Covered in §3.3 — `bot_replay.py` drives the
literal `Bot` class, not a reimplementation. One minor, low-severity note
uncovered while checking this: `Bot._check_loop_deadman`
(`swing_bot.py:603-623`) compares a historical replay `now_ts` against
`self.loop_log`'s **real, current** file mtime (`bot_replay.replay`
doesn't override `loop_log`, `bot_replay.py:60-61`), so during replay the
computed "age" is always deeply negative and the deadman check trivially
never fires. This doesn't affect strategy fidelity (the deadman is a
pure real-time operational guard, not a strategy input), it just means
replay can't be used to validate deadman behavior itself — not worth
fixing unless someone specifically wants to replay-test that feature.

**6.3 Unbounded live exposure from a crash or the §5.2/§1.1 stuck-state
bugs.** Kalshi's KXBTC15M markets are binary options that settle
automatically at a fixed 15-minute expiry regardless of what the bot's
local state thinks is happening. Even in the worst case from §1.1 or §5.2
(the bot loses track of, or can't sell, an open live position), the
position still resolves on its own within at most ~15 minutes, capped in
size by `live_qty`/`live_cap_usd`. This meaningfully bounds the financial
consequence of the bugs in this report — they're real correctness/ops
bugs worth fixing, but they can't produce runaway, unbounded loss the way
a similar bug in a continuously-rebalanced or leveraged system could.

**6.4 Two independent manual gates required before any live order can be
placed.** Going live requires *both* a hand-edit of `config.json["mode"]`
to `"live"` (no dashboard control sets this — confirmed by grep, no
`"mode"` write path exists in `web.py`) *and* `BOT_LIVE=1` set in the
process environment (`bot_broker.live_capability_ok`, `bot_broker.py:94-104`
— also not settable from the dashboard, and not referenced anywhere in
`start.sh`, so it must be exported by hand before the process starts).
This is good defense-in-depth against an accidental flip from the web UI.

**6.5 Fail-safe direction of `load_config`'s error fallback.** Flagged as
a gap in §2.3 because it's *silent*, but the *direction* is correct:
`DEFAULT_CONFIG`'s `mode` is `"paper"` and `live_sessions_requested` is
`[]` (`bot_core.py:27,29`), so any config corruption reverts to the safest
possible state (no live trading) rather than preserving or defaulting to
something riskier. Good instinct in the original design even though the
silence around it should be fixed.

**6.6 Float/money precision.** All PnL/fee arithmetic consistently rounds
to 4 decimal places at each computation step (`round(..., 4)` appears
throughout `bot_core.py`/`bot_broker.py`/`swing_bot.py`), which at
cents-scale dollar amounts avoids the usual binary-float accumulation
drift over the trade counts this system will see. No `Decimal` usage
anywhere, but not a practical concern at this scale and rounding
discipline.
