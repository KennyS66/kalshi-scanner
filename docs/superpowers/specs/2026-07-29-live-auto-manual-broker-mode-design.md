# Live Auto/Manual Broker Mode — Design

## Problem

`bot_broker.LiveBroker` is currently an intentionally locked stub (raises
unconditionally — "order code not shipped in v1", per the original
2026-07-15 swing-bot design spec). All live trading tonight is manual: the
paper daemon fires a simulated "enter" event, and a human places the
matching real order by hand in the Kalshi app, reading the ticker/side/
price/qty off an ad hoc scratchpad script that tails `bot_events.jsonl`.

Kenny wants the capability to eventually flip to automated live order
placement, without losing the manual, human-confirmed mode he's using
tonight. This spec designs both as first-class, symmetric modes rather than
"one real mode plus a side script."

## Non-goals (this spec does not change)

- The existing `bot_broker.live_unlock_ok` 4-way per-session gate (100
  settled trades AND positive net avg, independently, in each of
  weekday_day/weekday_night/weekend_day/weekend_night) is unchanged and
  stays the sole authority for whether live trading of any kind is even
  constructible. No session-specific carve-out for auto mode. As of
  2026-07-29 only weekday_night passes this gate — auto mode cannot
  activate until all 4 do, same as manual live trading always could not
  (manual placement tonight sidesteps the code path entirely by being a
  human acting outside the bot, not an exception to the gate).
- Tonight's running $20 manual test is not modified by this spec. It keeps
  working exactly as it does now until this feature ships and is
  deliberately turned on.
- No auto-scaling sizing ladder. Sizing starts at whatever the manual test
  is using at the time (flat contract count, dollar cap) and Kenny raises
  it by hand later, the same way the $1 cap became the $20 cap.

## Architecture

A second, orthogonal config axis: `broker_mode` (`"manual"` | `"auto"`,
default `"manual"`), alongside the existing `mode` (`"paper"` | `"live"`).
Four resulting combinations, of which two exist today and two are new:

| mode  | broker_mode | Behavior |
|-------|-------------|----------|
| paper | (n/a)       | Simulated fills — unchanged, today's default. |
| live  | manual      | **New.** Same entry logic as live, but instead of placing a real order, writes a structured `live_signal` event (ticker, side, price, qty, tier) and displays it on `/bot`/`/trade`. Formalizes tonight's scratchpad script as a real, tested code path. |
| live  | auto        | **New.** Places a real order via `live_broker.py`. |

`live_unlock_ok` gates construction of either live sub-mode identically —
manual live mode is not a way around the gate, it's the same gate with a
human doing the final click instead of an API call.

### New file: `live_broker.py`

Replaces the raise-only body of `LiveBroker.buy`/`sell` for the `auto`
sub-mode with real signed `POST /trade-api/v2/portfolio/orders` calls,
reusing `account.py`'s existing credential-loading and request-signing
(`_load_env`, the PSS-SHA256 signing scheme) — no new credential path.
`LiveBroker.__init__` keeps its existing `live_unlock_ok` check unchanged;
only what happens *after* unlock (the actual `buy`/`sell` bodies) is new,
and only when `broker_mode == "auto"`. When `broker_mode == "manual"`,
`LiveBroker.buy`/`sell` instead call a new `emit_live_signal(...)` helper
that writes the structured event and returns without touching the Kalshi
order API at all.

### `live_signal` event (new)

Appended to a new `data/bot/live_signals.jsonl` (mirrors `bot_events.jsonl`'s
append-only, one-line-per-row shape) with: `ts`, `ticker`, `side`, `qty`,
`price`, `tier` (`"aggressive"`/`"patient"`/`"market"`), `pool`. `/bot` and
`/trade` poll this the same way the live-test panel already polls
`/api/live_test` (shipped tonight) — a small addition to that same panel
rather than a new page. Once this ships and `live+manual` mode is turned on
(same gate as today, plus `mode=live` — a smaller bar than `auto`, since it
never places a real order), the scratchpad watcher script from tonight
(`live_test_watch.py`) is retired: it was a stand-in for this exact code
path, not a permanent tool.

## Fill mechanics (auto only)

Mirrors the paper fill simulation exactly, so live results stay comparable
to everything already validated in paper: place a real resting limit order
at the tier price (`bot_core.entry_tier`'s aggressive/patient offsets, same
sub-35¢ price gate that skips the limit attempt entirely below that price).
Poll order status via `GET /trade-api/v2/portfolio/orders/{id}`. If
untouched after `limit_fill_timeout_secs` (same config value paper already
uses), cancel it and place a market (IOC) order for the same qty. Both the
resting fill and the chase-fill get logged with `maker`/`taker` fee
attribution the same way `bot_broker.PaperBroker.fill` already does, so
`bot_trades.jsonl` rows stay structurally identical between paper and live
(`mode: "live"` on the row is the only marker, same as today).

## Sizing and risk controls

Auto mode reads sizing from the same config keys the manual test uses at
the time (flat contract count, e.g. `live_qty=1`, and a dollar cap,
e.g. `live_cap_usd=20.0`) rather than paper's %-of-pool `trade_budget`
formula — that formula was proven this session to produce unreasonable
sizing (up to 33 contracts) when transplanted onto a small real account.
Because there is no human backstop in auto mode, the hard stop
(`live_hard_stop_usd`, e.g. -8.0) and daily soft stop
(`live_daily_soft_stop_usd`, e.g. -3.0) become **code-enforced pre-entry
checks** against the live account's actual balance (reusing
`bot_broker._balance_dollars`), not just a dashboard number a human reads —
an entry that would take the account past either stop is blocked and
logged the same way any other `entry_blockers` reason is, before the order
is ever placed.

## Error handling

Any order-placement failure, rejection, or partial fill: log a structured
event to `live_signals.jsonl` with an `error` field, skip that entry
(no retry), and halt new **auto** entries for that trade's specific session
pool — reusing the existing per-pool halt flag already used by
`_check_day_stop`/`_check_max_loss` (`ps["halted"] = True`) — until Kenny
manually resumes via the existing pause/resume control. Other pools and
manual-mode flagging are unaffected by one pool's halt, same as the
existing per-pool halt behavior today.

## Testing

- Unit tests for the `broker_mode` routing in `_place_entry`/
  `_process_pending` (paper unaffected; live+manual emits `live_signal`
  and never calls the Kalshi order API; live+auto calls it).
- `live_broker.py`'s order placement/polling/cancel logic tested against a
  mocked Kalshi API (same `monkeypatch.setattr(bot_broker, "_balance_dollars", ...)`-
  style mocking already used throughout the test suite — no real API calls
  in tests, ever).
- Pre-entry stop-check tests: an entry that would breach the hard or daily
  soft stop is blocked and logged, not silently sized down or allowed
  through.
- Before this ever places a real auto order for the first time: a dry-run
  window in live+manual mode first (the formalized flagging path, same as
  tonight but through the new code instead of the scratchpad script), to
  confirm signal timing/pricing looks right before removing the human
  click — matching how every other change tonight was replay-validated
  before shipping.

## Out of scope (this pass)

- Actually enabling auto mode. This spec builds the capability; turning it
  on requires the 4-way gate to clear (not close as of 2026-07-29) plus the
  GUI toggle plus `BOT_LIVE=1`, exactly as originally designed — no new
  ritual on top of those two, per Kenny's explicit choice during
  brainstorming.
- Auto-scaling sizing ladder — Kenny raises `live_qty`/`live_cap_usd`
  manually, on his own timeline.
- Retry-on-failure logic — a failed order halts that pool rather than
  retrying, per Kenny's explicit choice (safety over uptime).
