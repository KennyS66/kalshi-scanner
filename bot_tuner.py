#!/usr/bin/env python3
"""Nightly replay tuner for the swing bot.

Sweeps the strategy knobs over the recent signal history using the REAL bot
code paths (bot_replay), with a train/validate split so a suggestion must
hold up out-of-sample. Writes data/bot/tuner_report.json with the full
evidence table and a suggested config — it NEVER applies anything; applying
a suggestion is a human edit to data/bot/config.json.

Suggestion bar: a combo must (a) place top-3 by net total on the train
window with >= MIN_TRADES round trips, (b) stay profitable on the validate
window, and (c) beat the current config's validate result. Otherwise the
report says "keep current config" — no change is a valid outcome.

Usage:  python3 bot_tuner.py [--days 14] [--quick] [--once]
        python3 bot_tuner.py --daemon     # sweep once per UTC day
"""
import argparse
import datetime as dt
import itertools
import json
import os
import shutil
import time
from pathlib import Path

from backtest_gate import FEATURE_LOG
from bot_core import DEFAULT_CONFIG, load_config
from bot_replay import _rows, replay

BOT_DIR = Path(__file__).parent / "data" / "bot"
REPORT = BOT_DIR / "tuner_report.json"
OFFSETS = Path(__file__).parent / "data" / "whales" / "banner_offsets.json"
SWEEP_DIR = BOT_DIR / "tuner_sweep"

GRID = {
    "flip_threshold": [2.0, 3.0],
    "min_entry_mins": [4.0, 6.0],
    "use_ranges": [True, False],
    "flip_exit": [True, False],
    "max_entry_momentum": [0.0, 25.0],
    "max_entries_per_market": [0, 2],
    "decided_lo": [0.05, 0.08],   # tighter band skips near-decided lottery
    "decided_hi": [0.95, 0.92],   # entries (the 5.6c NO x151 stop on Jul 15)
    "stop_loss_frac": [0.5, 1.0], # 1.0 = no mid-trade stop (time exit bounds);
    "min_edge_c": [2.0, 4.0],     # replay 2026-07-18: stop-off +$27/day, edge4 +$10
    "scale_out": [True, False],
}
QUICK_GRID = {
    "flip_threshold": [2.0, 3.0],
    "min_entry_mins": [4.0],
    "exit_mins": [2.0],
    "use_ranges": [True, False],
}
MIN_TRADES = 20          # train-window round trips required to qualify
TRAIN_FRACTION = 0.6     # by time: first 60% train, last 40% validate
DAEMON_CHECK_SECS = 1800


def _utc_day(ts=None) -> str:
    return dt.datetime.fromtimestamp(
        ts if ts is not None else time.time(), dt.timezone.utc
    ).strftime("%Y-%m-%d")


def window_rows(log_path, days: float):
    rows = _rows(log_path)
    if not rows:
        return [], []
    last_ts = rows[-1].get("ts", 0)
    rows = [r for r in rows if r.get("ts", 0) >= last_ts - days * 86400]
    if not rows:
        return [], []
    lo, hi = rows[0]["ts"], rows[-1]["ts"]
    split = lo + (hi - lo) * TRAIN_FRACTION
    return ([r for r in rows if r["ts"] < split],
            [r for r in rows if r["ts"] >= split])


def sweep(days: float, grid: dict, log_path=None, bot_dir=None,
          report_path=None, sweep_dir=None, offsets_file=None) -> dict:
    log_path = log_path or FEATURE_LOG
    bot_dir = Path(bot_dir) if bot_dir else BOT_DIR
    report_path = Path(report_path) if report_path else REPORT
    sweep_dir = Path(sweep_dir) if sweep_dir else SWEEP_DIR
    offsets_file = offsets_file or OFFSETS
    train, validate = window_rows(log_path, days)
    current_cfg = load_config(bot_dir / "config.json")

    def run_combo(cfg, rows, tag):
        r = replay(None, sweep_dir / tag, cfg_overrides=cfg,
                   offsets_file=offsets_file, rows=rows)
        win = round(100.0 * r["wins"] / r["trades"], 1) if r["trades"] else 0.0
        return {"trades": r["trades"], "win_pct": win,
                "net_total": r["net_total"], "net_avg": r["net_avg"]}

    keys = sorted(grid)
    results = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        params = dict(zip(keys, combo))
        cfg = {**current_cfg, **params}
        tag = "-".join(f"{k}{v}" for k, v in params.items())
        entry = {"params": params, "train": run_combo(cfg, train, tag)}
        results.append(entry)
    results.sort(key=lambda e: e["train"]["net_total"], reverse=True)

    current = {"params": {k: current_cfg.get(k, DEFAULT_CONFIG.get(k))
                          for k in keys},
               "train": run_combo(current_cfg, train, "current"),
               "validate": run_combo(current_cfg, validate, "current-val")}

    qualified = [e for e in results
                 if e["train"]["trades"] >= MIN_TRADES
                 and e["train"]["net_total"] > 0][:3]
    for e in qualified:
        tag = "val-" + "-".join(f"{k}{v}" for k, v in e["params"].items())
        e["validate"] = run_combo({**current_cfg, **e["params"]},
                                  validate, tag)
    survivors = [e for e in qualified
                 if e["validate"]["net_total"] > 0
                 and e["validate"]["net_total"]
                 > current["validate"]["net_total"]]
    survivors.sort(key=lambda e: e["validate"]["net_total"], reverse=True)

    if not train or not validate:
        suggested, note = None, "insufficient signal history in window"
    elif survivors:
        suggested = survivors[0]["params"]
        note = ("suggestion beat current config on BOTH the train and "
                "validate windows; apply by editing data/bot/config.json")
    elif not qualified:
        suggested = None
        note = (f"no combo reached {MIN_TRADES} profitable train-window "
                f"round trips — keep current config")
    else:
        suggested = None
        note = ("top train combos did not hold up on the validate window "
                "— keep current config")

    report = {
        "ran_ts": time.time(), "day": _utc_day(), "window_days": days,
        "train_rows": len(train), "validate_rows": len(validate),
        "min_trades": MIN_TRADES,
        "current": current,
        "suggested": suggested, "note": note,
        "results": results,
    }
    tmp = report_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, indent=1))
    os.replace(tmp, report_path)
    shutil.rmtree(sweep_dir, ignore_errors=True)
    return report


def _report_day():
    try:
        return json.loads(REPORT.read_text()).get("day")
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=14.0)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--daemon", action="store_true")
    args = ap.parse_args()
    grid = QUICK_GRID if args.quick else GRID

    def run():
        t0 = time.time()
        rep = sweep(args.days, grid)
        best = rep["results"][0] if rep["results"] else None
        print(f"tuner: {len(rep['results'])} combos over {args.days}d "
              f"({rep['train_rows']}+{rep['validate_rows']} rows) "
              f"in {time.time()-t0:.0f}s", flush=True)
        if best:
            print(f"  best train: {best['params']} -> {best['train']}", flush=True)
        print(f"  suggested: {rep['suggested']}  ({rep['note']})", flush=True)

    if not args.daemon:
        run()
        return
    print(f"bot_tuner daemon up — report {REPORT}", flush=True)
    while True:
        if _report_day() != _utc_day():
            try:
                run()
            except Exception as e:
                print(f"tuner error: {e!r}", flush=True)
        time.sleep(DAEMON_CHECK_SECS)


if __name__ == "__main__":
    main()
