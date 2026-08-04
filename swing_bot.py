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
import sys
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
            "open_plays": {}, "pending_entries": {},
            "heartbeat": 0.0, "last_control_nonce": 0}


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
                      weekend_curfew_blocker, session_tag, POOL_NAMES,
                      entry_tier)
from bot_broker import (PaperBroker, LiveBroker, round_trip_pnl, fetch_bankroll,
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
        # Recorded before load_state(): its fallback to fresh_state() on a
        # missing/corrupt file already includes placeholder pools (needed so
        # fresh_state() is directly usable in tests), which would otherwise
        # make the migration guard below think a truly first-ever boot was
        # already migrated and skip backfilling real trade history into it.
        had_state_file = (self.dir / STATE_FILE).exists()
        self.state = load_state(self.dir)
        self.state.setdefault("pending_entries", {})   # old state files predate this
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
        if self.cfg.get("mode") == "live":
            self.broker = LiveBroker(self.cfg, bot_dir=self.dir)
        else:
            self.broker = PaperBroker()
        self.feed_fails = 0
        # EV-gate stats + pool P&L: seeded from the closed-trade journal at
        # boot, then kept current incrementally in _enter/_scale_out/_exit.
        # Migration guard: only backfill pools from trade history on a
        # genuinely first-ever boot — either an old-schema state file (no
        # "pools" key) or no state file at all yet (had_state_file False;
        # fresh_state()'s placeholder pools don't count as "already
        # migrated"). Once real pools exist on disk, never re-derive them
        # (day_pnl/total_pnl already accumulate incrementally going
        # forward; re-running this would double-count).
        trades = self._read_trades()
        self.ev_stats = bucket_stats(trades)
        if "pools" not in self.state or not had_state_file:
            self.state["pools"] = _migrate_pools(
                trades, self.cfg.get("paper_bankroll") or 500.0,
                self.state.get("day"))
        self._reconcile_open_plays()

    def _reconcile_open_plays(self):
        """Every entry must have either exited or still be open. A gap means
        an exit booked P&L without leaving a trade row -- exactly how
        KXBTC15M-26JUL210515-15 was lost on 2026-07-21 and only noticed
        thirteen days later.

        Warns, never raises: the bot has to come up, and that historical
        orphan is a permanent +1 in the counts until the log is repaired.

        Only a gap *wider* than the accepted baseline warns. Firing on every
        boot over a known-bad history would bury the next orphan in its own
        noise, which is the one thing this check exists to prevent. A repaired
        log silently lowers the baseline and re-arms the check.
        """
        try:
            rows = [json.loads(l) for l in
                    (self.dir / EVENTS_FILE).read_text().splitlines() if l.strip()]
        except Exception:
            return          # no event log yet -- nothing to reconcile against
        enters = sum(1 for e in rows if e.get("action") == "enter")
        exits = sum(1 for e in rows if e.get("action") == "exit")
        open_n = len(self.state.get("open_plays") or {})
        gap = enters - exits - open_n
        # Any DEVIATION from the accepted baseline, in either direction.
        # Positive means an exit booked P&L without leaving a trade row.
        # Negative means a tracked position whose enter event never landed --
        # _enter writes open_plays before the event on purpose (the buy has
        # already happened, so losing the position is far worse than losing
        # an audit row), which makes this the shape that failure takes.
        # Testing only `>` swallowed the negative case and quietly lowered
        # the baseline underneath it.
        if gap != self.state.get("reconcile_baseline", 0):
            detail = (f"{gap:+d} entries with no exit and no open play -- "
                      f"trades likely missing from {TRADES_FILE}") if gap > 0 else (
                      f"{gap:+d} open plays with no enter event -- "
                      f"events likely missing from {EVENTS_FILE}")
            msg = f"{detail} ({enters} enter / {exits} exit / {open_n} open)"
            self._event("reconcile", msg)
            print(f"swing_bot WARNING: {msg}", file=sys.stderr, flush=True)
        self.state["reconcile_baseline"] = gap

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
    def _pool_and_budget(self, sig):
        """(pool, pool_state, dollar_budget) for an entry at this signal's
        ts, or None if the session pool can't be resolved. Shared by the
        immediate-market and resting-limit entry paths so budget sizing
        (including the profit-lock size cut) can't drift between them."""
        pool = session_tag(sig.get("ts"))
        ps = self.state.get("pools", {}).get(pool)
        if ps is None:
            self._event("skip", f"unknown session pool for ts={sig.get('ts')}",
                        sig["ticker"], sig)
            return None
        budget = trade_budget(ps["bankroll"], ps.get("total_pnl", 0.0), self.cfg)
        arm = self.cfg.get("profit_arm_usd") or 0.0
        if arm > 0 and ps.get("day_high", 0.0) >= arm:
            # profit lock armed: green day banked — risk small from here
            budget *= self.cfg.get("profit_size_frac", 0.5)
        return pool, ps, budget

    def _entry_qty(self, budget: float, price: float) -> int:
        """Live mode (manual or auto) always sizes flat at cfg['live_qty']
        once `budget` has already confirmed the entry is affordable at all
        -- paper's %-of-pool trade_budget formula was proven this session
        to size unreasonably large (up to 33 contracts) when transplanted
        onto a small real account, so live entries never use it for the
        actual quantity."""
        if self.broker.mode == "live":
            return self.cfg.get("live_qty", 1)
        return size_for_budget(budget, price)

    def _enter(self, side, sig, ranges=None):
        """Immediate market fill — the fallback path for entries too close
        to expiry to wait on a limit (see entry_tier), and the only path
        when cfg['limit_entries'] is off."""
        pb = self._pool_and_budget(sig)
        if pb is None:
            return
        pool, ps, budget = pb
        price = sig["yes_ask"] if side == "YES" else sig["no_ask"]
        qty = self._entry_qty(budget, price)
        if qty < 1:
            self._event("skip", f"budget too small for 1 contract at {price}",
                        sig["ticker"], sig)
            return
        try:
            fill = self.broker.buy(side, qty, sig)
        except Exception as e:
            if self.broker.mode == "live" and self.broker.broker_mode == "auto":
                ps["halted"] = True
                self._event("halt", f"[{pool}] order_error: {e} -- auto entries "
                            f"blocked until manually resumed", sig["ticker"], sig)
                return
            raise
        me = self.state.setdefault("market_entries", {})
        me[sig["ticker"]] = me.get(sig["ticker"], 0) + 1
        self.state["open_plays"][sig["ticker"]] = {
            "side": side, "qty": qty, "entry": fill, "ranges": ranges,
            "entry_sig": _snap(sig), "last_sig": dict(sig), "pool": pool}
        tgt = (f" target {ranges['sell_low']:.1f}c"
               f" (stretch {ranges['sell_high']:.1f}c)") if ranges else ""
        self._event("enter", f"{side} x{qty} @ {fill['price']}{tgt}",
                    sig["ticker"], sig)

    def _place_entry(self, side, sig, ranges=None):
        """Route to a resting limit order (aggressive/patient, same tiers
        /trade's panel shows) when there's enough time to wait for one;
        otherwise fall back to _enter's immediate market fill. A limit
        order never fills the instant it's placed by construction (its
        price is strictly below the current ask) -- see _process_pending
        for the fill/chase/cancel handling on later ticks."""
        ask = sig["yes_ask"] if side == "YES" else sig["no_ask"]
        tier = entry_tier(sig.get("mins_left"), ask) if self.cfg.get("limit_entries", True) else None
        if tier is None:
            # Kalshi charges makers nothing and takers ~0.07*p*(1-p) per
            # contract (confirmed against real fills 2026-08-03). Under
            # maker_only there is no taker fallback: skipping is the only
            # maker-consistent outcome this close to expiry.
            if self.cfg.get("maker_only"):
                self._event("skip", "maker_only: no resting tier available "
                            "(too close to expiry to rest a limit)",
                            sig.get("ticker"), sig)
                return
            self._enter(side, sig, ranges)
            return
        tier_name, offset = tier
        pb = self._pool_and_budget(sig)
        if pb is None:
            return
        pool, ps, budget = pb
        limit_price = max(0.01, round(ask - offset, 4))
        qty = self._entry_qty(budget, limit_price)
        if qty < 1:
            self._event("skip", f"budget too small for 1 contract at {limit_price}",
                        sig["ticker"], sig)
            return
        pend = {"side": side, "qty": qty, "limit_price": limit_price,
                "tier": tier_name, "placed_ts": sig.get("ts") or 0.0,
                "ranges": ranges, "entry_sig": _snap(sig), "pool": pool}
        if self.broker.mode == "live" and self.broker.broker_mode == "auto":
            import live_broker
            try:
                order = live_broker.place_order(
                    "yes" if side == "YES" else "no", "buy", sig["ticker"], qty,
                    limit_price, "limit")
            except Exception as e:
                ps["halted"] = True
                self._event("halt", f"[{pool}] order_error: {e} -- auto entries "
                            f"blocked until manually resumed", sig["ticker"], sig)
                return
            pend["order_id"] = order["order_id"]
        self.state["pending_entries"][sig["ticker"]] = pend
        self._event("place", f"{side} x{qty} limit @ {limit_price:.3f} ({tier_name})",
                    sig["ticker"], sig)

    def _fill_pending(self, ticker, pend, sig, maker, chase=False):
        del self.state["pending_entries"][ticker]
        price = (sig["yes_ask"] if pend["side"] == "YES" else sig["no_ask"]) \
                if chase else pend["limit_price"]
        order_id = None if chase else pend.get("order_id")
        try:
            fill = self.broker.fill(price, pend["qty"], sig.get("ts") or 0.0, maker=maker,
                                    sig={**sig, "side": pend["side"]}, order_id=order_id)
        except Exception as e:
            if self.broker.mode == "live" and self.broker.broker_mode == "auto":
                ps = self.state["pools"][pend["pool"]]
                ps["halted"] = True
                self._event("halt", f"[{pend['pool']}] order_error: {e} -- auto entries "
                            f"blocked until manually resumed", ticker, sig)
                return
            raise
        me = self.state.setdefault("market_entries", {})
        me[ticker] = me.get(ticker, 0) + 1
        self.state["open_plays"][ticker] = {
            "side": pend["side"], "qty": pend["qty"], "entry": fill,
            "ranges": pend["ranges"], "entry_sig": pend["entry_sig"],
            "last_sig": dict(sig), "pool": pend["pool"]}
        kind = "chased to market" if chase else f"limit filled ({pend['tier']})"
        self._event("enter", f"{pend['side']} x{pend['qty']} @ {fill['price']} — {kind}",
                    ticker, sig)

    def _cancel_pending(self, t, pend, sig, reason):
        """Drop a resting entry, cancelling the real order first in auto
        mode. A cancel that fails halts the pool rather than leaving an
        orphaned live order the bot has stopped tracking."""
        if pend.get("order_id"):
            import live_broker
            try:
                live_broker.cancel_order(pend["order_id"])
            except Exception as e:
                self.state["pools"][pend["pool"]]["halted"] = True
                del self.state["pending_entries"][t]
                self._event("halt", f"[{pend['pool']}] order_error: {e} -- "
                            f"auto entries blocked until manually resumed", t, sig)
                return
        del self.state["pending_entries"][t]
        self._event("cancel", reason, t, sig)

    def _process_pending(self, sig):
        """Advance every resting entry order by one tick: fill if the
        market has traded down to the limit, chase to market once the
        timeout elapses, or cancel (no cost -- nothing was ever risked) if
        its market rolled away before either happened. Roll detection
        mirrors the exit loop: only a genuine ticker mismatch (or a
        non-ok row) counts as rolled. A same-ticker row with no usable
        quote (routine in replay) just waits for the next tick instead of
        being cancelled -- folding the quote check into the roll condition
        was a bug that nuked resting orders on their own market's quiet
        ticks. In live+auto mode (order_id present), fill/no-fill is
        decided by polling the REAL order's status, not by comparing the
        limit price to the tick's quote -- the quote can lag or jitter
        around the exact touch price in ways paper's simulation doesn't
        need to worry about, but a real resting order's own status is
        authoritative."""
        ticker = sig.get("ticker")
        timeout = self.cfg.get("limit_fill_timeout_secs", 30)
        paused_sessions = self.cfg.get("paused_sessions") or []
        for t in list(self.state["pending_entries"]):
            pend = self.state["pending_entries"][t]
            # A pause must stop resting orders too, not just new flips. This
            # loop runs BEFORE entry_blockers, so without this a limit order
            # placed pre-STOP would still fill -- or chase to MARKET on
            # timeout -- and open a position the user was told could not
            # happen (kill-switch confirm: "STOP only blocks NEW entries").
            # Cancelling is the safe direction: nothing was risked yet.
            if self.state["paused"] or pend.get("pool") in paused_sessions:
                self._cancel_pending(t, pend, sig, "paused before limit filled")
                continue
            if sig.get("status") != "ok" or t != ticker:
                self._cancel_pending(t, pend, sig,
                                     "rolled before limit filled or chased")
                continue
            if pend.get("order_id"):
                import live_broker
                try:
                    status = live_broker.get_order(pend["order_id"])
                except Exception as e:
                    ps = self.state["pools"][pend["pool"]]
                    ps["halted"] = True
                    del self.state["pending_entries"][t]
                    self._event("halt", f"[{pend['pool']}] order_error: {e} -- "
                                f"auto entries blocked until manually resumed", t, sig)
                    continue
                if status.get("status") == "executed":
                    self._fill_pending(t, pend, sig, maker=True)
                elif (sig.get("ts") or 0.0) - pend["placed_ts"] >= timeout:
                    if self.cfg.get("maker_only"):
                        self._cancel_pending(t, pend, sig,
                                             "maker_only: limit timed out, not chasing")
                        continue
                    try:
                        live_broker.cancel_order(pend["order_id"])
                    except Exception as e:
                        ps = self.state["pools"][pend["pool"]]
                        ps["halted"] = True
                        del self.state["pending_entries"][t]
                        self._event("halt", f"[{pend['pool']}] order_error: {e} -- "
                                    f"auto entries blocked until manually resumed", t, sig)
                        continue
                    self._fill_pending(t, pend, sig, maker=False, chase=True)
                continue
            if sig.get("yes_ask") is None or sig.get("no_ask") is None:
                continue  # same market, no usable quote this tick -- wait
            ask = sig["yes_ask"] if pend["side"] == "YES" else sig["no_ask"]
            if ask <= pend["limit_price"]:
                self._fill_pending(t, pend, sig, maker=True)
            elif (sig.get("ts") or 0.0) - pend["placed_ts"] >= timeout:
                if self.cfg.get("maker_only"):
                    self._cancel_pending(t, pend, sig,
                                         "maker_only: limit timed out, not chasing")
                    continue
                self._fill_pending(t, pend, sig, maker=False, chase=True)
            # else: still waiting, leave it pending

    def _scale_out(self, ticker, play, sig):
        """Bank half the position at the win line; the rest rides to stretch.
        Books its own journal row; entry fill is re-apportioned so the
        runner's later _exit math stays exact."""
        fill_sig = sig
        if sig.get("yes_ask") is None or sig.get("no_ask") is None:
            fill_sig = play["last_sig"]
        if self.broker.mode == "live" and self.broker.broker_mode == "auto":
            backoff = self.cfg.get("live_sell_retry_backoff_secs", 30.0)
            last_err = play.get("sell_error_ts")
            now = sig.get("ts") or 0.0
            if last_err is not None and (now - last_err) < backoff:
                return   # still backing off from the last failed sell attempt
        half = play["qty"] // 2
        # A previous attempt may have sold and then failed to write the trade
        # row. That half is already gone from the account, so reuse its fill
        # instead of banking a second one.
        fill = play.get("scale_fill")
        if fill is None:
            if self.broker.mode == "live" and self.broker.broker_mode == "auto":
                try:
                    fill = self.broker.sell(play["side"], half, fill_sig)
                except Exception as e:
                    play["sell_error_ts"] = sig.get("ts") or 0.0
                    self._event("skip", f"live sell failed, backing off {backoff:.0f}s: {e}",
                                ticker, sig)
                    return
            else:
                fill = self.broker.sell(play["side"], half, fill_sig)
            play["scale_fill"] = fill      # persisted by tick()'s finally
        entry = play["entry"]
        entry_fee_half = round(entry["fee_total"] * half / entry["qty"], 4)
        pnl = round((fill["price"] - entry["price"]) * half
                    - entry_fee_half - fill["fee_total"], 4)
        pool = _play_pool(play)
        # Durable record BEFORE crediting the pool or shrinking the play.
        # Crediting first leaves the play looking unscaled at full size when
        # the append fails, so the next tick banks the same half over again --
        # a double-count on top of the lost row, worse than the _exit case.
        row = {
            "ticker": ticker, "mode": self.broker.mode, "side": play["side"],
            "qty": half, "entry_price": entry["price"],
            "exit_price": fill["price"], "entry_ts": entry["ts"],
            "exit_ts": fill["ts"],
            "fees": round(entry_fee_half + fill["fee_total"], 4),
            "net_pnl": pnl, "exit_reason": "target_half",
            "entry_sig": play["entry_sig"], "exit_sig": _snap(sig),
            "status": "closed"}
        if entry.get("signal_only") or fill.get("signal_only"):
            row["signal_only"] = True      # see bot_core.is_signal_only
        append_jsonl(self.dir / TRADES_FILE, row)
        ps = self.state["pools"].setdefault(pool, _fresh_pool())
        ps["day_pnl"] = round(ps["day_pnl"] + pnl, 4)
        ps["total_pnl"] = round(ps.get("total_pnl", 0.0) + pnl, 4)
        entry["qty"] -= half
        entry["fee_total"] = round(entry["fee_total"] - entry_fee_half, 4)
        play["qty"] -= half
        play["scaled"] = {"pnl": pnl}
        play.pop("scale_fill", None)       # consumed
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
        if self.broker.mode == "live" and self.broker.broker_mode == "auto":
            backoff = self.cfg.get("live_sell_retry_backoff_secs", 30.0)
            last_err = play.get("sell_error_ts")
            now = sig.get("ts") or 0.0
            if last_err is not None and (now - last_err) < backoff:
                return   # still backing off from the last failed sell attempt
        # A previous attempt may have sold and then failed to write the trade
        # row (see below). The position is already gone from the account, so
        # reuse that recorded fill instead of dumping it a second time.
        fill = play.get("exit_fill")
        if fill is None:
            if self.broker.mode == "live" and self.broker.broker_mode == "auto":
                try:
                    fill = self.broker.sell(play["side"], play["qty"], fill_sig)
                except Exception as e:
                    play["sell_error_ts"] = sig.get("ts") or 0.0
                    self._event("skip", f"live sell failed, backing off {backoff:.0f}s: {e}",
                                ticker, sig)
                    return
            else:
                fill = self.broker.sell(play["side"], play["qty"], fill_sig)
            # Persisted by tick()'s finally, so a crash between here and the
            # trade-row write still can't cause a second sell on restart.
            play["exit_fill"] = fill
        pnl = round_trip_pnl(play["entry"], fill)
        pool = _play_pool(play)
        # Durable record BEFORE any state mutation. Booking the P&L or
        # deleting the play first means a failed append loses the trade
        # from bot_trades.jsonl for good while its P&L stays on the books
        # -- that is how KXBTC15M-26JUL210515-15 vanished on 2026-07-21.
        # Raising here leaves state untouched, so the exit simply retries.
        row = {
            "ticker": ticker, "mode": self.broker.mode, "side": play["side"],
            "qty": play["qty"], "entry_price": play["entry"]["price"],
            "exit_price": fill["price"], "entry_ts": play["entry"]["ts"],
            "exit_ts": fill["ts"],
            "fees": round(play["entry"]["fee_total"] + fill["fee_total"], 4),
            "net_pnl": pnl, "exit_reason": reason,
            "entry_sig": play["entry_sig"], "exit_sig": _snap(sig),
            "status": "closed"}
        # Either leg synthetic makes the whole round trip synthetic. Journal
        # it, but keep it out of the live-unlock and EV gates -- see
        # bot_core.is_signal_only. Key is omitted entirely on real fills so
        # the paper row format is unchanged.
        if play["entry"].get("signal_only") or fill.get("signal_only"):
            row["signal_only"] = True
        append_jsonl(self.dir / TRADES_FILE, row)
        ps = self.state["pools"].setdefault(pool, _fresh_pool())
        ps["day_pnl"] = round(ps["day_pnl"] + pnl, 4)
        ps["total_pnl"] = round(ps.get("total_pnl", 0.0) + pnl, 4)
        del self.state["open_plays"][ticker]
        self.detector.forget(ticker)
        # one EV sample per entry decision: fold any banked scale-out leg in.
        # Synthetic rows are skipped for the same reason bucket_stats drops
        # them at boot -- otherwise the EV gate stays contaminated for the
        # whole session and only cleans up on the next restart.
        if not row.get("signal_only"):
            update_bucket_stats(self.ev_stats, play["side"], play["entry_sig"],
                                round(pnl + (play.get("scaled") or {}).get("pnl", 0.0), 4))
        self._event("exit", f"{reason} pnl {pnl:+.2f}", ticker, sig)

    def _flatten(self, reason, pool=None):
        for ticker in list(self.state["open_plays"]):
            play = self.state["open_plays"][ticker]
            if pool is not None and _play_pool(play) != pool:
                continue
            self._exit(ticker, play, play["last_sig"], reason)
        # A pending (unfilled) entry never risked capital, so it's a plain
        # cancel, not an _exit -- but it still must not be allowed to fill
        # later into a pool that was just halted/flattened. In live+auto
        # mode a pending entry carries a real resting order on the exchange
        # (order_id set by _place_entry) -- dropping it from local state
        # without cancelling it would leave that order live on Kalshi's
        # book, able to fill later into a position nothing here is
        # tracking. Mirror _process_pending's cancel_order handling: a
        # cancel failure halts that entry's own pool (not the others) and
        # the loop moves on to clean up the rest.
        for ticker in list(self.state["pending_entries"]):
            pend = self.state["pending_entries"][ticker]
            if pool is not None and _play_pool(pend) != pool:
                continue
            if pend.get("order_id"):
                import live_broker
                try:
                    live_broker.cancel_order(pend["order_id"])
                except Exception as e:
                    ps = self.state["pools"][pend["pool"]]
                    ps["halted"] = True
                    del self.state["pending_entries"][ticker]
                    self._event("halt", f"[{pend['pool']}] order_error: {e} -- "
                                f"auto entries blocked until manually resumed", ticker)
                    continue
            del self.state["pending_entries"][ticker]
            self._event("cancel", f"{reason}: pending entry cancelled", ticker)

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
        # Live money only. Paper has nothing to protect, and unattended
        # paper collection is the whole point of paper mode -- the
        # 680-trade history was gathered exactly that way. Scoping it here
        # (rather than zeroing loop_deadman_mins in config for a paper
        # run) means the guard returns automatically the moment mode flips
        # to live, with no config anyone has to remember to restore.
        if self.broker.mode != "live":
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
        for p in POOL_NAMES:
            ps = self.state["pools"][p]
            stop = self.cfg["day_stop_pct"] * ps["bankroll"]
            if not ps["halted"] and ps["day_pnl"] <= -stop:
                ps["halted"] = True
                self._flatten("halt", pool=p)
                self._event("halt", f"[{p}] day_pnl {ps['day_pnl']:+.2f} <= -{stop:.2f}")

    def _check_profit_lock(self):
        """Trail each pool's own profit peak: once armed, halt that pool
        before a give-back erases it."""
        arm = self.cfg.get("profit_arm_usd") or 0.0
        if arm <= 0:
            return
        for p in POOL_NAMES:
            ps = self.state["pools"][p]
            hi = max(ps.get("day_high", 0.0), ps["day_pnl"])
            ps["day_high"] = hi
            if ps["halted"] or hi < arm:
                continue
            floor = hi * self.cfg.get("profit_keep_frac", 0.5)
            if ps["day_pnl"] <= floor:
                ps["halted"] = True
                self._flatten("halt", pool=p)
                self._event("halt", f"[{p}] profit_lock: day peaked {hi:+.2f}, "
                            f"banking {ps['day_pnl']:+.2f} (floor {floor:.2f})")

    def _check_max_loss(self):
        """Hard cap on each pool's own TOTAL loss. Flat max_loss_usd,
        identical per pool (not divided by 4) — see design spec."""
        for p in POOL_NAMES:
            ps = self.state["pools"][p]
            capped = loss_headroom(ps.get("total_pnl", 0.0), self.cfg) <= 0
            if capped and not ps.get("loss_capped"):
                ps["loss_capped"] = True
                self._flatten("max_loss", pool=p)
                self._event("halt", f"[{p}] MAX LOSS CAP: total_pnl "
                            f"{ps.get('total_pnl', 0.0):+.2f} <= "
                            f"-{self.cfg.get('max_loss_usd', 0):.0f} — trading "
                            f"blocked until max_loss_usd is raised")
            elif not capped and ps.get("loss_capped"):
                ps.pop("loss_capped", None)
                self._event("resume", f"[{p}] max-loss cap released (config raised)")

    def _check_live_stop(self):
        """Code-enforced hard/daily-soft dollar stop against the REAL
        account balance -- only meaningful in live+auto mode, since manual
        mode always has a human reading the dashboard before placing
        anything, and paper mode has no real balance to check. Halts every
        pool (not just one) since this reads one account-wide balance, not
        a per-pool P&L -- unlike _check_day_stop/_check_max_loss, which are
        genuinely per-pool because paper's bankroll is split 4 ways."""
        if not (self.broker.mode == "live"
                and getattr(self.broker, "broker_mode", None) == "auto"):
            return
        from bot_broker import _balance_dollars
        try:
            balance = _balance_dollars()
        except BaseException:
            return   # transient API failure -- try again next tick, don't halt on a blip
        if "live_baseline_balance" not in self.state:
            self.state["live_baseline_balance"] = balance
            return
        pnl = round(balance - self.state["live_baseline_balance"], 4)
        hard = self.cfg.get("live_hard_stop_usd", -8.0)
        daily = self.cfg.get("live_daily_soft_stop_usd", -3.0)
        already_halted = all(p.get("halted") for p in self.state["pools"].values())
        if pnl <= hard and not already_halted:
            for p in self.state["pools"].values():
                p["halted"] = True
            self._flatten("live_hard_stop")
            self._event("halt", f"live_hard_stop: pnl {pnl:+.2f} <= {hard:.2f} "
                        f"vs baseline {self.state['live_baseline_balance']:.2f}")
        elif pnl <= daily and not already_halted:
            for p in self.state["pools"].values():
                p["halted"] = True
            self._flatten("live_daily_soft_stop")
            self._event("halt", f"live_daily_soft_stop: pnl {pnl:+.2f} <= {daily:.2f} "
                        f"vs baseline {self.state['live_baseline_balance']:.2f}")

    # ── main tick ─────────────────────────────────────────────────────
    def tick(self, now_ts=None):
        now_ts = now_ts if now_ts is not None else time.time()
        try:
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
            self._check_live_stop()

            sig = self.fetch()
            if sig is None:
                self.feed_fails += 1
                if self.feed_fails == 3:
                    self._event("feed_down", "3 consecutive fetch failures")
            else:
                self.feed_fails = 0
                self._manage(sig)
        finally:
            # Heartbeat/state ALWAYS persist, even when the tick dies mid-
            # _manage -- otherwise an every-tick exception leaves the last
            # good state unsaved while the process looks alive, and the
            # deadman reads a stale heartbeat as a dead loop.
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

        self._process_pending(sig)

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
        # opposite-flip cancels a still-pending entry the same way — the
        # thesis it was placed on has already reversed
        pend = self.state["pending_entries"].get(ticker)
        if pend and flip and flip != pend["side"]:
            del self.state["pending_entries"][ticker]
            self._event("cancel", "opposite flip before limit filled", ticker, sig)
            return
        if not flip:
            return
        ranges = self._ranges_for(flip, sig)
        pool = session_tag(sig.get("ts"))
        ps = self.state.get("pools", {}).get(pool, {})
        committed = {**self.state["open_plays"], **self.state["pending_entries"]}
        blockers = entry_blockers(sig, self.cfg, committed,
                                  ps.get("halted", False), self.state["paused"],
                                  ranges)
        # Per-session pause (GUI per-pool control). Lives in config, not
        # state, so it survives roll_day_if_needed's per-pool reset and a
        # bot restart -- a session Kenny paused stays paused until he
        # un-pauses it. Blocks NEW entries only; open plays still exit
        # normally, same as the global pause.
        if pool in (self.cfg.get("paused_sessions") or []):
            blockers.append(f"{pool} paused")
        # Live mode: a session that isn't toggled live takes no entries at
        # all -- the 2026-07-29 spec's "decline individually, not refuse to
        # run". Skipping here (like paused_sessions) keeps the broker's own
        # session raise as an unreachable backstop instead of a tick-killer.
        if self.broker.mode == "live":
            from bot_broker import live_unlock_ok
            ok, _why = live_unlock_ok(self.cfg, getattr(self.broker, "env", {}),
                                      pool)
            if not ok:
                blockers.append(f"{pool} not live")
        ev = ev_gate_blocker(flip, sig, self.ev_stats, self.cfg)
        if ev:
            blockers.append(ev)
        cur = weekend_curfew_blocker(sig.get("ts") or time.time(), self.cfg)
        if cur:
            blockers.append(cur)
        if ps.get("loss_capped"):
            blockers.append("max_loss_cap")
        mcap = self.cfg.get("max_entries_per_market") or 0
        n_mkt = self.state.get("market_entries", {}).get(ticker, 0)
        if mcap and n_mkt >= mcap:
            blockers.append(f"market_entries {n_mkt} >= {mcap} — whipsaw guard")
        if blockers:
            self._event("skip", "; ".join(blockers), ticker, sig)
            return
        self._place_entry(flip, sig, ranges)

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
