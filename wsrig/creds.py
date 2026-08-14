"""Kalshi credentials, resolved the same way start.sh does.

start.sh:31 reads `${KALSHI_API_KEY:-${KALSHI_API_KEY_ID:-}}`. The name in
~/.kalshi/trading.env is KALSHI_API_KEY_ID; reading only KALSHI_API_KEY finds
nothing and signs with empty headers.
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_FILE = Path.home() / ".kalshi" / "trading.env"


def _from_env_file() -> dict:
    out = {}
    if not ENV_FILE.exists():
        return out
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_creds() -> tuple[str | None, str | None]:
    """(api_key_id, private_key_path). Process env wins over the file."""
    f = _from_env_file()
    key_id = (os.environ.get("KALSHI_API_KEY")
              or os.environ.get("KALSHI_API_KEY_ID")
              or f.get("KALSHI_API_KEY") or f.get("KALSHI_API_KEY_ID"))
    key_path = (os.environ.get("KALSHI_PRIVATE_KEY_PATH")
                or f.get("KALSHI_PRIVATE_KEY_PATH"))
    return key_id or None, key_path or None
