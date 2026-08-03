# Alpha deep-dive — 2026-08-02

Data-driven search for new tradeable edge in swing_bot's (KXBTC15M) history. Read-only:
no code, config, or live-state files were changed. All scripts live under
`/tmp/claude-1000/.../scratchpad/` (throwaway, not part of the repo).

## Headline result

**No new filter or feature survived proper train/test discipline.** Two candidates
looked strong on a naive distributional split and both failed when actually
backtested end-to-end. More importantly:

**The weekday_night "live-ready" edge is not established on held-out data.** The
number currently driving the live-unlock decision (+$0.229/trade from
`bot_trades.jsonl`, n=292) is the most optimistic of three ways to measure the same
thing, and it is not reproducible under the *current* config on the most recent,
never-touched slice of history. See "Methodology note" below — this is the
single most decision-relevant finding in this report and belongs above the fold.

## Methodology

- **Data**: `data/bot/bot_trades.jsonl` (680 actual paper trades) cross-checked
  against a full-history replay of `data/whales/signal_feature_log.jsonl`
  (2026-06-30 to 2026-08-03, 1246 trades) run with `cfg_overrides=data/bot/config.json`
  in full (current config, stop-loss effectively disabled, 4c edge floor, etc.) —
  per the standing instruction, replay was never run with bare defaults.
- **Feature join**: every trade's `entry_sig` (which carries the `ts` of the
  signal row that triggered it) was matched back to
  `data/whales/signal_feature_log.jsonl` by `(ticker, ts)` with a 3s tolerance,
  recovering richer fields not stored in `bot_trades.jsonl` — `spread`,
  `distance`, `confidence`, `is_flush`, `flush_score`, `btc_vol_per_min`,
  `yes_pct`, `whale_count`, `buy_pressure`. Join rate: 1246/1246 (100%) on the
  full-history replay dataset.
- **Train/test split**: chronological, cutoff **2026-07-23 00:00 UTC**
  (train: 697 trades / test: 549 trades on the full-history replay). This date
  was picked from the commit log, not from the data — it's when the last
  execution-mechanic change landed (`e595ce8` resting-limit-entry tuning), so
  both slices run under materially the same order-fill logic. All bucket
  edges/thresholds below were computed from TRAIN only and applied unchanged
  to TEST.
- Every "promising-looking" distributional gap was additionally **causally
  backtested** by monkey-patching `bot_core.entry_blockers` in a throwaway
  script (not editing the repo) to add the candidate filter, then re-running
  the full `Bot`/`bot_replay.py` simulation end-to-end (not just deleting rows
  from the existing trade list) so downstream effects — freed capital,
  freed `max_open_plays` slots reallocating to other trades, and the
  **stateful EV-gate** (`ev_stats` accumulates online and starts blocking
  buckets once `n >= ev_gate_min_samples` with negative net_avg) — are
  captured. This matters: a filtered run isn't "the same run minus some
  trades," it's a *different run* that trains its own EV gate differently.
  Caveat that applies to both backtests below: a single replay path through
  a stateful-gate system supports "no evidence of improvement," not a strong
  causal "actively harmful" claim.

## Weekday_night's edge — the load-bearing number, checked three ways

| Source | n | net_avg/trade |
|---|---|---|
| Actual paper journal (`bot_trades.jsonl`, feeds the live-unlock gate) | 292 | **+$0.229** |
| Full-history replay, current config, continuous | 552 | +$0.075 |
| Full-history replay, TRAIN slice only (< 7/23) | 328 | +$0.109 (SE $0.113, 95% CI [-0.11, +0.33]) |
| Full-history replay, TEST slice only (>= 7/23), continuous | 224 | +$0.025 (SE $0.202, 95% CI [-0.37, +0.42]) |
| Full-history replay, TEST slice only, standalone (cold-state) run | 212 | -$0.043 |

The TEST mean of +$0.025/trade is **not statistically distinguishable from
zero** (z ≈ 0.12) and **not distinguishable from the TRAIN mean of +$0.109**
(z ≈ 0.36 on the difference) — the per-trade variance (sd ≈ $2-3, this is a
binary-ish settle-to-0-or-1 market) swamps a sample this size. The standalone
cold-state run going slightly negative is very likely because the EV gate and
pool state start empty in that run (no accumulated bucket history), not
because the edge reversed — the continuous run, which carries the real
accumulated state into the test period, is the fairer estimate and it reads
flat, not negative.

**Bottom line: this dataset cannot currently distinguish "weekday_night has a
real small edge" from "weekday_night is breakeven and the +$0.229 in the live
journal is a favorable draw."** That's not the same as "the edge is gone" —
it's "the premise the live gate is about to act on hasn't been confirmed
out-of-sample." Worth flagging before the $20 live test scales up, independent
of anything else in this report.

## Candidates investigated

For each: what was tested, TRAIN result, TEST (held-out) result, verdict.

### 1. Order-book spread at entry — RULED OUT (no variance)
`spread` in the signal log is 0.01 for 1209/1246 (97%) of trades, with only a
handful of outliers (0.001, 0.02, 0.03). There's no usable signal here — the
feature is effectively a constant in this dataset. Not testable, not a matter
of train/test.

### 2. Finer time-of-day granularity within weekday_night — RULED OUT (sign flip)
Split weekday_night by UTC hour; TRAIN showed hours 00-04Z clearly positive
(net_avg +$0.39, n=188) vs 05-12Z negative (-$0.27, n=140). Applying the same
cutoff blind to TEST: 00-04Z **flipped to -$0.087** (n=99), 05-12Z **flipped
to +$0.114** (n=125). Complete sign reversal — this is the same
"sign flips by window" pattern already ruled out for blanket fade filtering.
**Ruled out**, cite as a repeat of that pattern.

### 3. Entry price vs recent realized volatility (`btc_vol_per_min`) — RULED OUT
Terciles fit on TRAIN (weekday_night and all-pool) do not hold their rank
order on TEST — the best tercile in TRAIN becomes middling or worst in TEST
in both cuts. No stable relationship.

### 4. Consecutive win/loss streaks in the bot's own results — RULED OUT (and the naive version was a look-ahead artifact)
First pass, bucketing by exit-order streaks (i.e., "the last trade *to close*
before this one"), showed a striking pattern: after exactly one win, the next
trade closed at 85-95% win rate in both TRAIN and TEST. That looked like
real momentum — until rebuilt using only information actually available at
**entry time** (the last trade whose *exit* had happened before *this* trade's
*entry*, correctly handling the bot's up-to-3 concurrent open plays). Under
that causal definition the pattern **disappears and flips sign** between
TRAIN and TEST in every bucket (e.g. "last close was a loss, streak=1":
net_avg -$0.036 train vs +$0.53 test). The original version was measuring
information the bot's own entry decision could never have had. Worth keeping
in mind for any future streak-style feature: always define the "prior state"
as of the entry timestamp, not the exit-chronology.

### 5. Entry qty vs outcome — SUPERSEDED by #6 (confounded with price)
Qty terciles showed large-qty trades underperforming in both TRAIN (-$0.33)
and TEST (-$0.40), and per-contract-normalized correlation was negative and
stable (train -0.18, test -0.18). But qty and entry price are strongly
anti-correlated (r ≈ -0.65 to -0.69, since `qty ≈ budget / price`) — this is
almost entirely the price-band effect below wearing a different hat, not an
independent sizing signal.

### 6. Entry price band (cheap <35c / mid 35-65c / rich >65c) — RULED OUT after backtest, despite a strong, consistent, growing distributional gap
This was the strongest naive signal found. Using the price-band split already
defined in `bot_core.entry_bucket` (not a threshold I mined):

| | TRAIN net_avg | TEST net_avg |
|---|---|---|
| cheap (all pools) | +$0.108 (n=124) | **+$0.395** (n=164) |
| rich (all pools) | -$0.192 (n=208) | **-$0.418** (n=183) |
| rich (weekday_night) | -$0.029 (n=81) | -$0.389 (n=59) |

Consistent direction in both slices, and the gap *widens* on test rather than
shrinking — exactly what you'd want to see before trusting it. So it was
causally backtested: patched `entry_blockers` to reject any entry with
signal-time ask ≥ 65c, reran the full bot end-to-end.

| | overall net (baseline → filtered) | weekday_night net (baseline → filtered) |
|---|---|---|
| TRAIN | -$48.85 → **-$60.33** (worse) | +$35.59 → +$27.89 (worse) |
| TEST | -$45.54 → **-$52.42** (worse) | -$9.20 → -$19.89 (worse) |

Blocking "rich" entries made results worse in both slices, in both the
overall book and weekday_night specifically. One caveat on why the direct
comparison isn't perfectly apples-to-apples: the distributional split
bucketed on realized fill (`entry_price`), while the causal filter gated on
the signal-time ask at decision time — with limit orders, those aren't
identically the same set of trades. That doesn't rescue the finding (the
filtered arm still underperforms), but it means "this specific gate doesn't
help" is the precise claim, not "price band has zero information content."
**Verdict: no evidence blocking rich entries helps; don't ship it.** This is
the clearest illustration in this deep-dive of methodology point #3 — a real,
growing, both-directions distributional gap that still doesn't translate into
a profitable filter once reallocation and the stateful EV gate are accounted
for.

### 7. `min_edge_c` / edge-cushion calibration — RULED OUT
Computed the actual net-edge-at-win-target (`sell_low - ask - fees`, the same
quantity the live `min_edge_c` gate checks) per trade using zero-offset ranges
(matching replay's deterministic default), bucketed into terciles above the
4c floor. TRAIN best tercile (mid, +$0.26) became TEST's worst (-$0.28); no
monotonic relationship in either weekday_night or all-pool cuts. Same failure
mode as the already-ruled-out distance-to-strike test — continuous edge
cushion doesn't calibrate cleanly to outcome.

### 8. `is_flush` (existing flush-bounce detector) — RULED OUT after backtest, despite the strongest distributional gap found
`is_flush` is computed in `web.py`'s `/api/crypto/signal` handler: when spot
is notably below strike but buy pressure is strongly positive, the endpoint
floors its own `combined` signal at -0.15 to suppress aggressive NO calls —
but that floor only touches the `direction`/`confidence` fields the endpoint
returns, not the raw `whale_trend`/`momentum` the swing bot's `FlipDetector`
actually keys off. So the bot's flip-based entry logic never consults
`is_flush` and can still fire straight through a flush condition.

Distributional gap (all pools): flush net_avg -$0.123 train / **-$0.392**
test (n=248/174); no-flush +$-0.041 train / +$0.119 test — consistent
direction, growing on test, present in both weekday_night alone and the full
book, and present on both YES and NO sides (ruling out "this is just the
NO-suppression logic reappearing"). This looked like the best candidate in
the whole sweep.

Causal backtest (patched `entry_blockers` to reject any `is_flush` entry,
full replay):

| | overall net (baseline → filtered) | weekday_night net (baseline → filtered) |
|---|---|---|
| TRAIN | -$48.85 → **-$17.92** (better) | +$35.59 → +$37.49 (~flat) |
| TEST | -$45.54 → **-$91.47** (much worse) | -$9.20 → -$20.76 (worse) |

TRAIN looked good (overall loss cut by 63%, though weekday_day got *worse*
under the filter — the freed capital/slots visibly reallocated into worse
trades elsewhere). TEST reversed hard: every single pool was worse under the
filter, overall net roughly doubled its loss. **Verdict: does not survive
held-out validation — no evidence blocking flush entries helps.** Given the
stateful-EV-gate caveat above, the honest framing is "no improvement shown,"
not "actively harmful," but either way it should not ship as-is. Second
clear illustration of methodology point #3.

### 9. `confidence` (signal magnitude) — INCONCLUSIVE, not pursued further
Within weekday_night, high-confidence entries were negative in both TRAIN
(-$0.184) and TEST (-$0.131) — directionally consistent — but the same
tercile split at the all-pool level was strongly negative in TRAIN (-$0.209)
and flat in TEST (+$0.005), i.e. it doesn't generalize. `confidence` is
`|combined|*100` from `web.py`, moderately correlated (r=0.46) with
`|momentum|`, which the bot already caps via `max_entry_momentum`. Not
causally backtested — flag for a future window with a dedicated train/test
split, not a re-read of this one.

### 10. Market-implied-probability mispricing (`yes_pct/100 - price`, signed toward trade side) — INCONCLUSIVE
Only the extreme "favor" bucket (whale flow strongly agreeing with the trade
beyond what price implies) was consistently positive on weekday_night; the
"against" extreme was consistently negative at the all-pool level but not
within weekday_night specifically. Middle buckets flip. Not causally
backtested due to time. Candidate for a future window.

### 11. `|distance to strike|` (general, not fade-conditioned) — INCONCLUSIVE / weak
Near-strike tercile was best in both TRAIN (+$0.182) and TEST (+$0.269), but
the middle tercile flipped sign (train +$0.097, test -$0.332), so there's no
clean monotonic or two-bucket rule here. Distinct from the already-ruled-out
"distance-to-strike within fade trades" test (this one isn't fade-conditioned)
but shares its instability.

### 12. `|buy_pressure|` — RULED OUT
No stable rank ordering between TRAIN and TEST terciles.

### 13. `whale_count` — INCONCLUSIVE, flagged as overfitting-shaped
Middle tercile was best in both TRAIN (+$0.307) and TEST (+$0.383), both
extremes worse — consistent, but a "middle is best" 3-bucket shape with two
free edges is exactly the kind of pattern that's easy to get by chance with
this many features tested. Not causally backtested. Needs a fresh window
before being taken seriously, same as #9-11.

### 14. `exit_mins` tuning for weekday_night — no change indicated; current 2.0 is best-of-6 on held-out data
Swept `exit_mins` in {1.0, 1.5, 2.0, 2.5, 3.0, 4.0} as a genuine causal
replay (not a distributional read) — full `Bot` re-simulation with signal
rows pre-split by the same 7/23 cutoff, one independent replay per value per
slice, weekday_night bucket read off each run:

| exit_mins | TRAIN weekday_night net_avg | TEST weekday_night net_avg |
|---|---|---|
| 1.0 | +$0.131 | -$0.151 |
| 1.5 | +$0.096 | -$0.221 |
| **2.0 (current)** | +$0.109 | **-$0.043 (best of 6)** |
| 2.5 | +$0.010 | -$0.134 |
| 3.0 | +$0.040 | -$0.136 |
| 4.0 | +$0.056 | -$0.091 |

TRAIN's apparent optimum (1.0, tightest fuse) is the **worst or
near-worst** value on TEST — a clean case of a training-set optimum not
generalizing, caught before it was ever proposed as a change. All TEST
values are negative in this standalone-run harness (same cold-EV-gate bias
noted above, so treat the absolute levels as pessimistic), but *relative to
each other*, current `exit_mins=2.0` is unambiguously the best of the six
values tried. **No change to `exit_mins` is indicated.**

`flip_threshold` was not swept — out of time budget for this pass, listed
below for follow-up.

## Prioritized follow-ups

1. **Re-run the weekday_night edge check on a later, independent window**
   before scaling up the live test — the current +$0.229/trade in
   `bot_trades.jsonl` is the most favorable of three measurements and isn't
   confirmed out-of-sample. This is more urgent than any new-feature search.
2. `flip_threshold` sweep — same causal-replay method as `exit_mins`, not yet
   done.
3. `confidence`, mispricing-divergence, `whale_count`, and `|distance|` are
   each partially-consistent-but-not-clean — worth a proper causal replay
   backtest (like the price-band/flush tests above) on a **fresh** train/test
   split once more history accumulates, since this session already spent its
   one look at the current window on ~14 features.
4. If `is_flush` is revisited: the real fix implied by the mechanism (its
   suppression floor touches `combined`/`direction` but not the
   `whale_trend`/`momentum` the swing bot's `FlipDetector` reads) would be a
   code change, not a data filter — out of scope for this read-only pass,
   but worth noting since the current implementation only half-applies the
   protection it already has.
5. Consider a rolling/expanding train-test scheme instead of one fixed
   cutoff, given the whole dataset only spans ~5 weeks and every trained
   filter here suffered from small-sample noise in the held-out slice.
