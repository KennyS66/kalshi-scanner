# GUI kill-switch — design

**Date:** 2026-08-02
**Status:** approved scope, pending spec review
**Context:** Kenny is about to run a $10 live test on `weekday_night`
(Monday 2026-08-03). Before real money moves, he wants a stop control he
can hit from the browser at any moment, with a clear warning about any
positions still open.

## The finding that shapes this design

**A global kill-switch already exists and already behaves exactly as
required.** `POST /api/bot/control {"cmd":"pause"}` sets
`state["paused"]`, which `entry_blockers` consumes to block all new
entries while open plays continue to exit normally
(`swing_bot.py:588-601`, `_handle_control`).

What is missing is everything around it:

- It is a small `Pause` button (`bot_page.py:417`), one of four
  equal-weight buttons, mid-page under "Risk & Health". In an emergency
  you must scroll to it and pick the right one.
- No confirmation, and no feedback that the command was received.
- **It says nothing about open positions** — the actual gap Kenny named.
- It exists only on `/bot`, but Kenny's default screen is `/trade`
  (memory: `user-prefers-trade-screen`).

So this work is **promote and harden**, not build new. No new backend
endpoint, no new persisted state, no new way for the bot to be wrong.

## Scope

Confirmed with Kenny 2026-08-02:

- STOP **pauses new entries only**. Open positions keep managing to their
  normal exits. **No force-flatten** in the kill-switch — the existing
  `Flatten` button stays where it is as a separate, deliberate action.
- STOP appears on **both `/bot` and `/trade`**.

Explicitly out of scope: changing what pause does, touching the
per-session pause/live toggles, and any change to `live_sessions_requested`.

## Architecture

A new module `stop_control.py` exports three strings — `STOP_CSS`,
`STOP_HTML`, `STOP_JS` — injected into both pages at marker comments.

```python
# bot_page.py / web.py
BOT_HTML = _RAW.replace("/*STOP_CSS*/", STOP_CSS) \
               .replace("<!--STOP_BAR-->", STOP_HTML) \
               .replace("//STOP_JS", STOP_JS)
```

Both pages already share the identical CSS variable palette (`--bg`,
`--red`, `--yellow`, `--mono`…) and both already have a `position:sticky`
`<header>` at `z-index:20`, so one component drops into both unchanged.

**The control lives inside the existing sticky header**, not in a second
sticky bar stacked beneath it. Stacking two sticky elements costs vertical
space on mobile and complicates `top:` offsets on both pages for no gain.
The header is already always-visible — that was the entire requirement.

The persistent open-positions banner renders directly below the header,
inside the same sticky container so it cannot scroll out of view.

### Data source

`STOP_JS` polls `GET /api/bot/status` every 5s and reads:

| Field | Use |
|---|---|
| `state.paused`, `state.paused_by` | button state; distinguishes a manual stop from a deadman pause |
| `state.heartbeat` | liveness — a dead bot must not look like a stopped bot |
| `state.open_plays` | the warning list |
| `config.live_sessions_requested` | the resume warning |

On `/bot` this duplicates the poll `pollBot()` already makes. That is a
deliberate trade: one extra localhost request per 5s buys a component that
is byte-identical on both pages, with no cross-page coupling to
`pollBot`'s lifecycle. Not worth optimizing.

## The control

Three visual states, driven by the poll:

| Condition | Appearance |
|---|---|
| running | red **■ STOP** button |
| paused | yellow **▶ RESUME** pill + `STOPPED` label |
| heartbeat > 30s | grey **BOT OFFLINE** label, button disabled |

The offline state matters: `/bot` already computes it (`bot_page.py:990`)
but it is not next to the control. A stopped bot and a dead bot are
completely different situations and must never look alike.

### Latency, stated honestly

`control.json` is read at the top of the bot's next tick, so STOP takes
effect within `poll_secs` (5s), not instantly. On click the button shows
**stopping…** and only becomes `STOPPED` once a poll confirms
`state.paused` is true. It will not claim success it hasn't observed.

## The open-positions warning

**On click, before anything is sent** — a confirm listing every open play:

```
Stop the bot?

2 positions will stay OPEN:
  KXBTC15M-…-T92   YES  x3   $1.56 at risk
  KXBTC15M-…-T94   NO   x1   $0.48 at risk

These keep managing to their normal exits (target / stop / time).
STOP only blocks NEW entries.

To close them instead, cancel and use Flatten.
```

Fields are `ticker`, `side`, `qty`, and cost (`entry.price × qty`) — all
present in `open_plays` with no live quote needed, which keeps the
component page-independent. (Unrealized P&L needs a live mark that only
`/bot` has, so it is deliberately omitted.)

If flat: a one-line confirm, no list, no alarm.

**After stopping with positions still open** — a persistent banner under
the header, visible until the last position closes:

```
⚠ STOPPED — 2 positions still open, managing to exit
```

This is the piece with no equivalent today. It exists so that "I stopped
the bot" can never be mistaken for "I am flat".

## The resume footgun

STOP does **not** clear `live_sessions_requested` — the axes stay
independent, matching the existing per-session design. The consequence:
after a stop, `weekday_night` is still armed, and Resume re-arms
real-money trading immediately.

Rather than change that behavior, the **resume confirm names it**:

```
Resume the bot?
weekday_night is LIVE — it will trade real money.
```

Only shown when `state.mode === "live"` and
`live_sessions_requested` is non-empty. This mirrors the existing pattern
at `bot_page.py:530-533`, where resuming a live session already confirms.

## CLI fallback: `stop.sh`

If the web UI is down, the browser is closed, or the machine is headless,
there is currently **no kill-switch at all**. `stop.sh` writes
`control.json` directly with the same nonce-increment logic as
`bot_control_write` (`web.py:1509`):

```bash
./stop.sh          # pause
./stop.sh resume
```

It writes via a `.tmp` + `os.replace`-equivalent (`mv`) so a torn read is
impossible, matching the existing writer. It does not import the web app,
so it works even if `web.py` cannot start.

## Verified behaviours (no work needed, recorded so they are not re-derived)

- **A manual stop is sticky against the deadman.** `_handle_control` pops
  `paused_by` (`swing_bot.py:593`), and `_check_loop_deadman` only
  auto-resumes when `paused_by == "deadman"` (`swing_bot.py:619-623`).
  A manual STOP will not silently un-stop itself.
- **A stop survives a bot restart.** `paused` lives in `bot_state.json`,
  and `roll_day_if_needed` resets `day_pnl`/`halted` but not `paused`.
- **A stop survives a page reload** — it is bot state, not browser state.

## Testing

Backend (`tests/test_bot_web.py`, extend):
- `bot_control_write` nonce increments and round-trips — already covered;
  add a `stop.sh` equivalence test asserting the shell writer produces
  the same `control.json` shape as the Python writer.

Frontend: the pages have no JS test harness today and this design does not
introduce one. The component's logic is deliberately thin (render from
polled JSON, POST on click, re-poll). The state-mapping function
(`paused`/`heartbeat`/`open_plays` → button label + banner text) is
extracted as a pure function so it can be tested if a harness is added
later, and is verified manually against all four states before commit:
running, stopped-flat, stopped-with-positions, offline.

Manual verification checklist before commit:
1. `/bot` and `/trade` both render the control, identical appearance.
2. Click STOP with no positions → simple confirm → button reaches
   `STOPPED` within ~5s → `bot_state.json` shows `paused: true`.
3. Click STOP with a seeded open play → confirm lists it → banner appears
   and persists across a page reload.
4. Kill the bot process → both pages show `BOT OFFLINE`, button disabled.
5. `./stop.sh` from a terminal → both pages reflect it within ~5s.

## Risks

- **Injection-marker drift**: if a marker comment is deleted from a page,
  `.replace()` silently no-ops and the control vanishes with no error. The
  loader asserts each marker was actually replaced and raises at import
  time otherwise — a missing kill-switch must fail loudly, at startup, not
  silently at 2am.
- **`/trade` gains a dependency on `/api/bot/status`.** If that endpoint
  errors, the component renders its offline state rather than throwing —
  it must never break the page it is embedded in.
