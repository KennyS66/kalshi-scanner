import json

from bot_tuner import sweep, window_rows


def _mklog(path, n_markets=30, base_ts=1_700_000_000.0):
    """Synthetic feature log: per market a seed row, a flip row, and an
    exit-window row so replay produces one YES round trip per market."""
    rows = []
    for i in range(n_markets):
        t0 = base_ts + i * 900
        tick = f"KXBTC15M-TEST{i:04d}-00"
        common = {"status": "ok", "ticker": tick, "price": 0.50,
                  "yes_ask": 0.50, "no_ask": 0.50, "spread": 0.01,
                  "yes_pct": 60.0}
        rows.append({**common, "ts": t0, "mins_left": 12.0,
                     "whale_trend": -3.0, "momentum": -30.0})
        rows.append({**common, "ts": t0 + 120, "mins_left": 10.0,
                     "whale_trend": 3.0, "momentum": 30.0})
        rows.append({**common, "ts": t0 + 700, "mins_left": 1.5,
                     "yes_ask": 0.56, "no_ask": 0.44,
                     "whale_trend": 3.0, "momentum": 30.0})
    path.write_text("\n".join(json.dumps(r) for r in rows))
    return rows


def test_window_rows_splits_by_time(tmp_path):
    log = tmp_path / "log.jsonl"
    _mklog(log, n_markets=10)
    train, val = window_rows(log, days=30)
    assert train and val
    assert train[-1]["ts"] < val[0]["ts"]


def test_sweep_writes_report_with_evidence(tmp_path):
    log = tmp_path / "log.jsonl"
    _mklog(log, n_markets=40)
    bot_dir = tmp_path / "bot"
    bot_dir.mkdir()
    report_path = tmp_path / "tuner_report.json"
    grid = {"flip_threshold": [2.0, 99.0]}   # 99 = never flips -> 0 trades
    rep = sweep(30, grid, log_path=log, bot_dir=bot_dir,
                report_path=report_path, sweep_dir=tmp_path / "sweep",
                offsets_file=tmp_path / "no_offsets.json")
    assert report_path.exists()
    on_disk = json.loads(report_path.read_text())
    assert on_disk["day"] == rep["day"]
    by_thresh = {e["params"]["flip_threshold"]: e for e in rep["results"]}
    assert by_thresh[99.0]["train"]["trades"] == 0
    assert by_thresh[2.0]["train"]["trades"] > 0
    assert rep["current"]["validate"]["trades"] >= 0
    assert "note" in rep
    assert not (tmp_path / "sweep").exists()   # sweep dir cleaned up
