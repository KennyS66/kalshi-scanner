#!/usr/bin/env bash
# Start the Kalshi whale scanner against prod with web dashboard.
set -euo pipefail
cd "$(dirname "$0")"

# Pull Kalshi creds from the daedalus-mm .env (single source of truth).
DAEDALUS_ENV="$HOME/bots/daedalus-mm/.env"
if [[ -f "$DAEDALUS_ENV" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$DAEDALUS_ENV"
  set +a
fi

API_KEY="${KALSHI_API_KEY:-${KALSHI_API_KEY_ID:-}}"
KEY_FILE="${KALSHI_PRIVATE_KEY_PATH:-$HOME/.kalshi/private_key.pem}"

if [[ -z "$API_KEY" ]]; then
  echo "error: KALSHI_API_KEY (or KALSHI_API_KEY_ID in $DAEDALUS_ENV) is not set" >&2
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
start_bg btc_monitor  "btc_monitor.sh"  ./btc_monitor.sh
start_bg exit_watcher "exit_watcher.py" .venv/bin/python -u exit_watcher.py

echo
echo "Scanner: http://localhost:9050  (dashboards: /whales, /crypto)"
echo ">> Live market-analysis loop: type  /marketloop  in Claude Code to start it."
echo "   (a Python launch can't spawn the Claude reasoning loop; this is the one command)"
echo

exec .venv/bin/python main.py \
  --api-key "$API_KEY" \
  --key-file "$KEY_FILE" \
  --web \
  --web-port 9050 \
  "$@"
