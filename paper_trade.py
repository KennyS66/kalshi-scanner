#!/usr/bin/env python3
"""
Paper-trade ledger for the live entry signals.

Reconstructs every FLUSH_BOUNCE / NO_ENTRY entry from the durable signal
feature log (data/whales/signal_feature_log.jsonl), joins settled outcomes,
and records a hold-to-settlement paper trade for each. No real orders — this
just builds an honest forward-test track record while capital stays sidelined.

Writes the ledger to data/whales/paper_trades.jsonl and prints a summary.
Reuses the exact entry predicates from backtest_gate.py so the paper trades
match what the gate evaluates.

Usage:  python3 paper_trade.py
"""
import json
from pathlib import Path

from backtest_gate import (
    DATA, OUTCOMES, FEATURE_LOG, fee, flush_entry, no_entry,
)

LEDGER = DATA / "paper_trades.jsonl"

ENTRIES = [
    # (label, side, predicate)
    ("FLUSH_BOUNCE", "YES", flush_entry),
    ("NO_ENTRY", "NO", no_entry),
]


def load():
    y = {t: (1 if v == "YES" else 0) for t, v in json.load(open(OUTCOMES)).items()} if OUTCOMES.exists() else {}
    by_ticker = {}
    if FEATURE_LOG.exists():
        with open(FEATURE_LOG) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                t = r.get("ticker")
                if not t or "BTC15M" not in t:
                    continue
                by_ticker.setdefault(t, []).append(r)
    for t in by_ticker:
        by_ticker[t].sort(key=lambda r: r.get("ts", 0))
    return by_ticker, y


def build_ledger():
    by_ticker, y = load()
    trades = []
    for t, rows in by_ticker.items():
        for label, side, pred in ENTRIES:
            entry = next((r for r in rows if pred(r)), None)
            if entry is None:
                continue
            yes_price = entry.get("price") or 0.0
            cost = yes_price if side == "YES" else (1 - yes_price)
            f = fee(cost)
            outcome = y.get(t)  # 1 / 0 / None
            won = None
            settle_pnl = None
            if outcome is not None:
                won = (outcome == 1) if side == "YES" else (outcome == 0)
                settle_pnl = round((1.0 if won else 0.0) - cost - f, 4)
            trades.append({
                "ticker": t,
                "type": label,
                "side": side,
                "entry_ts": round(entry.get("ts", 0), 1),
                "entry_yes_price": round(yes_price, 4),
                "cost": round(cost, 4),
                "fee": round(f, 4),
                "mins_left": entry.get("mins_left"),
                "features": {
                    "flush_score": entry.get("flush_score"),
                    "buy_pressure": entry.get("buy_pressure"),
                    "sig_combined": entry.get("sig_combined"),
                    "whale_trend": entry.get("whale_trend"),
                    "distance": entry.get("distance"),
                },
                "outcome": "YES" if outcome == 1 else "NO" if outcome == 0 else None,
                "won": won,
                "settle_pnl": settle_pnl,
            })
    trades.sort(key=lambda x: x["entry_ts"])
    return trades


def summarize(trades):
    resolved = [t for t in trades if t["settle_pnl"] is not None]
    openp = [t for t in trades if t["settle_pnl"] is None]
    print(f"\n=== PAPER-TRADE LEDGER  ({len(trades)} entries: "
          f"{len(resolved)} settled, {len(openp)} open) ===")
    if not trades:
        print("No paper entries yet — waiting for the first ENTRY signal to fire.")
        return
    print(f"{'when':>12} {'ticker':32} {'type':12} {'side':4} {'cost':>6} {'outcome':>7} {'pnl/contract':>12}")
    for t in trades:
        pnl = f"{t['settle_pnl']:+.3f}" if t["settle_pnl"] is not None else "  (open)"
        oc = t["outcome"] or "—"
        print(f"{t['entry_ts']:>12.0f} {t['ticker']:32} {t['type']:12} {t['side']:4} "
              f"{t['cost']:>6.3f} {oc:>7} {pnl:>12}")
    if resolved:
        tot = sum(t["settle_pnl"] for t in resolved)
        wins = sum(1 for t in resolved if t["won"])
        print(f"\nSETTLED: {len(resolved)} trades | win {100*wins/len(resolved):.0f}% | "
              f"total P&L ${tot:+.3f}/contract | avg ${tot/len(resolved):+.4f}/contract")
        for label, _, _ in ENTRIES:
            sub = [t for t in resolved if t["type"] == label]
            if sub:
                st = sum(x["settle_pnl"] for x in sub)
                w = sum(1 for x in sub if x["won"])
                print(f"  {label:12} n={len(sub):2} win={100*w/len(sub):3.0f}% avg=${st/len(sub):+.4f}")
        print("\n(Hold-to-settlement, after approx fees. Negative avg = no edge — keep capital out.)")
    else:
        print("\nNo settled paper trades yet.")


def main():
    trades = build_ledger()
    DATA.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("w") as f:
        for t in trades:
            f.write(json.dumps(t) + "\n")
    summarize(trades)
    print(f"\nLedger written to {LEDGER}")


if __name__ == "__main__":
    main()
