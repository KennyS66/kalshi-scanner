#!/usr/bin/env python3
"""Health check for the scanner stack — catches the silent failures.

Built 2026-08-08 after `exit_watcher` was found dead for an unknown number
of hours. Everything looked fine: the systemd unit was `active`, the port
was bound, the dashboard rendered. One collector had crashed and nothing
said so.

Each check here exists because that exact thing failed silently:

  process     exit_watcher died on a TypeError and nobody noticed. Also
              flags DUPLICATES -- two of one daemon means two writers on
              one journal, which happened twice during development.
  age         swing_bot's heartbeat, the loop-log deadman, and spot
              freshness. A stale heartbeat with a live process is the
              signature of a wedged tick.
  latency     the TAIL, not the median. feed_down was caused by the
              signal endpoint's p90 (4.73s) exceeding fetch_signal's 4s
              timeout while its median stayed a healthy 0.75s. A mean or
              median check would have reported everything fine.

Exits 0 when all checks pass, 1 otherwise, so cron or a watchdog can use
it directly.

Usage:  python3 health_check.py [--json]
"""
import argparse
import json
import subprocess
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).parent
SIGNAL_URL = "http://localhost:9050/api/crypto/signal"
FETCH_TIMEOUT = 4.0        # must match swing_bot.fetch_signal's timeout
LATENCY_SAMPLES = 8
LATENCY_TAIL_TOLERANCE = 0.10   # fraction of samples allowed over timeout

# name -> pgrep pattern. Bracket-quoted so the check never matches itself.
COLLECTORS = {
    "main":          r"python.*[m]ain\.py",
    "swing_bot":     r"python.*[s]wing_bot\.py",
    "btc_monitor":   r"[b]tc_monitor\.sh",
    "exit_watcher":  r"python.*[e]xit_watcher\.py",
    "target_grader": r"python.*[t]arget_grader\.py",
    "trade_grader":  r"python.*[t]rade_grader\.py",
}


def check_process(name, count):
    """Exactly one instance. Zero is death; more than one is two writers."""
    if count == 1:
        return {"name": f"process:{name}", "ok": True, "detail": "1 running"}
    if count == 0:
        return {"name": f"process:{name}", "ok": False,
                "detail": f"{name} is NOT running"}
    return {"name": f"process:{name}", "ok": False,
            "detail": f"{count} instances of {name} — duplicate writers"}


def check_age(name, age_s, limit):
    """Freshness. `age_s` None means the thing has never been seen."""
    if age_s is None:
        return {"name": f"age:{name}", "ok": False,
                "detail": f"{name} never seen"}
    ok = age_s <= limit
    return {"name": f"age:{name}", "ok": ok,
            "detail": f"{age_s:.0f}s old (limit {limit:.0f}s)"}


def check_latency(samples, timeout, tolerance=LATENCY_TAIL_TOLERANCE):
    """Tail latency against the timeout its CONSUMER actually uses.

    Deliberately not a mean or median: the failure this was written for had
    a p50 of 0.75s and a p90 of 4.73s against a 4s timeout. Averages said
    healthy while 12.5% of requests were being silently dropped.
    """
    if not samples:
        return {"name": "latency:signal", "ok": False,
                "detail": "no samples collected"}
    over = [s for s in samples if s > timeout]
    ok = len(over) <= tolerance * len(samples)
    s = sorted(samples)
    return {"name": "latency:signal", "ok": ok,
            "detail": f"{len(over)}/{len(samples)} over {timeout:.0f}s "
                      f"(p50 {s[len(s)//2]:.2f}s, max {s[-1]:.2f}s)"}


def overall(checks):
    return all(c["ok"] for c in checks)


# ── live collection ───────────────────────────────────────────────────────

def _count(pattern):
    try:
        out = subprocess.run(["pgrep", "-fc", pattern], capture_output=True,
                             text=True, timeout=10)
        return int((out.stdout or "0").strip() or 0)
    except Exception:
        return 0


def _age_of(path):
    p = Path(path)
    return (time.time() - p.stat().st_mtime) if p.exists() else None


def _state_heartbeat_age(path):
    try:
        return time.time() - json.loads(Path(path).read_text())["heartbeat"]
    except Exception:
        return None


def _sample_latency(n=LATENCY_SAMPLES):
    out = []
    for _ in range(n):
        t0 = time.time()
        try:
            urllib.request.urlopen(SIGNAL_URL, timeout=15).read()
            out.append(time.time() - t0)
        except Exception:
            out.append(999.0)      # a failure is an infinitely slow response
        time.sleep(0.2)
    return out


def collect():
    checks = [check_process(n, _count(p)) for n, p in COLLECTORS.items()]
    checks.append(check_age("swing_bot_heartbeat",
                            _state_heartbeat_age(BASE / "data/bot/bot_state.json"),
                            limit=120))
    checks.append(check_age("signal_feature_log",
                            _age_of(BASE / "data/whales/signal_feature_log.jsonl"),
                            limit=600))
    checks.append(check_latency(_sample_latency(), FETCH_TIMEOUT))
    return checks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    checks = collect()
    if args.json:
        print(json.dumps({"ok": overall(checks), "checks": checks}, indent=2))
    else:
        for c in checks:
            print(f"  {'PASS' if c['ok'] else 'FAIL'}  {c['name']:28} {c['detail']}")
        print(f"\n  OVERALL: {'HEALTHY' if overall(checks) else 'DEGRADED'}")
    raise SystemExit(0 if overall(checks) else 1)


if __name__ == "__main__":
    main()
