#!/usr/bin/env bash
# Start the Kalshi whale scanner against prod with web dashboard.
set -euo pipefail
cd "$(dirname "$0")"

# Load .env if present (keeps secrets out of git)
[ -f .env ] && set -a && source .env && set +a

exec venv/bin/python main.py \
  --api-key "4d9db65c-cd77-48bd-8282-5abdd128ffbd" \
  --key-file "$HOME/.kalshi/private_key.pem" \
  --web \
  --web-port 9050 \
  "$@"
