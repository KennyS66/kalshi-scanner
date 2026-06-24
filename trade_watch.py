#!/usr/bin/env python3
"""Emit autotrader trade events (entries + closes w/ win/loss) since last check.

Keeps a cursor in data/whales/.trade_watch_cursor.json so each run only reports
NEW activity: positions that opened and rounds that closed. Prints nothing (exit
0, "NO_CHANGE") when there's nothing new, so a poller can stay quiet.
"""
import json
from pathlib import Path

DATA = Path(__file__).parent / "data" / "whales"
LEDGER = DATA / "autotrader_ledger.jsonl"
STATE = DATA / "autotrader_state.json"
CURSOR = DATA / ".trade_watch_cursor.json"


def load(p, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def main():
    cur = load(CURSOR, {"ledger_n": 0, "open_keys": []})
    rows = []
    if LEDGER.exists():
        rows = [json.loads(l) for l in LEDGER.read_text().splitlines() if l.strip()]
    state = load(STATE, {})

    events = []

    # --- new closes (win/loss) ---
    for r in rows[cur["ledger_n"]:]:
        pnl = r.get("pnl_c", 0)
        verdict = "WIN ✅" if pnl > 0.5 else ("LOSS ❌" if pnl < -0.5 else "SCRATCH ➖")
        events.append(
            f"CLOSE  {verdict}  {r.get('ticker','?')[-20:]} {r.get('side','?').upper()} "
            f"| entry {r.get('entry_c','?')}¢ -> exit {r.get('exit_c','?')}¢ "
            f"[{r.get('exit_reason','?')}] | pnl {pnl:+.1f}¢ "
            f"| bankroll ${r.get('bankroll_after', state.get('bankroll',0)):.2f}"
        )

    # --- new entries (open positions not seen before) ---
    open_now = state.get("open") or []
    def key(p):
        return f"{p.get('ticker')}|{p.get('entry_c')}|{p.get('count')}"
    seen = set(cur.get("open_keys", []))
    keys_now = []
    for p in open_now:
        k = key(p)
        keys_now.append(k)
        if k not in seen:
            events.append(
                f"ENTRY  {p.get('ticker','?')[-20:]} {p.get('side','?').upper()} x{p.get('count','?')} "
                f"@ {p.get('entry_c','?')}¢ -> target {p.get('target_c','?')}¢"
            )

    # persist cursor
    CURSOR.write_text(json.dumps({"ledger_n": len(rows), "open_keys": keys_now}))

    if not events:
        print("NO_CHANGE")
        return

    wins = sum(1 for r in rows if r.get("pnl_c", 0) > 0.5)
    n = len(rows)
    print("\n".join(events))
    if n:
        net = sum(r.get("pnl_c", 0) for r in rows)
        print(f"--- totals: {wins}/{n} wins | net {net:+.1f}¢ (${net/100:+.2f}) "
              f"| bankroll ${state.get('bankroll',0):.2f} | mode {state.get('mode','?')} ---")


if __name__ == "__main__":
    main()
