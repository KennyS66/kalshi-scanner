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
                rows.append(r)
    rows.sort(key=lambda r: r.get("ts", 0))
    return rows


def replay(log_path, out_dir, bankroll=500.0, cfg_overrides=None,
           offsets_file=None, rows=None) -> dict:
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    if rows is None:
        rows = _rows(log_path)
    # Curfews are a live-trading overlay; replay's job is measuring the raw
    # strategy in every session (it is the evidence engine for reopening a
    # curfewed zone). Explicit overrides may still turn them back on.
    cfg = {"overnight_curfew": False, "weekend_curfew": False}
    cfg.update(cfg_overrides or {})
    out.mkdir(parents=True, exist_ok=True)
    (out / "config.json").write_text(json.dumps(cfg))
    it = iter(rows)
    bot = Bot(out, fetch_fn=lambda: next(it, None),
              offsets_file=offsets_file or (out / "banner_offsets.json"))
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
    quoted = sum(1 for r in rows if r.get("yes_ask") is not None and r.get("no_ask") is not None)
    return {"trades": len(closed), "wins": wins, "net_total": total,
            "net_avg": round(total / len(closed), 4) if closed else 0.0,
            "signals": len(rows), "quoted": quoted}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=str(FEATURE_LOG))
    ap.add_argument("--out", default="data/bot/replay")
    ap.add_argument("--bankroll", type=float, default=500.0)
    ap.add_argument("--offsets", default=None,
                    help="banner_offsets.json to calibrate ranges from "
                         "(default: zero offsets, deterministic)")
    args = ap.parse_args()
    r = replay(args.log, args.out, bankroll=args.bankroll,
               offsets_file=args.offsets)
    print(f"signals={r['signals']} quoted={r['quoted']}  round trips={r['trades']}  wins={r['wins']}")
    win_pct = 100 * r["wins"] / r["trades"] if r["trades"] else 0.0
    print(f"REPLAY_RESULT: trades={r['trades']} win={win_pct:.0f}% "
          f"net_avg=${r['net_avg']:+.4f} net_total=${r['net_total']:+.2f}")


if __name__ == "__main__":
    main()
