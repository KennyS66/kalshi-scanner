# Swing Bot — paper-first flow-flip scalper (design)

Date: 2026-07-15
Status: approved by Kenny (rollout, strategy, risk model, architecture chosen via Q&A)

## Goal

An autonomous bot that runs Kenny's flow-flip scalp on Kalshi 15-minute BTC
contracts faster than a human can, with a dedicated GUI screen, and logs rich
enough to grade and tune every play. Paper mode from day one; live execution
exists behind a hard unlock bar. The near-term objective is **proving and
raising the win rate**, not deploying capital.

## Decisions made

| Question | Decision |
|---|---|
| Rollout | Paper-first; live locked behind unlock bar |
| Strategy | Flow-flip scalp (enter on whale-flow sign flip, exit on opposite flip or ≤2 min) |
| Sizing | 2% of bankroll per trade; day halt at −10%; bankroll = real Kalshi cash balance (read-only fetch, hourly) |
| Architecture | Standalone daemon `swing_bot.py` + `/bot` page in web.py; file-based IPC in `data/bot/` |

## Components

### swing_bot.py (daemon)
- Started by `start.sh` alongside the scanner; own process, own logs.
- Loop every 5s: fetch `http://localhost:9050/api/crypto/signal`, run the
  strategy state machine, manage open plays, write state atomically.
- Reads `data/bot/config.json` (hot-reload each tick) and
  `data/bot/control.json` (GUI commands: pause / resume / flatten).
- Heartbeat timestamp in `bot_state.json` every tick.

### Strategy v1 — flow-flip scalp
- **Trigger:** `whale_trend` sign flip with magnitude ≥ `flip_threshold`
  (config; seeded from scalp_gate defaults), and momentum sign agreement.
- **Entry:** bullish flip → buy YES at `yes_ask`; bearish flip → buy NO at
  `no_ask`. One position per market; respect `max_open_plays`.
- **No-entry conditions:** market decided (yes price <5c or >95c),
  `mins_left < min_entry_mins` (config, default 4), day halted, paused,
  feed stale.
- **Exit:** opposite flip, or `mins_left ≤ exit_mins` (default 2). Always flat
  before settlement; outcome of the market never decides P&L.
- All thresholds in config — tunable without code edits.

### Sizing & risk (percent-of-bankroll)
- Bankroll: Kalshi cash balance via the read-only signed GET from
  `account.py`'s credential scheme; refreshed hourly; cached in
  `bot_state.json`; fallback to last cached value, then to $500 default.
- Per-trade budget: `risk_pct` (default 2%) of bankroll → contracts =
  floor(budget / cost-per-contract incl. fee), min 1.
- Day stop: cumulative day P&L ≤ −`day_stop_pct` (default 10%) of bankroll →
  status HALTED until next UTC day (no new entries; exits still managed).

### Paper fill model (honest = pessimistic)
- Buy at ask; sell at bid approximated as `ask − spread` from the signal feed.
- Kalshi fee via `backtest_gate.fee(price)` charged on both sides.
- Fills recorded with the full signal snapshot at entry and exit.

### Data files (`data/bot/`)
- `bot_trades.jsonl` — one row per completed round trip (and open stubs):
  ticker, side, qty, entry/exit price+ts, fees, net P&L, entry & exit signal
  snapshots, exit reason (`flip`, `time`, `flatten`, `halt`).
- `bot_events.jsonl` — every decision including skips, with reason strings
  (e.g. `skip: flip 8200 < threshold 10000`). This is the tuning dataset.
- `bot_state.json` — mode, running/paused/halted, open plays, day P&L,
  bankroll + fetch ts, heartbeat. Written atomically (tmp+rename).
- `config.json` — thresholds, risk percentages, exit windows, live flag.
- `control.json` — GUI → bot commands with a nonce; bot acks in state.

### GUI — `/bot` screen (web.py)
- New route `/bot` (own screen, linked from the existing dashboards; uses the
  token-based skin from the July re-skin).
- Panels: status header (PAPER/LIVE badge, running/paused/halted, heartbeat
  freshness), bankroll + day P&L vs stop bar, open plays, trade history table,
  win-rate tiles (today / all-time / by exit reason), decision-log tail.
- Controls: Pause, Resume, Flatten-all. LIVE toggle rendered but locked, with
  the unlock bar displayed.
- Endpoints: `GET /api/bot/status` (state + stats), `POST /api/bot/control`.
  web.py only reads/writes the files — no direct coupling to the daemon.
  All handlers non-blocking (scanner hang history: no per-request outbound
  HTTP from web.py).

### Live unlock bar (enforced in code)
Live order placement refuses to run unless ALL of:
1. ≥100 settled paper scalps in `bot_trades.jsonl`
2. Net average P&L per contract > 0 after fees+spread across those trades
3. GUI live toggle set by the user
4. `BOT_LIVE=1` present in the environment

Until then the LiveBroker path raises and the bot stays in paper mode.
Live mode (when unlocked, later session): signed `POST
/trade-api/v2/portfolio/orders`, limit-at-ask IOC, same risk box, same
ledgers with `mode: "live"`.

## Error handling
- Feed down/stale (3 consecutive failures): no new entries; existing plays
  exit on time-window using last known prices; event logged.
- Crash/restart: state reloaded from `bot_state.json`; open plays resume.
- GUI shows stale heartbeat warning if bot silent >30s.

## Testing
- Unit: flip detection on synthetic sequences, fill/fee math, sizing and
  day-stop enforcement, config hot-reload.
- Replay harness: run the strategy over historical
  `data/whales/signal_feature_log.jsonl` to verify trigger frequency and
  produce a first tuning report before the daemon is armed.
- TDD for implementation.

## Out of scope (v1)
- Live order code path beyond the locked stub.
- Multiple simultaneous strategies (design leaves the strategy pluggable, but
  only flow-flip ships).
- Non-BTC-15m markets.
