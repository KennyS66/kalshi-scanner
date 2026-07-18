# Trade Grader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A sidecar daemon that grades every closed paper-bot trade after market settlement (verdicts, counterfactuals, path stats, day-context) and archives the feature log daily.

**Architecture:** One new module `trade_grader.py` of pure, individually-testable functions plus a thin poll loop; append-only output to `data/bot/bot_trade_grades.jsonl`; incremental feature-log reader so the growing log is read fully only once at startup. Zero changes to trading code; one line added to `start.sh`.

**Tech Stack:** Python 3 stdlib only (json, gzip, pathlib, time). pytest 9 for tests (existing `tests/` conventions: plain functions, `tmp_path`).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-17-trade-grader-design.md` — schema and verdict table are copied verbatim there; do not rename fields.
- Append-only: never rewrite `bot_trades.jsonl`, `bot_trade_grades.jsonl`, or truncate `signal_feature_log.jsonl`.
- The daemon must survive any bad data row (per-trade try/except, stderr logging, keep looping).
- Grade key for dedup: `f"{ticker}|{entry_ts}"`.
- Expiry: `entry_ts + entry_sig["mins_left"] * 60`; grading waits `GRADE_DELAY_S = 90` past expiry.
- Interpreter: `.venv/bin/python`; run tests with `.venv/bin/python -m pytest`.

---

### Task 1: Core grading functions (verdict, settlement, path stats)

**Files:**
- Create: `trade_grader.py`
- Test: `tests/test_trade_grader.py`

**Interfaces:**
- Produces: `verdict_for(exit_reason: str, side: str, settled: str) -> str`;
  `infer_settlement(ticks: list[dict], expiry: float) -> tuple[str, str]` (settled `"YES"|"NO"|"unknown"`, basis `"strike"|"price"|"none"`);
  `hold_path_stats(ticks, side, entry_price, entry_ts, exit_ts) -> tuple[float|None, float|None]` (mfe, mae);
  `expiry_of(trade: dict) -> float`. Ticks are parsed feature-log rows: dicts with `ts`, `spot`, `floor_strike`, `price`.
- Constants: `SETTLE_WINDOW_S = 120`, `GRADE_DELAY_S = 90`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_trade_grader.py
import pytest

from trade_grader import (
    verdict_for, infer_settlement, hold_path_stats, expiry_of,
)


def tick(ts, spot=64000.0, strike=63950.0, price=0.5, ticker="T-1"):
    return {"ts": ts, "spot": spot, "floor_strike": strike,
            "price": price, "ticker": ticker}


@pytest.mark.parametrize("reason,side,settled,expected", [
    ("stop",   "YES", "YES", "whipsaw_stop"),
    ("stop",   "YES", "NO",  "good_stop"),
    ("target", "NO",  "NO",  "clean_win"),
    ("target", "NO",  "YES", "lucky_exit"),
    ("deadman","YES", "YES", "left_money"),
    ("flip",   "NO",  "YES", "good_exit"),
    ("stop",   "YES", "unknown", "ungraded"),
])
def test_verdict_table(reason, side, settled, expected):
    assert verdict_for(reason, side, settled) == expected


def test_expiry_from_entry_sig():
    t = {"entry_ts": 1000.0, "entry_sig": {"mins_left": 10.0}}
    assert expiry_of(t) == 1600.0


def test_settlement_strike_basis_yes_and_no():
    # last tick 30s before expiry -> inside SETTLE_WINDOW_S -> strike basis
    up = [tick(900), tick(970, spot=64000.0, strike=63950.0)]
    assert infer_settlement(up, expiry=1000.0) == ("YES", "strike")
    dn = [tick(900), tick(970, spot=63900.0, strike=63950.0)]
    assert infer_settlement(dn, expiry=1000.0) == ("NO", "strike")


def test_settlement_price_fallback_when_no_late_tick():
    # last tick 300s before expiry, but market already decided
    decided = [tick(700, price=0.97)]
    assert infer_settlement(decided, expiry=1000.0) == ("YES", "price")
    decided_no = [tick(700, price=0.03)]
    assert infer_settlement(decided_no, expiry=1000.0) == ("NO", "price")
    undecided = [tick(700, price=0.5)]
    assert infer_settlement(undecided, expiry=1000.0) == ("unknown", "none")
    assert infer_settlement([], expiry=1000.0) == ("unknown", "none")


def test_settlement_ignores_ticks_after_expiry():
    ticks = [tick(970, spot=64000.0, strike=63950.0),
             tick(1050, spot=60000.0, strike=63950.0)]  # next market's data
    assert infer_settlement(ticks, expiry=1000.0) == ("YES", "strike")


def test_hold_path_stats_yes_side():
    ticks = [tick(100, price=0.50), tick(160, price=0.62), tick(220, price=0.41)]
    mfe, mae = hold_path_stats(ticks, "YES", entry_price=0.50,
                               entry_ts=100, exit_ts=220)
    assert mfe == pytest.approx(0.12)
    assert mae == pytest.approx(0.09)


def test_hold_path_stats_no_side_uses_inverted_price():
    # NO side price = 1 - price
    ticks = [tick(100, price=0.50), tick(160, price=0.30), tick(220, price=0.70)]
    mfe, mae = hold_path_stats(ticks, "NO", entry_price=0.50,
                               entry_ts=100, exit_ts=220)
    assert mfe == pytest.approx(0.20)   # 1-0.30=0.70 vs 0.50
    assert mae == pytest.approx(0.20)   # 1-0.70=0.30 vs 0.50


def test_hold_path_stats_no_ticks_in_window():
    assert hold_path_stats([], "YES", 0.5, 100, 200) == (None, None)
    outside = [tick(50), tick(300)]
    assert hold_path_stats(outside, "YES", 0.5, 100, 200) == (None, None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_trade_grader.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'trade_grader'`

- [ ] **Step 3: Write the implementation**

```python
#!/usr/bin/env python3
"""Trade grader - post-settlement loss forensics for the swing paper bot.

Sidecar daemon (started by start.sh). Grades every closed trade in
data/bot/bot_trades.jsonl once its market has settled: settlement side,
counterfactual P&L vs holding, max favorable/adverse excursion during the
hold, a verdict (good_stop / whipsaw_stop / clean_win / lucky_exit /
good_exit / left_money), and the day's bias/regime context. Appends one
row per trade to data/bot/bot_trade_grades.jsonl. Never touches trading
code or rewrites existing files. Also snapshots signal_feature_log.jsonl
to data/whales/archive/<date>/ once per UTC day.

Spec: docs/superpowers/specs/2026-07-17-trade-grader-design.md
"""
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).parent
BOT_DIR = BASE / "data" / "bot"
WHALES_DIR = BASE / "data" / "whales"
TRADES_PATH = BOT_DIR / "bot_trades.jsonl"
GRADES_PATH = BOT_DIR / "bot_trade_grades.jsonl"
FEATURES_PATH = WHALES_DIR / "signal_feature_log.jsonl"
THESIS_PATH = WHALES_DIR / "daily_thesis.jsonl"
REGIME_PATH = WHALES_DIR / "intraday_regime.jsonl"
ARCHIVE_DIR = WHALES_DIR / "archive"

POLL_SEC = 60
SETTLE_WINDOW_S = 120   # a tick this close to expiry supports strike-basis settlement
GRADE_DELAY_S = 90      # wait this long past expiry so final ticks are on disk
PRICE_DECIDED_HI = 0.95
PRICE_DECIDED_LO = 0.05


def expiry_of(trade):
    return trade["entry_ts"] + trade["entry_sig"]["mins_left"] * 60.0


def verdict_for(exit_reason, side, settled):
    if settled == "unknown":
        return "ungraded"
    favorable = settled == side
    if exit_reason == "stop":
        return "whipsaw_stop" if favorable else "good_stop"
    if exit_reason == "target":
        return "clean_win" if favorable else "lucky_exit"
    return "left_money" if favorable else "good_exit"


def infer_settlement(ticks, expiry):
    before = [t for t in ticks if t.get("ts") is not None and t["ts"] <= expiry]
    if not before:
        return "unknown", "none"
    last = before[-1]
    spot, strike = last.get("spot"), last.get("floor_strike")
    if last["ts"] >= expiry - SETTLE_WINDOW_S and spot is not None and strike is not None:
        return ("YES" if spot >= strike else "NO"), "strike"
    price = last.get("price")
    if price is not None:
        if price > PRICE_DECIDED_HI:
            return "YES", "price"
        if price < PRICE_DECIDED_LO:
            return "NO", "price"
    return "unknown", "none"


def hold_path_stats(ticks, side, entry_price, entry_ts, exit_ts):
    prices = [t["price"] for t in ticks
              if t.get("price") is not None
              and t.get("ts") is not None and entry_ts <= t["ts"] <= exit_ts]
    if not prices:
        return None, None
    if side == "NO":
        prices = [1.0 - p for p in prices]
    mfe = round(max(prices) - entry_price, 4)
    mae = round(entry_price - min(prices), 4)
    return mfe, mae
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_trade_grader.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add trade_grader.py tests/test_trade_grader.py
git commit -m "feat(grader): core grading functions — verdicts, settlement inference, hold-path stats"
```

---

### Task 2: Day-context join and full grade-row assembly

**Files:**
- Modify: `trade_grader.py` (append functions)
- Test: `tests/test_trade_grader.py` (append tests)

**Interfaces:**
- Consumes: Task 1's `verdict_for`, `infer_settlement`, `hold_path_stats`, `expiry_of`.
- Produces: `day_context(entry_ts: float, thesis_rows: list[dict], regime_rows: list[dict]) -> dict` with keys `day_bias, day_key, day_conviction, regime, regime_lo, regime_hi`;
  `grade_trade(trade, ticks, thesis_rows, regime_rows) -> dict` returning the full spec schema row;
  `trade_key(row: dict) -> str`.
- Thesis rows look like `{"date": "2026-07-18", "bias": "WAIT", "conviction": 1, "level": "64000", ...}` (**level is a string**). Regime rows look like `{"ts": 1784333262.9, "regime": "range", "range_lo": 63400.0, "range_hi": 64100.0, ...}`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_trade_grader.py`)

```python
from trade_grader import day_context, grade_trade, trade_key

THESIS = [
    {"date": "2026-07-17", "bias": "UP", "conviction": 1, "level": "64000"},
    {"date": "2026-07-18", "bias": "WAIT", "conviction": 1, "level": "64000"},
]
# 2026-07-17 12:00:00 UTC
TS_JUL17 = 1784289600.0
REGIMES = [
    {"ts": TS_JUL17 - 3600, "regime": "range", "range_lo": 63000.0, "range_hi": 64000.0},
    {"ts": TS_JUL17 + 3600, "regime": "breakout_watch", "range_lo": 63400.0, "range_hi": 64100.0},
]


def test_day_context_joins_thesis_by_utc_date_and_latest_regime():
    ctx = day_context(TS_JUL17, THESIS, REGIMES)
    assert ctx["day_bias"] == "UP"
    assert ctx["day_key"] == 64000.0          # cast from string
    assert ctx["day_conviction"] == 1
    assert ctx["regime"] == "range"           # latest entry at/before entry_ts
    assert ctx["regime_lo"] == 63000.0


def test_day_context_missing_rows_is_safe():
    ctx = day_context(TS_JUL17, [], [])
    assert ctx["day_bias"] is None and ctx["regime"] == "none"


def make_trade(**kw):
    t = {"ticker": "T-1", "mode": "paper", "side": "YES", "qty": 10,
         "entry_price": 0.60, "exit_price": 0.30,
         "entry_ts": TS_JUL17, "exit_ts": TS_JUL17 + 300,
         "fees": 0.4, "net_pnl": -3.4, "exit_reason": "stop",
         "entry_sig": {"mins_left": 10.0}, "status": "closed"}
    t.update(kw)
    return t


def test_grade_trade_whipsaw_stop_full_row():
    trade = make_trade()
    exp = expiry_of(trade)  # TS_JUL17 + 600
    ticks = [tick(TS_JUL17 + 60, price=0.65), tick(TS_JUL17 + 200, price=0.28),
             tick(exp - 30, spot=64010.0, strike=63950.0, price=0.97)]
    row = grade_trade(trade, ticks, THESIS, REGIMES)
    assert row["verdict"] == "whipsaw_stop"
    assert row["settled"] == "YES" and row["settle_basis"] == "strike"
    assert row["held_pnl_gross"] == pytest.approx(4.0)    # 10*(1-0.60)
    assert row["delta_vs_held"] == pytest.approx(-7.0)    # 10*(0.30-0.60) - 4.0
    assert row["mfe"] == pytest.approx(0.05)
    assert row["mae"] == pytest.approx(0.32)
    assert row["day_bias"] == "UP" and row["aligned"] is True
    assert row["data_gap"] is False
    assert row["exit_reason"] == "stop" and row["net_pnl"] == -3.4
    assert trade_key(row) == trade_key(trade)


def test_grade_trade_unknown_settlement_flags_gap():
    trade = make_trade()
    row = grade_trade(trade, [], THESIS, REGIMES)
    assert row["verdict"] == "ungraded"
    assert row["settled"] == "unknown"
    assert row["held_pnl_gross"] is None and row["delta_vs_held"] is None
    assert row["data_gap"] is True


def test_aligned_null_when_bias_not_directional():
    trade = make_trade(entry_ts=TS_JUL17 + 86400.0,
                       exit_ts=TS_JUL17 + 86400.0 + 300)  # Jul 18 -> WAIT
    row = grade_trade(trade, [], THESIS, REGIMES)
    assert row["aligned"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_trade_grader.py -v`
Expected: new tests FAIL with `ImportError: cannot import name 'day_context'`

- [ ] **Step 3: Write the implementation** (append to `trade_grader.py`)

```python
def trade_key(row):
    return f"{row['ticker']}|{row['entry_ts']}"


def day_context(entry_ts, thesis_rows, regime_rows):
    date = time.strftime("%Y-%m-%d", time.gmtime(entry_ts))
    ctx = {"day_bias": None, "day_key": None, "day_conviction": None,
           "regime": "none", "regime_lo": None, "regime_hi": None}
    for row in thesis_rows:
        if row.get("date") == date:
            ctx["day_bias"] = row.get("bias")
            try:
                ctx["day_key"] = float(row.get("level"))
            except (TypeError, ValueError):
                ctx["day_key"] = None
            ctx["day_conviction"] = row.get("conviction")
    latest = None
    for row in regime_rows:
        ts = row.get("ts")
        if ts is not None and ts <= entry_ts and (latest is None or ts > latest["ts"]):
            latest = row
    if latest is not None:
        ctx["regime"] = latest.get("regime", "none")
        ctx["regime_lo"] = latest.get("range_lo")
        ctx["regime_hi"] = latest.get("range_hi")
    return ctx


def grade_trade(trade, ticks, thesis_rows, regime_rows):
    side, qty = trade["side"], trade["qty"]
    entry, exit_ = trade["entry_price"], trade["exit_price"]
    settled, basis = infer_settlement(ticks, expiry_of(trade))
    mfe, mae = hold_path_stats(ticks, side, entry,
                               trade["entry_ts"], trade["exit_ts"])
    if settled == "unknown":
        held = delta = None
    else:
        payout = 1.0 if settled == side else 0.0
        held = round(qty * (payout - entry), 2)
        delta = round(qty * (exit_ - entry) - held, 2)
    ctx = day_context(trade["entry_ts"], thesis_rows, regime_rows)
    if ctx["day_bias"] in ("UP", "DOWN"):
        aligned = (side == "YES") == (ctx["day_bias"] == "UP")
    else:
        aligned = None
    return {
        "ticker": trade["ticker"], "entry_ts": trade["entry_ts"],
        "exit_ts": trade["exit_ts"], "side": side, "qty": qty,
        "entry_price": entry, "exit_price": exit_,
        "net_pnl": trade.get("net_pnl"), "exit_reason": trade.get("exit_reason"),
        "settled": settled, "settle_basis": basis,
        "held_pnl_gross": held, "delta_vs_held": delta,
        "mfe": mfe, "mae": mae,
        "verdict": verdict_for(trade.get("exit_reason"), side, settled),
        **ctx, "aligned": aligned,
        "data_gap": settled == "unknown" or mfe is None,
        "graded_ts": time.time(),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_trade_grader.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add trade_grader.py tests/test_trade_grader.py
git commit -m "feat(grader): day-context join and full grade-row assembly"
```

---

### Task 3: Incremental feature-log reader (FeatureIndex)

**Files:**
- Modify: `trade_grader.py` (append class)
- Test: `tests/test_trade_grader.py` (append tests)

**Interfaces:**
- Consumes: nothing from earlier tasks (standalone).
- Produces: `class FeatureIndex` with `__init__(self, path: Path)`, `refresh(self) -> None` (reads only bytes appended since last call; full read on first call; resets on truncation), `ticks(self, ticker: str) -> list[dict]` (ticks sorted by arrival, each `{"ts","spot","floor_strike","price"}`). Tickers whose newest tick is older than `KEEP_S = 48*3600` are pruned during refresh.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_trade_grader.py`)

```python
import json as _json

from trade_grader import FeatureIndex


def _write_lines(path, rows, mode="a"):
    with open(path, mode) as f:
        for r in rows:
            f.write(_json.dumps(r) + "\n")


def test_feature_index_incremental_read(tmp_path):
    log = tmp_path / "feat.jsonl"
    _write_lines(log, [tick(100, ticker="A"), tick(110, ticker="B")], mode="w")
    idx = FeatureIndex(log)
    idx.refresh()
    assert len(idx.ticks("A")) == 1 and len(idx.ticks("B")) == 1
    _write_lines(log, [tick(120, ticker="A")])
    idx.refresh()
    assert len(idx.ticks("A")) == 2          # incremental append picked up
    assert idx.ticks("A")[-1]["ts"] == 120
    assert idx.ticks("MISSING") == []


def test_feature_index_skips_bad_lines_and_handles_truncation(tmp_path):
    log = tmp_path / "feat.jsonl"
    _write_lines(log, [tick(100, ticker="A")], mode="w")
    with open(log, "a") as f:
        f.write("not json\n")
    idx = FeatureIndex(log)
    idx.refresh()
    assert len(idx.ticks("A")) == 1
    # fresh-start style truncation: file replaced with smaller content
    _write_lines(log, [tick(200, ticker="C")], mode="w")
    idx.refresh()
    assert idx.ticks("A") == [] and len(idx.ticks("C")) == 1


def test_feature_index_prunes_stale_tickers(tmp_path):
    log = tmp_path / "feat.jsonl"
    now = 1784333262.0
    _write_lines(log, [tick(now - 60 * 3600, ticker="OLD"),
                       tick(now - 60, ticker="NEW")], mode="w")
    idx = FeatureIndex(log)
    idx.refresh(now=now)
    assert idx.ticks("OLD") == [] and len(idx.ticks("NEW")) == 1


def test_feature_index_missing_file_is_safe(tmp_path):
    idx = FeatureIndex(tmp_path / "nope.jsonl")
    idx.refresh()
    assert idx.ticks("A") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_trade_grader.py -v`
Expected: new tests FAIL with `ImportError: cannot import name 'FeatureIndex'`

- [ ] **Step 3: Write the implementation** (append to `trade_grader.py`)

```python
KEEP_S = 48 * 3600


class FeatureIndex:
    """Incremental per-ticker view of signal_feature_log.jsonl.

    Full read on first refresh (backfill needs history); afterwards reads
    only newly appended bytes. If the file shrinks (fresh-start reset),
    starts over from byte 0.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.pos = 0
        self.by_ticker = {}

    def refresh(self, now=None):
        now = time.time() if now is None else now
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size < self.pos:
            self.pos = 0
            self.by_ticker = {}
        if size == self.pos:
            self._prune(now)
            return
        with open(self.path) as f:
            f.seek(self.pos)
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                ticker = row.get("ticker")
                if not ticker:
                    continue
                self.by_ticker.setdefault(ticker, []).append(
                    {"ts": row.get("ts"), "spot": row.get("spot"),
                     "floor_strike": row.get("floor_strike"),
                     "price": row.get("price")})
            self.pos = f.tell()
        self._prune(now)

    def _prune(self, now):
        for ticker in list(self.by_ticker):
            ticks = self.by_ticker[ticker]
            newest = ticks[-1]["ts"] if ticks and ticks[-1]["ts"] else None
            if newest is None or newest < now - KEEP_S:
                del self.by_ticker[ticker]

    def ticks(self, ticker):
        return self.by_ticker.get(ticker, [])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_trade_grader.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add trade_grader.py tests/test_trade_grader.py
git commit -m "feat(grader): incremental feature-log reader with truncation reset and stale-ticker pruning"
```

---

### Task 4: Archival, cycle runner, and daemon loop

**Files:**
- Modify: `trade_grader.py` (append functions + `__main__`)
- Test: `tests/test_trade_grader.py` (append tests)

**Interfaces:**
- Consumes: everything above — `grade_trade`, `trade_key`, `expiry_of`, `FeatureIndex`, `GRADE_DELAY_S`.
- Produces: `read_jsonl(path) -> list[dict]`; `append_jsonl(path, row)`; `archive_features(now: float) -> bool` (True if a snapshot was written); `run_cycle(idx: FeatureIndex, now: float | None = None) -> int` (number graded). CLI: `--once` runs one cycle and exits; default loops every `POLL_SEC`.
- Tests monkeypatch the module path constants (`trade_grader.TRADES_PATH` etc.) to `tmp_path` files.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_trade_grader.py`)

```python
import gzip
import time as _time

import trade_grader as tg


def test_archive_features_writes_once_per_day(tmp_path, monkeypatch):
    feat = tmp_path / "signal_feature_log.jsonl"
    _write_lines(feat, [tick(100)], mode="w")
    monkeypatch.setattr(tg, "FEATURES_PATH", feat)
    monkeypatch.setattr(tg, "ARCHIVE_DIR", tmp_path / "archive")
    now = 1784333262.0  # 2026-07-18 UTC
    assert tg.archive_features(now) is True
    day_dir = tmp_path / "archive" / "2026-07-18"
    gz = day_dir / "signal_feature_log.jsonl.gz"
    assert gz.exists()
    with gzip.open(gz, "rt") as f:
        assert "floor_strike" in f.read()
    assert tg.archive_features(now) is False   # second call same day: skip


def test_run_cycle_grades_settled_trades_and_dedups(tmp_path, monkeypatch):
    trades = tmp_path / "bot_trades.jsonl"
    grades = tmp_path / "bot_trade_grades.jsonl"
    feat = tmp_path / "signal_feature_log.jsonl"
    thesis = tmp_path / "daily_thesis.jsonl"
    regime = tmp_path / "intraday_regime.jsonl"
    for name, path in [("TRADES_PATH", trades), ("GRADES_PATH", grades),
                       ("FEATURES_PATH", feat), ("THESIS_PATH", thesis),
                       ("REGIME_PATH", regime),
                       ("ARCHIVE_DIR", tmp_path / "archive")]:
        monkeypatch.setattr(tg, name, path)

    trade = make_trade()                       # expiry = TS_JUL17 + 600
    exp = expiry_of(trade)
    _write_lines(trades, [trade,
                          make_trade(status="open", ticker="T-OPEN"),
                          make_trade(ticker="T-FUTURE",
                                     entry_sig={"mins_left": 9999.0})], mode="w")
    _write_lines(feat, [tick(exp - 30, spot=64010.0, strike=63950.0, price=0.97)],
                 mode="w")
    _write_lines(thesis, [{"date": "2026-07-17", "bias": "UP",
                           "conviction": 1, "level": "64000"}], mode="w")
    _write_lines(regime, [], mode="w")

    idx = tg.FeatureIndex(feat)
    now = exp + tg.GRADE_DELAY_S + 1
    assert tg.run_cycle(idx, now=now) == 1     # only the settled closed trade
    rows = tg.read_jsonl(grades)
    assert len(rows) == 1 and rows[0]["verdict"] == "whipsaw_stop"
    assert tg.run_cycle(idx, now=now) == 0     # dedup: nothing regraded
    assert len(tg.read_jsonl(grades)) == 1


def test_run_cycle_survives_corrupt_trade_row(tmp_path, monkeypatch):
    trades = tmp_path / "bot_trades.jsonl"
    grades = tmp_path / "bot_trade_grades.jsonl"
    feat = tmp_path / "signal_feature_log.jsonl"
    for name, path in [("TRADES_PATH", trades), ("GRADES_PATH", grades),
                       ("FEATURES_PATH", feat),
                       ("THESIS_PATH", tmp_path / "t.jsonl"),
                       ("REGIME_PATH", tmp_path / "r.jsonl"),
                       ("ARCHIVE_DIR", tmp_path / "archive")]:
        monkeypatch.setattr(tg, name, path)
    good = make_trade()
    bad = {"status": "closed", "ticker": "T-BAD"}   # missing everything else
    _write_lines(trades, [bad, good], mode="w")
    _write_lines(feat, [tick(expiry_of(good) - 30, spot=64010.0,
                             strike=63950.0)], mode="w")
    idx = tg.FeatureIndex(feat)
    now = expiry_of(good) + tg.GRADE_DELAY_S + 1
    assert tg.run_cycle(idx, now=now) == 1          # bad row skipped, good graded
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_trade_grader.py -v`
Expected: new tests FAIL with `AttributeError` / `ImportError` on `archive_features` / `run_cycle`

- [ ] **Step 3: Write the implementation** (append to `trade_grader.py`)

```python
import gzip


def read_jsonl(path):
    rows = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def append_jsonl(path, row):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(row) + "\n")


def archive_features(now):
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    out = Path(ARCHIVE_DIR) / day / "signal_feature_log.jsonl.gz"
    if out.exists() or not Path(FEATURES_PATH).exists():
        return False
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".gz.tmp")
    with open(FEATURES_PATH, "rb") as src, gzip.open(tmp, "wb") as dst:
        while chunk := src.read(1 << 20):
            dst.write(chunk)
    tmp.rename(out)
    return True


def run_cycle(idx, now=None):
    now = time.time() if now is None else now
    try:
        archive_features(now)
    except OSError as e:
        print(f"archive error: {e}", file=sys.stderr)
    graded = {trade_key(g) for g in read_jsonl(GRADES_PATH)}
    pending = []
    for t in read_jsonl(TRADES_PATH):
        if t.get("status") != "closed":
            continue
        try:
            if trade_key(t) in graded or now < expiry_of(t) + GRADE_DELAY_S:
                continue
        except (KeyError, TypeError):
            continue
        pending.append(t)
    if not pending:
        return 0
    idx.refresh(now=now)
    thesis = read_jsonl(THESIS_PATH)
    regime = read_jsonl(REGIME_PATH)
    n = 0
    for t in pending:
        try:
            row = grade_trade(t, idx.ticks(t["ticker"]), thesis, regime)
            append_jsonl(GRADES_PATH, row)
            n += 1
        except Exception as e:
            print(f"grade error {t.get('ticker')}: {e}", file=sys.stderr)
    return n


def main():
    once = "--once" in sys.argv
    idx = FeatureIndex(FEATURES_PATH)
    while True:
        try:
            n = run_cycle(idx)
            if n:
                print(f"graded {n} trade(s)", flush=True)
        except Exception as e:
            print(f"cycle error: {e}", file=sys.stderr)
        if once:
            break
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
```

Note: move the `import gzip` to the top of the file with the other imports rather than mid-file.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_trade_grader.py -v`
Expected: all PASS

- [ ] **Step 5: Run the whole suite to check nothing else broke**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all PASS (existing bot tests unaffected)

- [ ] **Step 6: Commit**

```bash
git add trade_grader.py tests/test_trade_grader.py
git commit -m "feat(grader): archival snapshot, cycle runner, daemon loop with --once"
```

---

### Task 5: start.sh wiring + real-data backfill validation

**Files:**
- Modify: `start.sh:60` (add one `start_bg` line after `bot_tuner`)
- No new tests (validation is a live run).

**Interfaces:**
- Consumes: `trade_grader.py --once` CLI from Task 4.

- [ ] **Step 1: Wire the daemon into start.sh**

In `start.sh`, after the `start_bg bot_tuner ...` line, add:

```bash
start_bg trade_grader  "trade_grader.py"  "$PY" -u trade_grader.py
```

- [ ] **Step 2: Backfill run over real data**

Run: `.venv/bin/python trade_grader.py --once`
Expected: prints `graded N trade(s)` where N ≈ the number of settled closed trades in `data/bot/bot_trades.jsonl`; stderr may list rows with data gaps.

- [ ] **Step 3: Sanity-check the verdicts against known trades**

Run:
```bash
.venv/bin/python - <<'EOF'
import json, collections
rows = [json.loads(l) for l in open("data/bot/bot_trade_grades.jsonl")]
print(len(rows), "graded")
print(collections.Counter(r["verdict"] for r in rows))
for r in rows:
    if r["exit_reason"] == "stop":
        print(r["ticker"], r["side"], r["net_pnl"], "->", r["verdict"],
              "delta_vs_held:", r["delta_vs_held"], "gap:", r["data_gap"])
EOF
```
Expected: every settled trade has a verdict; the known stop losses show either `good_stop` or `whipsaw_stop` with a numeric `delta_vs_held`. Manually eyeball at least one against the feature log before calling it done.

- [ ] **Step 4: Start the daemon**

Run: `nohup .venv/bin/python -u trade_grader.py > /tmp/trade_grader.log 2>&1 &` then `pgrep -f trade_grader.py`
Expected: one pid; log quiet after backfill.

- [ ] **Step 5: Commit**

```bash
git add start.sh
git commit -m "feat(grader): launch trade_grader with the collector fleet"
```
