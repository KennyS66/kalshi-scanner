#!/usr/bin/env python3
"""Swing bot daemon — paper-first flow-flip scalper (spec 2026-07-15).

Polls /api/crypto/signal every poll_secs, enters on whale-flow sign flips,
exits on the opposite flip or at exit_mins to settlement. Paper fills only;
live is locked behind bot_broker.live_unlock_ok. All state under data/bot/.

Run:  python3 -u swing_bot.py &
"""
import datetime as dt
import json
import os
import time
import urllib.request
from pathlib import Path

BOT_DIR = Path(__file__).parent / "data" / "bot"

STATE_FILE = "bot_state.json"
CONTROL_FILE = "control.json"
TRADES_FILE = "bot_trades.jsonl"
EVENTS_FILE = "bot_events.jsonl"
CONFIG_FILE = "config.json"


def _utc_day(ts: float) -> str:
    return dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")


def fresh_state() -> dict:
    return {"mode": "paper", "paused": False, "halted": False,
            "day": _utc_day(0.0), "day_pnl": 0.0,
            "bankroll": 500.0, "bankroll_ts": 0.0,
            "open_plays": {}, "heartbeat": 0.0, "last_control_nonce": 0}


def load_state(bot_dir) -> dict:
    try:
        return json.loads((Path(bot_dir) / STATE_FILE).read_text())
    except Exception:
        return fresh_state()


def save_state(bot_dir, state: dict) -> None:
    bot_dir = Path(bot_dir)
    bot_dir.mkdir(parents=True, exist_ok=True)
    tmp = bot_dir / (STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, bot_dir / STATE_FILE)


def read_control(bot_dir, last_nonce: int):
    """Return (cmd, nonce). cmd is None unless a NEW nonce appeared."""
    try:
        c = json.loads((Path(bot_dir) / CONTROL_FILE).read_text())
        nonce = int(c.get("nonce", 0))
        if nonce > last_nonce and c.get("cmd") in ("pause", "resume", "flatten"):
            return c["cmd"], nonce
        return None, max(nonce, last_nonce)
    except Exception:
        return None, last_nonce


def roll_day_if_needed(state: dict, now_ts: float) -> bool:
    today = _utc_day(now_ts)
    if state.get("day") == today:
        return False
    state.update({"day": today, "day_pnl": 0.0, "halted": False})
    return True


def append_jsonl(path, row: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")


from bot_core import (FlipDetector, load_config, entry_blockers,
                      should_time_exit, should_target_exit, should_stop_exit,
                      size_contracts, compute_side_ranges, load_offsets,
                      bucket_stats, update_bucket_stats, ev_gate_blocker)
from bot_broker import (PaperBroker, round_trip_pnl, fetch_bankroll,
                        FALLBACK_BANKROLL)

SIG_SNAPSHOT_KEYS = ("price", "yes_ask", "no_ask", "mins_left",
                     "whale_trend", "momentum", "buy_pressure")
BANKROLL_REFRESH_SECS = 3600


def _snap(sig: dict) -> dict:
    return {k: sig.get(k) for k in SIG_SNAPSHOT_KEYS}


def fetch_signal():
    try:
        with urllib.request.urlopen(
                "http://localhost:9050/api/crypto/signal", timeout=4) as r:
            return json.loads(r.read())
    except Exception:
        return None


WHALES_DIR = Path(__file__).parent / "data" / "whales"


class Bot:
    def __init__(self, bot_dir=None, fetch_fn=fetch_signal, offsets_file=None,
                 loop_log=None):
        self.dir = Path(bot_dir) if bot_dir else BOT_DIR
        self.fetch = fetch_fn
        self.state = load_state(self.dir)
        self.cfg = load_config(self.dir / CONFIG_FILE)
        self.offsets_file = (Path(offsets_file) if offsets_file
                             else WHALES_DIR / "banner_offsets.json")
        self.loop_log = (Path(loop_log) if loop_log
                         else WHALES_DIR / "loop_log.jsonl")
        # Boot: whatever is in control.json predates this process — mark it
        # consumed so a restart never replays the last command (a replayed
        # "resume" could un-pause a bot the user deliberately paused).
        try:
            disk_nonce = int(json.loads(
                (self.dir / CONTROL_FILE).read_text()).get("nonce", 0))
        except Exception:
            disk_nonce = 0
        self.state["last_control_nonce"] = max(
            self.state.get("last_control_nonce", 0), disk_nonce)
        self.detector = FlipDetector(self.cfg["flip_threshold"])
        self.broker = PaperBroker()   # LiveBroker only via unlock bar (not v1)
        self.feed_fails = 0
        # EV-gate stats: seeded from the closed-trade journal at boot, then
        # kept current incrementally in _exit (no per-tick file scans).
        self.ev_stats = bucket_stats(self._read_trades())

    def _read_trades(self):
        try:
            return [json.loads(l) for l in
                    (self.dir / TRADES_FILE).read_text().splitlines() if l.strip()]
        except Exception:
            return []

    def _ranges_for(self, side, sig):
        """Calibrated buy/sell range for `side`, or None when gating is off."""
        if not self.cfg.get("use_ranges", True):
            return None
        offs = load_offsets(self.offsets_file)
        return compute_side_ranges(sig.get("price") or 0.0,
                                   sig.get("yes_pct") or 50.0,
                                   side, offs.get(side.lower(), {}))

    # ── logging ───────────────────────────────────────────────────────
    def _event(self, action, reason="", ticker="", sig=None, ts=None):
        append_jsonl(self.dir / EVENTS_FILE,
                     {"ts": ts if ts is not None else time.time(),
                      "ticker": ticker, "action": action, "reason": reason,
                      "sig": _snap(sig) if sig else None})

    # ── trade lifecycle ───────────────────────────────────────────────
    def _enter(self, side, sig, ranges=None):
        price = sig["yes_ask"] if side == "YES" else sig["no_ask"]
        qty = size_contracts(self.state["bankroll"], price, self.cfg["risk_pct"])
        if qty < 1:
            self._event("skip", f"budget too small for 1 contract at {price}",
                        sig["ticker"], sig)
            return
        fill = self.broker.buy(side, qty, sig)
        self.state["open_plays"][sig["ticker"]] = {
            "side": side, "qty": qty, "entry": fill, "ranges": ranges,
            "entry_sig": _snap(sig), "last_sig": dict(sig)}
        tgt = (f" target {ranges['sell_low']:.1f}c"
               f" (stretch {ranges['sell_high']:.1f}c)") if ranges else ""
        self._event("enter", f"{side} x{qty} @ {fill['price']}{tgt}",
                    sig["ticker"], sig)

    def _exit(self, ticker, play, sig, reason):
        # Historical replay rows can be status="ok" but lack yes_ask/no_ask
        # (no live quote at that point in the recording). The entry sig
        # always has both (entry_blockers requires them), and last_sig only
        # ever advances from a fully-quoted row (see _manage), so falling
        # back to it here always yields a usable sell fill.
        fill_sig = sig
        if sig.get("yes_ask") is None or sig.get("no_ask") is None:
            fill_sig = play["last_sig"]
        fill = self.broker.sell(play["side"], play["qty"], fill_sig)
        pnl = round_trip_pnl(play["entry"], fill)
        self.state["day_pnl"] = round(self.state["day_pnl"] + pnl, 4)
        del self.state["open_plays"][ticker]
        self.detector.forget(ticker)
        append_jsonl(self.dir / TRADES_FILE, {
            "ticker": ticker, "mode": self.broker.mode, "side": play["side"],
            "qty": play["qty"], "entry_price": play["entry"]["price"],
            "exit_price": fill["price"], "entry_ts": play["entry"]["ts"],
            "exit_ts": fill["ts"],
            "fees": round(play["entry"]["fee_total"] + fill["fee_total"], 4),
            "net_pnl": pnl, "exit_reason": reason,
            "entry_sig": play["entry_sig"], "exit_sig": _snap(sig),
            "status": "closed"})
        update_bucket_stats(self.ev_stats, play["side"], play["entry_sig"], pnl)
        self._event("exit", f"{reason} pnl {pnl:+.2f}", ticker, sig)

    def _flatten(self, reason):
        for ticker in list(self.state["open_plays"]):
            play = self.state["open_plays"][ticker]
            self._exit(ticker, play, play["last_sig"], reason)

    # ── maintenance ───────────────────────────────────────────────────
    def _refresh_bankroll(self, now_ts):
        # Paper mode with a configured paper bankroll: fixed stake, no live
        # balance fetch. Live mode (future) always uses the real balance.
        pb = self.cfg.get("paper_bankroll") or 0
        if self.broker.mode == "paper" and pb > 0:
            self.state["bankroll"] = float(pb)
            self.state["bankroll_ts"] = now_ts
            return
        if now_ts - self.state["bankroll_ts"] < BANKROLL_REFRESH_SECS:
            return
        bal = fetch_bankroll()
        if bal is not None:
            self.state["bankroll"] = bal
        elif not self.state["bankroll"]:
            self.state["bankroll"] = FALLBACK_BANKROLL
        self.state["bankroll_ts"] = now_ts

    def _handle_control(self):
        cmd, nonce = read_control(self.dir, self.state["last_control_nonce"])
        self.state["last_control_nonce"] = nonce
        if cmd == "pause":
            self.state["paused"] = True
            self.state.pop("paused_by", None)   # manual: deadman won't resume it
            self._event("pause", "control")
        elif cmd == "resume":
            self.state["paused"] = False
            self.state.pop("paused_by", None)
            self._event("resume", "control")
        elif cmd == "flatten":
            self._flatten("flatten")
            self._event("flatten", "control")

    def _check_loop_deadman(self, now_ts):
        """No unsupervised trading: pause when the marketloop heartbeat
        (loop_log.jsonl mtime) goes stale — tokens exhausted, session closed,
        machine asleep. Auto-resume only a deadman pause; a manual pause from
        the GUI stays paused."""
        mins = self.cfg.get("loop_deadman_mins") or 0
        if not mins:
            return
        try:
            age = now_ts - self.loop_log.stat().st_mtime
        except OSError:
            return   # loop never ran on this machine — don't enforce
        if age > mins * 60 and not self.state["paused"]:
            self.state["paused"] = True
            self.state["paused_by"] = "deadman"
            self._event("pause", f"loop heartbeat stale {age/60:.0f}m — deadman")
        elif (age <= mins * 60 and self.state["paused"]
              and self.state.get("paused_by") == "deadman"):
            self.state["paused"] = False
            self.state.pop("paused_by", None)
            self._event("resume", "loop heartbeat back — deadman released")

    def _check_day_stop(self):
        stop = self.cfg["day_stop_pct"] * self.state["bankroll"]
        if not self.state["halted"] and self.state["day_pnl"] <= -stop:
            self.state["halted"] = True
            self._flatten("halt")
            self._event("halt", f"day_pnl {self.state['day_pnl']:+.2f} <= -{stop:.2f}")

    # ── main tick ─────────────────────────────────────────────────────
    def tick(self, now_ts=None):
        now_ts = now_ts if now_ts is not None else time.time()
        if roll_day_if_needed(self.state, now_ts):
            self._event("day_roll", self.state["day"])
        self.cfg = load_config(self.dir / CONFIG_FILE)   # hot-reload
        self.detector.flip_threshold = self.cfg["flip_threshold"]
        self._refresh_bankroll(now_ts)
        self._handle_control()
        self._check_loop_deadman(now_ts)
        self._check_day_stop()

        sig = self.fetch()
        if sig is None:
            self.feed_fails += 1
            if self.feed_fails == 3:
                self._event("feed_down", "3 consecutive fetch failures")
        else:
            self.feed_fails = 0
            self._manage(sig)

        self.state["heartbeat"] = now_ts
        save_state(self.dir, self.state)

    def _manage(self, sig):
        ticker = sig.get("ticker")
        # exits / rolled markets first
        for t in list(self.state["open_plays"]):
            play = self.state["open_plays"][t]
            if sig.get("status") == "ok" and t == ticker:
                # Only advance last_sig from a fully-quoted row — it's the
                # fallback _exit uses for askless replay rows, so it must
                # always carry a usable yes_ask/no_ask.
                if sig.get("yes_ask") is not None and sig.get("no_ask") is not None:
                    play["last_sig"] = dict(sig)
                if should_target_exit(play, sig):
                    self._exit(t, play, sig, "target")
                elif should_stop_exit(play, sig, self.cfg):
                    self._exit(t, play, sig, "stop")
                elif should_time_exit(sig, self.cfg):
                    self._exit(t, play, sig, "time")
            else:
                self._exit(t, play, play["last_sig"], "rolled")

        if sig.get("status") != "ok":
            return
        flip = self.detector.update(ticker, sig.get("whale_trend") or 0.0,
                                    sig.get("momentum") or 0.0)
        # opposite-flip exit for a still-open play on this market
        play = self.state["open_plays"].get(ticker)
        if play and flip and flip != play["side"]:
            self._exit(ticker, play, sig, "flip")
            return
        if not flip:
            return
        ranges = self._ranges_for(flip, sig)
        blockers = entry_blockers(sig, self.cfg, self.state["open_plays"],
                                  self.state["halted"], self.state["paused"],
                                  ranges)
        ev = ev_gate_blocker(flip, sig, self.ev_stats, self.cfg)
        if ev:
            blockers.append(ev)
        if blockers:
            self._event("skip", "; ".join(blockers), ticker, sig)
            return
        self._enter(flip, sig, ranges)

    def run(self):
        print(f"swing_bot up — mode={self.broker.mode} dir={self.dir}", flush=True)
        while True:
            try:
                self.tick()
            except Exception as e:
                self._event("error", repr(e))
            time.sleep(self.cfg.get("poll_secs", 5))


if __name__ == "__main__":
    Bot().run()
