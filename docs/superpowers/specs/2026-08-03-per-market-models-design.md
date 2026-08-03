# Per-market model separation — design

**Date:** 2026-08-03
**Status:** design for review
**Brief (Kenny):** "figuring out to seperate out models on each market space
we are trying to beat"

## What the bot trades today

One market: `KXBTC15M`, BTC 15-minute strike contracts. `swing_bot.fetch_signal`
polls exactly one endpoint (`/api/crypto/signal`), which hard-filters on
`"KXBTC15M" not in ticker` (`web.py:614`). Everything downstream — one
`config.json`, one `bot_trades.jsonl`, four session pools — assumes that
single market implicitly. Nothing is per-market because there has only ever
been one market.

Adjacent infrastructure already scans more (`alpha.py` covers NBA/MLB/NHL/NFL,
`crypto_dashboard.py` covers BTC/ETH/SOL/XBT), but none of it is wired to
trading.

## Which market spaces are actually viable — measured, not assumed

Live whale flow and distinct-ticker counts sampled 2026-08-03:

| Family | Whale prints | Live tickers | Verdict |
|---|---|---|---|
| `KXBTC` (incl. 15M) | 103 | 2 | current market |
| `KXBTCD` (BTC daily) | 31 | 4 | **the one realistic second market** |
| `KXETH` | 9 | 2 | too thin to gate on |
| `KXDOGE`/`KXXRP`/`KXHYPE`/`KXSOLD`/`KXBNB` | 1–5 each | 1–2 | not viable |
| Sports (`KXMVE*`, `KXMLBHR`, …) | 19 + long tail | 40 | structurally different (below) |

**Sports is not a near-term target**, and it is worth writing down why so it
isn't revisited casually. The entire signal stack is `whale_trend` + `momentum`
+ `spot`-vs-`strike`. A basketball market has no spot price, so
`spot`-vs-`strike` has no analog, `momentum` has no underlying to measure, and
the flip detector — which fires on whale flow *aligning* with spot momentum —
degenerates to whale flow alone, a signal already ruled out on its own
(see the whale_trend dead-end in `giveback-loss-pattern-time-rolled-exits`).
Trading sports means a genuinely new model, not a re-parameterised one.

**BTC daily is different in cadence, not in kind** — same spot, same whale
feed, same strike structure, same flip logic. That makes it the only
market where "separate the model" means "retune the same model," which is
the cheap and honest first step.

## The finding that drives the design: the gate does not transplant

The live-unlock gate is 100 settled trades + positive net avg **per session**,
four sessions. At `KXBTC15M`'s observed rate (680 trades in ~18 days, ≈38/day)
that is already slow — weekday_night took 292 trades to get here.

BTC daily runs ~4 live strikes resolving **once a day**. Optimistically that is
~4 trades/day, ~10× slower. Four sessions × 100 trades = 400 trades ≈ **100
days** before a single session could unlock. Worse, the partition is
meaningless: a market that spans a whole UTC day does not belong to
`weekday_night` — `session_tag(entry_ts)` would just record whichever hour the
entry happened to fire in, splitting one homogeneous population four ways for
no reason.

**Session partitioning is a property of the 15-minute market's cadence, not a
universal.** Any per-market design that copies the 4-way gate onto every market
is wrong on both statistics and semantics.

### Consequence: the gate becomes per-market, with a per-market partition

Each market declares how its evidence is partitioned:

```python
"btc15m": {"partition": "session"},   # 4 sub-gates, as today — unchanged
"btcd":   {"partition": "none"},      # one gate over the whole market
```

`bot_core.session_gate_stats` already takes trades and returns
`{key: {n, net_avg, ok}}`; it gains the partition function rather than
hardcoding `session_tag`. `partition: "none"` yields a single bucket. The
100-trade floor stays — the bar for risking money should not fall just
because a market is slow; a slow market simply takes longer to earn live
status, which is the correct answer.

## Architecture: strategy-as-plugin over a shared core

The industry research (`docs/superpowers/research/2026-08-02-industry-research.md`
§1) is unambiguous, and matches what this codebase's own history argues for:

> the isolation boundary that matters is *parameters and signal logic*
> (per-market, independently tunable, independently gated), not
> *infrastructure* (one execution path, one risk/kill-switch layer, one
> logging schema, one replay harness).

> Building N fully separate bots per market is the anti-pattern every source
> implicitly warns against — fixes to fill-simulation, gating, or logging then
> have to be replicated N times and drift.

That warning is not abstract here. This repo has already been burned by
exactly this class of bug: live and replay independently implementing fill
logic (§4 of the same report), and — twice today — a second definition of
something drifting from the first. It also already ran two competing bots and
turned one off for splitting attention and CPU
(`daedalus-disabled-swing-bot-focus`). **One process, one execution path, many
markets** is both the researched answer and the one this project has already
paid to learn.

### What stays shared

The kill-switch and `stop.sh`, `LiveBroker`/`PaperBroker` and the live-unlock
mechanism, the fill/fee simulation, `bot_trades.jsonl`'s schema, the replay
harness, `/api/bot/series` and the dashboard, day-roll and deadman.

### What becomes per-market

Signal source, strategy parameters, EV buckets, gate + live toggle, pool
bankroll, and the calibrated sell ranges.

## Concrete shape

### 1. A market registry

New `markets.py`, the single source of truth:

```python
MARKETS = {
  "btc15m": {
     "label": "BTC 15m",
     "match": lambda t: "KXBTC15M" in t.upper(),
     "signal_url": "/api/crypto/signal",
     "partition": "session",
     "enabled": True,
  },
  "btcd": {
     "label": "BTC daily",
     "match": lambda t: t.upper().startswith("KXBTCD"),
     "signal_url": "/api/crypto/signal_daily",   # to build
     "partition": "none",
     "enabled": False,        # ships disabled; paper-only until gated
  },
}
```

`market_of(ticker)` resolves a ticker to its key and is the *only* place that
mapping exists — the same discipline that put `session_tag` server-side in
`/api/bot/series` rather than reimplementing it in JS.

### 2. Config becomes per-market, with a shared base

```jsonc
{
  "base": { /* today's ~35 keys, unchanged */ },
  "markets": {
    "btc15m": {},                                  // inherits base as-is
    "btcd":   {"min_entry_mins": 90, "exit_mins": 30,
               "flip_threshold": 3.0, "max_entry_momentum": 60}
  }
}
```

Inheritance matters because most keys (`risk_pct`, `day_stop_pct`, stops) are
risk policy and should not silently diverge per market, while the timing keys
*must*: `min_entry_mins: 4.0` and `exit_mins: 2.0` are minutes-before-expiry
values that are meaningless on a market with a 24-hour life.

A migration reads today's flat config as `base` with a single `btc15m` entry,
so existing files keep working untouched.

### 3. `market` becomes a first-class field

Written on every trade and event; `bot_status_payload`/`bot_series_payload`
group by it; pools key on `(market, partition_key)`. Existing rows lack the
field and are backfilled as `btc15m` at read time via `market_of(ticker)` —
which works because every historical ticker is a `KXBTC15M`.

### 4. The loop iterates markets

`Bot.tick` fetches each enabled market's signal and calls the existing
`_manage(sig)` per market, with `self.cfg` resolved per market. `_manage` is
already parameterised on `sig` and `cfg`; it needs no per-market branching —
which is the test that this decomposition is right.

## Staging — three steps, each shippable and verifiable

**Step 1 — make the implicit market explicit (pure refactor, zero behaviour
change).** Add `markets.py`, write `market: "btc15m"` on trades/events, add
the config `base`/`markets` shape with one entry, key pools on
`(market, session)`. Verified by: the full replay over the 46-day log
produces **bit-identical** output to before, and all tests pass. No new
market, no new risk. This is most of the work and carries none of the danger.

**Step 2 — add the BTC-daily signal endpoint and run it paper-only.** New
`/api/crypto/signal_daily` mirroring the existing builder with the daily
ticker filter and hour-scale `mins_left`. `btcd` enabled, `partition: "none"`,
paper. Accumulates evidence toward its own gate while `btc15m` is untouched.

**Step 3 — judge `btcd` on its own gate,** the same way `weekday_night` was
judged: 100 settled, positive net avg, and — per the significance caveat in
`dual-100-trade-gate` — a held-out slice that is actually distinguishable from
zero before any live money.

**Do not start Step 2 before Monday's $10 live test is done and read.** One
change at a time to a system about to touch real money.

## Two things this unlocks that are worth naming

**A per-market gate makes the "is this edge real" question answerable per
market** instead of pooling dissimilar populations — the same reason the 4-way
session split beat the original 2-way one.

**The favorite-longshot bias is a per-market, untested alpha hypothesis.**
Whelan's Kalshi paper (research report §3.1) finds contracts priced 5–20¢ win
only ~2–12% and 80–95¢ contracts win ~96–98% — and **explicitly excludes
Kalshi's hourly-reset crypto markets**, i.e. exactly the ones this bot trades.
That is a pricing-level mispricing claim, structurally different from the
entry-timing hypotheses the alpha deep-dive ruled out, and it is directly
testable against `bot_trades.jsonl`'s `entry_price` and outcomes — per market.
Worth a dedicated investigation regardless of whether this design is built.

## Risks

- **Sample dilution.** Splitting attention across markets slows evidence
  accumulation on `btc15m`, the only market with a real edge candidate. Step 1
  is refactor-only precisely so this cost is not paid until Step 2 is a
  deliberate choice.
- **Config sprawl.** ~35 keys × N markets is a lot of surface. The
  `base` + per-market-override shape exists to keep the diff small and make
  every deviation from base explicit and reviewable.
- **Attention split** — the concrete reason daedalus-mm was turned off. One
  process and one dashboard keeps this from becoming a second bot to babysit;
  if `btcd` ever needs its own process, that is the signal to stop and
  reconsider rather than to spawn it.
- **`mins_left` semantics.** Timing keys silently meaning "minutes" everywhere
  is the most likely source of a subtle bug on a daily market. Step 2 should
  add an explicit unit assertion rather than trusting the config.
