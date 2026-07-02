#!/usr/bin/env python3
"""
Read-only Kalshi account view — balance + open positions.

Reads credentials from ~/.kalshi/trading.env (KALSHI_API_KEY_ID +
KALSHI_PRIVATE_KEY_PATH) or the matching environment variables.

This script ONLY reads (GET /portfolio/...). It contains no order-placement
code by design — manual trading is done in the Kalshi app.

Usage:  python3 account.py
"""
import base64
import json
import os
import time
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

HOST = "https://api.elections.kalshi.com"
ENV_FILE = Path.home() / ".kalshi" / "trading.env"


def _load_env():
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    kid = os.environ.get("KALSHI_API_KEY_ID")
    kp = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not kid or not kp:
        raise SystemExit("Missing KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH "
                         f"(set them or put them in {ENV_FILE})")
    return kid, kp


def _get(key_id, pk, full_path):
    ts = str(int(time.time() * 1000))
    sig = pk.sign(f"{ts}GET{full_path}".encode(),
                  padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                              salt_length=padding.PSS.MAX_LENGTH),
                  hashes.SHA256())
    headers = {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
    }
    r = requests.get(HOST + full_path, headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()


def main():
    key_id, kp = _load_env()
    with open(kp, "rb") as f:
        pk = serialization.load_pem_private_key(f.read(), password=None)

    bal = _get(key_id, pk, "/trade-api/v2/portfolio/balance")
    pos = _get(key_id, pk, "/trade-api/v2/portfolio/positions")

    dollars = bal.get("balance_dollars") or f"{bal.get('balance', 0) / 100:.2f}"
    print(f"=== Kalshi account {key_id[:8]}… (READ-ONLY) ===")
    print(f"Cash balance : ${dollars}")
    print(f"Portfolio val: ${(bal.get('portfolio_value') or 0) / 100:.2f}")

    # Field names per the live API (see web.py's account poller): quantities
    # are position_fp, money fields are *_dollars and already in dollars.
    mp = [p for p in pos.get("market_positions", []) if float(p.get("position_fp") or 0)]
    if not mp:
        print("Open positions: none")
    else:
        print("Open positions:")
        for p in mp:
            qty = float(p.get("position_fp") or 0)
            side = "yes" if qty > 0 else "no"
            print(f"  {p.get('ticker'):28} {side:>3} qty={abs(qty):>5g} "
                  f"exposure=${float(p.get('market_exposure_dollars') or 0):.2f} "
                  f"realized=${float(p.get('realized_pnl_dollars') or 0):+.2f}")


if __name__ == "__main__":
    main()
