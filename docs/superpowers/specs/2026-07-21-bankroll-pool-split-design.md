# Split paper bankroll/P&L/risk into 4 session pools

**Date:** 2026-07-21 · **Status:** approved

## Problem

The swing bot tracks one shared `bankroll`, `day_pnl`/`day_high`, and `total_pnl` in
`bot_state.json`, and one shared set of risk halts (`day_stop_pct`, `profit_lock`,
`max_loss_usd`). Tonight's work established that the four market sessions
(`weekday_day`/`weekday_night`/`weekend_day`/`weekend_night`, via `session_tag()`)
behave very differently — `weekday_night` is now a multi-window-validated, profitable
session; `weekend_night` is deliberately exploratory and currently net negative. With
shared capital and shared halts, a bad `weekend_night` stretch can (a) shrink the
bankroll a `weekday_day` trade sizes against, and (b) trip a day-stop or profit-lock
that halts `weekday_day` and `weekday_night` too, even though nothing is wrong with
those sessions. Kenny wants each session's capital and risk state fully isolated, and
that separation visible on the dashboard.

## Solution

### State schema

`bot_state.json` gains a `pools` dict keyed by the four `session_tag()` values, each
holding its own:

```json
{"bankroll": 125.0, "day_pnl": 0.0, "day_high": 0.0,
 "total_pnl": 0.0, "halted": false, "loss_capped": false}
```

Top-level fields (`mode`, `paused`, `day`, `open_plays`, `heartbeat`,
`last_control_nonce`, `market_entries`) stay global and unchanged — `paused` gates the
loop-heartbeat deadman, a machine/session-liveness concern, not a per-session risk
concern. The day-roll (00Z) still fires once and resets every pool's `day_pnl`/
`day_high`/`halted` together; "day" is one shared calendar concept, not per-pool.

### Entry routing and sizing

A new position's pool is `session_tag(sig["ts"])` at entry time — whichever session is
live right now. `trade_budget(bankroll, total_pnl, cfg)` and `loss_headroom(total_pnl,
cfg)` (both in `bot_core.py`) get called with that pool's numbers instead of
`self.state["bankroll"]`/`self.state["total_pnl"]`. Their signatures don't need to
change — callers just pass a different (still float) bankroll/total_pnl. `_refresh_bankroll`
changes to write `state["pools"][p]["bankroll"] = paper_bankroll / 4` for each of the
four pools instead of one `state["bankroll"]`.

### Risk checks, fully isolated

`_check_day_stop`, `_check_profit_lock`, `_check_max_loss` each iterate the four pools
and check that pool's own `day_pnl`/`day_high`/`total_pnl` against the existing config
thresholds — no new config keys, but two different kinds of threshold behave
differently once bankroll is per-pool:
- `day_stop_pct` is a *percentage* of bankroll (`stop = day_stop_pct * bankroll`), so
  it naturally scales down with each pool's smaller ($125) bankroll — a pool's day-stop
  dollar amount shrinks proportionally, which is exactly the isolation this spec wants.
- `profit_arm_usd` and `max_loss_usd` are *flat dollar* thresholds today. This spec
  keeps them flat and identical per pool (not divided by 4) — simplest for v1, and
  conservative: a $125 pool needs a proportionally bigger % gain to arm profit-lock
  than the old $500 view did, which just makes it harder to trip, not easier. Revisit
  if that turns out too conservative in practice.

Each check sets that pool's own `halted`/`loss_capped`.

`_flatten(reason)` currently does:
```python
def _flatten(self, reason):
    for ticker in list(self.state["open_plays"]):
        play = self.state["open_plays"][ticker]
        self._exit(ticker, play, play["last_sig"], reason)
```
— it exits every open play, no pool awareness. Changes to `_flatten(self, reason,
pool=None)`: when `pool` is given, only exits plays where
`session_tag(play["entry_sig"]["ts"]) == pool` (the `ts` field survives in
`entry_sig` since this session's earlier `SIG_SNAPSHOT_KEYS` fix — confirmed by
reading the current play-dict construction, not assumed). Bot-wide flatten (manual
"Flatten" button, deadman pause) keeps calling `_flatten(reason)` with no pool —
unchanged behavior there.

`entry_blockers` gains a per-pool check: resolve the entry's pool from the current
`sig`, look up `state["pools"][pool]["halted"]`/`["loss_capped"]`, block if either is
set — replacing the current single `if self.state.get("loss_capped")` global check.

### Staying global, unchanged

- `max_open_plays` / `max_entries_per_market` — concurrency/liquidity controls, not
  capital attribution. No reason to let 4 pools each independently open 3 positions.
- The 100-weekday + 100-weekend live-unlock gate (`bot_broker.live_unlock_ok`) — a
  different question ("is there enough evidence to go live at all") from how paper
  capital happens to be partitioned for risk isolation. Untouched.

### Migration

On boot, if `state.get("pools")` is missing (old schema): initialize all four pools
with `bankroll = paper_bankroll / 4`, `halted = False`, `loss_capped = False`, then
backfill both `total_pnl` (full trade history) and `day_pnl`/`day_high` (today's
trades only) by grouping closed trades through `session_tag(entry_ts)` — reusing the
same `entry_ts`-fallback pattern `bucket_stats()` already uses for trades that predate
the `entry_sig.ts` fix, so no history is lost or misattributed. One-time; once
`state["pools"]` exists, this path never runs again.

### Dashboard

Overview's single day-P&L/bankroll/loss-budget tiles become 4 pool cards (bankroll,
day P&L, halted/loss-capped status), styled consistently with the session tiles already
shipped in the EV-gate panel tonight (same `wd_day`/`wd_night`/`we_day`/`we_night`
badge classes). Deep Dive gains a new table: rows = UTC date, columns = the 4 pools,
cells = that pool's net P&L for that day — the per-date breakdown from Kenny's ask.
The EV-gate table's session split (shipped earlier tonight) already covers "ev gate
data included separately"; no further change needed there.

## Testing

Same approach as tonight's other two changes: unit-check `compute_findings`-style pool
math against real state data before wiring in, then a live restart + browser
verification (claude-in-chrome) — confirm no console errors, confirm the 4 pool cards
render with real numbers, confirm the migration path produced sane backfilled totals
(spot-check against `session_report.py`'s existing per-session totals, which are
computed independently from the same trade history and should roughly agree),
and confirm a simulated pool halt (temporarily lowering `day_stop_pct` in a throwaway
config copy, not live) only flattens that pool's plays.

## Out of scope

- No change to `risk_pct`/`trade_risk_frac` — sizing formulas are unchanged, they just
  now receive a smaller bankroll/total_pnl input per pool.
- No UI to manually rebalance capital between pools — allocation is fixed at
  `paper_bankroll / 4` for now.
- No change to how `session_report.py`'s replay-based numbers work — that tool already
  operates independently of live `bot_state.json`.
