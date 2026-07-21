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


def _fresh_pool(bankroll: float = 125.0) -> dict:
    return {"bankroll": bankroll, "day_pnl": 0.0, "day_high": 0.0,
            "total_pnl": 0.0, "halted": False, "loss_capped": False}


def fresh_state() -> dict:
    return {"mode": "paper", "paused": False,
            "day": _utc_day(0.0),
            "pools": {p: _fresh_pool() for p in POOL_NAMES},
            "bankroll_ts": 0.0,
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
    for ps in state.get("pools", {}).values():
        ps["day_pnl"] = 0.0
        ps["day_high"] = 0.0
        ps["halted"] = False
    state.update({"day": today, "market_entries": {}})
    return True


def append_jsonl(path, row: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")


from bot_core import (FlipDetector, RegimeTracker, load_config, entry_blockers,
                      should_time_exit, should_target_exit, should_stop_exit,
                      should_stretch_exit,
                      size_for_budget, trade_budget, loss_headroom,
                      compute_side_ranges, load_offsets,
                      bucket_stats, update_bucket_stats, ev_gate_blocker,
                      weekend_curfew_blocker, session_tag, POOL_NAMES)
from bot_broker import (PaperBroker, round_trip_pnl, fetch_bankroll,
                        FALLBACK_BANKROLL)

SIG_SNAPSHOT_KEYS = ("price", "yes_ask", "no_ask", "mins_left",
                     "whale_trend", "momentum", "buy_pressure", "ts")
BANKROLL_REFRESH_SECS = 3600


def _snap(sig: dict) -> dict:
    return {k: sig.get(k) for k in SIG_SNAPSHOT_KEYS}


def _migrate_pools(trades: list, paper_bankroll: float, today: str) -> dict:
    """One-time backfill for the old single-bankroll schema: splits the
    configured paper_bankroll evenly across the 4 pools, then attributes
    every closed trade to a pool via session_tag(entry_ts) — same
    entry_ts-fallback bucket_stats() uses — to seed total_pnl (full
    history) and day_pnl/day_high (today's trades only, replayed in
    exit-ts order so day_high tracks the same running peak
    _check_profit_lock would have produced live)."""
    per_pool = (paper_bankroll or 500.0) / 4
    pools = {p: _fresh_pool(per_pool) for p in POOL_NAMES}
    todays_rows = {p: [] for p in POOL_NAMES}
    for t in trades:
        if t.get("status") != "closed" or t.get("net_pnl") is None:
            continue
        sig = dict(t.get("entry_sig") or {})
        sig.setdefault("ts", t.get("entry_ts"))
        pool = session_tag(sig.get("ts"))
        if pool not in pools:
            continue   # "unknown": entry_ts missing on very old rows
        pools[pool]["total_pnl"] = round(pools[pool]["total_pnl"] + t["net_pnl"], 4)
        if t.get("exit_ts") and _utc_day(t["exit_ts"]) == today:
            todays_rows[pool].append(t)
    for pool, rows in todays_rows.items():
        rows.sort(key=lambda t: t.get("exit_ts") or 0)
        running = peak = 0.0
        for t in rows:
            running = round(running + t["net_pnl"], 4)
            peak = max(peak, running)
        pools[pool]["day_pnl"] = running
        pools[pool]["day_high"] = peak
    return pools


def _play_pool(play: dict) -> str:
    """Pool a play belongs to — stored at entry time (see _enter); falls
    back to re-deriving it from entry_sig for plays that predate this field
    (an open position carried over a live restart under the old schema)."""
    return play.get("pool") or session_tag((play.get("entry_sig") or {}).get("ts"))


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
        self.regime = RegimeTracker()
        self.broker = PaperBroker()   # LiveBroker only via unlock bar (not v1)
        self.feed_fails = 0
        # EV-gate stats + pool P&L: seeded from the closed-trade journal at
        # boot, then kept current incrementally in _enter/_scale_out/_exit.
        # Migration guard: only backfill pools from trade history if this is
        # an old-schema state file (no "pools" key yet) — once pools exist,
        # never re-derive them (day_pnl/total_pnl already accumulate
        # incrementally going forward; re-running this would double-count).
        trades = self._read_trades()
        self.ev_stats = bucket_stats(trades)
        if "pools" not in self.state:
            self.state["pools"] = _migrate_pools(
                trades, self.cfg.get("paper_bankroll") or 500.0,
                self.state.get("day"))

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
        pool = session_tag(sig.get("ts"))
        ps = self.state.get("pools", {}).get(pool)
        if ps is None:
            self._event("skip", f"unknown session pool for ts={sig.get('ts')}",
                        sig["ticker"], sig)
            return
        price = sig["yes_ask"] if side == "YES" else sig["no_ask"]
        budget = trade_budget(ps["bankroll"], ps.get("total_pnl", 0.0), self.cfg)
        arm = self.cfg.get("profit_arm_usd") or 0.0
        if arm > 0 and ps.get("day_high", 0.0) >= arm:
            # profit lock armed: green day banked — risk small from here
            budget *= self.cfg.get("profit_size_frac", 0.5)
        qty = size_for_budget(budget, price)
        if qty < 1:
            self._event("skip", f"budget too small for 1 contract at {price}",
                        sig["ticker"], sig)
            return
        fill = self.broker.buy(side, qty, sig)
        me = self.state.setdefault("market_entries", {})
        me[sig["ticker"]] = me.get(sig["ticker"], 0) + 1
        self.state["open_plays"][sig["ticker"]] = {
            "side": side, "qty": qty, "entry": fill, "ranges": ranges,
            "entry_sig": _snap(sig), "last_sig": dict(sig), "pool": pool}
        tgt = (f" target {ranges['sell_low']:.1f}c"
               f" (stretch {ranges['sell_high']:.1f}c)") if ranges else ""
        self._event("enter", f"{side} x{qty} @ {fill['price']}{tgt}",
                    sig["ticker"], sig)

    def _scale_out(self, ticker, play, sig):
        """Bank half the position at the win line; the rest rides to stretch.
        Books its own journal row; entry fill is re-apportioned so the
        runner's later _exit math stays exact."""
        fill_sig = sig
        if sig.get("yes_ask") is None or sig.get("no_ask") is None:
            fill_sig = play["last_sig"]
        half = play["qty"] // 2
        fill = self.broker.sell(play["side"], half, fill_sig)
        entry = play["entry"]
        entry_fee_half = round(entry["fee_total"] * half / entry["qty"], 4)
        pnl = round((fill["price"] - entry["price"]) * half
                    - entry_fee_half - fill["fee_total"], 4)
        pool = _play_pool(play)
        ps = self.state["pools"].setdefault(pool, _fresh_pool())
        ps["day_pnl"] = round(ps["day_pnl"] + pnl, 4)
        ps["total_pnl"] = round(ps.get("total_pnl", 0.0) + pnl, 4)
        append_jsonl(self.dir / TRADES_FILE, {
            "ticker": ticker, "mode": self.broker.mode, "side": play["side"],
            "qty": half, "entry_price": entry["price"],
            "exit_price": fill["price"], "entry_ts": entry["ts"],
            "exit_ts": fill["ts"],
            "fees": round(entry_fee_half + fill["fee_total"], 4),
            "net_pnl": pnl, "exit_reason": "target_half",
            "entry_sig": play["entry_sig"], "exit_sig": _snap(sig),
            "status": "closed"})
        entry["qty"] -= half
        entry["fee_total"] = round(entry["fee_total"] - entry_fee_half, 4)
        play["qty"] -= half
        play["scaled"] = {"pnl": pnl}
        self._event("scale", f"banked {half} @ {fill['price']} pnl {pnl:+.2f}"
                    f" — {play['qty']} ride to stretch", ticker, sig)

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
        pool = _play_pool(play)
        ps = self.state["pools"].setdefault(pool, _fresh_pool())
        ps["day_pnl"] = round(ps["day_pnl"] + pnl, 4)
        ps["total_pnl"] = round(ps.get("total_pnl", 0.0) + pnl, 4)
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
        # one EV sample per entry decision: fold any banked scale-out leg in
        update_bucket_stats(self.ev_stats, play["side"], play["entry_sig"],
                            round(pnl + (play.get("scaled") or {}).get("pnl", 0.0), 4))
        self._event("exit", f"{reason} pnl {pnl:+.2f}", ticker, sig)

    def _flatten(self, reason):
        for ticker in list(self.state["open_plays"]):
            play = self.state["open_plays"][ticker]
            self._exit(ticker, play, play["last_sig"], reason)

    # ── maintenance ───────────────────────────────────────────────────
    def _refresh_bankroll(self, now_ts):
        pools = self.state.setdefault("pools", {})
        for p in POOL_NAMES:
            pools.setdefault(p, _fresh_pool())
        # Paper mode with a configured paper bankroll: fixed stake, no live
        # balance fetch. Live mode (future) always uses the real balance.
        pb = self.cfg.get("paper_bankroll") or 0
        if self.broker.mode == "paper" and pb > 0:
            for p in POOL_NAMES:
                pools[p]["bankroll"] = float(pb) / 4
            self.state["bankroll_ts"] = now_ts
            return
        if now_ts - self.state["bankroll_ts"] < BANKROLL_REFRESH_SECS:
            return
        bal = fetch_bankroll()
        if bal is not None:
            for p in POOL_NAMES:
                pools[p]["bankroll"] = bal / 4
        elif not any(pools[p]["bankroll"] for p in POOL_NAMES):
            for p in POOL_NAMES:
                pools[p]["bankroll"] = FALLBACK_BANKROLL / 4
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

    def _check_profit_lock(self):
        """Trail the day's profit peak: once armed, halt before a give-back
        erases it — bank at least keep_frac of the best point."""
        arm = self.cfg.get("profit_arm_usd") or 0.0
        if arm <= 0:
            return
        hi = max(self.state.get("day_high", 0.0), self.state["day_pnl"])
        self.state["day_high"] = hi
        if self.state["halted"] or hi < arm:
            return
        floor = hi * self.cfg.get("profit_keep_frac", 0.5)
        if self.state["day_pnl"] <= floor:
            self.state["halted"] = True
            self._flatten("halt")
            self._event("halt", f"profit_lock: day peaked {hi:+.2f}, banking "
                        f"{self.state['day_pnl']:+.2f} (floor {floor:.2f})")

    def _check_max_loss(self):
        """Hard cap on TOTAL loss. Unlike the day stop it never resets on a
        day roll — trading stays blocked until the user raises max_loss_usd
        (or the journal is reset). Condition-based, so a config raise
        releases it without touching state."""
        capped = loss_headroom(self.state.get("total_pnl", 0.0), self.cfg) <= 0
        if capped and not self.state.get("loss_capped"):
            self.state["loss_capped"] = True
            self._flatten("max_loss")
            self._event("halt", f"MAX LOSS CAP: total_pnl "
                        f"{self.state.get('total_pnl', 0.0):+.2f} <= "
                        f"-{self.cfg.get('max_loss_usd', 0):.0f} — trading "
                        f"blocked until max_loss_usd is raised")
        elif not capped and self.state.get("loss_capped"):
            self.state.pop("loss_capped", None)
            self._event("resume", "max-loss cap released (config raised)")

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
        self._check_profit_lock()
        self._check_max_loss()

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
        self.regime.update(sig.get("ts"), sig.get("spot"))
        regime = self.regime.classify()
        # exits / rolled markets first
        for t in list(self.state["open_plays"]):
            play = self.state["open_plays"][t]
            if sig.get("status") == "ok" and t == ticker:
                # Only advance last_sig from a fully-quoted row — it's the
                # fallback _exit uses for askless replay rows, so it must
                # always carry a usable yes_ask/no_ask.
                if sig.get("yes_ask") is not None and sig.get("no_ask") is not None:
                    play["last_sig"] = dict(sig)
                scaled = play.get("scaled")
                if (not scaled and self.cfg.get("scale_out", True)
                        and play["qty"] >= 2 and should_target_exit(play, sig)):
                    self._scale_out(t, play, sig)
                elif (should_stretch_exit(play, sig) if scaled
                      else should_target_exit(play, sig)):
                    self._exit(t, play, sig, "stretch" if scaled else "target")
                elif should_stop_exit(play, sig, self.cfg):
                    self._exit(t, play, sig, "stop")
                elif should_time_exit(sig, self.cfg, regime=regime):
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
            if self.cfg.get("flip_exit", True):
                self._exit(ticker, play, sig, "flip")
            return   # flip_exit off: hold — target/stop/time resolve it
        if not flip:
            return
        ranges = self._ranges_for(flip, sig)
        blockers = entry_blockers(sig, self.cfg, self.state["open_plays"],
                                  self.state["halted"], self.state["paused"],
                                  ranges)
        ev = ev_gate_blocker(flip, sig, self.ev_stats, self.cfg)
        if ev:
            blockers.append(ev)
        cur = weekend_curfew_blocker(sig.get("ts") or time.time(), self.cfg)
        if cur:
            blockers.append(cur)
        if self.state.get("loss_capped"):
            blockers.append("max_loss_cap")
        mcap = self.cfg.get("max_entries_per_market") or 0
        n_mkt = self.state.get("market_entries", {}).get(ticker, 0)
        if mcap and n_mkt >= mcap:
            blockers.append(f"market_entries {n_mkt} >= {mcap} — whipsaw guard")
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
