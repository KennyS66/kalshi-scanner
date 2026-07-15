#!/usr/bin/env python3
"""Replay the swing-bot strategy over the historical signal feature log.

Runs the REAL Bot (same code paths as the daemon) against every logged
signal row in ts order, filling into a throwaway data dir. Use this to
sanity-check trigger frequency and tune config before arming the daemon.

Usage:  python3 bot_replay.py [--log data/whales/signal_feature_log.jsonl]
                              [--out data/bot/replay] [--bankroll 500.0]
Final line: "REPLAY_RESULT: trades=N win=P% net_avg=$X net_total=$X"
"""
import argparse
import json
import shutil
from pathlib import Path

from backtest_gate import FEATURE_LOG
from swing_bot import Bot, TRADES_FILE


def _rows(log_path):
    rows = []
    with open(log_path) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if "BTC15M" in (r.get("ticker") or ""):
                r.setdefault("status", "ok")
                # Compute yes_ask and no_ask if missing (for real logs with price/spread)
                if "yes_ask" not in r:
                    price = r.get("price", 0.50)
                    spread = r.get("spread") or 0.02
                    direction = r.get("direction", "YES")
                    if direction == "YES":
                        r["yes_ask"] = price
                        r["no_ask"] = round(1 - price + spread, 3)
                    else:
                        r["no_ask"] = price
                        r["yes_ask"] = round(1 - price + spread, 3)
                rows.append(r)
    rows.sort(key=lambda r: r.get("ts", 0))
    return rows


def replay(log_path, out_dir, bankroll=500.0) -> dict:
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    rows = _rows(log_path)
    it = iter(rows)
    bot = Bot(out, fetch_fn=lambda: next(it, None))
    # Replay must be offline and deterministic: historical row timestamps make
    # now_ts - bankroll_ts >= BANKROLL_REFRESH_SECS constantly, which would
    # otherwise make Bot._refresh_bankroll call the live signed Kalshi balance
    # GET on (near) every tick. Pin bankroll so that guard never fires.
    bot.state["bankroll"] = float(bankroll)
    bot.state["bankroll_ts"] = 10**12
    for r in rows:
        bot.tick(now_ts=r.get("ts", 0))
    trades_file = out / TRADES_FILE
    trades = ([json.loads(l) for l in trades_file.read_text().splitlines()]
              if trades_file.exists() else [])
    closed = [t for t in trades if t["status"] == "closed"]
    wins = sum(1 for t in closed if t["net_pnl"] > 0)
    total = round(sum(t["net_pnl"] for t in closed), 4)
    return {"trades": len(closed), "wins": wins, "net_total": total,
            "net_avg": round(total / len(closed), 4) if closed else 0.0,
            "signals": len(rows)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=str(FEATURE_LOG))
    ap.add_argument("--out", default="data/bot/replay")
    ap.add_argument("--bankroll", type=float, default=500.0)
    args = ap.parse_args()
    r = replay(args.log, args.out, bankroll=args.bankroll)
    print(f"signals={r['signals']}  round trips={r['trades']}  wins={r['wins']}")
    win_pct = 100 * r["wins"] / r["trades"] if r["trades"] else 0.0
    print(f"REPLAY_RESULT: trades={r['trades']} win={win_pct:.0f}% "
          f"net_avg=${r['net_avg']:+.4f} net_total=${r['net_total']:+.2f}")


if __name__ == "__main__":
    main()
