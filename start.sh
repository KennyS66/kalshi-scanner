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

exec .venv/bin/python main.py \
  --api-key "$API_KEY" \
  --key-file "$KEY_FILE" \
  --web \
  --web-port 9050 \
  "$@"
