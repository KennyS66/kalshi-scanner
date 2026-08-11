#!/usr/bin/env python3
"""Sample /api/debug/memory into logs/memdiag.jsonl so the leak has a curve.

The 2026-08-10 restart reset RSS from 1858MB to 143MB, which means the only
record of how it got there is 40 timestamped footprint lines scraped out of
health-alerts.log. That was barely enough. This records the composition --
RSS, gc object count, per-container lengths -- alongside it, so the next
question ("which container grew while RSS grew?") is answerable from data
instead of another audit.

Deliberately does NOT pass trim=1: malloc_trim would hand memory back and
flatten the very curve being measured. Run the discriminator by hand, once,
at the END of a growth window:

    curl -s 'localhost:9050/api/debug/memory?trim=1' | jq .trim

Usage:  nohup python3 memdiag_sampler.py [interval_s] [hours] &
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

URL = "http://localhost:9050/api/debug/memory?top=15"
OUT = Path(__file__).parent / "logs" / "memdiag.jsonl"


def sample() -> dict:
    with urllib.request.urlopen(URL, timeout=30) as r:
        d = json.loads(r.read())
    # Keep only what answers the question; the full type histogram every 5
    # minutes for a day would be megabytes of noise.
    return {
        "ts": time.time(),
        "rss_mb": d.get("rss_mb"),
        "gc_objects": d.get("gc_objects"),
        "threads": d.get("threads"),
        "types": dict(d.get("types", [])[:8]),
        "containers": {label: {k: v for k, v in sizes.items() if v > 50}
                       for label, sizes in (d.get("containers") or {}).items()},
    }


def main():
    interval = float(sys.argv[1]) if len(sys.argv) > 1 else 300.0
    hours = float(sys.argv[2]) if len(sys.argv) > 2 else 24.0
    deadline = time.time() + hours * 3600
    OUT.parent.mkdir(exist_ok=True)
    while time.time() < deadline:
        try:
            row = sample()
        except Exception as e:                      # scanner restart, timeout
            row = {"ts": time.time(), "error": str(e)[:200]}
        with OUT.open("a") as f:
            f.write(json.dumps(row) + "\n")
        time.sleep(interval)


if __name__ == "__main__":
    main()
