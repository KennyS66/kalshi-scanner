import json
from web import bot_status_payload, bot_control_write, _read_jsonl_tail


def _seed(tmp_path):
    (tmp_path / "bot_state.json").write_text(json.dumps(
        {"mode": "paper", "paused": False, "halted": False, "day": "2026-07-15",
         "day_pnl": 1.5, "bankroll": 500.0, "bankroll_ts": 0.0,
         "open_plays": {}, "heartbeat": 123.0, "last_control_nonce": 0}))
    trades = [{"ticker": f"T{i}", "net_pnl": 0.5 if i % 2 else -0.4,
               "status": "closed", "exit_reason": "flip", "side": "YES",
               "qty": 5, "entry_price": 0.5, "exit_price": 0.55,
               "entry_ts": i, "exit_ts": i + 1, "fees": 0.1, "mode": "paper"}
              for i in range(4)]
    (tmp_path / "bot_trades.jsonl").write_text(
        "\n".join(json.dumps(t) for t in trades))
    (tmp_path / "bot_events.jsonl").write_text(
        json.dumps({"ts": 1, "ticker": "T0", "action": "skip",
                    "reason": "paused", "sig": None}))


def test_status_payload_aggregates(tmp_path):
    _seed(tmp_path)
    p = bot_status_payload(tmp_path)
    assert p["state"]["day_pnl"] == 1.5
    assert p["stats"]["all_time"]["n"] == 4
    assert p["stats"]["all_time"]["win_pct"] == 50.0
    assert len(p["trades"]) == 4 and len(p["events"]) == 1
    assert p["unlock"]["ok"] is False           # 4 trades < 100


def test_status_payload_empty_dir_is_safe(tmp_path):
    p = bot_status_payload(tmp_path)
    assert p["state"] == {} and p["stats"]["all_time"]["n"] == 0


def test_control_write_bumps_nonce(tmp_path):
    n1 = bot_control_write(tmp_path, "pause")
    n2 = bot_control_write(tmp_path, "resume")
    assert (n1, n2) == (1, 2)
    c = json.loads((tmp_path / "control.json").read_text())
    assert c == {"nonce": 2, "cmd": "resume"}


def test_control_write_rejects_unknown_cmd(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        bot_control_write(tmp_path, "fire_the_missiles")


def test_read_jsonl_tail_skips_torn_lines(tmp_path):
    p = tmp_path / "trades.jsonl"
    p.write_text('{"a": 1}\n{not valid json at all\n{"a": 2}\n')
    rows = _read_jsonl_tail(p, 50)
    assert rows == [{"a": 1}, {"a": 2}]


def test_api_bot_control_rejects_malformed_json():
    from fastapi.testclient import TestClient
    from web import app
    client = TestClient(app)
    r = client.post("/api/bot/control", content=b"{not json",
                     headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.json() == {"ok": False, "error": "bad json"}


def test_trade_stats_today_block_filters_by_utc_day():
    from web import _trade_stats
    import time as _time
    now = _time.time()
    trades = [
        {"status": "closed", "net_pnl": 1.0, "exit_ts": now, "exit_reason": "flip"},
        {"status": "closed", "net_pnl": -1.0, "exit_ts": now - 3 * 86400,
         "exit_reason": "flip"},
    ]
    stats = _trade_stats(trades)
    assert stats["today"]["n"] == 1
    assert stats["all_time"]["n"] == 2


def test_status_payload_includes_config(tmp_path):
    payload = bot_status_payload(tmp_path)
    assert payload["config"]["use_ranges"] is True
    assert payload["config"]["risk_pct"] == 0.02


def test_status_payload_includes_grades(tmp_path):
    _seed(tmp_path)
    grades = [
        {"ticker": "T0", "entry_ts": 0, "verdict": "good_stop",
         "exit_reason": "stop", "delta_vs_held": 3.1, "data_gap": False},
        {"ticker": "T1", "entry_ts": 1, "verdict": "lucky_exit",
         "exit_reason": "target", "delta_vs_held": 2.0, "data_gap": False},
        {"ticker": "T2", "entry_ts": 2, "verdict": "ungraded",
         "exit_reason": "stop", "delta_vs_held": None, "data_gap": True},
    ]
    (tmp_path / "bot_trade_grades.jsonl").write_text(
        "\n".join(json.dumps(g) for g in grades))
    p = bot_status_payload(tmp_path)
    assert len(p["grades"]) == 3
    s = p["grade_summary"]
    assert s["n"] == 3 and s["gaps"] == 1
    assert s["verdicts"] == {"good_stop": 1, "lucky_exit": 1, "ungraded": 1}
    assert s["exit_edge_usd"] == 5.1
    assert s["stops_saved_usd"] == 3.1


def test_status_payload_no_grades_file_is_safe(tmp_path):
    _seed(tmp_path)
    p = bot_status_payload(tmp_path)
    assert p["grades"] == [] and p["grade_summary"]["n"] == 0


def test_status_payload_by_day_and_gate_split(tmp_path):
    _seed(tmp_path)   # 4 trades, exit_ts 1..4 (1970-01-01, a Thursday)
    p = bot_status_payload(tmp_path)
    assert p["stats"]["by_day"] == {"1970-01-01": 0.2}
    assert p["gate"] == {"weekday": 4, "weekend": 0}


def test_status_payload_by_session(tmp_path):
    _seed(tmp_path)   # exits at epoch ~1s: Thu (wd) 00Z -> wd|asia
    p = bot_status_payload(tmp_path)
    assert list(p["stats"]["by_session"]) == ["wd|asia"]
    assert p["stats"]["by_session"]["wd|asia"]["n"] == 4
