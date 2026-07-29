# Per-Session Live-Trading Toggle — Design

## Problem

The manual/auto broker mode feature shipped earlier tonight (see
`2026-07-29-live-auto-manual-broker-mode-design.md`) is gated by
`bot_broker.live_unlock_ok`, which requires **all 4 sessions**
(weekday_day/weekday_night/weekend_day/weekend_night) to independently
clear 100 settled trades + positive net avg before **any** live trading —
manual or auto — unlocks at all. As of 2026-07-29 only weekday_night
clears that bar (255 trades, net avg +$0.22); the other 3 do not, and two
of them (weekend_day, weekend_night) are actively net-negative.

Kenny wants to change this: a session that clears the bar on its own
should be individually eligible for live trading — manual or auto — right
away, without waiting on the other 3, controlled by a per-session dashboard
toggle rather than hand-editing `config.json`.

**This is an explicit, deliberate reversal** of the "wait for all 4"
decision made earlier the same night (see the manual/auto broker mode
spec's brainstorming notes) — surfaced and confirmed with Kenny directly
before this spec was written, not assumed.

## Decisions (from brainstorming)

- Applies to **both** manual and auto broker mode — a session that clears
  the gate is eligible for real orders under whichever `broker_mode` is
  currently set, with no per-trade human check required once auto is on
  for that session.
- `broker_mode` (manual/auto) stays **one global setting** — it decides
  *how* any live session behaves, independent of *which* sessions are
  live. No per-session manual/auto mixing in this pass.
- Toggling a session live requires a **confirmation step** in the
  dashboard before it takes effect.
- `BOT_LIVE=1` (the machine-level environment variable gate) is
  **unchanged** — still required on top of everything else, exactly as
  designed in the original 2026-07-15 spec and reaffirmed in tonight's
  manual/auto broker mode brainstorm. This spec only changes the
  per-session trade-count/net-avg half of the gate and how the GUI-toggle
  half is expressed (per-session list instead of one bool) — it does not
  touch the env-var half at all.

## Gate change

**This requires splitting what `live_unlock_ok` currently does into two
separate checks, not just adding a parameter to it** — a construction-time
check and a per-entry check. The original design's construction-time
all-or-nothing raise (`LiveBroker.__init__` calls `live_unlock_ok` once
and refuses to construct at all if it fails) doesn't fit per-session
unlock: with only weekday_night eligible, the bot must still start
successfully in live mode and simply decline weekend_day/weekend_night/
weekday_day entries individually, not refuse to run at all.

- **`live_capability_ok(cfg, env)`** (new, construction-time, replaces
  today's `live_unlock_ok` call inside `LiveBroker.__init__`): a coarse,
  cheap check — `BOT_LIVE=1` is set, and at least one session is both
  gate-passing (`session_gate_stats`) and present in
  `cfg.get("live_sessions_requested", [])`. If nothing is live-eligible at
  all, constructing a `LiveBroker` is pointless (identical to today's
  behavior in spirit — refuse to start in live mode with nothing unlocked
  — just no longer requiring ALL 4 sessions to be that "something").
  Returns `(ok, reason)`, same shape as today.
- **`live_unlock_ok(trades, cfg, env, session)`** (existing name kept,
  `session` becomes a required parameter, not optional): the per-entry
  check. Checks ONLY that specific session's own 100-trades/positive-net-avg
  bar (via `session_gate_stats`), AND that it appears in
  `cfg.get("live_sessions_requested", [])`, AND `BOT_LIVE=1`. Other
  sessions' state is irrelevant — this is the actual behavior change from
  "all 4 or nothing" to "each on its own." Called from `LiveBroker.buy`/
  `sell`/`fill`, resolving `session` from the entry's own `sig` via
  `bot_core.session_tag(sig.get("ts"))` (same helper already used
  everywhere else in the codebase for this) — NOT at construction time.
  If the resolved session isn't unlocked, the call raises the same
  `"live trading locked: <reason>"` `RuntimeError` construction used to
  raise, but now scoped to that one entry rather than the whole bot.

`session_gate_stats` (already built, unchanged) remains the single source
of truth for each session's own n/net_avg/gate-pass state — this spec
reuses it, doesn't duplicate it.

## Config shape

Replace `live_requested: bool` with:
```python
"live_sessions_requested": [],  # list of session names (subset of
    # bot_core.POOL_NAMES) the dashboard has toggled live. A session's
    # presence here + its own trade/net_avg gate + BOT_LIVE=1 together
    # unlock live trading FOR THAT SESSION ONLY -- other sessions are
    # unaffected by this list containing or omitting them.
```
`broker_mode` (manual/auto) is unchanged — still one global key.

## Dashboard

Four small controls on `/bot`, placed directly below the existing
per-session gate tiles (`unlockProgTiles`, already showing each session's
n/100 and net_avg with a ✓ checkmark when it individually passes) — same
row, same session ordering (WD-DAY, WD-NIGHT, WE-DAY, WE-NIGHT), so the
gate state and the toggle for a session are visually adjacent.

Three visual states per session button, driven by the same
`session_gate_stats` data already powering the tiles above them:
- **Locked** (grey, disabled, unclickable): `ok: false` from
  `session_gate_stats` — the session hasn't cleared its own bar yet.
- **Available** (outlined, clickable): `ok: true` but not in
  `live_sessions_requested` — eligible, not yet turned on.
- **Live** (filled green, clickable to turn back off): in
  `live_sessions_requested` (and implicitly `ok: true`, since a session
  can only get added to the list through this same gated flow).

Clicking an **available** button opens a confirmation
(`Go live on weekday_night? This places real orders.` /
`This flags weekday_night entries for you to place manually.` — wording
depends on the current global `broker_mode`, so the confirmation
accurately describes what's about to happen) before the toggle takes
effect. Clicking a **live** button to turn it back off does not require
confirmation (turning trading off is the safe direction, same asymmetry
as the existing pause/resume controls).

## Backend

New endpoint `POST /api/bot/live_session`, body `{"session": "weekday_night",
"action": "enable"}` (or `"disable"`), mirroring the existing
`/api/bot/control` pause/resume/flatten pattern (nonce-based write to
`config.json`, same file-write helper). On `"enable"`:
1. Re-derive `session_gate_stats` from the live trade history server-side
   — **never trust the button's client-side state alone**. If that
   session's own gate isn't actually passing (e.g. a stale dashboard, or
   a session that regressed since the page loaded), reject with an error
   the dashboard surfaces instead of silently no-op'ing.
2. If it passes, add `session` to `live_sessions_requested` (idempotent —
   no duplicate entries) and write `config.json`.

On `"disable"`: remove `session` from the list unconditionally (no gate
check needed to turn something off).

`LiveBroker.__init__` itself does NOT take a `session` parameter and does
NOT change at its call site in `Bot.__init__` — it constructs successfully
as long as `live_capability_ok(cfg, env)` passes (see Gate change above).
The per-session resolution happens inside `buy`/`sell`/`fill` themselves,
each deriving `session = bot_core.session_tag(sig.get("ts"))` (the same
helper used everywhere else in the codebase for this) from the entry's own
signal and checking `live_unlock_ok(trades, cfg, env, session)` before
proceeding — so a weekday_night entry is evaluated against weekday_night's
own state and a weekend_day entry against weekend_day's on that same
already-constructed broker instance, never blended.

## Testing

- `live_unlock_ok(trades, cfg, env, session="X")` unit tests: a session in
  `live_sessions_requested` with a passing gate unlocks; the same session
  NOT in the list stays locked even with a passing gate; a session in the
  list but with a failing gate stays locked; one session's state never
  leaks into another's check (e.g. weekend_night failing never blocks a
  weekday_night check).
- `live_capability_ok(cfg, env)` unit tests: succeeds when at least one
  session is both gate-passing and requested; fails when
  `live_sessions_requested` is empty even if a session's own gate passes
  (never requested); fails when `BOT_LIVE` isn't set even with a
  requested, gate-passing session.
- `LiveBroker` construction-time behavior: constructs successfully in live
  mode as long as `live_capability_ok` passes, REGARDLESS of how many
  individual sessions are actually eligible (one is enough) — this is the
  key regression guard against reintroducing the old all-4-sessions
  construction-time gate. A `buy`/`sell`/`fill` call for an eligible
  session's entry succeeds; the same call for an ineligible session's
  entry raises, on the SAME already-constructed `LiveBroker` instance —
  proving the check moved from construction-time to per-call.
- `POST /api/bot/live_session` tests: enabling a gate-passing session
  succeeds and is idempotent; enabling a gate-failing session is rejected
  server-side even if asked; disabling always succeeds.
- Dashboard: the three button states render correctly per session based
  on `session_gate_stats` + `live_sessions_requested`, verified live in
  the browser per this session's established practice for GUI changes.

## Out of scope (this pass)

- Per-session `broker_mode` (manual vs auto per session) — explicitly
  deferred per Kenny's choice; one global mode for now.
- Any change to `BOT_LIVE=1`'s role or the confirmation-free "disable"
  direction's asymmetry — both carried forward unchanged from the
  existing design.
- Auto-disabling a session if its gate later regresses after being
  toggled live (e.g. a losing streak drops net_avg below zero after the
  fact) — this pass only gates the *enable* transition server-side; a
  session already live stays live until manually disabled or hits its own
  hard/daily-soft stop (unchanged existing mechanism from the manual/auto
  broker mode feature). Worth a future pass, not blocking this one.
