"""Brokers for the swing bot.

PaperBroker fills pessimistically: buy at the ask, sell at ask - spread,
Kalshi fee (backtest_gate.fee) charged per contract on both sides. If paper
wins under these costs, live has a real shot.
"""
import os
import requests  # noqa: F401 — kept so manual-mode tests can assert no network path
from cryptography.hazmat.primitives import serialization

import account
from backtest_gate import fee, maker_fee

import json
import time
from pathlib import Path

LIVE_SIGNALS_FILE = "live_signals.jsonl"


def emit_live_signal(bot_dir, ticker: str, side: str, qty: int, price: float,
                     tier: str, pool: str, error: str = None) -> None:
    """Append one row to <bot_dir>/live_signals.jsonl -- the manual-mode
    broker path: instead of placing a real order, this is the signal a
    human reads and places by hand. Formalizes the ad hoc scratchpad
    watcher script used for the first night of the $20 live test into a
    real, tested code path (see the 2026-07-29 design spec)."""
    bot_dir = Path(bot_dir)
    bot_dir.mkdir(parents=True, exist_ok=True)
    row = {"ts": time.time(), "ticker": ticker, "side": side, "qty": qty,
           "price": price, "tier": tier, "pool": pool, "error": error}
    with (bot_dir / LIVE_SIGNALS_FILE).open("a") as f:
        f.write(json.dumps(row) + "\n")


class PaperBroker:
    mode = "paper"

    def buy(self, side: str, qty: int, sig: dict) -> dict:
        price = sig["yes_ask"] if side == "YES" else sig["no_ask"]
        return self.fill(price, qty, sig.get("ts") or 0.0)

    def sell(self, side: str, qty: int, sig: dict) -> dict:
        ask = sig["yes_ask"] if side == "YES" else sig["no_ask"]
        # A crossed book (yes_ask + no_ask < 1) yields a negative spread;
        # clamp at 0 so the sell price never lands above the ask.
        spread = max(0.0, sig.get("spread") or 0.0)
        price = max(0.01, round(ask - spread, 4))
        return self.fill(price, qty, sig.get("ts") or 0.0)

    def fill(self, price: float, qty: int, ts: float, maker: bool = False,
             sig: dict = None, order_id: str = None) -> dict:
        """A fill at an explicit price -- buy()/sell() are always taker
        (market-style, immediate); a resting limit order that actually
        waited to be touched calls this directly with maker=True. See
        backtest_gate.maker_fee for the maker-rate caveat. `sig`/`order_id`
        are unused here (PaperBroker doesn't need them) -- accepted only
        so callers can pass one shared signature to either PaperBroker or
        LiveBroker (see LiveBroker.fill's docstring for what order_id
        actually controls there)."""
        f = maker_fee if maker else fee
        return {"price": price, "qty": qty,
                "fee_total": round(f(price) * qty, 4),
                "ts": ts, "maker": maker}


def round_trip_pnl(entry_fill: dict, exit_fill: dict) -> float:
    gross = (exit_fill["price"] - entry_fill["price"]) * entry_fill["qty"]
    return round(gross - entry_fill["fee_total"] - exit_fill["fee_total"], 4)


FALLBACK_BANKROLL = 500.0


def _balance_dollars() -> float:
    """Signed read-only balance GET, reusing account.py's credential scheme."""
    key_id, kp_path = account._load_env()
    with open(kp_path, "rb") as f:
        pk = serialization.load_pem_private_key(f.read(), password=None)
    bal = account._get(key_id, pk, "/trade-api/v2/portfolio/balance")
    if bal.get("balance_dollars") is not None:
        return float(bal["balance_dollars"])
    return float(bal.get("balance", 0)) / 100.0


def fetch_bankroll():
    """Cash balance in dollars, or None on any failure (caller keeps cache)."""
    try:
        return _balance_dollars()
    except BaseException:   # account._load_env raises SystemExit when creds missing
        return None


def live_capability_ok(cfg: dict, env: dict):
    """Coarse, cheap, construction-time check: is live trading available
    AT ALL right now, for at least one session? Never looks at trade
    history -- that only matters once, at the moment a session is
    toggled live via POST /api/bot/live_session (see web.py). Returns
    (ok, reason)."""
    if not cfg.get("live_sessions_requested"):
        return False, "no session has been toggled live"
    if env.get("BOT_LIVE") != "1":
        return False, "BOT_LIVE=1 not set in environment"
    return True, "unlocked"


def live_unlock_ok(cfg: dict, env: dict, session: str):
    """Per-entry check: is THIS specific session unlocked for live
    trading? Per Kenny 2026-07-29: once a session is toggled live it
    stays live regardless of later performance (no auto-disable on
    regression) -- so this never re-derives session_gate_stats from
    trade history, it only checks live_sessions_requested + BOT_LIVE.
    The 100-trade/positive-net-avg bar is checked exactly once, at
    enable-time, server-side in web.py's live_session endpoint. Returns
    (ok, reason)."""
    if session not in (cfg.get("live_sessions_requested") or []):
        return False, f"{session} not toggled live"
    if env.get("BOT_LIVE") != "1":
        return False, "BOT_LIVE=1 not set in environment"
    return True, "unlocked"


class LiveBroker:
    """Real order placement, gated by live_unlock_ok. broker_mode controls
    what buy/sell/fill actually do once unlocked:
      "manual" (default): emit a live_signal event, no order API call.
      "auto": place a real order via live_broker.py.
    See the 2026-07-29 design spec for the full mode matrix."""
    mode = "live"

    def __init__(self, cfg: dict, env: dict = None, bot_dir=None):
        ok, reason = live_capability_ok(cfg, env if env is not None else dict(os.environ))
        if not ok:
            raise RuntimeError(f"live trading locked: {reason}")
        self.cfg = cfg
        self.env = env if env is not None else dict(os.environ)
        self.bot_dir = bot_dir
        self.broker_mode = cfg.get("broker_mode", "manual")

    def _pool_of(self, sig: dict) -> str:
        from bot_core import session_tag
        return session_tag(sig.get("ts"))

    def _signal_fill(self, side, qty, price, sig, tier="market") -> dict:
        emit_live_signal(self.bot_dir, sig.get("ticker"), side, qty, price,
                         tier, self._pool_of(sig))
        return {"price": price, "qty": qty, "fee_total": 0.0,
                "ts": sig.get("ts") or 0.0, "maker": False, "signal_only": True}

    def _auto_fill(self, side, action, qty, price, sig, order_type, tier) -> dict:
        import live_broker
        kalshi_side = "yes" if side == "YES" else "no"
        try:
            order = live_broker.place_order(kalshi_side, action, sig.get("ticker"),
                                            qty, price, order_type)
        except Exception as e:
            emit_live_signal(self.bot_dir, sig.get("ticker"), side, qty, price,
                             tier, self._pool_of(sig), error=str(e))
            raise
        fee_price_key = "yes_price" if kalshi_side == "yes" else "no_price"
        fill_price = (order.get(fee_price_key) or round(price * 100)) / 100.0
        maker = order_type == "limit"
        from backtest_gate import fee as taker_fee, maker_fee
        f = maker_fee if maker else taker_fee
        return {"price": fill_price, "qty": qty,
                "fee_total": round(f(fill_price) * qty, 4),
                "ts": sig.get("ts") or 0.0, "maker": maker,
                "order_id": order.get("order_id")}

    def buy(self, side: str, qty: int, sig: dict) -> dict:
        price = sig["yes_ask"] if side == "YES" else sig["no_ask"]
        if self.broker_mode == "manual":
            return self._signal_fill(side, qty, price, sig)
        return self._auto_fill(side, "buy", qty, price, sig, "market", "market")

    def sell(self, side: str, qty: int, sig: dict) -> dict:
        ask = sig["yes_ask"] if side == "YES" else sig["no_ask"]
        spread = max(0.0, sig.get("spread") or 0.0)
        price = max(0.01, round(ask - spread, 4))
        if self.broker_mode == "manual":
            return self._signal_fill(side, qty, price, sig)
        return self._auto_fill(side, "sell", qty, price, sig, "market", "market")

    def fill(self, price: float, qty: int, ts: float, maker: bool = False,
             sig: dict = None, order_id: str = None) -> dict:
        """Explicit-price fill -- the resting-limit-order path
        (swing_bot._process_pending calls this the same way it calls
        PaperBroker.fill). sig is required here (unlike PaperBroker) to
        resolve the ticker/pool for emit_live_signal / place_order.

        order_id matters only in auto mode: swing_bot._place_entry already
        places the REAL resting limit order up front (to get an order_id to
        poll) -- by the time _process_pending confirms it executed and
        calls fill(..., maker=True) to finalize the accounting, the order
        has ALREADY happened. Without this parameter, fill() would call
        _auto_fill -> place_order again and place a SECOND real order for
        the same intended position. When order_id is provided, skip
        placement entirely and just build the fill dict from the already-
        known execution price. order_id is None for a genuinely new
        placement (the chase-to-market path, after the original resting
        order was cancelled, and plain buy()/sell() calls)."""
        sig = sig or {}
        side = sig.get("side", "YES")
        tier = "patient" if maker else "market"
        if self.broker_mode == "manual":
            return self._signal_fill(side, qty, price, sig, tier=tier)
        if order_id is not None:
            from backtest_gate import fee as taker_fee, maker_fee
            f = maker_fee if maker else taker_fee
            return {"price": price, "qty": qty,
                    "fee_total": round(f(price) * qty, 4),
                    "ts": ts, "maker": maker, "order_id": order_id}
        order_type = "limit" if maker else "market"
        return self._auto_fill(side, "buy", qty, price, sig, order_type, tier)
