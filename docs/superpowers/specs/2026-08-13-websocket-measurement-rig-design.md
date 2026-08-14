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
