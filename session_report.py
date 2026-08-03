#!/usr/bin/env python3
"""Break down swing-bot performance by market session: weekday vs weekend,
day vs night (the same weekday/weekend split as the live-unlock gate, the
same day/night boundary as the overnight curfew — see session_tag() in
bot_core.py).

The live bot only ever trades weekday_day / weekend_day (night is
curfewed), so weekday_night / weekend_night here come from a replay
with curfews disabled — the same replay-evidence path that reopens a
curfewed zone once it's proven profitable.

Usage:  python3 session_report.py [--replay-days 21]
"""
import argparse
import json
import time
from collections import defaultdict
from pathlib import Path

from bot_core import session_tag
from bot_replay import replay, _rows

TRADES_FILE = Path("data/bot/bot_trades.jsonl")
FEATURE_LOG = Path("data/whales/signal_feature_log.jsonl")
CONFIG_FILE = Path("data/bot/config.json")


def _load_trades(path):
    """Read a bot_trades.jsonl, skipping malformed lines.

    An append that was in flight when the machine lost power leaves a
    truncated or NUL-padded line behind (ext4 delayed allocation). Skip
    just that line rather than letting the whole weekly report die.
    A missing file means no trades yet (a short replay window can produce
    none), which is an empty report, not an error.
    """
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def _closed(trades):
    return [t for t in trades if t.get("status") == "closed"
            and t.get("net_pnl") is not None]


def _by_session(trades):
    out = defaultdict(lambda: {"n": 0, "wins": 0, "net": 0.0})
    for t in trades:
        ts = t.get("entry_ts") or (t.get("entry_sig") or {}).get("ts")
        s = session_tag(ts)
        d = out[s]
        d["n"] += 1
        d["wins"] += 1 if t["net_pnl"] > 0 else 0
        d["net"] += t["net_pnl"]
    return out


def _print_table(title, by_session):
    print(f"\n{title}")
    print(f"  {'session':16s} {'n':>4s} {'win%':>6s} {'net_avg':>9s} {'net_total':>10s}")
    order = ["weekday_day", "weekday_night", "weekend_day", "weekend_night"]
    for s in order:
        d = by_session.get(s)
        if not d or d["n"] == 0:
            print(f"  {s:16s}    -      -         -          -")
            continue
        win_pct = 100 * d["wins"] / d["n"]
        avg = d["net"] / d["n"]
        print(f"  {s:16s} {d['n']:4d} {win_pct:5.1f}% {avg:+9.2f} {d['net']:+10.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay-days", type=int, default=21,
                    help="lookback window for the curfew-off replay (default 21)")
    args = ap.parse_args()

    # 1. Live trades — real weekday_day / weekend_day performance (night is
    # curfewed, so those rows will be empty here by construction).
    live_trades = []
    if TRADES_FILE.exists():
        live_trades = _load_trades(TRADES_FILE)
    live_closed = _closed(live_trades)
    _print_table(f"LIVE paper trades (n={len(live_closed)}) — real day/weekend split, night is curfewed:",
                _by_session(live_closed))

    # 2. Replay with curfews off — the only way to see weekday_night /
    # weekend_night performance, using the CURRENT live config so it's an
    # apples-to-apples read against what's actually running.
    cfg = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    for k in ("loop_deadman_mins", "live_requested", "mode", "paper_bankroll"):
        cfg.pop(k, None)

    rows_all = _rows(str(FEATURE_LOG))
    cutoff = time.time() - args.replay_days * 86400
    rows = [r for r in rows_all if r.get("ts", 0) >= cutoff]
    replay("data/whales/signal_feature_log.jsonl", "/tmp/session_report_replay",
          bankroll=500.0, cfg_overrides=cfg, rows=rows)
    replay_trades = _load_trades(Path("/tmp/session_report_replay", "bot_trades.jsonl"))
    replay_closed = _closed(replay_trades)
    _print_table(f"REPLAY, curfews off, last {args.replay_days}d (n={len(replay_closed)}) — "
                f"includes night, current config, research only:",
                _by_session(replay_closed))
    print("\nNote: replay's ev_gate learns from a blank slate each run (no memory of the "
        "live bot's already-learned buckets), so its trade selection differs from live "
        "even for weekday_day/weekend_day — treat replay numbers as directional, not as "
        "'what live actually made'.")


if __name__ == "__main__":
    main()
