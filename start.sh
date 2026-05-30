#!/usr/bin/env bash
# Start the Kalshi whale scanner against prod with web dashboard.
set -euo pipefail
cd "$(dirname "$0")"

# Load .env if present (keeps secrets out of git)
[ -f .env ] && set -a && source .env && set +a

exec venv/bin/python main.py \
  --api-key "${KALSHI_API_KEY}" \
  --key-file "${KALSHI_PRIVATE_KEY_PATH:-$HOME/.kalshi/private_key.pem}" \
  --web \
  --web-port 9050 \
  "$@"
