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
    # entry_ts=0 (trade i=0) is falsy -> session_tag returns "unknown" and is
    # dropped from the per-session breakdown, same edge case pool_by_date_stats
    # already has; i=1,2,3 all land in weekday_night (epoch hour 0 < CURFEW_END_HOUR)
    # with net_pnls 0.5, -0.4, 0.5 -> n=3, net_avg=0.2, ok=False (n<100).
    # Per-session only (bot_core.session_gate_stats) -- no weekday/weekend
    # combined total anymore, since that's not what the actual live-unlock
    # gate (bot_broker.live_unlock_ok) requires.
    assert p["gate"]["sessions"] == {
        "weekday_day":   {"n": 0, "net_avg": 0.0, "ok": False},
        "weekday_night": {"n": 3, "net_avg": 0.2, "ok": False},
        "weekend_day":   {"n": 0, "net_avg": 0.0, "ok": False},
        "weekend_night": {"n": 0, "net_avg": 0.0, "ok": False},
    }


def test_status_payload_by_session(tmp_path):
    _seed(tmp_path)   # exits at epoch ~1s: Thu (wd) 00Z -> wd|asia
    p = bot_status_payload(tmp_path)
    assert list(p["stats"]["by_session"]) == ["wd|asia"]
    assert p["stats"]["by_session"]["wd|asia"]["n"] == 4


def test_status_payload_includes_pool_by_date(tmp_path):
    trades = [{"status": "closed", "net_pnl": 0.2, "entry_ts": 1.0, "exit_ts": 100.0,
              "entry_sig": {"ts": 1.0}}]
    (tmp_path / "bot_trades.jsonl").write_text(
        "\n".join(json.dumps(t) for t in trades))
    p = bot_status_payload(tmp_path)
    assert "1970-01-01" in p["pool_by_date"]
    assert p["pool_by_date"]["1970-01-01"]["weekday_night"] == 0.2


def test_candles_from_log_buckets_ohlc(tmp_path):
    from web import candles_from_log
    log = tmp_path / "feat.jsonl"
    rows = []
    # two 15m buckets starting at t=900000 (aligned); spot path 100,105,95,102 | 103,110
    for ts, spot in [(900010, 100.0), (900300, 105.0), (900600, 95.0),
                     (900890, 102.0), (900910, 103.0), (901200, 110.0)]:
        rows.append(json.dumps({"ts": ts, "spot": spot}))
    rows.append("not json")
    rows.append(json.dumps({"ts": 900950, "spot": None}))   # null spot skipped
    log.write_text("\n".join(rows))
    out = candles_from_log(log, mins=15, hours=2, now=901800)
    assert [c["t"] for c in out] == [900000, 900900]
    assert out[0] == {"t": 900000, "o": 100.0, "h": 105.0, "l": 95.0, "c": 102.0}
    assert out[1]["o"] == 103.0 and out[1]["c"] == 110.0


def test_candles_from_log_missing_file(tmp_path):
    from web import candles_from_log
    assert candles_from_log(tmp_path / "nope.jsonl", 15, 8, now=1000) == []


def test_status_payload_unlock_reflects_live_capability(tmp_path, monkeypatch):
    _seed(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps(
        {"live_sessions_requested": ["weekday_night"]}))
    monkeypatch.setenv("BOT_LIVE", "1")
    p = bot_status_payload(tmp_path)
    assert p["unlock"]["ok"] is True

    (tmp_path / "config.json").write_text(json.dumps({"live_sessions_requested": []}))
    p2 = bot_status_payload(tmp_path)
    assert p2["unlock"]["ok"] is False


# ── per-session live toggle endpoint (2026-07-29 design) ─────────────

def test_bot_live_session_write_enable_requires_passing_gate(tmp_path):
    from web import bot_live_session_write
    # 50 trades, net avg positive -- fails the 100-trade floor
    trades = [{"status": "closed", "net_pnl": 0.1, "entry_ts": 1784592000.0,
              "entry_sig": {"ts": 1784592000.0}} for _ in range(50)]
    (tmp_path / "config.json").write_text(json.dumps({"live_sessions_requested": []}))
    result = bot_live_session_write(tmp_path, "weekday_night", "enable", trades)
    assert result["ok"] is False
    cfg = json.loads((tmp_path / "config.json").read_text())
    assert "weekday_night" not in cfg.get("live_sessions_requested", [])


def test_bot_live_session_write_enable_succeeds_when_gate_passes(tmp_path):
    from web import bot_live_session_write
    trades = [{"status": "closed", "net_pnl": 0.1, "entry_ts": 1784592000.0,
              "entry_sig": {"ts": 1784592000.0}} for _ in range(100)]
    (tmp_path / "config.json").write_text(json.dumps({"live_sessions_requested": []}))
    result = bot_live_session_write(tmp_path, "weekday_night", "enable", trades)
    assert result["ok"] is True
    cfg = json.loads((tmp_path / "config.json").read_text())
    assert cfg["live_sessions_requested"] == ["weekday_night"]


def test_bot_live_session_write_enable_is_idempotent(tmp_path):
    from web import bot_live_session_write
    trades = [{"status": "closed", "net_pnl": 0.1, "entry_ts": 1784592000.0,
              "entry_sig": {"ts": 1784592000.0}} for _ in range(100)]
    (tmp_path / "config.json").write_text(json.dumps(
        {"live_sessions_requested": ["weekday_night"]}))
    result = bot_live_session_write(tmp_path, "weekday_night", "enable", trades)
    assert result["ok"] is True
    cfg = json.loads((tmp_path / "config.json").read_text())
    assert cfg["live_sessions_requested"] == ["weekday_night"]   # no duplicate


def test_bot_live_session_write_disable_never_needs_gate(tmp_path):
    from web import bot_live_session_write
    (tmp_path / "config.json").write_text(json.dumps(
        {"live_sessions_requested": ["weekday_night", "weekend_day"]}))
    result = bot_live_session_write(tmp_path, "weekday_night", "disable", trades=[])
    assert result["ok"] is True
    cfg = json.loads((tmp_path / "config.json").read_text())
    assert cfg["live_sessions_requested"] == ["weekend_day"]


def test_bot_live_session_write_rejects_unknown_session(tmp_path):
    from web import bot_live_session_write
    (tmp_path / "config.json").write_text(json.dumps({"live_sessions_requested": []}))
    result = bot_live_session_write(tmp_path, "not_a_real_session", "enable", trades=[])
    assert result["ok"] is False


def test_api_live_session_endpoint_enable_and_disable(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import web
    monkeypatch.setattr(web, "_BOT_DIR", tmp_path)
    trades = [{"status": "closed", "net_pnl": 0.1, "entry_ts": 1784592000.0,
              "entry_sig": {"ts": 1784592000.0}} for _ in range(100)]
    (tmp_path / "bot_trades.jsonl").write_text(
        "\n".join(json.dumps(t) for t in trades))
    (tmp_path / "config.json").write_text(json.dumps({"live_sessions_requested": []}))
    client = TestClient(web.app)
    r = client.post("/api/bot/live_session", json={"session": "weekday_night", "action": "enable"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    cfg = json.loads((tmp_path / "config.json").read_text())
    assert cfg["live_sessions_requested"] == ["weekday_night"]
    r2 = client.post("/api/bot/live_session", json={"session": "weekday_night", "action": "disable"})
    assert r2.status_code == 200
    cfg2 = json.loads((tmp_path / "config.json").read_text())
    assert cfg2["live_sessions_requested"] == []
