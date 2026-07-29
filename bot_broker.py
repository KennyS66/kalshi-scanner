"""Brokers for the swing bot.

PaperBroker fills pessimistically: buy at the ask, sell at ask - spread,
Kalshi fee (backtest_gate.fee) charged per contract on both sides. If paper
wins under these costs, live has a real shot.
"""
import os
from cryptography.hazmat.primitives import serialization

import account
from backtest_gate import fee, maker_fee


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

    def fill(self, price: float, qty: int, ts: float, maker: bool = False) -> dict:
        """A fill at an explicit price -- buy()/sell() are always taker
        (market-style, immediate); a resting limit order that actually
        waited to be touched calls this directly with maker=True. See
        backtest_gate.maker_fee for the maker-rate caveat."""
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


def live_unlock_ok(trades: list, cfg: dict, env: dict):
    """The unlock bar: 100 settled trades AND a positive net avg, each
    independently proven in EVERY session (weekday_day, weekday_night,
    weekend_day, weekend_night — per Kenny 2026-07-21, replacing the
    original 2026-07-17 weekday+weekend-combined bar). A strong weekday
    can no longer mask a negative weekend average, or vice versa — each
    of the 4 tape regimes must clear the bar on its own. Session is taken
    from the entry, same as bot_core.session_tag's other callers (EV
    buckets, pool attribution, session_report.py), not the exit — this
    was previously exit_ts-keyed and weekday/weekend-only; both changed
    together since the finer split needs the finer (entry-based) tag.
    Returns (ok, reason)."""
    from bot_core import POOL_NAMES, session_gate_stats
    stats = session_gate_stats(trades)
    short = []
    for p in POOL_NAMES:
        st = stats[p]
        if st["n"] < 100:
            short.append(f"{p} {st['n']}/100")
        elif st["net_avg"] <= 0:
            short.append(f"{p} net avg {st['net_avg']:+.4f} <= 0")
    if short:
        return False, "not proven: " + "; ".join(short)
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
