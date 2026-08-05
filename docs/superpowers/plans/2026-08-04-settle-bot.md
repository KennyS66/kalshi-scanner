# settle_bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A second paper strategy that enters on the *level* of `sig_combined`, buys that side as a maker, and holds to settlement — with no exit logic at all.

**Architecture:** A standalone `settle_bot.py` daemon polling the same `:9050` signal feed as `swing_bot`, with its own config, state, journal, and systemd service. It shares only pure helpers from `bot_core` (`session_tag`) and the `PaperBroker` from `bot_broker`. It never imports `swing_bot` and never writes to `data/bot/`.

**Tech Stack:** Python 3.11, stdlib only (`json`, `urllib.request`, `pathlib`, `time`, `datetime`, `math`), pytest, systemd user service.

Spec: `docs/superpowers/specs/2026-08-04-settle-bot-design.md`

## Global Constraints

- **Paper only.** `settle_bot` constructs `bot_broker.PaperBroker` unconditionally. No live code path, no `BOT_LIVE`, no `LiveBroker` import.
- **Never write to `data/bot/`.** All state under `data/settle/`. A `settle_bot` row reaching `data/bot/bot_trades.jsonl` would contaminate `bot_core.session_gate_stats` (the live-unlock gate) and `bucket_stats` (the EV gate) — the exact bug fixed in `a838f93`.
- **Write ordering.** Durable record BEFORE state mutation on settlement (the `_exit`/`_scale_out`/`_fill_pending` defect found three times on 2026-08-03). The one exception is opening a position: state first, then the event, because the buy already happened and losing the position is worse than losing an audit row.
- **Flat size.** `qty` is always 1. No signal-strength scaling.
- **Never chase.** An unfilled resting order is cancelled when the entry window closes. It is never converted to a market order.
- **Entry window `[5.0, 11.0]` minutes, threshold `10.0`** — deliberately wider/looser than the in-sample optimum (peak was 7–9 min; threshold 20 scored higher but broke down in the third window at −0.037).
- Python 3.11, stdlib only. Run tests with `.venv/bin/python -m pytest`.

---

### Task 1: Module foundation — config, state, journal I/O

**Files:**
- Create: `settle_bot.py`
- Test: `tests/test_settle_bot.py`

**Interfaces:**
- Consumes: nothing
- Produces: `DEFAULT_CONFIG: dict`, `load_config(path) -> dict`, `fresh_state() -> dict`, `load_state(d) -> dict`, `save_state(d, state) -> None`, `append_jsonl(path, row) -> None`, and the module constants `SETTLE_DIR`, `CONFIG_FILE`, `STATE_FILE`, `TRADES_FILE`, `EVENTS_FILE`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_settle_bot.py
import json

from settle_bot import (DEFAULT_CONFIG, load_config, fresh_state, load_state,
                        save_state, append_jsonl)


def test_default_config_has_the_spec_values():
    assert DEFAULT_CONFIG["entry_threshold"] == 10.0
    assert DEFAULT_CONFIG["min_mins_left"] == 5.0
    assert DEFAULT_CONFIG["max_mins_left"] == 11.0
    assert DEFAULT_CONFIG["qty"] == 1
    assert DEFAULT_CONFIG["mode"] == "paper"


def test_load_config_merges_file_over_defaults(tmp_path):
    p = tmp_path / "settle_config.json"
    p.write_text(json.dumps({"entry_threshold": 15.0}))
    cfg = load_config(p)
    assert cfg["entry_threshold"] == 15.0
    assert cfg["max_mins_left"] == DEFAULT_CONFIG["max_mins_left"]


def test_load_config_missing_or_corrupt_gives_defaults(tmp_path):
    assert load_config(tmp_path / "nope.json") == DEFAULT_CONFIG
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_config(bad) == DEFAULT_CONFIG


def test_state_roundtrip_is_atomic(tmp_path):
    s = fresh_state()
    s["open"]["T1"] = {"side": "YES", "qty": 1}
    save_state(tmp_path, s)
    assert load_state(tmp_path) == s
    assert not list(tmp_path.glob("*.tmp"))


def test_load_state_missing_gives_fresh(tmp_path):
    s = load_state(tmp_path)
    assert s == fresh_state() | {"day": s["day"]}


def test_append_jsonl_creates_parents_and_appends(tmp_path):
    p = tmp_path / "sub" / "x.jsonl"
    append_jsonl(p, {"a": 1})
    append_jsonl(p, {"b": 2})
    rows = [json.loads(l) for l in p.read_text().splitlines()]
    assert rows == [{"a": 1}, {"b": 2}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_settle_bot.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'settle_bot'`

- [ ] **Step 3: Write minimal implementation**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_settle_bot.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add settle_bot.py tests/test_settle_bot.py
git commit -m "settle_bot: config, state and journal foundations"
```

---

### Task 2: Entry decision

**Files:**
- Modify: `settle_bot.py`
- Test: `tests/test_settle_bot.py`

**Interfaces:**
- Consumes: `DEFAULT_CONFIG` (Task 1)
- Produces: `entry_decision(sig: dict, cfg: dict) -> str | None` returning `"YES"`, `"NO"`, or `None`

- [ ] **Step 1: Write the failing test**

```python
from settle_bot import entry_decision


def _sig(**over):
    base = {"status": "ok", "ticker": "M1", "yes_ask": 0.42, "no_ask": 0.59,
            "spread": 0.01, "mins_left": 8.0, "sig_combined": 0.0,
            "distance": -18.0, "ts": 1000.0}
    base.update(over)
    return base


def test_entry_fires_on_the_signal_sign():
    cfg = dict(DEFAULT_CONFIG)
    assert entry_decision(_sig(sig_combined=15.0), cfg) == "YES"
    assert entry_decision(_sig(sig_combined=-15.0), cfg) == "NO"
    assert entry_decision(_sig(sig_combined=10.0), cfg) == "YES"   # inclusive
    assert entry_decision(_sig(sig_combined=-10.0), cfg) == "NO"


def test_entry_blocked_below_threshold():
    cfg = dict(DEFAULT_CONFIG)
    assert entry_decision(_sig(sig_combined=9.9), cfg) is None
    assert entry_decision(_sig(sig_combined=-9.9), cfg) is None
    assert entry_decision(_sig(sig_combined=0.0), cfg) is None


def test_entry_only_inside_the_time_window():
    cfg = dict(DEFAULT_CONFIG)
    assert entry_decision(_sig(sig_combined=15.0, mins_left=5.0), cfg) == "YES"
    assert entry_decision(_sig(sig_combined=15.0, mins_left=11.0), cfg) == "YES"
    assert entry_decision(_sig(sig_combined=15.0, mins_left=4.9), cfg) is None
    assert entry_decision(_sig(sig_combined=15.0, mins_left=11.1), cfg) is None


def test_entry_needs_a_usable_quote_and_ok_status():
    cfg = dict(DEFAULT_CONFIG)
    assert entry_decision(_sig(sig_combined=15.0, status="between_markets"), cfg) is None
    assert entry_decision(_sig(sig_combined=15.0, yes_ask=None), cfg) is None
    assert entry_decision(_sig(sig_combined=15.0, no_ask=None), cfg) is None
    assert entry_decision(_sig(sig_combined=None), cfg) is None
    assert entry_decision(_sig(sig_combined=15.0, mins_left=None), cfg) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_settle_bot.py -k entry -v`
Expected: FAIL with `ImportError: cannot import name 'entry_decision'`

- [ ] **Step 3: Write minimal implementation**

Append to `settle_bot.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_settle_bot.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add settle_bot.py tests/test_settle_bot.py
git commit -m "settle_bot: entry decision on sig_combined level"
```

---

### Task 3: Resting-order lifecycle — place, fill check, cancel, never chase

**Files:**
- Modify: `settle_bot.py`
- Test: `tests/test_settle_bot.py`

**Interfaces:**
- Consumes: `entry_decision` (Task 2)
- Produces: `limit_price(sig: dict, side: str) -> float | None`, `limit_filled(sig: dict, pend: dict) -> bool`

`pend` dict shape (stored in `state["pending"][ticker]`):
`{"side": str, "limit": float, "qty": int, "placed_ts": float, "entry_sig": dict}`

- [ ] **Step 1: Write the failing test**

```python
from settle_bot import limit_price, limit_filled


def test_limit_price_joins_the_bid_never_crosses():
    # yes_ask 0.42, spread 0.01 -> rest at 0.41, strictly below the ask
    assert limit_price(_sig(), "YES") == 0.41
    assert limit_price(_sig(), "NO") == 0.58        # no_ask 0.59 - 0.01
    assert limit_price(_sig(yes_ask=None), "YES") is None


def test_limit_price_clamps_at_one_cent_and_handles_crossed_book():
    assert limit_price(_sig(yes_ask=0.01, spread=0.05), "YES") == 0.01
    # crossed book -> negative spread must not push the limit ABOVE the ask
    assert limit_price(_sig(yes_ask=0.42, spread=-0.03), "YES") == 0.42


def test_limit_fills_only_when_the_ask_reaches_it():
    pend = {"side": "YES", "limit": 0.41, "qty": 1, "placed_ts": 1000.0,
            "entry_sig": {}}
    assert limit_filled(_sig(yes_ask=0.41), pend) is True
    assert limit_filled(_sig(yes_ask=0.40), pend) is True
    assert limit_filled(_sig(yes_ask=0.42), pend) is False
    assert limit_filled(_sig(yes_ask=None), pend) is False
    no_pend = dict(pend, side="NO", limit=0.58)
    assert limit_filled(_sig(no_ask=0.57), no_pend) is True
    assert limit_filled(_sig(no_ask=0.59), no_pend) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_settle_bot.py -k limit -v`
Expected: FAIL with `ImportError: cannot import name 'limit_price'`

- [ ] **Step 3: Write minimal implementation**

Append to `settle_bot.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_settle_bot.py -v`
Expected: 13 passed

- [ ] **Step 5: Commit**

```bash
git add settle_bot.py tests/test_settle_bot.py
git commit -m "settle_bot: maker limit price and fill check"
```

---

### Task 4: The Bot class — place, fill, cancel (no chasing)

**Files:**
- Modify: `settle_bot.py`
- Test: `tests/test_settle_bot.py`

**Interfaces:**
- Consumes: everything from Tasks 1–3
- Produces: `fetch_signal() -> dict | None`, `class Bot` with `__init__(self, bot_dir=None, fetch_fn=fetch_signal)`, `_event(action, reason="", ticker="", sig=None)`, `_place(side, sig)`, `_process_pending(sig)`, `tick(now_ts=None)`

`state["open"][ticker]` shape:
`{"side": str, "qty": int, "entry_price": float, "fee_total": float, "entry_ts": float, "entry_sig": dict, "last_sig": dict}`

- [ ] **Step 1: Write the failing test**

```python
import bot_broker
from settle_bot import Bot, TRADES_FILE, EVENTS_FILE


def _rows(tmp_path, name):
    p = tmp_path / name
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def _mkbot(tmp_path, sigs):
    it = iter(sigs)
    return Bot(tmp_path, fetch_fn=lambda: next(it, None))


def test_qualifying_signal_rests_a_limit(tmp_path):
    bot = _mkbot(tmp_path, [_sig(sig_combined=15.0)])
    bot.tick(now_ts=1000.0)
    assert list(bot.state["pending"]) == ["M1"]
    assert bot.state["pending"]["M1"]["limit"] == 0.41
    assert bot.state["pending"]["M1"]["qty"] == 1
    assert bot.state["open"] == {}
    assert any(e["action"] == "place" for e in _rows(tmp_path, EVENTS_FILE))


def test_resting_order_fills_when_the_ask_reaches_it(tmp_path):
    sigs = [_sig(sig_combined=15.0),                       # place at 0.41
            _sig(sig_combined=15.0, yes_ask=0.41, mins_left=7.0)]   # fills
    bot = _mkbot(tmp_path, sigs)
    bot.tick(now_ts=1000.0)
    bot.tick(now_ts=1005.0)
    assert bot.state["pending"] == {}
    assert bot.state["open"]["M1"]["entry_price"] == 0.41
    assert bot.state["open"]["M1"]["qty"] == 1
    assert bot.state["open"]["M1"]["fee_total"] == 0.0     # maker fee is zero
    assert any(e["action"] == "enter" for e in _rows(tmp_path, EVENTS_FILE))


def test_unfilled_order_is_cancelled_at_window_exit_never_chased(tmp_path):
    sigs = [_sig(sig_combined=15.0),                              # place
            _sig(sig_combined=15.0, mins_left=4.5, yes_ask=0.42)] # window closed
    bot = _mkbot(tmp_path, sigs)
    bot.tick(now_ts=1000.0)
    bot.tick(now_ts=1005.0)
    assert bot.state["pending"] == {}
    assert bot.state["open"] == {}, "an expired limit was chased into a position"
    assert any(e["action"] == "cancel" for e in _rows(tmp_path, EVENTS_FILE))


def test_one_attempt_per_market(tmp_path):
    sigs = [_sig(sig_combined=15.0), _sig(sig_combined=15.0),
            _sig(sig_combined=15.0)]
    bot = _mkbot(tmp_path, sigs)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert len(_rows(tmp_path, EVENTS_FILE)) == 1     # placed once, not thrice
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_settle_bot.py -k "rests or fills or cancelled or one_attempt" -v`
Expected: FAIL with `ImportError: cannot import name 'Bot'`

- [ ] **Step 3: Write minimal implementation**

Append to `settle_bot.py`:

```python
from bot_broker import PaperBroker


def fetch_signal():
    try:
        with urllib.request.urlopen(
                "http://localhost:9050/api/crypto/signal", timeout=4) as r:
            return json.loads(r.read())
    except Exception:
        return None


class Bot:
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
            if not self._seen(sig.get("ticker") or ""):
                side = entry_decision(sig, self.cfg)
                if side:
                    self._place(side, sig)
        finally:
            self.state["heartbeat"] = now_ts
            save_state(self.dir, self.state)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_settle_bot.py -v`
Expected: 17 passed

- [ ] **Step 5: Commit**

```bash
git add settle_bot.py tests/test_settle_bot.py
git commit -m "settle_bot: resting entry lifecycle, never chases"
```

---

### Task 5: Settlement — resolve, journal first, book P&L

**Files:**
- Modify: `settle_bot.py`
- Test: `tests/test_settle_bot.py`

**Interfaces:**
- Consumes: `Bot` (Task 4)
- Produces: `settle_side(last_sig: dict) -> str`, `settle_pnl(pos: dict, settled: str) -> float`, `Bot._resolve(ticker, pos, sig)`

`settle_trades.jsonl` row shape:
`{"ticker", "mode", "side", "qty", "entry_price", "entry_ts", "settle_ts", "settled", "net_pnl", "fees", "entry_sig", "status": "settled"}`

- [ ] **Step 1: Write the failing test**

```python
from settle_bot import settle_side, settle_pnl


def test_settle_side_reads_the_sign_of_distance():
    assert settle_side({"distance": 12.5}) == "YES"
    assert settle_side({"distance": -3.0}) == "NO"
    assert settle_side({"distance": 0.0}) == "NO"      # at/below strike = NO


def test_settle_pnl_pays_one_minus_entry_on_a_win():
    pos = {"side": "YES", "qty": 1, "entry_price": 0.41, "fee_total": 0.0}
    assert settle_pnl(pos, "YES") == 0.59
    assert settle_pnl(pos, "NO") == -0.41
    no_pos = {"side": "NO", "qty": 1, "entry_price": 0.58, "fee_total": 0.0}
    assert settle_pnl(no_pos, "NO") == 0.42
    assert settle_pnl(no_pos, "YES") == -0.58


def test_position_resolves_when_its_market_rolls_away(tmp_path):
    sigs = [_sig(sig_combined=15.0),
            _sig(sig_combined=15.0, yes_ask=0.41, mins_left=7.0),   # fill
            _sig(ticker="M1", yes_ask=0.98, mins_left=0.1, distance=25.0,
                 sig_combined=0.0),                                  # near expiry
            _sig(ticker="M2", mins_left=14.0, sig_combined=0.0)]     # M1 gone
    bot = _mkbot(tmp_path, sigs)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert bot.state["open"] == {}
    rows = _rows(tmp_path, TRADES_FILE)
    assert len(rows) == 1
    assert rows[0]["settled"] == "YES" and rows[0]["net_pnl"] == 0.59
    assert rows[0]["status"] == "settled"


def test_failed_journal_write_leaves_the_position_open(tmp_path, monkeypatch):
    """Write-ordering: the durable row lands before the position is forgotten."""
    import settle_bot
    sigs = [_sig(sig_combined=15.0),
            _sig(sig_combined=15.0, yes_ask=0.41, mins_left=7.0),
            _sig(ticker="M1", yes_ask=0.98, mins_left=0.1, distance=25.0,
                 sig_combined=0.0),
            _sig(ticker="M2", mins_left=14.0, sig_combined=0.0)]
    bot = _mkbot(tmp_path, sigs)
    bot.tick(now_ts=1000.0)
    bot.tick(now_ts=1005.0)
    assert "M1" in bot.state["open"]

    real = settle_bot.append_jsonl

    def boom(path, row):
        if str(path).endswith(TRADES_FILE):
            raise OSError("disk full")
        return real(path, row)

    monkeypatch.setattr(settle_bot, "append_jsonl", boom)
    with pytest.raises(OSError):
        bot.tick(now_ts=1010.0)
    assert "M1" in bot.state["open"], "position forgotten with no journal row"
    assert _rows(tmp_path, TRADES_FILE) == []


def test_settle_rows_never_touch_the_swing_bot_journal(tmp_path):
    """The contamination guard: data/bot/ feeds the swing bot's live gates."""
    sigs = [_sig(sig_combined=15.0),
            _sig(sig_combined=15.0, yes_ask=0.41, mins_left=7.0),
            _sig(ticker="M1", yes_ask=0.98, mins_left=0.1, distance=25.0,
                 sig_combined=0.0),
            _sig(ticker="M2", mins_left=14.0, sig_combined=0.0)]
    bot = _mkbot(tmp_path, sigs)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert _rows(tmp_path, TRADES_FILE)                      # it did write
    assert not (tmp_path / "bot_trades.jsonl").exists()
    assert not (tmp_path / "bot_events.jsonl").exists()
```

Add `import pytest` to the test file's imports.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_settle_bot.py -k "settle or resolves or journal or contamin" -v`
Expected: FAIL with `ImportError: cannot import name 'settle_side'`

- [ ] **Step 3: Write minimal implementation**

Append the two helpers to `settle_bot.py`:

```python
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
```

Add `_resolve` to `Bot`:

```python
    SETTLE_MINS = 0.5      # a tick this close to expiry decides settlement

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
```

Wire it into `tick`, replacing the `last_sig` refresh loop:

**AMENDED 2026-08-05 (human ruling, supersedes the version below).** Resolve only after
the market has been absent for `cfg["absent_ticks_to_resolve"]` consecutive ticks
(add it to `DEFAULT_CONFIG` as 3), resetting the counter whenever the position's own
ticker is seen:

```python
            for t in list(self.state["open"]):
                pos = self.state["open"][t]
                if t == sig.get("ticker") and sig.get("status") == "ok":
                    pos["last_sig"] = dict(sig)
                    pos["absent"] = 0
                elif sig.get("ticker"):
                    pos["absent"] = pos.get("absent", 0) + 1
                    if pos["absent"] >= self.cfg.get("absent_ticks_to_resolve", 3):
                        self._resolve(t, pos, sig)
```

Why: resolving on the FIRST ticker mismatch meant one stray off-ticker tick booked a
mid-hold position `unresolved`, dropped it from the journal, and `_seen` then blocked
re-entry forever — silently biasing the fill-rate and edge numbers this build exists to
measure. Superseded original:

```python
            for t in list(self.state["open"]):
                if t == sig.get("ticker") and sig.get("status") == "ok":
                    self.state["open"][t]["last_sig"] = dict(sig)
                elif sig.get("ticker") and t != sig.get("ticker"):
                    self._resolve(t, self.state["open"][t], sig)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_settle_bot.py -v`
Expected: 22 passed

- [ ] **Step 5: Commit**

```bash
git add settle_bot.py tests/test_settle_bot.py
git commit -m "settle_bot: settlement resolution, journal before state"
```

---

### Task 6: Boot reconciliation

**Files:**
- Modify: `settle_bot.py`
- Test: `tests/test_settle_bot.py`

**Interfaces:**
- Consumes: `Bot` (Tasks 4–5)
- Produces: `Bot._reconcile()`, called at the end of `Bot.__init__`; writes a `reconcile` event and a stderr warning

- [ ] **Step 1: Write the failing test**

```python
def test_reconcile_warns_when_an_entry_has_no_settle(tmp_path):
    for row in [{"ts": 1.0, "ticker": "A", "action": "enter"},
                {"ts": 2.0, "ticker": "A", "action": "settle"},
                {"ts": 3.0, "ticker": "B", "action": "enter"}]:   # B lost
        append_jsonl(tmp_path / EVENTS_FILE, row)
    Bot(tmp_path, fetch_fn=lambda: None)
    flagged = [e for e in _rows(tmp_path, EVENTS_FILE)
               if e["action"] == "reconcile"]
    assert len(flagged) == 1 and "+1" in flagged[0]["reason"]


def test_reconcile_quiet_when_an_open_position_explains_the_gap(tmp_path):
    for row in [{"ts": 1.0, "ticker": "A", "action": "enter"},
                {"ts": 2.0, "ticker": "A", "action": "settle"},
                {"ts": 3.0, "ticker": "B", "action": "enter"}]:
        append_jsonl(tmp_path / EVENTS_FILE, row)
    s = fresh_state()
    s["open"]["B"] = {"side": "YES", "qty": 1, "entry_price": 0.4,
                      "fee_total": 0.0, "entry_ts": 3.0}
    save_state(tmp_path, s)
    Bot(tmp_path, fetch_fn=lambda: None)
    assert [e for e in _rows(tmp_path, EVENTS_FILE)
            if e["action"] == "reconcile"] == []


def test_reconcile_stays_quiet_at_a_steady_baseline(tmp_path):
    for row in [{"ts": 1.0, "ticker": "A", "action": "enter"}]:
        append_jsonl(tmp_path / EVENTS_FILE, row)
    b1 = Bot(tmp_path, fetch_fn=lambda: None)
    save_state(tmp_path, b1.state)
    before = len([e for e in _rows(tmp_path, EVENTS_FILE)
                  if e["action"] == "reconcile"])
    Bot(tmp_path, fetch_fn=lambda: None)
    after = len([e for e in _rows(tmp_path, EVENTS_FILE)
                 if e["action"] == "reconcile"])
    assert after == before, "warned again at an unchanged baseline"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_settle_bot.py -k reconcile -v`
Expected: FAIL — no `reconcile` event is ever written

- [ ] **Step 3: Write minimal implementation**

Add to `Bot`, and call `self._reconcile()` as the last line of `__init__`:

```python
    def _reconcile(self):
        """Every entry either settled or is still open.

        Mirrors swing_bot._reconcile_open_plays: warn on ANY deviation from
        the stored baseline, in either direction. Positive means a position
        was forgotten without a journal row; negative means a fill whose
        event never landed. Warns, never raises -- the bot has to come up.
        """
        try:
            rows = [json.loads(l) for l in
                    (self.dir / EVENTS_FILE).read_text().splitlines() if l.strip()]
        except Exception:
            return
        enters = sum(1 for e in rows if e.get("action") == "enter")
        settles = sum(1 for e in rows if e.get("action") in ("settle", "unresolved"))
        open_n = len(self.state.get("open") or {})
        gap = enters - settles - open_n
        if gap != self.state.get("reconcile_baseline", 0):
            msg = (f"{gap:+d} entries unaccounted for "
                   f"({enters} enter / {settles} settled / {open_n} open)")
            self._event("reconcile", msg)
            print(f"settle_bot WARNING: {msg}", file=sys.stderr, flush=True)
        self.state["reconcile_baseline"] = gap
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_settle_bot.py -v`
Expected: 25 passed

- [ ] **Step 5: Commit**

```bash
git add settle_bot.py tests/test_settle_bot.py
git commit -m "settle_bot: boot reconciliation tripwire"
```

---

### Task 7: Daemon entrypoint

**Files:**
- Modify: `settle_bot.py`
- Test: `tests/test_settle_bot.py`

**Interfaces:**
- Consumes: `Bot` (Tasks 4–6)
- Produces: `Bot.run()`, `if __name__ == "__main__"` block

- [ ] **Step 1: Write the failing test**

```python
def test_run_survives_a_tick_that_raises(tmp_path, monkeypatch):
    """A bad tick must log an error event and keep the loop alive."""
    bot = Bot(tmp_path, fetch_fn=lambda: None)
    calls = {"n": 0}

    def boom(now_ts=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        raise KeyboardInterrupt      # end the loop on the second pass

    monkeypatch.setattr(bot, "tick", boom)
    bot.cfg["poll_secs"] = 0        # cfg is a plain dict -- set the key, do
                                    # NOT monkeypatch.setattr a dict method
    with pytest.raises(KeyboardInterrupt):
        bot.run()
    assert any(e["action"] == "error" for e in _rows(tmp_path, EVENTS_FILE))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_settle_bot.py -k run_survives -v`
Expected: FAIL with `AttributeError: 'Bot' object has no attribute 'run'`

- [ ] **Step 3: Write minimal implementation**

```python
    def run(self):
        # Timestamped: logs/settle_bot.log appends across restarts, so
        # untimestamped banners from consecutive runs are indistinguishable.
        print(f"[{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}] "
              f"settle_bot up — mode={self.broker.mode} dir={self.dir}", flush=True)
        while True:
            try:
                self.tick()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                self._event("error", repr(e))
            time.sleep(self.cfg.get("poll_secs", 5))


if __name__ == "__main__":
    Bot().run()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_settle_bot.py -v`
Expected: 26 passed

- [ ] **Step 5: Commit**

```bash
git add settle_bot.py tests/test_settle_bot.py
git commit -m "settle_bot: daemon loop and entrypoint"
```

---

### Task 8: Replay validation — pin the implementation to the measurement

**Files:**
- Create: `settle_replay.py`
- Test: run it manually (too slow for the unit suite — 189MB feature log)

**Interfaces:**
- Consumes: `entry_decision`, `limit_price`, `settle_side` (Tasks 2, 3, 5)
- Produces: `settle_replay.replay(rows, cfg) -> dict` with keys `n`, `edge`, `wins`

- [ ] **Step 1: Write the script**

```python
#!/usr/bin/env python3
"""Replay settle_bot's entry rule over the historical feature log.

Acceptance gate from the spec: edge must land in +0.02..+0.05 per contract
and be positive in all three time windows. It will NOT reproduce +0.0406
exactly -- that came from one observation per market at ~10 minutes, while
the live rule takes the first qualifying tick anywhere in [5, 11]. A result
outside the band means the implementation does not match the rule that was
measured, and is a bug rather than a new finding.

Usage: .venv/bin/python settle_replay.py
"""
import datetime as dt
import json
import math
from collections import defaultdict

from settle_bot import DEFAULT_CONFIG, entry_decision, limit_price, settle_side

LOG = "data/whales/signal_feature_log.jsonl"
CUT = dt.datetime(2026, 6, 30, tzinfo=dt.timezone.utc).timestamp()


def load():
    by = defaultdict(list)
    with open(LOG) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if "BTC15M" not in (r.get("ticker") or ""):
                continue
            if (r.get("ts") or 0) < CUT or r.get("yes_ask") is None:
                continue
            r.setdefault("status", "ok")
            by[r["ticker"]].append(r)
    for rs in by.values():
        rs.sort(key=lambda r: r["ts"])
    return by


def replay(by, cfg):
    pnls = []
    for ticker, rs in by.items():
        settled = settle_side(rs[-1])
        for r in rs:                       # first qualifying tick wins
            side = entry_decision(r, cfg)
            if not side:
                continue
            px = limit_price(r, side)
            if px is None:
                break
            won = (side == settled)
            pnls.append((1.0 - px) if won else -px)
            break
    n = len(pnls)
    edge = sum(pnls) / n if n else 0.0
    return {"n": n, "edge": edge,
            "wins": sum(1 for p in pnls if p > 0)}


def main():
    cfg = dict(DEFAULT_CONFIG)
    by = load()
    tickers = sorted(by, key=lambda t: by[t][0]["ts"])
    third = len(tickers) // 3
    windows = [("W1", tickers[:third]), ("W2", tickers[third:2 * third]),
               ("W3", tickers[2 * third:])]
    allpos = True
    for name, ts in windows:
        r = replay({t: by[t] for t in ts}, cfg)
        allpos &= r["edge"] > 0
        print(f"{name}: n={r['n']:4} edge={r['edge']:+.4f}")
    total = replay(by, cfg)
    print(f"ALL: n={total['n']:4} edge={total['edge']:+.4f}")
    ok = allpos and 0.02 <= total["edge"] <= 0.05
    print(f"GATE: {'PASS' if ok else 'FAIL'} "
          f"(need +0.02..+0.05 overall and positive in 3/3)")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python settle_replay.py`
Expected: three window lines, an ALL line, and `GATE: PASS`.

If it prints `GATE: FAIL`, stop and diagnose — the implementation does not match the measured rule. Do not adjust the band to make it pass.

- [ ] **Step 3: Commit**

```bash
git add settle_replay.py
git commit -m "settle_bot: replay gate pinning the rule to its measurement"
```

---

### Task 9: systemd service and deployment

**Files:**
- Create: `deploy/settle-bot.service`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `settle_bot.py` (Task 7)
- Produces: an installable user unit

- [ ] **Step 1: Write the unit file**

```ini
[Unit]
# Install:
#   cp deploy/settle-bot.service ~/.config/systemd/user/
#   systemctl --user daemon-reload && systemctl --user enable --now settle-bot
# Linger is already enabled for this box, so it starts at boot without a login.
#
# Independent of kalshi-scanner.service on purpose: either can restart without
# disturbing the other. It does need :9050 for the signal feed and simply
# retries until that is up.
Description=settle_bot: directional hold-to-settlement paper strategy
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory=/home/kenny/bots/kalshi-scanner
ExecStart=/home/kenny/bots/kalshi-scanner/.venv/bin/python -u /home/kenny/bots/kalshi-scanner/settle_bot.py
Restart=always
RestartSec=15
StandardOutput=append:/home/kenny/bots/kalshi-scanner/logs/settle_bot.log
StandardError=append:/home/kenny/bots/kalshi-scanner/logs/settle_bot.log

[Install]
WantedBy=default.target
```

- [ ] **Step 2: Confirm `data/settle/` is journalled, not ignored**

Run: `grep -n "data/" .gitignore`

`logs/` is already ignored. `data/settle/` must NOT be ignored — the journal is the forward-test evidence and needs committing by `auto_commit_cron.sh`. If a broad `data/` rule exists, add `!data/settle/`.

- [ ] **Step 3: Install and verify**

```bash
mkdir -p logs data/settle
cp deploy/settle-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now settle-bot
sleep 15
systemctl --user is-active settle-bot
tail -3 logs/settle_bot.log
```

Expected: `active`, and a timestamped `settle_bot up — mode=paper` banner.

- [ ] **Step 4: Verify isolation on the live box**

```bash
ls data/settle/
git status --short data/bot/ | head
```

Expected: `settle_state.json` and `settle_events.jsonl` exist under `data/settle/`, and `data/bot/` shows no new files attributable to settle_bot.

- [ ] **Step 5: Commit**

```bash
git add deploy/settle-bot.service .gitignore
git commit -m "settle_bot: systemd user service"
```

---

## Self-Review

**Spec coverage.** Entry rule → Task 2. Maker-only/never-chase → Tasks 3, 4. No exit → Task 5. Write ordering → Task 5 (journal-before-state, with a failing-append test) and Task 4 (state-before-event on open). Isolation → Task 5's contamination guard plus Task 9's live check. Boot reconciliation → Task 6. Testing → every task. Deployment → Task 9. Replay acceptance gate → Task 8. Flat `qty=1` → Tasks 1, 4. Unresolved bucket → Task 5.

**Not covered by any task, deliberately:** the spec's kill criteria (below +0.01/contract after 200 fills, or fill rate under 20%) are an operating decision, not code. Revisit once the journal has 200 fills.

**Type consistency.** `pend` keys (`side`, `limit`, `qty`, `placed_ts`, `entry_sig`) are used identically in Tasks 3 and 4. `state["open"][t]` keys (`side`, `qty`, `entry_price`, `fee_total`, `entry_ts`, `entry_sig`, `last_sig`) are written in Task 4 and read in Tasks 5 and 6. `settle_pnl` reads `fee_total` via `.get` so a hand-built position in a test cannot `KeyError`. `entry_decision`, `limit_price`, `limit_filled`, `settle_side`, `settle_pnl` keep one signature throughout, including in Task 8's replay.

**Known rough edge for the implementer.** Task 4's `tick` refreshes `last_sig` and Task 5 replaces that loop with one that also resolves rolled markets. Apply Task 5's version — it supersedes Task 4's.
