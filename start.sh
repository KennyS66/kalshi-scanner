#!/usr/bin/env bash
# Start the Kalshi whale scanner against prod with web dashboard.
set -euo pipefail
cd "$(dirname "$0")"

# Resolve the venv interpreter. Local checkouts use .venv; the cloud env uses
# venv. Prefer whichever exists so one start.sh works on every machine.
PY=".venv/bin/python"
[[ -x "$PY" ]] || PY="venv/bin/python"

# Sync the latest daily thesis (pushed by the cloud pre-market routine) so
# day_plan.py / the /marketloop configures from today's bias, not a stale one.
# Best-effort and non-blocking: a sync problem must never stop the scanner.
# --autostash tucks the constantly-rewritten data/*.json picks aside during the
# rebase; GIT_TERMINAL_PROMPT=0 keeps it from hanging on an auth prompt.
echo "Syncing repo (latest daily thesis)..."
GIT_TERMINAL_PROMPT=0 git pull --rebase --autostash 2>&1 | tail -3 \
  || echo "  (git pull skipped/failed — continuing with the local thesis)"

# Pull Kalshi creds from ~/.kalshi/trading.env (single source of truth for
# the scanner's own production key). daedalus-mm/.env belongs to a separate
# bot and may intentionally point at demo credentials — never borrow it here.
KALSHI_ENV="$HOME/.kalshi/trading.env"
if [[ -f "$KALSHI_ENV" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$KALSHI_ENV"
  set +a
fi

API_KEY="${KALSHI_API_KEY:-${KALSHI_API_KEY_ID:-}}"
KEY_FILE="${KALSHI_PRIVATE_KEY_PATH:-$HOME/.kalshi/private_key.pem}"

if [[ -z "$API_KEY" ]]; then
  echo "error: KALSHI_API_KEY (or KALSHI_API_KEY_ID in $KALSHI_ENV) is not set" >&2
  exit 1
fi
if [[ ! -f "$KEY_FILE" ]]; then
  echo "error: private key not found at $KEY_FILE" >&2
  exit 1
fi

# Launch background data collectors (idempotent) so the full pipeline comes up
# with the scanner. They poll :9050 and retry until it's listening, and they
# keep the signal feature log / alerts flowing for the gate + analysis tools.
start_bg() {  # <name> <pgrep-pattern> <command...>
  local name="$1" pat="$2"; shift 2
  if pgrep -f "$pat" >/dev/null 2>&1; then
    echo "  $name already running"
  else
    nohup "$@" >"/tmp/${name}.log" 2>&1 &
    echo "  started $name (pid $!)"
  fi
}
echo "Starting data collectors..."
start_bg btc_monitor    "btc_monitor.sh"   ./btc_monitor.sh
start_bg exit_watcher   "exit_watcher.py"  "$PY" -u exit_watcher.py
start_bg target_grader  "target_grader.py" "$PY" -u target_grader.py
start_bg swing_bot      "swing_bot.py"     "$PY" -u swing_bot.py
start_bg bot_tuner      "bot_tuner.py"     "$PY" -u bot_tuner.py --daemon

echo
echo "Scanner: http://localhost:9050  (dashboards: /whales, /crypto)"
echo

# Check if today's thesis is set; auto-run daily_research.py if stale/missing.
TODAY=$(date -u +%Y-%m-%d)
THESIS_CHECK=$("$PY" day_plan.py 2>/dev/null | grep "^PLAN:" | head -1)
NEEDS_RESEARCH=false
if [[ -z "$THESIS_CHECK" ]] || echo "$THESIS_CHECK" | grep -q "bias=NONE\|stale=yes"; then
  NEEDS_RESEARCH=true
fi

if $NEEDS_RESEARCH; then
  echo "📡 No fresh thesis for $TODAY — running daily_research.py..."
  "$PY" daily_research.py 2>&1 | tail -20
  echo
  # Re-read plan after research
  THESIS_CHECK=$("$PY" day_plan.py 2>/dev/null | grep "^PLAN:" | head -1)
fi

if [[ -z "$THESIS_CHECK" ]]; then
  echo "⚠️  WARNING: Research failed — no thesis for $TODAY. Set manually:"
  echo "   $PY daily_thesis.py record <UP|DOWN|WAIT> <spot> --level <key> --conviction <1-5> --date $TODAY"
  echo
elif echo "$THESIS_CHECK" | grep -q "bias=NONE\|bias=WAIT"; then
  echo "ℹ️  Thesis for $TODAY: $(echo "$THESIS_CHECK" | grep -o 'bias=[^ ]*')"
  echo "   WAIT bias — no directional trade today."
  echo
else
  echo "✅ Thesis for $TODAY: $THESIS_CHECK"
  echo
fi

echo ">> VITAL: start /marketloop in Claude Code to arm the analysis loop."
echo "   (Claude must be active — type /marketloop in the Claude Code terminal)"
echo

exec "$PY" main.py \
  --api-key "$API_KEY" \
  --key-file "$KEY_FILE" \
  --web \
  --web-port 9050 \
  "$@"
