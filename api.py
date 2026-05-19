"""Kalshi API client for market data, trades, and orderbooks."""

import time
import hashlib
import base64
import requests

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
DEMO_URL = "https://external-api.demo.kalshi.co/trade-api/v2"


class KalshiAPI:
    """Lightweight client for Kalshi's public + authenticated endpoints."""

    def __init__(self, api_key=None, private_key_path=None, use_demo=False):
        self.base = DEMO_URL if use_demo else BASE_URL
        self.session = requests.Session()
        self.api_key = api_key
        self.private_key_path = private_key_path

        # Auth headers are only needed for orderbook; trades and markets are public
        if api_key and private_key_path:
            self._setup_auth()

    def _setup_auth(self):
        """Load RSA private key for request signing."""
        try:
            from cryptography.hazmat.primitives import serialization
            with open(self.private_key_path, "rb") as f:
                self._private_key = serialization.load_pem_private_key(f.read(), password=None)
        except Exception:
            self._private_key = None

    def _sign_request(self, method, path):
        """Generate auth headers for authenticated endpoints."""
        if not self.api_key or not hasattr(self, '_private_key') or not self._private_key:
            return {}

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        ts = str(int(time.time() * 1000))
        message = f"{ts}{method}{path}"
        signature = self._private_key.sign(
            message.encode(),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        }

    def _get(self, path, params=None, auth=False):
        url = f"{self.base}{path}"
        headers = self._sign_request("GET", path) if auth else {}
        resp = self.session.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()

    # ── Public endpoints ──────────────────────────────────────────────

    def get_markets(self, status="open", limit=200, cursor=None, event_ticker=None):
        """Fetch markets with optional filters."""
        params = {"limit": limit, "status": status}
        if cursor:
            params["cursor"] = cursor
        if event_ticker:
            params["event_ticker"] = event_ticker
        return self._get("/markets", params=params)

    def get_all_open_markets(self, max_pages=10):
        """Page through all active markets."""
        all_markets = []
        cursor = None
        for _ in range(max_pages):
            data = self.get_markets(status="open", limit=1000, cursor=cursor)
            markets = data.get("markets", [])
            all_markets.extend(markets)
            cursor = data.get("cursor", "")
            if not cursor:
                break
        return all_markets

    def get_markets_by_tickers(self, tickers):
        """Fetch specific markets by their tickers (comma-separated)."""
        params = {"tickers": ",".join(tickers), "limit": len(tickers)}
        return self._get("/markets", params=params)

    def get_trades(self, ticker=None, limit=1000, cursor=None, min_ts=None, max_ts=None):
        """Fetch trades, optionally filtered by ticker and time range."""
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if cursor:
            params["cursor"] = cursor
        if min_ts:
            params["min_ts"] = min_ts
        if max_ts:
            params["max_ts"] = max_ts
        return self._get("/markets/trades", params=params)

    def get_orderbook(self, ticker, depth=0):
        """Fetch orderbook for a market (requires auth)."""
        path = f"/markets/{ticker}/orderbook"
        params = {"depth": depth} if depth else {}
        return self._get(path, params=params, auth=True)
