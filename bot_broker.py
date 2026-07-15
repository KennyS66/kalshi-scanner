"""Brokers for the swing bot.

PaperBroker fills pessimistically: buy at the ask, sell at ask - spread,
Kalshi fee (backtest_gate.fee) charged per contract on both sides. If paper
wins under these costs, live has a real shot.
"""
import os
from cryptography.hazmat.primitives import serialization

import account
from backtest_gate import fee


class PaperBroker:
    mode = "paper"

    def buy(self, side: str, qty: int, sig: dict) -> dict:
        price = sig["yes_ask"] if side == "YES" else sig["no_ask"]
        return {"price": price, "qty": qty,
                "fee_total": round(fee(price) * qty, 4),
                "ts": sig.get("ts") or 0.0}

    def sell(self, side: str, qty: int, sig: dict) -> dict:
        ask = sig["yes_ask"] if side == "YES" else sig["no_ask"]
        price = max(0.01, round(ask - (sig.get("spread") or 0.0), 4))
        return {"price": price, "qty": qty,
                "fee_total": round(fee(price) * qty, 4),
                "ts": sig.get("ts") or 0.0}


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


def live_unlock_ok(trades: list, cfg: dict, env: dict):
    """The four-condition unlock bar from the spec. Returns (ok, reason)."""
    closed = [t for t in trades if t.get("status") == "closed"
              and t.get("net_pnl") is not None]
    if len(closed) < 100:
        return False, f"only {len(closed)}/100 settled paper trades"
    avg = sum(t["net_pnl"] for t in closed) / len(closed)
    if avg <= 0:
        return False, f"net avg {avg:+.4f} <= 0 — no proven edge"
    if not cfg.get("live_requested"):
        return False, "GUI live toggle not set"
    if env.get("BOT_LIVE") != "1":
        return False, "BOT_LIVE=1 not set in environment"
    return True, "unlocked"


class LiveBroker:
    """Locked stub — order placement intentionally unimplemented (spec v1)."""
    mode = "live"

    def __init__(self, trades: list, cfg: dict, env: dict = None):
        ok, reason = live_unlock_ok(trades, cfg, env if env is not None else dict(os.environ))
        if not ok:
            raise RuntimeError(f"live trading locked: {reason}")
        raise RuntimeError("live trading locked: order code not shipped in v1")

    def buy(self, *a, **k):
        raise RuntimeError("live trading locked")

    def sell(self, *a, **k):
        raise RuntimeError("live trading locked")
