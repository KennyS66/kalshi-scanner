#!/usr/bin/env python3
"""Sync the live Kalshi account's fill history and grade manual plays.

READ-ONLY against the account, by design and on purpose: it reuses
account.py's GET-only credential scheme and contains no order code. It
exists so Kenny's discretionary manual trading can be studied with the
same rigor as the bot's paper log, without ever mixing the two:

- `bot_trades.jsonl` stays the bot's own record; nothing here writes it.
- Account fills land in data/manual/account_fills.jsonl, tagged
  `bot` (matched a live_signals.jsonl row: same ticker+side within a
  time window) or `manual` (everything else -- Kenny's own plays).
  The tag is also the separation the balance-based live stop will need
  if broker_mode ever goes auto (see 2026-08-03 taint discussion).

Usage:
  python3 fills_sync.py            # sync fills+settlements, print report
  python3 fills_sync.py --report   # report only, no API calls
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
MANUAL_DIR = BASE / "data" / "manual"
FILLS_FILE = MANUAL_DIR / "account_fills.jsonl"
SETTLE_FILE = MANUAL_DIR / "account_settlements.jsonl"
BALANCE_FILE = MANUAL_DIR / "balance_history.jsonl"
LIVE_SIGNALS = BASE / "data" / "bot" / "live_signals.jsonl"

TAG_WINDOW_S = 180.0   # fill within this many seconds of a same-ticker+side
                       # live signal counts as bot-initiated


# ── pure logic (unit-tested) ──────────────────────────────────────────

def _parse_ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def normalize_fill(f: dict) -> dict:
    """One API fill -> the minimal row we store.

    Field names verified against the LIVE API 2026-08-03: prices are
    dollar strings (`yes_price_dollars`), counts are fractional strings
    (`count_fp`, e.g. "8.05"), fees are itemized (`fee_cost`), and an
    epoch `ts` is provided. The older docs' cents/int schema is kept as
    a fallback so recorded fixtures and any API rollback still parse."""
    side = (f.get("side") or "").upper()
    if f.get("yes_price_dollars") is not None:
        price = float(f["yes_price_dollars"] if side == "YES"
                      else f["no_price_dollars"])
    else:
        price_c = f.get("yes_price") if side == "YES" else f.get("no_price")
        price = (price_c or 0) / 100.0
    qty = float(f["count_fp"]) if f.get("count_fp") is not None \
        else float(f.get("count") or 0)
    ts = float(f["ts"]) if f.get("ts") is not None \
        else _parse_ts(f["created_time"])
    return {"id": f.get("trade_id") or f.get("fill_id"),
            "ts": ts,
            "ticker": f.get("ticker"),
            "side": side,
            "action": f.get("action"),
            "qty": qty,
            "price": round(price, 4),
            "fee": float(f.get("fee_cost") or 0.0),
            "taker": bool(f.get("is_taker"))}


def merge_fills(old: list, new: list) -> list:
    """Dedupe by id (first write wins), chronological order."""
    seen, out = set(), []
    for r in list(old) + list(new):
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        out.append(r)
    out.sort(key=lambda r: r["ts"])
    return out


def tag_fills(fills: list, signals: list, window_s: float = TAG_WINDOW_S) -> list:
    """source=bot when a live signal for the same ticker+side exists within
    window_s of the fill; source=manual otherwise. Conservative on
    purpose: an untagged bot fill only understates the bot, while a
    mis-tagged manual fill would pollute the bot-vs-manual comparison."""
    out = []
    for f in fills:
        hit = any(s.get("ticker") == f["ticker"]
                  and (s.get("side") or "").upper() == f["side"]
                  and abs((s.get("ts") or 0) - f["ts"]) <= window_s
                  for s in signals)
        out.append({**f, "source": "bot" if hit else "manual"})
    return out


def grade_markets(fills: list, settlements: dict) -> list:
    """Collapse fills per market into one graded play.

    **Deliberately does NOT compute P&L.** Kalshi reports each fill from
    both book sides (a row carries yes_price_dollars AND no_price_dollars
    summing to 1.00) and Kenny flips sides inside a single market, so
    naive position reconstruction double counts: an earlier version of
    this function scored the account at +$408 lifetime on an account that
    has never held more than ~$30 and sat at $7.35 when checked. Rather
    than ship a number that cannot be reconciled with the balance, this
    reports only what the API states outright -- cash legs, itemized
    fees, Kalshi's own settlement `revenue`, and the settled result --
    and leaves true P&L to balance snapshots (balance_history.jsonl).

    `lean` is the side he committed the most buy-dollars to, i.e. his
    directional read; `won` is whether that read matched settlement. That
    is the learning signal, and it is unaffected by the accounting
    ambiguity above."""
    by = {}
    for f in sorted(fills, key=lambda r: r["ts"]):
        g = by.setdefault(f["ticker"], {
            "ticker": f["ticker"], "source": f["source"], "entry_ts": f["ts"],
            "cost": 0.0, "proceeds": 0.0, "fees": 0.0, "buy_cost": 0.0,
            "bought": 0.0, "sold": 0.0,
            "buy_by_side": {"YES": 0.0, "NO": 0.0}})
        g["fees"] += f.get("fee") or 0.0
        if f["action"] == "buy":
            g["cost"] += f["price"] * f["qty"]
            g["buy_cost"] += f["price"] * f["qty"]
            g["bought"] += f["qty"]
            g["buy_by_side"][f["side"]] += f["price"] * f["qty"]
        else:
            g["proceeds"] += f["price"] * f["qty"]
            g["sold"] += f["qty"]
    out = []
    for g in by.values():
        s = settlements.get(g["ticker"])
        g["avg_entry_price"] = round(g["buy_cost"] / g["bought"], 4) if g["bought"] else None
        for k in ("cost", "proceeds", "fees"):
            g[k] = round(g[k], 4)
        g["lean"] = max(g["buy_by_side"], key=g["buy_by_side"].get) \
            if any(g["buy_by_side"].values()) else None
        if s is None:
            g["result"], g["revenue"], g["won"] = None, None, None
        else:
            g["result"] = (s.get("result") or "").upper() or None
            g["revenue"] = round(float(s.get("revenue") or 0.0), 4)
            # "won" = was the directional read right, NOT a P&L claim.
            g["won"] = (g["lean"] == g["result"]) if g["lean"] and g["result"] else None
        out.append(g)
    out.sort(key=lambda r: r["entry_ts"])
    return out


def session_report(graded: list) -> dict:
    """{source: {session: {n, wins, fees}}} over settled plays.
    `wins` counts correct DIRECTIONAL READS (lean vs settlement), not
    profitable trades -- see grade_markets on why P&L is not derived."""
    from bot_core import session_tag
    rep = {}
    for g in graded:
        if g.get("won") is None:
            continue
        sess = session_tag(g.get("entry_ts"))
        b = rep.setdefault(g["source"], {}).setdefault(
            sess, {"n": 0, "wins": 0, "fees": 0.0})
        b["n"] += 1
        b["wins"] += 1 if g["won"] else 0
        b["fees"] = round(b["fees"] + (g["fees"] or 0.0), 4)
    return rep


# ── I/O + API (thin) ──────────────────────────────────────────────────

def _read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def _write_jsonl(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
    tmp.replace(path)


def _paged(get, path: str, key: str) -> list:
    """Drain a cursor-paginated GET endpoint. `get` takes (path, params):
    Kalshi's signature covers ONLY the path -- a query string in the
    signed text is a guaranteed 401 (verified live, 2026-08-03)."""
    out, cursor = [], None
    for _ in range(50):                        # hard page cap, no infinite loop
        params = {"limit": 200, **({"cursor": cursor} if cursor else {})}
        d = get(path, params)
        out.extend(d.get(key) or [])
        cursor = d.get("cursor")
        if not cursor:
            break
    return out


def sync() -> tuple[list, dict]:
    import account
    from cryptography.hazmat.primitives import serialization
    key_id, kp_path = account._load_env()
    with open(kp_path, "rb") as fh:
        pk = serialization.load_pem_private_key(fh.read(), password=None)

    def get(path, params=None):
        # account._get signs whatever it's handed, so keep the query OUT of
        # the signed path and pass it via requests instead.
        import base64
        import requests
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        ts = str(int(time.time() * 1000))
        sig = pk.sign(f"{ts}GET{path}".encode(),
                      padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                  salt_length=padding.PSS.MAX_LENGTH),
                      hashes.SHA256())
        r = requests.get(account.HOST + path, params=params or {}, headers={
            "KALSHI-ACCESS-KEY": key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()},
            timeout=15)
        r.raise_for_status()
        return r.json()

    raw_fills = _paged(get, "/trade-api/v2/portfolio/fills", "fills")
    raw_settle = _paged(get, "/trade-api/v2/portfolio/settlements", "settlements")

    fills = merge_fills(_read_jsonl(FILLS_FILE),
                        [normalize_fill(f) for f in raw_fills])
    fills = tag_fills(fills, _read_jsonl(LIVE_SIGNALS))
    _write_jsonl(FILLS_FILE, fills)

    settle_rows = _read_jsonl(SETTLE_FILE)
    known = {s["ticker"] for s in settle_rows}
    for s in raw_settle:
        if s.get("ticker") and s["ticker"] not in known:
            settle_rows.append({"ticker": s["ticker"],
                                "result": s.get("market_result"),
                                # authoritative payout, arrives in CENTS
                                "revenue": round((s.get("revenue") or 0) / 100.0, 4),
                                "settled_ts": _parse_ts(s["settled_time"])
                                if s.get("settled_time") else None})
            known.add(s["ticker"])
    _write_jsonl(SETTLE_FILE, settle_rows)

    # Balance snapshot: the ONLY authoritative P&L. Appended (not
    # rewritten) so the series survives, and deduped on the exchange's
    # own updated_ts so repeated runs don't inflate it.
    bal = get("/trade-api/v2/portfolio/balance")
    snap = {"ts": float(bal.get("updated_ts") or time.time()),
            "balance": float(bal.get("balance_dollars")
                             or (bal.get("balance", 0) / 100.0)),
            "portfolio_value": float(bal.get("portfolio_value") or 0) / 100.0}
    hist = _read_jsonl(BALANCE_FILE)
    if not hist or hist[-1]["ts"] != snap["ts"]:
        with BALANCE_FILE.open("a") as fh:
            fh.write(json.dumps(snap) + "\n")

    return fills, {s["ticker"]: s for s in settle_rows if s.get("result")}


def report(fills: list, settlements: dict) -> str:
    graded = grade_markets(fills, settlements)
    rep = session_report(graded)
    lines = [f"account fills: {len(fills)} "
             f"(bot {sum(1 for f in fills if f['source'] == 'bot')} / "
             f"manual {sum(1 for f in fills if f['source'] == 'manual')}) · "
             f"markets: {len(graded)}"]
    for src in ("manual", "bot"):
        for sess, b in sorted((rep.get(src) or {}).items()):
            lines.append(f"  {src:6s} {sess:14s} n={b['n']:<3d} "
                         f"read-right {100 * b['wins'] / b['n']:.0f}%  "
                         f"fees {b['fees']:.2f}")
    hist = _read_jsonl(BALANCE_FILE)
    if hist:
        first, last = hist[0], hist[-1]
        lines.append(f"  balance {last['balance']:.2f} "
                     f"(first snapshot {first['balance']:.2f}, "
                     f"{len(hist)} snapshots) — deposits not netted out")
    settled = [g for g in graded if g["won"] is not None]
    if settled:
        lines.append(f"  {len(settled)} settled markets · "
                     f"fees paid {sum(g['fees'] for g in settled):.2f} · "
                     f"P&L: see balance_history.jsonl (not derivable from fills)")
    open_plays = [g for g in graded if g["won"] is None]
    if open_plays:
        lines.append("  unsettled: " + ", ".join(g["ticker"] for g in open_plays))
    return "\n".join(lines)


if __name__ == "__main__":
    if "--report" in sys.argv:
        fills = _read_jsonl(FILLS_FILE)
        settle = {s["ticker"]: s["result"]
                  for s in _read_jsonl(SETTLE_FILE) if s.get("result")}
    else:
        fills, settle = sync()
    print(report(fills, settle))
