# Settlement-based longshot study — KXBTC15M

**Date:** 2026-08-03 · read-only analysis over `bot_trade_grades.jsonl`
(680 graded trades, 2026-07-15 → 2026-07-30; 1 data-gap row excluded).
Ran via DuckDB directly over the JSONL — no ETL, queries in the doc's
history (this session's transcript).

## Why the earlier pass was not believable

The naive check bucketed `entry_price` against `net_pnl > 0`, which credits
scale-outs and early target exits — it measures the *exit strategy's* hit
rate, not price calibration. This study uses `settled` vs `side` from the
trade grader: did the contract actually finish in the money.

## Headline: calibration by entry price (settlement truth)

| entry | n | implied | settled | Wilson 95% CI |
|---|---|---|---|---|
| 0–10¢ | 9 | 9.1 | 0.0 | [0, 29.9] |
| 10–20¢ | 19 | 14.7 | 10.5 | [2.9, 31.4] |
| 20–30¢ | 42 | 24.6 | 33.3 | [21.0, 48.4] |
| **30–40¢** | 94 | 34.3 | **48.9** | **[39.1, 58.9]** |
| **40–50¢** | 92 | 45.0 | **59.8** | **[49.6, 69.2]** |
| 50–60¢ | 142 | 54.7 | 53.5 | [45.3, 61.5] |
| 60–70¢ | 115 | 64.2 | 58.3 | [49.1, 66.9] |
| 70–80¢ | 65 | 73.8 | 80.0 | [68.7, 87.9] |
| 80–90¢ | 81 | 84.1 | 86.4 | [77.3, 92.2] |
| 90–100¢ | 20 | 91.7 | 100.0 | [83.9, 100] |

Only **30–40¢ and 40–50¢** exclude their implied probability from the 95%
CI. Everything else is consistent with fair pricing — including the rich
(70¢+) buckets, so the P&L bleed on expensive entries seen in the naive
pass is **fees and exit mechanics, not mispricing**.

## This is NOT the Whelan market-level claim — and the distinction matters

Every row here is a trade the bot *chose* to enter after a flip signal
(whale flow aligned with momentum). So the correct reading is:

> **Conditional on the bot's entry signal, contracts entered at 20–50¢
> settle in the money far more often than their price implies.**

That is evidence the flip signal carries real directional information in
the cheap-mid band — not evidence that Kalshi misprices 30–50¢ contracts
for everyone. An unconditional Whelan-style test needs all contracts'
prices and settlements (the `signal_feature_log` tick history could
support this later); the bias direction here is also *opposite* to
Whelan's slow-market finding (longshots overpriced), which is what you'd
expect if the effect is signal-selection, not market miscalibration.

## Stability: chronological halves (20–50¢ band, n=228)

| half | n | implied | settled | CI |
|---|---|---|---|---|
| 1st | 114 | 39.5 | 47.4 | [38.4, 56.5] |
| 2nd | 114 | 34.2 | 53.5 | [44.4, 62.4] |

Same direction both halves (+7.9 and +19.3 pts); the second half is
significant on its own, the first borderline. Not a single-window fluke.

## Sessions: the edge lives exactly where the P&L does

20–50¢ entries, by session (UTC):

| session | n | implied | settled | actual P&L | held-to-expiry gross |
|---|---|---|---|---|---|
| weekday_day | 71 | 38.0 | **56.3** | +$2.09 | **+$48.55** |
| weekday_night | 102 | 36.3 | **54.9** | +$42.55 | **+$101.55** |
| weekend_day | 40 | 35.2 | 37.5 | −$53.75 | −$7.49 |
| weekend_night | 15 | 39.7 | 26.7 | −$29.87 | −$35.67 |

Three things fall out:

1. **The settlement edge is a weekday phenomenon** — +18/+19 points on
   weekdays, absent (weekend_day) or inverted (weekend_night, tiny n) on
   weekends. This independently corroborates the session-gate P&L split
   with a *different outcome variable*: it is not one lucky streak showing
   up twice, it is the signal being informative on weekday tape and
   uninformative on weekend tape.
2. **weekday_day's signal is as good as weekday_night's** (56.3 vs 54.9)
   yet its P&L is ~zero vs +$42.55. weekday_day's losing record is an
   *execution/exit* problem, not a signal problem. Untested hypothesis;
   next lever if weekday_day is ever to be rescued.
3. **Early exits give back a lot of this edge**: on weekday 20–50¢
   entries, holding to expiry grosses +$150 vs +$45 actually realized.
   The grader's overall +$171 exit-edge is earned elsewhere (rich entries,
   weekends) — within this band, exits cost money.

## What this does NOT license

Do **not** ship a "hold 20–50¢ weekday entries to expiry" rule from this
table. It is one 15-day sample, gross of the variance that holding binary
contracts to settlement adds (0-or-1 outcomes vs banked partial exits),
and precisely the kind of finding the 3-window replay discipline exists
for. Point 4 of the giveback-loss memory applies verbatim.

## Verdict and next steps, ranked

- **Confirmed (this sample):** flip-signal entries at 20–50¢ on weekdays
  settle ITM ~+18 pts over implied; stable across halves; corroborates the
  session gate from an independent outcome variable. Mildly strengthens
  the case for the weekday_night live test.
- **Promising, unvalidated:** wider targets / longer holds for 20–50¢
  weekday entries (the +$150-vs-+$45 gap). Needs replay over disjoint
  windows with realistic settlement variance before any config change.
- **Worth a separate study:** unconditional calibration from
  `signal_feature_log` ticks to test true market-level mispricing.
- **Closed:** rich (70¢+) entries are fairly priced; their P&L drag is
  cost structure, not miscalibration — stop suspecting the market there.
