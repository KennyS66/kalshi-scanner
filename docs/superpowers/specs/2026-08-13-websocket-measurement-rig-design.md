# Websocket Measurement Rig — design

**Date:** 2026-08-13
**Status:** approved for implementation
**Scope:** measurement only. No order path, no live money, no changes to the running scanner.

## Purpose

Decide, on evidence, whether a sub-second execution path is worth building.

The momentum filter is the only signal ever measured in this repo that cleared
its own transaction costs: **+0.0567/contract net of taker fees, t=+4.49, 3/3
windows, 23/32 days**, buying at the ask and holding to settlement. It was
killed by latency, not by edge:

| action delay | edge | t | windows |
|---|---|---|---|
| 0s | +0.0572 | 4.63 | 3/3 |
| 5s | +0.0362 | 2.94 | 2/3 |
| 10s | +0.0231 | 1.89 | — |
| 20s | +0.0086 | — | — |
| 60s | −0.0054 | — | — |

The problem: **that curve was measured on 5.1s-cadence data.** The 0s column is
an extrapolation from ticks never actually observed. Real action latency today
is 10-15s (5s poll + HTTP + placement), where t is 0.7-1.9.

This rig collects the data needed to replace the extrapolated 0s figure with a
measured one. It answers one question: **does the edge survive at a realistic
sub-second action latency?**

## Non-goals

- Order placement or any live trading
- Replacing the scanner's 5s Coinbase poller
- Dashboard or UI work
- Improving the momentum signal (Arm B is exploratory only; see Analysis)

## Why isolated

A new standalone service with its own systemd unit, writing only to
`data/wsrig/`. It reads nothing from and writes nothing to the scanner's state.

The scanner has cost real time twice in the last week to GIL contention and
unbounded buffers. A websocket client pushing tens of messages a second into
that same process is exactly the wrong place for it. The cost of isolation is
some duplicated feed code if Phase 2 proceeds, which is cheap against the risk.

## Architecture

| Component | Job | Origin |
|---|---|---|
| `ws_spot.py` | Coinbase WS ticker → spot ticks | adapt `daedalus/data/external/coinbase_ws.py` (85 lines) |
| `ws_kalshi.py` | Kalshi WS → book for active KXBTC15M | adapt `daedalus/venue/kalshi_ws.py` (297 lines) |
| `market_tracker.py` | REST poll (60s) for the active market; subscribe/unsubscribe on roll | new |
| `settlement.py` | after expiry, poll Kalshi REST for `result` | new |
| `tape.py` | gzipped append-only writer, hourly rotation | new |
| `wsrig_main.py` | asyncio supervisor | new |

Adapted rather than imported: daedalus's chain pulls in `structlog` and
pydantic models we do not want, and `api.py` already has RSA request signing.
The daedalus implementations already handle signed-header auth, exponential
backoff reconnect, heartbeat watchdog, and **sequence-gap detection**, which is
the part that matters most here — a silent gap would corrupt the decay curve.

Production WS endpoints:
- Coinbase `wss://ws-feed.exchange.coinbase.com`, `ticker` channel
- Kalshi `wss://api.elections.kalshi.com/trade-api/ws/v2`, `orderbook_delta` + `ticker`

`websockets` must be added to the venv. `cryptography` (48.0.0) is present.

## Data captured

Every record carries **both a local receive timestamp and the exchange
timestamp**. The entire question is latency; conflating the two would silently
invalidate the measurement.

- **spot**: `ts_local`, `ts_exchange`, `price`
- **kalshi**: `ts_local`, `ts_exchange`, `ticker`, `yes_bid`, `yes_ask`, `no_bid`, `no_ask`, `seq`, depth
- **gap**: sequence-gap events, recorded explicitly so corrupted windows are excluded rather than averaged in
- **settlement**: `ticker`, `result`, from REST after expiry

`orderbook_delta` is captured, not just top-of-book: depth is cheap at 1-2
markets and Phase 2 would need it.

Volume is small — median **1** KXBTC15M market open at a time (p95 1, max 2) —
roughly 1 GB/week gzipped against 834 GB free.

## Analysis — two arms

### Arm A — pre-registered primary

Reconstruct a **5s-sampled** series from the tape and compute momentum
*identically to production* (`web.py:827-833`: two-point endpoint slope over
the trailing 90s). Apply the original trigger: `|mom| >= 30` and
`mins_left` in 5-11. Take the side momentum points to, buy at the ask.

The only variable versus the original study is that the fill price at δ is now
**observed** rather than extrapolated. That isolates latency, which is the
question.

Measure edge at δ = 0, 250ms, 500ms, **1s (primary)**, 2s, 5s, 10s, 20s.

### Arm B — exploratory, cannot decide Phase 2

Full-resolution spot with a noise-robust estimator (regression slope over the
window), threshold re-tuned on window 1 only and validated on 2 and 3. Reported
separately and labelled exploratory.

**Why the split:** the production estimator uses only the oldest and newest
sample in the 90s window. At 5s cadence those are REST snapshots; at 20+ msg/s
they are individual trades, so endpoint jitter enters the numerator directly
and the trigger fires at a different rate on different moments. Without Arm A,
a change in measured edge could be entirely the estimator rather than the
latency — we would have disproved the wrong thing.

## Acceptance bar — fixed before any data is collected

Registered in `hypothesis_gate.py` before capture starts, so the bar is
enforced by the tool rather than by discipline a week later. The harness fixes
the protocol, verdicts on the **taker** basis (which always fills — correct
here), and hashes the predicate so a failed rule cannot be quietly renamed.

At **δ = 1s**, Arm A, net of the one-way taker fee `0.07·p·(1−p)`:

- n ≥ 30 events per window across **3 disjoint chronological windows**
- positive sign in **all 3**
- **|t| ≥ 2.64** — the harness's current Bonferroni bar, five comparisons
  already recorded on this data, one of which is "follow momentum |mom|>=30"
- magnitude ≥ **+0.02/contract**
- survives **drop-two-best-days**

δ = 1s is the **single** primary endpoint. All other δ are descriptive; testing
each as a hypothesis would create seven new comparisons.

One event per market (first trigger). Counting trigger *ticks* would overstate
n by roughly 15× — 491 trigger-ticks/day against 33.5 distinct markets/day.

Result is valid **for 1-contract sizing only**. Deeper size needs the depth
data the tape carries but this bar does not test.

### Kill criterion

**If the bar is not met, the idea is dead and we write it up.** No re-slicing to
a filter that passes, no relaxing the threshold, no promoting Arm B. That
failure mode has cost this repo more than any other.

## Risks

**Retired empirically:**
- *Clock skew* — NTP synchronized, chrony active, system time 0.47 ms fast. δ ≥ 250 ms is safe.
- *Disk* — 834 GB free against ~1 GB/week.
- *Feed volume* — 1-2 markets at a time.

**Live:**

| Risk | Mitigation |
|---|---|
| Estimator does not transfer to sub-second data | Arm A reproduces the production estimator exactly |
| Isolated rig has no settlement source | `settlement.py`, Kalshi REST `result` after expiry — authoritative, not inferred from `sign(distance)` |
| One week is one regime (original was 32 days) | Record realized vol during capture; if the week lands in an outlier decile, extend rather than conclude |
| Event independence | One event per market; keep day-level checks and drop-two-best-days |
| Silent rig death mid-capture | `health_check.py` entry on tape mtime and expected message rate |
| Rig repeats the scanner's memory faults | Bounded buffers, hourly tape rotation, footprint entry in health_check |
| Confirmation pressure after a week invested | Bar registered in hypothesis_gate before capture |
| Kalshi WS auth, connection or subscription limits | Phase 0 confirms |

## Phasing

- **Phase 0** (~half day): 1-hour smoke capture. Verify the Kalshi payload carries both asks, measure real message rate, confirm clock and gap handling. Kills wrong assumptions before a week is committed.
- **Phase 1** (~1.5 days build, then 5-7 days passive capture): rig under systemd. The n≥30 bar needs ~3 days at 33.5 markets/day; 5-7 gives margin.
- **Phase 1b** (~1 day): Arm A and Arm B analysis, go/no-go against the bar.
- **Phase 2**: execution path — only if the bar is met, and it gets its own spec.

**Total to a decision: ~2.5 days of work plus a week of waiting.**

## Expected outcome

Arm A is a genuine falsification test and **the most likely result is that it
kills the idea.** The known curve drops to t = 0.7-1.9 at 10-15s; sub-second
execution has to recover that entirely, and the 0s figure it would need to
recover to was never directly observed. A clean kill is a good outcome — it
closes the last open trading thesis in this repo for the cost of a week of
passive collection.

## Open questions for Phase 0

1. Does Kalshi's `ticker` channel carry both `yes_ask` and `no_ask`, or is
   `orderbook_delta` required to derive them?
2. Actual message rate per market, to size rotation.
3. Does the WS spot price series match the REST `price` field the original
   study used, or is it a different field (last trade vs mid)?

## Phase 0 results — 2026-08-15 ~04:05-04:12 UTC

Answered by two read-only probes (a REST schema probe and a 75-second
authenticated WS capture on `KXBTC15M-26AUG150015-15`), *before* any long
capture was committed. This is the outcome Phase 0 was designed to produce:
**every live-API assumption baked into the plan's reference code was wrong**,
and the tests could not have caught any of them — the suite was 464/464 green
while four separate components would have silently captured nothing.

### The four broken assumptions (REST, all verified live)

| Assumed | Actual | Consequence if unfixed |
|---|---|---|
| `market["close_ts"]` (epoch int) | field does not exist; it is **`close_time`**, ISO8601 (`"2026-08-15T07:30:00Z"`) | `active_btc15m` returns `[]` forever — tracker completely dead |
| settled state is `status == "settled"` | it is **`"finalized"`** (`result: "yes"`, plus `expiration_value` as the settlement price; no `settled_time`) | `settle_record` never matches — zero settlements captured, `pending` grows forever |
| `get_markets(status="open", limit=200)` finds the series | **never returns KXBTC15M** — 0 hits across ~12,000 open markets, 12 pages. Requires the **`series_ticker`** parameter | tracker subscribes to nothing |
| prices are cent-denominated ints (`yes_bid`) | **`yes_bid_dollars` etc., dollar-denominated STRINGS** (`"0.9030"`) | `parse_book` yields `None` for every price; the `/100` would also be 100x wrong |

Also note the status vocabulary is **two vocabularies**: the query parameter
`status=open` is a filter keyword, while the returned object's own `status`
field reads `"active"`. Future markets are `"initialized"`; settled ones are
`"finalized"`. Do not compare a query keyword against an object field.

### Q1 — does `ticker` carry both asks? **NO. The gate fails.**

The `ticker` channel body carries **only the yes side**:
`price_dollars, yes_bid_dollars, yes_ask_dollars, yes_bid_size_fp,
yes_ask_size_fp, volume_fp, open_interest_fp, dollar_volume,
dollar_open_interest, last_trade_size_fp, ts, ts_ms, time, market_ticker,
market_id`. There is no `no_bid`/`no_ask` field of any spelling.

The plan's gate said: *"if both asks present is under 100%, the `ticker`
channel is insufficient and `orderbook_delta` must be reconstructed into
top-of-book."* It is 0%. **However, full book reconstruction is probably not
required**, because for a Kalshi binary the two sides are one book:

    no_ask = 1 - yes_bid        no_bid = 1 - yes_ask

(a resting YES bid at 0.90 *is* a NO offer at 0.10). Sample check:
yes_bid 0.9030 / yes_ask 0.9060 → no_bid 0.0940 / no_ask 0.0970, spread
preserved. Derived prices must be **labelled as derived**, never presented as
observed quotes. `orderbook_snapshot` carries genuine two-sided depth
(`yes_dollars_fp` and `no_dollars_fp`, each a list of `[price, size]`) and
could validate the identity at analysis time — but only if the capture design
subscribes to `orderbook_delta`, since the snapshot is that subscription's
init frame and cannot be requested on its own (see the Q2 decision below).

### Q2 — message rate: **~15x the spec's estimate, and 99.85% of it is depth**

Measured on one market, 4 minutes before its close (likely near peak activity):

| | rate | 7-day projection |
|---|---|---|
| all channels | **685.8 msg/s** | ~415M records, **~15 GB gzipped** |
| `orderbook_delta` alone | 684.7 msg/s (99.85%) | — |
| `ticker` alone | **0.99 msg/s** | ~0.6M records, **~27 MB gzipped** |

The spec's "roughly 1 GB/week" and "depth is cheap at 1-2 markets" are both
falsified — depth is the entire cost. Disk is not the binding constraint
(834 GB free), but 415M records materialised in memory is: `verify_tape` and
the Task 10 analysis both load the tape, and a 15 GB tape is not loadable that
way on this box (the scanner has an RSS-ratchet history on the same machine).

**`orderbook_snapshot` is NOT independently subscribable** — probed
2026-08-15 ~04:47 UTC: subscribing to `["orderbook_snapshot"]` alone is
rejected with `{"code": 8, "msg": "Unknown channel name"}`. It arrives only as
the `seq: 1` initialisation frame of an `orderbook_delta` subscription (same
sid). A `["ticker"]`-only subscription delivers 1 msg/s and no book data at
all. So "snapshots without the firehose" is not a thing the API offers
directly.

**Open design decision — must be settled before the long capture:**
1. *ticker-only* (~27 MB/week). Sufficient for the acceptance bar as written:
   the bar is explicitly 1-contract sizing, and the `ticker` body already
   carries `yes_bid_size_fp`/`yes_ask_size_fp`, which is exactly what proves a
   1-contract fill was available at the quoted ask. Cheapest and safest.
   Cost: the `no_ask = 1 - yes_bid` identity is never validated against
   observed two-sided depth (it is an exact property of Kalshi binaries, not
   an empirical guess, so this is a small cost).
2. *ticker + duty-cycled `orderbook_delta`*. Because the snapshot is the
   subscribe-time init frame, subscribing to `orderbook_delta`, taking the
   snapshot, and immediately unsubscribing yields a full two-sided depth
   snapshot for a brief burst of deltas. Repeat at whatever cadence is wanted
   (e.g. once per market roll) to get periodic real depth *and* an
   independent check on the identity, at a small fraction of the firehose.
   This is the achievable version of what "ticker + periodic snapshots" was
   meant to be; it costs some subscribe/unsubscribe machinery.
3. *everything* (~15 GB/week, 686 msg/s sustained). Only justified if Phase 2
   depth modelling is already committed — and it is not; Phase 2 is gated on
   this measurement passing first. Note this also cannot be trimmed by
   discarding deltas client-side: that saves disk but not network, parse, or
   CPU, and CPU contention on this box is a known hazard.

Recommendation: **option 1**, with option 2 if an independent check on the
identity is wanted cheaply. Whichever is chosen, the `ticker` body's
`yes_bid_size_fp`, `yes_ask_size_fp`, and `price_dollars` must be written to
the tape — they are free, and they are unrecoverable once the capture is over.

### Other WS facts confirmed

- Host `wss://api.elections.kalshi.com/trade-api/ws/v2` **works**; the plan's
  URL and the RSA-PSS `_auth_headers` signing path are both correct.
- One `subscribe` naming two channels allocates **two sids** (ticker→1,
  orderbook_delta→2), each with its own `seq` run. Per-sid `SeqTracker` is
  correct. A gap record's `sid` therefore identifies a *channel*, not a market.
- **`ts` has different types on different channels**: int epoch *seconds* on
  `ticker` (`1786767099`), but an ISO8601 *string* on `orderbook_delta`
  (`"2026-08-15T04:11:38.535397Z"`). Writing `body["ts"]` straight to the tape
  mixes int and str in one column. **`ts_ms` is a consistent int-millis on
  both** and is the field to use.
- `orderbook_delta` carries no bid/ask at all — it is a per-level
  `(price_dollars, side, delta_fp)` mutation, meaningless without book state.

### Q3 — Coinbase WS vs REST spot series: **YES, they are the same series**

Probed 2026-08-15 ~04:16 UTC, 45s WS capture against REST before and after:

    REST /products/BTC-USD/ticker  price = 63035.04
    WS   ticker channel            price = 63035.04     |diff| = 0.00

The WS `ticker` message carries `price` plus `best_bid`/`best_ask` separately,
and its `price` field tracks REST `price` exactly — both are **last trade**
(the sample shows `price == best_bid` with `"side":"sell"`, i.e. a sell that
hit the bid). The momentum study's series is therefore reproducible from the
WS feed with no transformation. Caveat: at the observed $0.01 spread, last and
mid are indistinguishable, so this sample cannot separate *those* two — it
does not need to, since WS and REST agree exactly.

**Measured spot feed characteristics** (these feed directly into the decay
analysis, which is a latency measurement):

| | measured |
|---|---|
| ticker rate | **1.58 msg/s** (~0.95M records / 7 days) |
| feed lag `local_recv - exchange_ts` | **p50 39 ms, p95 409 ms** |

The p50 lag of 39 ms is comfortably inside the δ=1s primary endpoint. The p95
of 409 ms is not negligible relative to the δ=250 ms and δ=500 ms descriptive
points — those two columns will be partly measuring Coinbase's own delivery
jitter rather than action latency, and should be read with that in mind.

**This measurement invalidates a default in the current code:** `verify_tape`'s
`DEFAULT_SPOT_RATE_HZ` was set to 2.0 with a 0.9 tolerance, i.e. a floor of
1.8/s — but the real rate is **1.58/s**, so a perfectly healthy tape would be
reported NOT TRUSTWORTHY. The rate check exists to catch catastrophic
degradation (a dead or 10x-degraded feed), not to police normal variation, so
the floor belongs well below the observed rate — on the order of 0.5/s — and
the observed rate should be re-derived from the first real capture rather than
guessed.

Combined ticker-only volume (Kalshi ~0.6M + spot ~0.95M ≈ **1.5M records /
week**) confirms option 1 in the Q2 decision is essentially free.

### Lookahead (tracker) — the parked Task 5 risk, now resolved

`series_ticker=KXBTC15M&status=open` returns **exactly one** market (confirming
the spec's "median 1 open"). Future markets exist but carry status
`"initialized"` and are **excluded by the `status=open` filter**, so the
lookahead as designed could never have fired. Seeing them requires querying the
series without the restrictive status filter and bounding by `close_time`
locally — note that unfiltered series query returns ~24h of future markets, so
the lookahead bound becomes load-bearing.
