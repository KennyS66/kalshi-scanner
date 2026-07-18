# Trade Grader — loss-diagnosis capture for the swing paper bot

**Date:** 2026-07-17 · **Status:** approved (option A — sidecar grader daemon)

## Problem

`bot_trades.jsonl` records entry/exit snapshots and P&L, but not what a loss *meant*:
whether a stop was a good save or a whipsaw, what the price path did during the hold,
or whether the trade fought the day's bias. Diagnosing losses today requires manual
archaeology across the 6-second feature log, the thesis journal, and the regime journal.
The forensics source of record (`signal_feature_log.jsonl`) also has no archival guarantee.

## Solution

A standalone collector daemon, `trade_grader.py`, launched by `start.sh` via `start_bg`
like the other collectors. It never touches trading code and never rewrites existing
files — it appends one grade row per closed trade to `data/bot/bot_trade_grades.jsonl`.

### Grading loop (every 60s)

1. Load closed trades from `data/bot/bot_trades.jsonl`; skip ones already graded
   (key: `ticker` + `entry_ts`).
2. A trade is gradeable once its market has expired. Expiry is derived without
   timezone parsing: `expiry = entry_ts + entry_sig.mins_left * 60`.
3. **Settlement inference** from `data/whales/signal_feature_log.jsonl` ticks for the
   ticker: last tick before expiry → `settled = YES if spot >= floor_strike else NO`
   (`settle_basis: "strike"`). If no tick lands within the final 120s, fall back to the
   last available tick's market price if decided (>0.95 → YES, <0.05 → NO,
   `settle_basis: "price"`); otherwise `settled: "unknown"`, `data_gap: true`.
4. **Hold-window path** from ticks in `[entry_ts, exit_ts]`, using the held side's
   price (`price` for YES, `1 - price` for NO; mid-based approximation, fees excluded):
   `mfe` = best excursion above entry, `mae` = worst below entry.
5. **Counterfactual**: `held_pnl_gross = qty * (payout - entry_price)` where payout is
   1.0 if settled == side else 0.0; `delta_vs_held = actual_gross - held_pnl_gross`
   (both gross of fees, compared like-for-like).
6. **Verdict** (settled favorable = settled == held side):

   | exit_reason | settled favorable | settled against |
   |---|---|---|
   | stop | `whipsaw_stop` | `good_stop` |
   | target | `clean_win` | `lucky_exit` |
   | anything else (deadman, flip, manual…) | `left_money` | `good_exit` |

   `settled == "unknown"` → `verdict: "ungraded"`.
7. **Day-context join** (post-hoc, by timestamp — zero bot changes): bias/key/conviction
   from the `daily_thesis.jsonl` entry covering the trade's UTC date; the latest
   `intraday_regime.jsonl` entry at or before `entry_ts` (regime, lo, hi);
   `aligned = (side == YES) == (bias == UP)` when bias is directional, else `null`.
8. Append the grade row. Per-trade `try/except`: one bad record logs to stderr and is
   skipped; the daemon never dies over data.

### Grade row schema

```json
{"ticker": "...", "entry_ts": 0, "exit_ts": 0, "side": "YES", "qty": 0,
 "entry_price": 0.0, "exit_price": 0.0, "net_pnl": 0.0, "exit_reason": "stop",
 "settled": "YES|NO|unknown", "settle_basis": "strike|price|none",
 "held_pnl_gross": 0.0, "delta_vs_held": 0.0, "mfe": 0.0, "mae": 0.0,
 "verdict": "good_stop|whipsaw_stop|clean_win|lucky_exit|good_exit|left_money|ungraded",
 "day_bias": "UP|DOWN|WAIT|null", "day_key": 0, "day_conviction": 0,
 "regime": "range|trend_up|trend_down|breakout_watch|none",
 "regime_lo": 0, "regime_hi": 0, "aligned": true,
 "data_gap": false, "graded_ts": 0.0}
```

### Feature-log archival (same daemon, once per UTC day)

On the first poll of each UTC day, write a gzip snapshot
`data/whales/archive/<YYYY-MM-DD>/signal_feature_log.jsonl.gz` (skip if it already
exists). The live file is never truncated or rewritten — no race with the scanner,
and forensics survive any future fresh-start reset. Snapshots are cumulative copies
(~1 MB/day compressed); dedup/pruning is out of scope until size matters.

## Error handling

- Missing/corrupt input files: log, sleep, retry next cycle.
- Feature-log gaps: grade with `data_gap: true` rather than stalling the queue.
- The grades file is append-only; a crash mid-append at worst loses one row, which is
  re-graded next cycle (dedup by key prevents doubles).

## Testing

- Unit tests (in `tests/`): verdict table, settlement inference (strike basis, price
  fallback, unknown), MFE/MAE for YES and NO sides, day-context join, dedup.
- Validation: backfill run over the existing real trades in `data/bot/bot_trades.jsonl`;
  manually sanity-check the graded verdicts for the known stop losses.

## Out of scope (YAGNI)

Dashboard/UI for grades, automated parameter tuning off verdicts, pruning of archives,
grading of archived pre-fresh-start trades.
