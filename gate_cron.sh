#!/usr/bin/env bash
# Weekly beat-the-mid gate re-check (installed in crontab).
# Runs backtest_gate.py, appends a timestamped result to gate_history.log,
# and on a PASS raises a desktop notification + writes a flag file.
set -uo pipefail
cd "$(dirname "$0")"

LOG="data/whales/gate_history.log"
FLAG="data/whales/GATE_PASSED.flag"
STAMP="$(date '+%Y-%m-%d %H:%M')"

OUT="$(.venv/bin/python backtest_gate.py 2>&1)"
RESULT_LINE="$(printf '%s\n' "$OUT" | grep '^GATE_RESULT:' || echo 'GATE_RESULT: ERROR no_output')"

{
  echo "===== $STAMP ====="
  printf '%s\n' "$OUT"
  echo
} >> "$LOG"

if printf '%s' "$RESULT_LINE" | grep -q 'GATE_RESULT: PASS'; then
  printf '%s\n%s\n' "$STAMP" "$RESULT_LINE" > "$FLAG"
  # Best-effort desktop notification (needs an active graphical session).
  export DISPLAY="${DISPLAY:-:0}"
  export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}"
  notify-send -u critical "Kalshi gate PASSED" "$RESULT_LINE" 2>/dev/null || true
fi
