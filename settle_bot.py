#!/usr/bin/env python3
"""Directional hold-to-settlement paper strategy.

Enters on the LEVEL of sig_combined, buys that side as a maker, holds to
settlement. No targets, no stops, no time exit.

Deliberately shares nothing with swing_bot but the :9050 signal feed and
pure helpers from bot_core. Its journal must never reach data/bot/ --
bot_core.session_gate_stats and bucket_stats read that directory to decide
whether the OTHER strategy may trade live.

Spec: docs/superpowers/specs/2026-08-04-settle-bot-design.md
Run:  python3 -u settle_bot.py
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).parent
SETTLE_DIR = BASE / "data" / "settle"
CONFIG_FILE = "settle_config.json"
STATE_FILE = "settle_state.json"
TRADES_FILE = "settle_trades.jsonl"
EVENTS_FILE = "settle_events.jsonl"

DEFAULT_CONFIG = {
    "entry_threshold": 10.0,   # |sig_combined| bar; 10 not 20 (20 broke in W3)
    "min_mins_left": 5.0,      # flat top of the edge curve, not its peak
    "max_mins_left": 11.0,
    "qty": 1,                  # flat, always
    "poll_secs": 5,
    "mode": "paper",
}


def load_config(path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(Path(path).read_text()))
    except Exception:
        pass
    return cfg


def _utc_day(ts=None) -> str:
    return dt.datetime.fromtimestamp(ts or time.time(),
                                     dt.timezone.utc).strftime("%Y-%m-%d")


def fresh_state() -> dict:
    return {"day": _utc_day(), "heartbeat": 0.0, "pending": {}, "open": {},
            "reconcile_baseline": 0, "unresolved": 0}


def load_state(bot_dir) -> dict:
    try:
        return json.loads((Path(bot_dir) / STATE_FILE).read_text())
    except Exception:
        return fresh_state()


def save_state(bot_dir, state: dict) -> None:
    d = Path(bot_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, d / STATE_FILE)


def append_jsonl(path, row: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")


def entry_decision(sig: dict, cfg: dict):
    """Side to buy for this signal row, or None.

    The rule measured on 2026-08-04 over 1,452 markets: buy the side
    sig_combined points to, inside the entry window. Nothing else.
    """
    if sig.get("status") != "ok":
        return None
    if sig.get("yes_ask") is None or sig.get("no_ask") is None:
        return None
    m = sig.get("mins_left")
    if m is None or not (cfg["min_mins_left"] <= m <= cfg["max_mins_left"]):
        return None
    sc = sig.get("sig_combined")
    if sc is None:
        return None
    thr = cfg["entry_threshold"]
    if sc >= thr:
        return "YES"
    if sc <= -thr:
        return "NO"
    return None


def limit_price(sig: dict, side: str):
    """Resting BUY price for `side` -- join the bid, never cross.

    Mirrors bot_core.sell_price_c / PaperBroker.sell so the maker price is
    the same quantity the rest of the codebase already agrees on. Posting
    any higher risks crossing and paying the taker fee, which is what the
    whole edge is made of: gross t=2.91 at the maker rate, t=1.94 as taker.
    """
    ask = sig.get("yes_ask") if side == "YES" else sig.get("no_ask")
    if ask is None:
        return None
    spread = max(0.0, sig.get("spread") or 0.0)   # crossed book -> clamp at 0
    return max(0.01, round(ask - spread, 4))


def limit_filled(sig: dict, pend: dict) -> bool:
    """True once the market has traded down to our resting limit."""
    ask = sig.get("yes_ask") if pend["side"] == "YES" else sig.get("no_ask")
    return ask is not None and ask <= pend["limit"]


def settle_side(last_sig: dict) -> str:
    """Which side settled in the money, from the last tick observed.

    Same quantity trade_grader uses; cross-checked at 99.5% (365/367)
    against its independent settlement record on 2026-08-04.
    """
    return "YES" if (last_sig.get("distance") or 0.0) > 0 else "NO"


def settle_pnl(pos: dict, settled: str) -> float:
    won = pos["side"] == settled
    gross = (1.0 - pos["entry_price"]) if won else -pos["entry_price"]
    return round(gross * pos["qty"] - pos.get("fee_total", 0.0), 4)


from bot_broker import PaperBroker


def fetch_signal():
    try:
        with urllib.request.urlopen(
                "http://localhost:9050/api/crypto/signal", timeout=4) as r:
            return json.loads(r.read())
    except Exception:
        return None


class Bot:
    SETTLE_MINS = 0.5      # a tick this close to expiry decides settlement

    def __init__(self, bot_dir=None, fetch_fn=fetch_signal):
        self.dir = Path(bot_dir) if bot_dir else SETTLE_DIR
        self.fetch = fetch_fn
        self.state = load_state(self.dir)
        self.cfg = load_config(self.dir / CONFIG_FILE)
        self.broker = PaperBroker()      # paper only, by construction

    def _event(self, action, reason="", ticker="", sig=None):
        append_jsonl(self.dir / EVENTS_FILE,
                     {"ts": (sig or {}).get("ts") or time.time(),
                      "ticker": ticker, "action": action, "reason": reason})

    def _seen(self, ticker) -> bool:
        """One attempt per market, ever -- pending, open, or already settled."""
        return (ticker in self.state["pending"]
                or ticker in self.state["open"]
                or ticker in self.state.setdefault("done", {}))

    def _place(self, side, sig):
        ticker = sig["ticker"]
        px = limit_price(sig, side)
        if px is None:
            return
        self.state["pending"][ticker] = {
            "side": side, "limit": px, "qty": self.cfg["qty"],
            "placed_ts": sig.get("ts") or 0.0, "entry_sig": dict(sig)}
        self._event("place", f"{side} x{self.cfg['qty']} limit {px}", ticker, sig)

    def _process_pending(self, sig):
        ticker = sig.get("ticker")
        for t in list(self.state["pending"]):
            pend = self.state["pending"][t]
            if t != ticker or sig.get("status") != "ok":
                continue          # not this market's tick -- leave it resting
            m = sig.get("mins_left")
            if limit_filled(sig, pend):
                fill = self.broker.fill(pend["limit"], pend["qty"],
                                        sig.get("ts") or 0.0, maker=True)
                del self.state["pending"][t]
                self.state["open"][t] = {
                    "side": pend["side"], "qty": pend["qty"],
                    "entry_price": fill["price"], "fee_total": fill["fee_total"],
                    "entry_ts": fill["ts"], "entry_sig": pend["entry_sig"],
                    "last_sig": dict(sig)}
                self._event("enter",
                            f"{pend['side']} x{pend['qty']} @ {fill['price']}",
                            t, sig)
            elif m is None or m < self.cfg["min_mins_left"]:
                del self.state["pending"][t]
                self.state.setdefault("done", {})[t] = "cancelled"
                self._event("cancel", "window closed, not chasing", t, sig)

    def _resolve(self, ticker, pos, sig):
        last = pos.get("last_sig") or {}
        if (last.get("mins_left") is None
                or last["mins_left"] > self.SETTLE_MINS):
            # rolled away without a near-expiry tick -- do not guess
            del self.state["open"][ticker]
            self.state.setdefault("done", {})[ticker] = "unresolved"
            self.state["unresolved"] = self.state.get("unresolved", 0) + 1
            self._event("unresolved", "no near-expiry tick", ticker, sig)
            return
        settled = settle_side(last)
        pnl = settle_pnl(pos, settled)
        # Durable row BEFORE forgetting the position (see Global Constraints).
        append_jsonl(self.dir / TRADES_FILE, {
            "ticker": ticker, "mode": self.broker.mode, "side": pos["side"],
            "qty": pos["qty"], "entry_price": pos["entry_price"],
            "entry_ts": pos["entry_ts"], "settle_ts": last.get("ts") or 0.0,
            "settled": settled, "net_pnl": pnl,
            "fees": pos.get("fee_total", 0.0),
            "entry_sig": pos.get("entry_sig") or {}, "status": "settled"})
        del self.state["open"][ticker]
        self.state.setdefault("done", {})[ticker] = "settled"
        self._event("settle", f"{settled} pnl {pnl:+.2f}", ticker, sig)

    def tick(self, now_ts=None):
        now_ts = now_ts if now_ts is not None else time.time()
        try:
            sig = self.fetch()
            if not sig:
                return
            self._process_pending(sig)
            for t in list(self.state["open"]):
                if t == sig.get("ticker") and sig.get("status") == "ok":
                    self.state["open"][t]["last_sig"] = dict(sig)
                elif sig.get("ticker") and t != sig.get("ticker"):
                    self._resolve(t, self.state["open"][t], sig)
            if not self._seen(sig.get("ticker") or ""):
                side = entry_decision(sig, self.cfg)
                if side:
                    self._place(side, sig)
        finally:
            self.state["heartbeat"] = now_ts
            save_state(self.dir, self.state)
