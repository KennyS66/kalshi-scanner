#!/usr/bin/env bash
# Weekly session-report snapshot (installed in crontab): appends a
# timestamped weekday/weekend x day/night breakdown to a running log,
# so trends across weeks are visible without re-running the report by hand.
set -uo pipefail
cd "$(dirname "$0")"

LOG="data/bot/session_report_history.log"
STAMP="$(date '+%Y-%m-%d %H:%M')"

{
  echo "===== $STAMP ====="
  .venv/bin/python session_report.py --replay-days 7
  echo
} >> "$LOG" 2>&1
