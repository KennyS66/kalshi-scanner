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
    idx.refresh(now=150.0)
    assert len(idx.ticks("A")) == 1 and len(idx.ticks("B")) == 1
    _write_lines(log, [tick(120, ticker="A")])
    idx.refresh(now=150.0)
    assert len(idx.ticks("A")) == 2          # incremental append picked up
    assert idx.ticks("A")[-1]["ts"] == 120
    assert idx.ticks("MISSING") == []


def test_feature_index_skips_bad_lines_and_handles_truncation(tmp_path):
    log = tmp_path / "feat.jsonl"
    _write_lines(log, [tick(100, ticker="A")], mode="w")
    with open(log, "a") as f:
        f.write("not json\n")
    idx = FeatureIndex(log)
    idx.refresh(now=150.0)
    assert len(idx.ticks("A")) == 1
    # fresh-start style truncation: file replaced with smaller content
    _write_lines(log, [tick(200, ticker="C")], mode="w")
    idx.refresh(now=250.0)
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


import gzip

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


def test_run_cycle_backfills_trades_older_than_prune_window(tmp_path, monkeypatch):
    """Regression: pending trades must protect their ticks from KEEP_S pruning."""
    trades = tmp_path / "bot_trades.jsonl"
    grades = tmp_path / "bot_trade_grades.jsonl"
    feat = tmp_path / "signal_feature_log.jsonl"
    for name, path in [("TRADES_PATH", trades), ("GRADES_PATH", grades),
                       ("FEATURES_PATH", feat),
                       ("THESIS_PATH", tmp_path / "t.jsonl"),
                       ("REGIME_PATH", tmp_path / "r.jsonl"),
                       ("ARCHIVE_DIR", tmp_path / "archive")]:
        monkeypatch.setattr(tg, name, path)
    old = make_trade()                       # entry TS_JUL17, expiry +600
    _write_lines(trades, [old], mode="w")
    _write_lines(feat, [tick(TS_JUL17 + 100, price=0.55),
                        tick(expiry_of(old) - 30, spot=64010.0,
                             strike=63950.0, price=0.97)], mode="w")
    idx = tg.FeatureIndex(feat)
    now = TS_JUL17 + 10 * 86400              # 10 days later, far past KEEP_S
    assert tg.run_cycle(idx, now=now) == 1
    rows = tg.read_jsonl(grades)
    assert rows[0]["settled"] == "YES" and rows[0]["data_gap"] is False


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
