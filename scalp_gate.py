#!/usr/bin/env python3
"""
Scalp gate — tests the wave / liquidity-flip momentum scalp with MARKET-ORDER
costs baked in. This is the strategy Kenny actually traded: enter on a flow
flip, ride the contract-price move, exit before settlement (flat — outcome
doesn't matter), crossing the spread both ways and paying Kalshi fees twice.

Entry : buy_pressure flips sign (|bp| >= threshold). Bullish flip -> buy YES,
        bearish -> buy NO. One position per market at a time.
Exit  : opposite flip, OR mins_left <= exit_mins (close before settlement).
Costs : market order => pay half-spread each side + fee 0.07*P*(1-P) each side.

Reads data/whales/signal_feature_log.jsonl (written by web.api_crypto_signal).
Prints GROSS (price move captured) vs NET (after costs) and a verdict.

For the scalp to beat market-order costs the NET avg must be > 0 — which the
first-pass momentum math said needs the gross edge to ~quadruple. This measures
whether YOUR specific flip trigger clears that bar.

Usage:  python3 scalp_gate.py [--bp 10000] [--exit-mins 2] [--min-n 30]
Final line: "SCALP_RESULT: <PASS|FAIL|INSUFFICIENT> ..."
"""
import argparse
import json

from backtest_gate import DATA, FEATURE_LOG, fee


def load():
    by = {}
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
                by.setdefault(t, []).append(r)
    for t in by:
        by[t].sort(key=lambda r: r.get("ts", 0))
    return by


def simulate(by, bp_thresh, exit_mins):
    trades = []
    for t, rows in by.items():
        pos = None
        prev_sign = None
        for r in rows:
            yes = r.get("price")
            if yes is None:
                continue
            bp = r.get("buy_pressure") or 0
            hs = (r.get("spread") or 0) / 2.0
            ml = r.get("mins_left")
            ml = 99 if ml is None else ml
            sign = 1 if bp > 0 else (-1 if bp < 0 else 0)

            # ---- exit if in a position ----
            if pos is not None:
                opp_flip = sign != 0 and sign != pos["dir"] and abs(bp) >= bp_thresh
                if opp_flip or ml <= exit_mins:
                    if pos["side"] == "YES":
                        exit_val = yes - hs - fee(yes)
                    else:
                        exit_val = (1 - yes) - hs - fee(1 - yes)
                    pos["exit_yes"] = round(yes, 4)
                    pos["net"] = round(exit_val - pos["entry_cost"], 4)
                    # gross = raw price move captured, ignoring spread+fees
                    if pos["side"] == "YES":
                        pos["gross"] = round(yes - pos["entry_yes"], 4)
                    else:
                        pos["gross"] = round((1 - yes) - (1 - pos["entry_yes"]), 4)
                    trades.append(pos)
                    pos = None

            # ---- entry on a fresh flip, only if flat ----
            if pos is None and sign != 0 and sign != prev_sign and abs(bp) >= bp_thresh and ml > exit_mins:
                if sign > 0:
                    side = "YES"; entry_cost = yes + hs + fee(yes); gross_entry = yes
                else:
                    side = "NO"; entry_cost = (1 - yes) + hs + fee(1 - yes); gross_entry = 1 - yes
                pos = {"ticker": t, "side": side, "dir": sign,
                       "entry_yes": round(yes, 4), "entry_cost": round(entry_cost, 4),
                       "gross_entry": round(gross_entry, 4), "entry_ml": ml}
            if sign != 0:
                prev_sign = sign
    return trades


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bp", type=float, default=10000, help="min |buy_pressure| to count as a flip")
    ap.add_argument("--exit-mins", type=float, default=2.0, help="force-exit when mins_left <= this")
    ap.add_argument("--min-n", type=int, default=30, help="min closed trades for a verdict")
    args = ap.parse_args()

    by = load()
    if not by:
        print("No signal_feature_log.jsonl yet.")
        print("SCALP_RESULT: INSUFFICIENT no_data")
        return
    trades = simulate(by, args.bp, args.exit_mins)
    n = len(trades)
    print(f"15m markets: {len(by)} | closed scalp trades: {n} "
          f"(bp>={args.bp:.0f}, exit<= {args.exit_mins}m)")
    if n == 0:
        print("SCALP_RESULT: INSUFFICIENT no_closed_trades")
        return
    gross = sum(t["gross"] for t in trades) / n
    net = sum(t["net"] for t in trades) / n
    wins = sum(1 for t in trades if t["net"] > 0) / n
    print(f"GROSS avg (move captured, no costs): {gross*100:+.2f}c/trade")
    print(f"NET   avg (after spread+fees x2):    {net*100:+.2f}c/trade")
    print(f"cost drag: {(gross-net)*100:.2f}c/trade | net win rate: {100*wins:.0f}%")
    if n < args.min_n:
        verdict = "INSUFFICIENT"
    elif net > 0:
        verdict = "PASS"
    else:
        verdict = "FAIL"
    print(f"SCALP_RESULT: {verdict} net={net*100:+.2f}c n={n} (need n>={args.min_n}, net>0)")


if __name__ == "__main__":
    main()
