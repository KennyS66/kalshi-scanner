#!/usr/bin/env python3
"""
Walk-forward rule search for the autotrader.

Splits the historical signal log into TRAIN (older 60% of markets) and TEST
(newer 40%). Grid-searches selective + wide-target + stop-loss rules on TRAIN,
picks the best by net PnL (with a minimum-trades guard so we don't crown a
2-trade fluke), then runs that single winner on TEST — data it never saw.

The TEST number is the only one that matters. If TRAIN looks great and TEST
doesn't, the rule was overfit noise and we do NOT go live.

Reuses autotrader.Trader so the simulation is byte-identical to the real bot.
"""
import json
from types import SimpleNamespace

from autotrader import Trader, FEATURE_LOG


def base_cfg(**over):
    cfg = dict(
        mode="replay", bankroll=10.0, max_trade_cents=50.0, daily_loss_cents=1000.0,
        max_dd_frac=0.99,  # don't let the breaker truncate a backtest
        exit_strategy="low", target_mode="fixed", target_cents=13.0, stop_cents=0.0,
        max_entry_cents=99.0, min_entry_mins=3.0, exit_mins=2.0, min_conf=5.0,
        decay_thresh=999.0,  # scratch off by default; the hard stop handles downside
        # conviction filter
        min_sig=0.0, min_agree=0,
        # concurrent positions (single during rule search)
        max_positions=1, max_per_market=1, reentry_gap_c=3.0,
        # sizing (flat during search — we optimize the rule, not the bet curve)
        sizing="flat", base_trade_cents=50.0, max_scale=1.0, size_window=40, size_min_n=20,
        kill_file="", verbose=False, write_files=False,
    )
    cfg.update(over)
    return SimpleNamespace(**cfg)


def load_split(train_frac=0.60):
    rows = []
    with FEATURE_LOG.open() as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    # ordered distinct tickers = chronological markets
    order = []
    for r in rows:
        t = r.get("ticker")
        if t and (not order or order[-1] != t):
            order.append(t)
    cut = int(len(order) * train_frac)
    train_tk, test_tk = set(order[:cut]), set(order[cut:])
    train = [r for r in rows if r.get("ticker") in train_tk]
    test = [r for r in rows if r.get("ticker") in test_tk]
    return train, test, len(order), cut


def run(rows, cfg):
    tr = Trader(cfg)
    for s in rows:
        tr.step(s)
    n = len(tr.rounds)
    if not n:
        return {"n": 0, "net_c": 0.0, "wr": 0.0, "end": cfg.bankroll}
    net = tr.realized_c
    wins = sum(1 for r in tr.rounds if r["pnl_c"] > 0)
    return {"n": n, "net_c": net, "wr": wins / n, "end": cfg.bankroll + net / 100, "halted": tr.halted}


def main():
    train, test, n_markets, cut = load_split()
    print(f"markets: {n_markets} | train (older): {cut} | test (newer): {n_markets - cut}\n")

    grid = []
    for target in (13, 16, 20):
        for stop in (8, 10):
            for max_entry in (40, 45):
                for conf in (5, 25):
                    for min_sig in (0, 20, 40):        # conviction: combined-signal strength
                        for min_agree in (0, 2, 3):    # conviction: sub-signals that must agree
                            grid.append(dict(target_cents=target, stop_cents=stop,
                                             max_entry_cents=max_entry, min_conf=conf,
                                             min_sig=min_sig, min_agree=min_agree))

    MIN_TRADES = 12  # don't trust a rule that barely traded on TRAIN
    scored = []
    for g in grid:
        r = run(train, base_cfg(**g))
        if r["n"] >= MIN_TRADES:
            scored.append((r["net_c"], g, r))
    scored.sort(reverse=True, key=lambda x: x[0])

    print("=== TRAIN: top 5 rule sets (by net) ===")
    for net, g, r in scored[:5]:
        print(f"  tgt+{g['target_cents']:>2} stop{g['stop_cents']:>2} maxEntry{g['max_entry_cents']:>2} "
              f"conf{g['min_conf']:>2} sig{g['min_sig']:>2} agree{g['min_agree']} | "
              f"n={r['n']:>3} wr={100*r['wr']:>3.0f}% net={net:>+7.1f}c end=${r['end']:.2f}")

    if not scored:
        print("No rule traded enough on TRAIN. Insufficient data — do NOT go live.")
        return

    best_g = scored[0][1]
    print(f"\n=== OUT-OF-SAMPLE TEST (newer markets, never seen) ===")
    print(f"chosen rule: target+{best_g['target_cents']}c, stop {best_g['stop_cents']}c, "
          f"max-entry {best_g['max_entry_cents']}c, min-conf {best_g['min_conf']}, "
          f"min-sig {best_g['min_sig']}, min-agree {best_g['min_agree']}")
    oos = run(test, base_cfg(**best_g))
    print(f"TEST result: n={oos['n']} wr={100*oos['wr']:.0f}% net={oos['net_c']:+.1f}c "
          f"(${oos['net_c']/100:+.2f}) ending ${oos['end']:.2f}")
    verdict = "PASS — real edge survives out-of-sample" if oos["net_c"] > 0 and oos["n"] >= 5 \
        else "FAIL — does not hold out-of-sample; DO NOT go live"
    print(f"\nVERDICT: {verdict}")
    # emit machine-readable line for the caller
    print(f"WF_RESULT: {'PASS' if oos['net_c']>0 and oos['n']>=5 else 'FAIL'} "
          f"target={best_g['target_cents']} stop={best_g['stop_cents']} "
          f"max_entry={best_g['max_entry_cents']} min_conf={best_g['min_conf']} "
          f"test_net_c={oos['net_c']:.1f} test_n={oos['n']}")


if __name__ == "__main__":
    main()
