# settle_bot — directional hold-to-settlement strategy — design

A second, independent paper strategy. Enters on the **level** of `sig_combined`,
buys the side it points to as a maker, and **holds to settlement**. No targets, no
stops, no time exit.

It exists because the signal the swing bot has been trading for a month turns out to
carry information — just not in the way the swing bot uses it.

## The finding that shapes this design

Measured 2026-08-04 over **1,452 distinct BTC15M markets** (2026-06-30 → 2026-08-04;
the feature log has no usable `yes_ask`/`no_ask` before 06-30). Settlement label is
`sign(distance)` at each market's last observed tick, which sits at expiry
(`mins_left` p50 = 0.1) and **agrees with `trade_grader`'s independent settlement on
365/367 markets (99.5%)**.

Rule tested, with no fitted parameters: at ~10 minutes left, if `sig_combined >= +10`
buy YES at `yes_ask`; if `<= -10` buy NO at `no_ask`; hold to settlement.

| check | result |
|---|---|
| gross edge (maker fill, zero fee) | **+0.0406 / contract, t = 2.91** (n=920) |
| net edge if taker fee paid | +0.0270, t = 1.94 |
| three equal time windows | **+0.0490 / +0.0410 / +0.0307 — positive 3/3** |
| per-day | **20 / 26 days positive** |
| excluding the two best days | +0.0316 ± 0.0151, **t = 2.10** |
| entry-time sensitivity (thr 10) | 3m +0.007 · 5m +0.022 · 7m +0.028 · 9m +0.028 · 11m +0.024 · 13m +0.014 |
| per session | weekday_day +0.050 · weekday_night +0.037 · weekend_day +0.036 · weekend_night +0.038 |

Two features of that table drive the design.

**The entry-time curve is a smooth hump peaking at 7–9 minutes**, decaying at both
ends. Too early the signal hasn't formed; too late it is already priced. An artifact
would spike at whatever value was first tried; this does not.

**All four sessions are positive with similar magnitude.** The swing bot's edge, such
as it was, lived only in `weekday_night` while the other three were solidly negative.
This is a different phenomenon.

### Why the same signal failed everywhere else

- **swing_bot** trades the *flip* — the change in the signal — and scalps out within
  minutes. It never holds to settlement. Net −0.37/trade over 693 trades.
- **daedalus-mm** used the same scanner direction feed as a market-making quote skew
  and zeroed it (`_WHALE_BIAS_MAGNITUDE = 0.0`) after it bled $30+. As a skew on a
  resting two-sided quote it was exposed to adverse selection, which dominates
  (−0.25/contract live, mechanism confirmed by exit-price distribution).

Neither tested the signal's **level, held to settlement**. That is the gap.

### What is NOT established

- **One regime.** Five weeks, July 2026. Nothing here says it survives a different
  volatility or trend environment.
- **Mild selection.** `sig_combined` was chosen after eyeballing three signals; its
  monotonic bucket gradient, 3/3 windows and 20/26 days are what make it credible
  anyway, but it is not a pre-registered hypothesis.
- **Fill rate is unmeasured.** The whole edge depends on maker fills. At taker fees
  t≈1.9 and this is not worth running. See Risks.

## Scope

**In:** a new `settle_bot.py`, its own journal/state/config, its own systemd user
service, unit tests, and a boot reconciliation check.

**Out:** any change to `swing_bot.py`, `bot_core.py` gates, the dashboard, or the
live-trading path. `settle_bot` is **paper only** in this iteration — it constructs
`PaperBroker` unconditionally and has no live code path at all. Live comes later, if
the forward test earns it.

## Architecture

Separate module, separate everything:

```
settle_bot.py            strategy loop
data/settle/
  settle_config.json     thresholds + window
  settle_state.json      open positions, heartbeat, day
  settle_trades.jsonl    one row per settled position
  settle_events.jsonl    enter / skip / fill / settle / reconcile
deploy/settle-bot.service
```

**Why not a mode inside `swing_bot`.** The swing bot is almost entirely exit
machinery — targets, stretch, scale-out, time fuse, stops — and this strategy has no
exits at all. Bolting it on would mean disabling most of the host. More importantly
its rows would land in `data/bot/bot_trades.jsonl`, which `bot_core.session_gate_stats`
(the live-unlock gate) and `bucket_stats` (the EV gate) both read. A second strategy
writing into the file that authorizes live trading for the first one is the exact
contamination class fixed in `a838f93`. Separate journal, structurally.

It reuses `bot_core.session_tag` and the `:9050` signal feed. It does not import
`swing_bot`.

## Entry rule

```
enter when:
    |sig_combined| >= entry_threshold      (default 10)
    5.0 <= mins_left <= 11.0               (default window)
    market not already held or attempted
    status == "ok" and both asks present
side = YES if sig_combined > 0 else NO
qty  = 1                                   (flat, always)
```

The window is the **flat top of the hump, not its peak** — 7–9 minutes is the
maximum, so a [5, 11] window is deliberately wider than the best-fitting choice. Same
reasoning for the threshold: 10 rather than 20, which scored higher in-sample but
broke down in the third window (−0.037).

`qty = 1` flat. Scaling with signal strength is a fitted parameter and buys nothing
the forward test needs.

## Maker-only, and skip if unfilled

Load-bearing. Kalshi's maker fee is **zero** (confirmed from real fills, 2026-08-03);
taker fees cut the edge from t≈2.9 to t≈1.9.

Rest a **buy limit at the current best bid for the chosen side** — joining the bid,
never crossing. Concretely, for YES that is `yes_ask - spread` (the same quantity
`bot_broker.sell_price_c` derives), floored at 0.01; for NO the mirror. Posting at the
bid is what makes the fill a maker fill and the fee zero; posting any higher risks
crossing and paying taker.

If it has not filled when `mins_left` leaves the window, **cancel and take nothing** —
never chase to market. A missed fill costs nothing; a chased fill converts a maker
edge into a taker loss.

Note this means the measured +0.04 is *conservative* as an entry price: the historical
figure assumed paying the **ask**, and a bid fill is better by the spread. It is
optimistic as a *fill assumption*, which is the risk below.

Recording every attempt (filled or not) in `settle_events.jsonl` is what makes the
forward test able to measure the realised fill rate — the one number this design
most needs and does not have.

## No exit

There is deliberately no exit logic. A position is held until its market settles.

**Settlement detection**, stated precisely because a live daemon cannot use
`trade_grader`'s post-hoc "final observed tick": an open position resolves when its
ticker stops appearing in the feed (the market has rolled) **or** its last seen
`mins_left` reached ~0. At that point the side is decided by the sign of `distance` on
the **last tick observed for that ticker**, which is the same quantity `trade_grader`
uses and which cross-checked at 99.5% against its independent record. P&L is booked as
`1 - entry_price` if the held side won, else `-entry_price`.

A position whose ticker vanishes without a near-expiry tick (a data gap) is booked as
`unresolved` and excluded from edge statistics rather than guessed at. The count of
unresolved positions is reported, since a large one would invalidate the measurement.

This is the main defence against overfitting — there are no exit parameters to tune,
which is where the swing bot's entire loss lives.

## Write ordering

Today's session found the same defect three times in `swing_bot` (`_exit`,
`_scale_out`, `_fill_pending`): state mutated before the durable record was written,
so a failed append lost a trade permanently. `settle_bot` follows the corrected order
everywhere:

1. irreversible action (broker fill)
2. **append the journal row**
3. mutate state
4. emit the event

The one deliberate exception is the same as `_enter`'s: when *opening* a position the
state record is written before the event, because the buy has already happened and
losing the position matters more than losing an audit row.

Boot runs a reconciliation mirroring `_reconcile_open_plays` — `enters - settles -
open` must equal the stored baseline, warning on any deviation in either direction.

## Testing

Unit tests, TDD, each watched fail first:

- entry fires inside the window and at threshold; does not fire outside either
- side selection follows the sign of `sig_combined`
- `qty` is always 1
- an unfilled resting order is cancelled at window exit and never chased
- settlement books `1 - entry` / `-entry` correctly for both sides
- a failed journal append leaves no half-applied position
- boot reconciliation warns on a positive and a negative gap
- **a settle_bot row never appears in `data/bot/bot_trades.jsonl`** (the contamination
  guard, asserted directly)

Plus a replay check pinning the implementation to the measurement that justified it:
running the entry rule over the historical feature log must produce an edge in the
**+0.02 to +0.05 / contract** band, positive in all three time windows. It will not
reproduce +0.0406 exactly — that figure came from a single observation per market at
~10 minutes, whereas the live rule takes the first qualifying tick anywhere in [5, 11].
A result outside that band means the implementation does not match the rule that was
measured, and is a bug rather than a new finding.

## Deployment

Own systemd user service, `deploy/settle-bot.service`, mirroring
`kalshi-scanner.service`: `Restart=always`, `RestartSec=15`,
`StartLimitIntervalSec=0`, logs appended to `logs/settle_bot.log`. Independent of the
scanner unit so either can be restarted without disturbing the other — though it does
depend on `:9050` being up for the feed, and retries until it is.

## Risks

**Fill rate is the whole ballgame.** If maker fills are rare or arrive only when the
market has already moved (adverse selection — precisely what killed daedalus-mm), the
realised edge will be far below +0.04 and possibly negative. The forward test measures
this directly by logging every attempt. **This is the number that decides whether the
strategy is real**, and it cannot be answered from historical data.

**One regime.** Five weeks. A trend/vol change could remove the effect entirely.

**Adverse selection on the resting order.** Being a maker means being lifted by
someone who knows more. daedalus lost −0.25/contract to exactly this. The mitigating
difference is that daedalus quoted *two-sided* continuously and had to exit into a
gapping book, whereas this rests one-sided, directionally, and never needs to exit —
settlement resolves it. That is a genuine structural difference, not a hope, but it is
untested.

**Kill criteria, stated in advance:** if after 200 filled positions the realised edge
is below +0.01/contract, or the fill rate is under 20%, the strategy is dead and gets
turned off rather than tuned.
