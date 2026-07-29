#!/usr/bin/env python3
"""Real Kalshi order placement -- the `auto` broker_mode's execution layer.

Reuses account.py's credential loading (_load_env) but NOT account.py
itself for signing, since account.py is deliberately read-only by design
("no order-placement code" per its own docstring). This file is the one
place in the codebase that ever calls POST/DELETE on the Kalshi order
API, and only when bot_broker.LiveBroker is unlocked AND broker_mode is
"auto" (see bot_broker.py) -- manual mode never imports this module's
placement functions at all, only account._load_env indirectly isn't even
needed there since emit_live_signal makes no API call.
"""
import base64
import time

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import account

HOST = account.HOST
ORDERS_PATH = "/trade-api/v2/portfolio/orders"


def _load_private_key(path):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def _sign(key_id: str, pk, method: str, full_path: str):
    ts = str(int(time.time() * 1000))
    sig = pk.sign(f"{ts}{method}{full_path}".encode(),
                  padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                              salt_length=padding.PSS.MAX_LENGTH),
                  hashes.SHA256())
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
    }


def _headers(method: str, full_path: str):
    key_id, kp_path = account._load_env()
    pk = _load_private_key(kp_path)
    return _sign(key_id, pk, method, full_path)


def place_order(side: str, action: str, ticker: str, qty: int,
                price: float, order_type: str) -> dict:
    """side: "yes"/"no". action: "buy"/"sell". price in dollars (converted
    to integer cents for the API here, matching Kalshi's *_price fields).
    order_type: "limit" or "market". Returns the API's order object."""
    headers = _headers("POST", ORDERS_PATH)
    price_key = "yes_price" if side == "yes" else "no_price"
    body = {"side": side, "action": action, "ticker": ticker, "count": qty,
            "type": order_type}
    if order_type == "limit":
        body[price_key] = round(price * 100)
    r = requests.post(HOST + ORDERS_PATH, headers=headers, json=body, timeout=15)
    r.raise_for_status()
    return r.json()["order"]


def get_order(order_id: str) -> dict:
    path = f"{ORDERS_PATH}/{order_id}"
    headers = _headers("GET", path)
    r = requests.get(HOST + path, headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()["order"]


def cancel_order(order_id: str) -> dict:
    path = f"{ORDERS_PATH}/{order_id}"
    headers = _headers("DELETE", path)
    r = requests.delete(HOST + path, headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()["order"]
