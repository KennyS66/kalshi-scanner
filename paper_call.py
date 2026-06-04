#!/usr/bin/env python3
"""
Active paper-trade journal — log your OWN call + conviction the moment a
TRADEABLE-ENTRY fires, then see whether your gut (and your conviction) had edge.

This is the human-in-the-loop companion to paper_trade.py (which auto-derives
the mechanical signal trades). Here YOU make the call; we grade it after settle.
No real orders.

record:  python3 paper_call.py record SIDE CONVICTION ["note"]
           SIDE       = YES | NO
           CONVICTION = 1..5
         Snapshots the current live signal (ticker, price, features) from
         the running scanner and appends it to data/whales/paper_calls.jsonl.

report:  python3 paper_call.py report
           Joins logged calls to settled outcomes; shows hit rate, paper P&L,
           and whether higher conviction actually meant higher win rate.
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

from backtest_gate import DATA, OUTCOMES, fee

CALLS = DATA / "paper_calls.jsonl"
API = "http://localhost:9050/api/crypto/signal"


def fetch_signal():
    with urllib.request.urlopen(API, timeout=4) as r:
        return json.loads(r.read())


def record(side, conviction, note):
    side = side.upper()
    if side not in ("YES", "NO"):
        sys.exit("SIDE must be YES or NO")
    try:
        conviction = int(conviction)
        assert 1 <= conviction <= 5
    except Exception:
        sys.exit("CONVICTION must be 1..5")
    sig = fetch_signal()
    if sig.get("status") != "ok":
        sys.exit(f"no live market (status={sig.get('status')})")
    yes_price = sig.get("price") or 0.0
    cost = yes_price if side == "YES" else (1 - yes_price)
    rec = {
        "ts": round(time.time(), 1),
        "ticker": sig.get("ticker"),
        "side": side,
        "conviction": conviction,
        "note": note or "",
        "yes_price": round(yes_price, 4),
        "cost": round(cost, 4),
        "fee": round(fee(cost), 4),
        "mins_left": sig.get("mins_left"),
        "features": {
            "model_dir": sig.get("direction"),
            "confidence": sig.get("confidence"),
            "flush_score": sig.get("flush_score"),
            "buy_pressure": sig.get("buy_pressure"),
            "sig_combined": sig.get("sig_combined"),
            "whale_trend": sig.get("whale_trend"),
            "distance": sig.get("distance"),
        },
    }
    DATA.mkdir(parents=True, exist_ok=True)
    with CALLS.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    agree = "agrees with" if rec["features"]["model_dir"] == side else "FADES"
    print(f"Logged: {side} conv={conviction} on {rec['ticker']} @ NO/YES cost "
          f"${cost:.3f} ({mins(rec['mins_left'])} left) — your call {agree} the model.")


def mins(m):
    return f"{m:.1f}m" if isinstance(m, (int, float)) else "?"


def report():
    if not CALLS.exists():
        print("No calls logged yet. Use: paper_call.py record SIDE CONVICTION \"note\"")
        return
    y = {t: (1 if v == "YES" else 0) for t, v in json.load(open(OUTCOMES)).items()} if OUTCOMES.exists() else {}
    calls = [json.loads(l) for l in open(CALLS) if l.strip()]
    print(f"=== YOUR PAPER CALLS ({len(calls)} logged) ===")
    print(f"{'when':>12} {'ticker':30} {'side':4} {'conv':>4} {'cost':>6} {'result':>8} {'pnl':>8}  note")
    resolved = []
    for c in calls:
        oc = y.get(c["ticker"])
        if oc is None:
            res, pnl = "open", None
        else:
            won = (oc == 1) if c["side"] == "YES" else (oc == 0)
            pnl = round((1.0 if won else 0.0) - c["cost"] - c["fee"], 4)
            res = "WIN" if won else "loss"
            resolved.append((c, won, pnl))
        pnls = f"{pnl:+.3f}" if pnl is not None else "  —"
        print(f"{c['ts']:>12.0f} {c['ticker']:30} {c['side']:4} {c['conviction']:>4} "
              f"{c['cost']:>6.3f} {res:>8} {pnls:>8}  {c['note'][:30]}")
    if resolved:
        tot = sum(p for _, _, p in resolved)
        wins = sum(1 for _, w, _ in resolved if w)
        n = len(resolved)
        print(f"\nSETTLED {n} | win {100*wins/n:.0f}% | P&L ${tot:+.3f}/contract | avg ${tot/n:+.4f}")
        print("By conviction:")
        for lvl in range(1, 6):
            sub = [(c, w, p) for c, w, p in resolved if c["conviction"] == lvl]
            if sub:
                w = sum(1 for _, ww, _ in sub if ww)
                p = sum(pp for _, _, pp in sub)
                print(f"  conv {lvl}: n={len(sub):2} win={100*w/len(sub):3.0f}% avg=${p/len(sub):+.4f}")
        print("\n(If higher conviction != higher win rate, your gut isn't information. "
              "If win% < breakeven for the price paid, neither gut nor signal has edge.)")
    else:
        print("\nNo settled calls yet.")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("record", "report"):
        print(__doc__)
        return
    if sys.argv[1] == "report":
        report()
        return
    if len(sys.argv) < 4:
        sys.exit('usage: paper_call.py record SIDE CONVICTION ["note"]')
    record(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "")


if __name__ == "__main__":
    main()
