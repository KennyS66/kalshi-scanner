#!/usr/bin/env bash
# Start the Kalshi whale scanner against prod with web dashboard.
set -euo pipefail
cd "$(dirname "$0")"

exec venv/bin/python main.py \
  --api-key "$KALSHI_API_KEY" \
  --key-file "$HOME/.kalshi/private_key.pem" \
  --web \
  --web-port 9050 \
  "$@"
