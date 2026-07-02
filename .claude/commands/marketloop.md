/loop ADAPTIVE BTC market-analysis loop — configures itself from the day's research, then watches.

SETUP (first run, and again at the start of each new UTC day or when a fresh daily thesis appears):
1. Ensure the scanner is up: `ss -ltn | grep 9050`; if not listening, run `./start.sh` (background).
2. Run `python3 day_plan.py` (or `.venv/bin/python day_plan.py`). It reads the latest daily thesis (bias + key level) and current spot and prints a PLAN line: `PLAN: bias=<UP|DOWN|WAIT|NONE> key=<level> spot=<spot> pos=<above|below|at> ... regime=<...>`. This is the day's configuration — re-read it; do not hardcode levels. The PLAN line also carries the latest intraday regime overlay (regime/rlo/rhi/session) if one has been journaled today.
3. Arm (or re-arm) a persistent level-watcher Monitor built FROM the plan's key level, not fixed numbers:
   - DOWN bias: watch the key level as SUPPORT below. Fire on TEST (spot within ~$400 above key) and a BREAK (spot < key).
   - UP bias: watch the key level as RESISTANCE above. Fire on TEST (spot within ~$400 below key) and a BREAK (spot > key).
   - WAIT/range/NONE: watch the key level only; fire on a confirmed break either way.
   The watcher must use the /api/crypto/spot value with a fallback to /api/crypto/signal's spot, and only emit FEED_DOWN after 3 consecutive failures (~90s). If a watcher is already armed for today's level, don't double-arm (TaskList first).

EACH ITERATION:
- Fetch /api/crypto/signal and /api/crypto/spot (use signal's spot if /spot is null). Read the current/new 15m market and BTC vs the day's key level.
- TAILOR the read to the day's bias:
  - DOWN: favor NO/short, trend-aligned; watch support; flag that the key level is high bounce-risk (bank short profit INTO it; a bounce = counter-trend long).
  - UP: favor YES/long; watch resistance; flag rejection risk at the level (bank long profit into it; a rejection = counter-trend short).
  - WAIT/range/NONE: no directional bias; only flag a CONFIRMED break of the key level; otherwise stay quiet.
- Only give a thought-out update on a genuinely meaningful event: a confirmed level test/break/hold, a notable move, a regime change, or a clean 15m setup aligned with the bias. STAY QUIET during chop. Do NOT narrate intrabar stabs as breakouts — require a confirmed/sustained move (a 15m close beyond the level, not a wick).
- If a 15m market is decided (price <5c/>95c or <2min), say wait for the next.
- MAINTAIN THE REGIME JOURNAL (`python3 intraday_regime.py record <range|trend_up|trend_down|breakout_watch> --lo L --hi H --note "..."`): append an entry at each session boundary (asia 00Z / europe 07Z / us 13Z / late 21Z), when the daily bias gets price-invalidated, or when the live range/trend visibly shifts. This is the watch-plan overlay — the graded daily bias NEVER changes intraday; the regime entry only updates the live levels the loop watches (and day_plan.py re-reads it). Also re-aim the level watcher at the live range edges when they're more relevant than the day key.

HEALTH & HYGIENE:
- Discount whale_trend/buy_pressure when they conflict with price action; treat extreme whale_trend at open with near-zero buy_pressure as noise.
- On FEED_DOWN or a stalled feed: check port 9050 + two probes; restart via ./start.sh ONLY if BOTH probes truly fail (kill any duplicate pids first); a single failed probe = busy, not down.
- Self-pace: ~5min in chop, tighten near the key level, faster only on a confirmed break.

RULES: Paper/analysis only — never claim profit or assert an unverified edge; the user trades their own account. Daily theses are tracked via daily_thesis.py; this loop reads them via day_plan.py. Be honest and own wrong calls.
