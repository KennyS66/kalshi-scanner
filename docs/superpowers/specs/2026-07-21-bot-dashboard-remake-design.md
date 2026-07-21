# /bot dashboard remake — Overview + Deep Dive, with auto-flagged findings

**Date:** 2026-07-21 · **Status:** approved

## Problem

`bot_page.py` (860 lines, 12 panels) has grown incrementally over a week of feature
commits. Every panel is individually useful, but the page has no hierarchy: health
signals, performance numbers, and forensic detail (candles, full bucket tables,
settlement grades, replay-tuner output) all sit at the same visual weight in one long
scroll. Answering "is anything actually wrong right now" means reading the whole page
and doing the pattern-matching by eye — the same work Claude did manually this session
(giveback-loss investigation, the `daytime_trades` bug that hid night data) to find
real problems. The dashboard displays data; it doesn't do any of the noticing.

## Solution

Two tabs, same single-page-app model the page already uses (one JSON fetch cycle,
client-side render, no new routes):

- **Overview** (default): status bar, an auto-computed **flagged findings** section,
  and the 2-3 highest-signal charts (equity curve, session P&L summary). This is the
  entire "is something wrong, is the strategy working" answer in one screen, no
  scrolling past unrelated panels to get it.
- **Deep Dive**: everything else that exists today (candles, range calibration,
  settlement grades, exit reasons, full EV-gate bucket table, replay tuner, trades
  table, session map), regrouped into three labeled sections — **Performance**, **Risk
  & Health**, **Strategy Detail** — instead of one flat list of 9 panels. Opened when a
  finding on Overview points here, not for routine checking.

Tabs are two `<div>`s toggled by a click handler; `evFilter`-style local state, no
backend involvement in switching.

**Density requirement:** no large dead-space regions on Overview — this is explicitly
about scan-ability, and blank space that pushes real content below the fold works
against that as much as clutter does. Concretely:
- Findings, status, and chart panels use CSS grid/flex so they reflow to fill row
  width rather than each claiming a fixed column that leaves gaps on wide viewports.
- No panel gets padding/margin sized for "breathing room" beyond the existing `--hair`/
  `--border` conventions already in the page — reuse those values, don't introduce
  larger ones.
- The equity chart and session P&L bars sit side-by-side in one row on Overview
  (matching the mockup shown during brainstorming), not stacked with a full-width gap
  between them.
- Below that row, a compact **loop log** panel reuses the existing `/api/loop_log`
  endpoint (already serves `{entries: [...]}`, no backend work needed) to fill the
  remaining Overview space with something genuinely useful — Claude's live
  marketloop commentary (regime calls, confirmed level breaks) — instead of leaving
  it blank. Filtered to `type != "HB"` (heartbeat pings are noise here, `/trade`
  already carries the full unfiltered feed for anyone who wants it) and capped to the
  last ~8 entries. Rows are single-line and tight — reuse `/trade`'s `.log-entry`
  formatting (ts/type/spot/msg) but at reduced row height, no per-entry padding
  beyond a 1px hairline separator.
- Deep Dive's three sections use the existing `.panel`/`.panel.wide` grid, tightened:
  panels that are mostly a small table (exit reasons, settlement grades) shouldn't
  reserve as much vertical space as the candle chart or trades table.

## Backend: `compute_findings`

New pure function in `bot_core.py`, next to the other derived-stats functions
(`bucket_stats`, `_grade_summary`-equivalents):

```python
def compute_findings(trades: list, state: dict, cfg: dict, ev_buckets: dict) -> list[dict]:
    """Returns [{"severity": "warn"|"info", "title": str, "detail": str}, ...],
    most severe first. Empty list = nothing flagged (a real, displayed state,
    not the absence of a section)."""
```

v1 rules, each a small independent check (easy to add more later without touching the
others or the render code):

1. **Bucket confirmed bleeding** — any `ev_buckets` entry with `n >= cfg.ev_gate_min_samples`
   and `net_avg < 0` (these are already auto-skipped by the live gate; this makes the
   *reason* visible without opening Deep Dive). `severity: warn`.
2. **Day giveback** — `state.day_high - state.day_pnl` exceeds 50% of `state.day_high`
   (only when `day_high > 0`). Same signal `profit_lock` reacts to, surfaced before/
   without needing the halt to fire. `severity: warn`.
3. **Bot health** — `paused`, `halted`, or heartbeat older than 30s. `severity: warn`.
   (Mirrors the existing `runBadge` logic in `pollBot()` — reuse that computation
   rather than re-deriving it.)
4. **Thin session** — any of the four `session_tag()` buckets with fewer total trades
   than `cfg.ev_gate_min_samples` — informational, not urgent. `severity: info`.
5. **Stuck-position rate spike** — among the last 20 closed trades, if `time`/`rolled`
   exits exceed 40%. Verified baseline before writing this: full trade history reads
   ~6% (171 trades, 10 time/rolled), but that mixes the old stop-enabled regime with
   the current stop-off one — restricted to trades since the Jul 18 `stop_loss_frac`
   change (the regime actually running today), it's 15.6% (64 trades, 10 time/rolled).
   40% gives real headroom above that current-regime baseline in a 20-trade sample
   without being so loose it never fires. `severity: warn`.

Wired into `/api/bot/status` (`web.py`) as a new `"findings"` key, computed alongside
the existing `ev_buckets`/`gate`/`grade_summary` fields in the same handler — no new
endpoint.

## Frontend

- `renderFindings(d)` — new function, called from `pollBot()` next to `renderEvGate`.
  Renders the flagged list, or the explicit "✓ nothing flagged" empty state.
- `renderOverview(d, cfg)` / existing panel renders move under a `Deep Dive` container;
  a `switchTab(name)` function toggles `hidden` on the two top-level containers and
  updates the active tab styling. Both tabs' content is always rendered on each poll
  (simplest correct option — avoids stale content when switching mid-session) since data
  volume here is small.
- Visual style, colors, and existing components (`.badge`, `.tile`, `.panel`,
  `.gatebar`) are reused as-is — this is a reorganization, not a restyle.

## Error handling

- `compute_findings` wraps each rule in its own `try/except`: one bad rule logs and is
  skipped, never blocks the others or crashes the endpoint (matches the existing
  per-record `try/except` convention in `trade_grader.py`).
- If `/api/bot/status` fails entirely, existing `runBadge` "API ERR" state covers it —
  no separate error path needed for findings specifically.

## Testing

Same verification approach used for the EV-gate session-tag change this session: live
restart of the scanner process, then a real browser check (claude-in-chrome) — load
`/bot`, confirm no console errors, screenshot both tabs, click through the tab switch
and confirm Deep Dive's regrouped sections render, and confirm at least one real
finding appears (there's live data right now — e.g., the `weekend_day` bucket is
already bleeding money) so the empty-state path can be distinguished from a broken one.

## Out of scope (explicitly not doing now)

- No changes to `/crypto`, `/whales`, or `/trade` — this is `/bot` only.
- No new backend data collection — findings are computed from data the endpoint
  already assembles.
- No color/typography/component-library changes.
- No persistence of dismissed findings — the list is always live-recomputed; nothing
  to mark as "seen" in v1.
