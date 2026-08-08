#!/usr/bin/env bash
# Health check every 15 minutes (installed in crontab).
#
# Logs a one-line summary on success and the FULL check detail on failure,
# so logs/health.log stays readable at 96 runs/day while a degraded run
# keeps everything needed to diagnose it. Failures are also appended to
# logs/health-alerts.log so they can be found without wading through
# thousands of PASS lines.
#
# Logs live under logs/, never /tmp: /tmp is wiped on reboot, which is the
# single reason the 2026-08-08 exit_watcher death and the two-week
# auto-commit push failure were both undiagnosable after the fact.
set -uo pipefail
cd "$(dirname "$0")"

mkdir -p logs
LOG="logs/health.log"
ALERTS="logs/health-alerts.log"
STAMP="$(date '+%Y-%m-%d %H:%M')"

OUT="$(.venv/bin/python health_check.py 2>&1)"; RC=$?

if [ "$RC" -eq 0 ]; then
  echo "$STAMP HEALTHY" >> "$LOG"
else
  {
    echo "===== $STAMP DEGRADED ====="
    echo "$OUT"
  } | tee -a "$ALERTS" >> "$LOG"
fi

# Keep the log bounded without losing recent history: rotate one generation
# at 2MB (~months of one-line successes, or a lot of failure detail).
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 2097152 ]; then
  mv -f "$LOG" "$LOG.old"
fi

exit "$RC"
