# Dashboard charts, controls & visual system — design

**Date:** 2026-08-02
**Status:** approved, queued behind the kill-switch
**Brief (Kenny):** "add more graphs controls so i can actually view them
decently … i want it to be seamless and eye appealing"

## Two findings that reframe the brief

### 1. The charts aren't unviewable because of styling — the data is capped

`bot_status_payload` sends `"trades": trades[-50:]` (`web.py:1378`). There
are **680 settled trades**; the equity chart can only ever draw 50. Daily
P&L shows 14 days. No amount of visual work fixes this — it is the whole
"can't view them decently" problem, and it is a backend one-liner.

### 2. The palette objectively fails, on the pair that matters most

Run, not eyeballed —
`node scripts/validate_palette.js "#3fd68c,#ff5c64,#ffc53d,#5ca8ff,#ff9f45,#c29bff" --mode dark --surface "#0b0f17"`:

```
[FAIL] Lightness band   all six at L 0.69–0.85; dark band is 0.48–0.67
[PASS] CVD separation   worst adjacent green↔red ΔE 8.0 (deutan)
```

ΔE 8.0 is the *floor* — legal only with secondary encoding — and it is the
profit/loss pair. Colors also sit above the dark-mode band, which is why
saturated fills glare against `#0b0f17`.

**Root cause, structurally:** green and red currently do double duty as
both *status* (profit/loss) and *series identity*. The dataviz rule is that
status colors are reserved and never reused for a series. Separating the
two is what makes a dense dashboard readable.

## Visual system

### Categorical — the four sessions

Computed at OKLCH L 0.665 (top of the dark band, maximum in-gamut chroma),
then validated:

| Session | Hex |
|---|---|
| weekday_day | `#00a5c0` cyan |
| weekday_night | `#c58500` amber |
| weekend_day | `#9578ff` violet |
| weekend_night | `#36b100` lime |

```
[PASS] Lightness band     all 4 inside L 0.48–0.67
[PASS] Chroma floor       all 4 >= 0.1
[PASS] CVD separation     worst adjacent ΔE 19.8 (protan) · tritan 10.8
[PASS] Normal-vision      worst adjacent ΔE 24.2
[PASS] Contrast vs surface all 4 >= 3:1
```

Assigned in fixed order and **never cycled** — a session keeps its hue no
matter which filter is active.

### Status — reserved, never a series

`#3fd68c` profit / `#ff5c64` loss, kept as-is (they pass contrast vs
surface). Always rendered with a `+`/`−` sign and a text label, so meaning
never rests on color alone. They may never be used for session identity.

### Engine

**Hand-rolled inline SVG, no library** (Kenny's call, 2026-08-02). Reasons
on the record: the existing charts already have working hover crosshairs
and tooltips; zero dependencies means the dashboard survives an internet
outage; the ask is filtering rather than zoom/pan; uPlot's advantage
appears at ~150k points and we have 680; and a library's own visual
language would fight "seamless."

Consequence to manage: five new charts must not become five copies of the
same axis/scale code. A small shared helper (`scale`, `axis`, `hoverLayer`)
is extracted first, and every chart — existing and new — is built on it.

## Controls

**One filter row above the entire chart grid**, not per-chart controls:

- **Range**: 7d · 30d · all
- **Session**: all · WD-day · WD-night · WE-day · WE-night

Both drive *every* chart simultaneously. Per-chart controls are the
documented anti-pattern; a single row is what makes a grid of charts
readable as one instrument. Session filtering re-uses the existing
click-a-session-tile behaviour rather than inventing a second mechanism.

State lives in the URL query string, so a filtered view is linkable and
survives reload.

## Backend change

`bot_status_payload` gains a `trades_limit` parameter (default: all).
`/api/bot/status` accepts `?trades=N`. The full 680-row payload is ~200KB
of JSON — acceptable for a localhost dashboard polling every 5s, and it is
measured before shipping; if it is not, the charts move to a dedicated
`/api/bot/series` endpoint that returns only the columns charts need
(`exit_ts`, `net_pnl`, `session`, `exit_reason`) rather than whole trade
rows.

## New charts

All five derive from data already in the payload — no new computation on
the bot side.

1. **Drawdown (underwater) curve** — cumulative equity minus running peak,
   filled to zero. Absent today, and the most important risk chart for
   someone about to trade live: it shows the depth *and duration* of every
   drawdown, which the equity line hides.
2. **Rolling win rate + net avg**, 30-trade window, as two small stacked
   panels sharing an x-axis (never a dual-axis chart). Directly answers the
   open question from the alpha deep-dive: is weekday_night's edge decaying
   or holding?
3. **Session small-multiples** — four mini equity curves on a *shared*
   y-scale, each in its session hue. Makes "weekday_night is the only
   positive session" visible at a glance instead of inferred from tiles.
4. **P&L distribution histogram** — diverging around zero. Surfaces the fat
   tails that every alpha investigation kept colliding with, and explains
   why median-based filters kept failing.
5. **Exit-reason net avg** — diverging horizontal bars, replacing the
   current table. `time`/`rolled` being the loss concentration becomes
   visual rather than a number to read.

Each gets the standard hover layer (crosshair + tooltip on the line/area
charts, per-mark tooltip on the bar/histogram), matching what the equity
and candle charts already do.

## Signature element: the session strip

A persistent horizontal band under the header: the four sessions as one
strip, the currently-active session lit, each carrying its own inline
equity sparkline in its own hue, plus n and net avg.

This is the page's one memorable device, and it is chosen because
session-partitioned edge is *the* organizing fact of this bot — the live
gate, the pools, the pause controls, and the entire alpha story are all
per-session. Encoding that in the page's structure states something true
about the system rather than decorating it. Clicking a segment filters the
whole grid to that session.

## Anti-patterns explicitly avoided

- No dual-axis charts anywhere (win rate and net avg get two panels).
- No rainbow sequential ramps.
- Series color follows the session entity, never its rank — filtering does
  not repaint survivors.
- No number printed on every point; direct labels are selective.
- Grid and axes stay recessive; text wears text tokens, never series color.

## Testing

Backend: `bot_status_payload(trades_limit=...)` truncation behaviour, and
the `?trades=N` query parsing, in `tests/test_bot_web.py`.

Frontend: the pure data transforms behind each chart — drawdown series,
rolling window, histogram bucketing, session grouping — are extracted as
standalone functions and unit-tested in Python-side equivalents where they
already exist (`bot_core.bucket_stats`, `pool_by_date_stats`), or kept
trivial enough to verify by inspection. There is no JS test harness today
and this design does not add one.

Manual checklist before commit: every chart renders at 680 trades and at
0 trades; the filter row drives all charts together; palette re-validated
with the script; page checked at mobile width; keyboard focus visible on
every control; `prefers-reduced-motion` respected (the page already has the
media query).

## Sequencing

Queued **after** the kill-switch
(`2026-08-02-kill-switch-design.md`), per Kenny 2026-08-02: the STOP
control must be in place and tested before Monday's $10 live test. This
work has no deadline pressure and should not compete with it.
