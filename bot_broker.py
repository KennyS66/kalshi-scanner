"""Brokers for the swing bot.

PaperBroker fills pessimistically: buy at the ask, sell at ask - spread,
Kalshi fee (backtest_gate.fee) charged per contract on both sides. If paper
wins under these costs, live has a real shot.
"""
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
