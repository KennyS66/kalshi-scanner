# hypothesis_gate — a falsification harness for trading ideas — design

A single reusable harness that takes an entry-rule hypothesis and tries to **kill** it,
returning SURVIVED or DIED with the evidence. Ideas still come from a human or an LLM;
what is automated is the discipline, not the guessing.

## Why this, and why not a tuner

This repo has already tried automated hypothesis search. `bot_tuner.py` swept strategy
knobs with `itertools`, then suggested whichever combination placed "top-3 by net total
on the train window" and stayed profitable on validate. That is best-of-N selection: it
manufactures winners by construction. Its one real suggestion (2026-07-28) lost on 2 of
3 independent windows when checked properly, it wrote ~126 MB of sweep output per run,
and it was disabled 2026-08-02.

The 2026-08-04/05 session is the counter-example. Six ideas were tested by hand:

| idea | outcome |
|---|---|
| stop-loss at 0.4 / 0.5 | DIED — 1/3 windows, worse in aggregate than baseline |
| hold losers to settlement | DIED — every exit reason already beat holding |
| fair-value model from `distance`/vol/time | DIED — zero information beyond the ask |
| cheap price-band settlement edge | DIED — −0.060 ± 0.021 against the ask |
| EV gate (relative + shrunk) | DIED — blocks nothing; no bucket clears 1 SE |
| `sig_combined` level, held to settlement | **SURVIVED** — 3/3 windows, 20/26 days |

The survivor became `settle_bot`. But every one of those validations was hand-written,
which is why only six ideas got tested in a full day. **The bottleneck is not idea
generation — it is the cost of a trustworthy kill.**

## Scope

**In:** entry-rule hypotheses only — "condition on features of one signal row → pick a
side → hold to settlement." Cheapest to run (seconds, no `Bot` replay), works on data
already on disk, and it is the exact shape that produced the only survivor.

**Out (this iteration):** strategy-parameter hypotheses needing a full `bot_replay`
(path-dependent, minutes per run, closer to tuner territory) and exit-policy
counterfactuals against `bot_trade_grades.jsonl`. Both are planned as a following
iteration; nothing here should make them harder to add.

**Also out:** applying anything. Like `bot_tuner`, the harness never edits a config or
starts a strategy. A survivor is a recommendation to a human, nothing more.

## The interface

A hypothesis is a pure predicate over one signal row, returning `"YES"`, `"NO"`, or
`None`, registered with its acceptance band declared up front:

```python
@hypothesis(name="sig_combined level >= 10, 5-11min",
            band=(0.02, 0.05), window=(5.0, 11.0))
def sig_level(row):
    sc = row.get("sig_combined")
    if sc is None:
        return None
    return "YES" if sc >= 10 else "NO" if sc <= -10 else None
```

That is the entire author-facing surface. Everything else is fixed by the harness —
deliberately, because a protocol that can be adjusted per-idea is where fudging lives.

The predicate must be pure: no I/O, no state, no randomness. It sees one row and
nothing else, which structurally prevents lookahead.

## The protocol

Applied identically to every hypothesis. This is the 2026-08-04 manual process,
mechanised:

1. **Settlement labels** from `sign(distance)` at each market's last observed tick.
   That tick sits at expiry (`mins_left` p50 = 0.1) and agrees with `trade_grader`'s
   independent record on 365/367 markets (99.5%).
2. **One observation per market.** The first row where the predicate fires inside the
   declared window. Never multiple ticks from one market — they are not independent.
3. **Three equal held-out windows**, split by market in time order.
4. **Edge, SE and t** per window and overall. Edge is `settle - entry_price` per
   contract, where entry price is the achievable resting bid (`limit_price`), not the ask.
5. **Per-day breakdown** and the drop-your-two-best-days check.
6. **Both fee models reported**: maker (zero, confirmed from real fills 2026-08-03) and
   taker. A hypothesis that only clears at the maker rate is flagged as fill-dependent.
7. **Verdict.** SURVIVED requires all three: positive in **3/3** windows; overall t at
   or above the adjusted bar (below); and still positive after dropping the two best days.
   Anything else is DIED, with the failing criterion named.

## Three guards against becoming bot_tuner again

**Pre-registration is enforced.** The harness refuses to run a hypothesis whose `band`
is absent. You cannot see the result and then decide what counts as good.

**No sweeping.** One hypothesis, one test. Testing five thresholds means registering
five hypotheses, and the harness counts them as five.

**A persistent registry with a multiple-comparisons penalty.** Every test ever run is
recorded — name, predicate hash, band, date, verdict, full numbers. The harness reports
how many comparisons have been made against this dataset and raises the significance bar
accordingly. This is the guard that would have caught `bot_tuner`: after 50 tests a
t≈2 survivor is *expected*, and the 2026-08-04 session alone burned six.

Re-testing an already-dead hypothesis (matched by predicate hash) is refused without an
explicit `--retest` override, so disproofs stop being re-derived. The three wrong
diagnoses I made on 2026-08-03 — scale-out double-counting, the keyring theory, the
autostash race — each cost real time precisely because nothing recorded that they were
already dead.

## Output

Two artefacts, both under `data/hypotheses/`:

- `registry.jsonl` — append-only, one row per test ever run. The permanent record.
- `report-<date>.md` — human-readable: verdict, the three window numbers, per-day table,
  both fee models, the adjusted bar and how many comparisons it accounts for.

Survivors are surfaced with their kill-evidence attached — what was tried to falsify
them and failed. A verdict with no evidence trail is not usable.

## Validation

The harness must reproduce known results before it is trusted. Acceptance test:

- Feed it the `settle_bot` rule with `band=(0.02, 0.05)` → **SURVIVED**, positive 3/3.
  `settle_replay.py` already scores this rule at **+0.0300** on the same bid-entry basis,
  so the harness must land near that, not near the +0.0406 in the settle_bot spec — that
  earlier figure assumed paying the **ask** and sampled one tick per market at ~10 min.
- Feed it the cheap-price-band idea → **DIED**. Expect roughly −0.05, *not* the −0.060
  originally measured: that number was against the ask, and entering at the bid is better
  by about the spread (~1c). The verdict must still be DIED; if a 1c improvement flips it
  to SURVIVED, the band was set too loose and the acceptance test has found a real problem.
- Feed it a deliberately random predicate → **DIED**.

The middle case matters most. A harness that cannot reproduce a known *death* is not
falsifying anything, and that is the failure mode that makes a tool like this dangerous
rather than merely useless.

## Risks

**It only measures what the feature log contains.** Fill rate, queue position and
adverse selection are invisible to it. A SURVIVED verdict means "worth paper-trading",
never "worth money" — `settle_bot` is currently demonstrating exactly that gap, with a
+0.0300 backtest against a live −0.0255 over its first 47 fills.

**The registry's comparison count is only honest if every test goes through the
harness.** Ad-hoc analysis in a shell does not get counted, and will quietly inflate the
real false-positive rate. The discipline is social as much as technical.

**One regime.** The feature log starts 2026-06-30 (no usable quotes before that). Every
verdict is conditioned on that period.
