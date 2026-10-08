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


def test_bot_session_pause_write_pause_and_resume(tmp_path):
    from web import bot_session_pause_write
    (tmp_path / "config.json").write_text(json.dumps({"paused_sessions": []}))
    assert bot_session_pause_write(tmp_path, "weekday_night", "pause")["ok"] is True
    cfg = json.loads((tmp_path / "config.json").read_text())
    assert cfg["paused_sessions"] == ["weekday_night"]
    # idempotent -- no duplicate entry
    bot_session_pause_write(tmp_path, "weekday_night", "pause")
    cfg = json.loads((tmp_path / "config.json").read_text())
    assert cfg["paused_sessions"] == ["weekday_night"]
    assert bot_session_pause_write(tmp_path, "weekday_night", "resume")["ok"] is True
    cfg = json.loads((tmp_path / "config.json").read_text())
    assert cfg["paused_sessions"] == []


def test_bot_session_pause_write_rejects_unknown_session_and_action(tmp_path):
    from web import bot_session_pause_write
    (tmp_path / "config.json").write_text(json.dumps({"paused_sessions": []}))
    assert bot_session_pause_write(tmp_path, "not_a_session", "pause")["ok"] is False
    assert bot_session_pause_write(tmp_path, "weekday_night", "explode")["ok"] is False


def test_bot_session_pause_never_touches_live_sessions(tmp_path):
    """Pausing and going live are independent axes: pausing a live session
    must not quietly revoke its live status, or resuming would silently
    need re-confirmation it never got."""
    from web import bot_session_pause_write
    (tmp_path / "config.json").write_text(json.dumps(
        {"live_sessions_requested": ["weekday_night"], "paused_sessions": []}))
    bot_session_pause_write(tmp_path, "weekday_night", "pause")
    cfg = json.loads((tmp_path / "config.json").read_text())
    assert cfg["live_sessions_requested"] == ["weekday_night"]
    assert cfg["paused_sessions"] == ["weekday_night"]


def test_api_session_pause_endpoint(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import web
    monkeypatch.setattr(web, "_BOT_DIR", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"paused_sessions": []}))
    client = TestClient(web.app)
    r = client.post("/api/bot/session_pause",
                    json={"session": "weekend_day", "action": "pause"})
    assert r.status_code == 200 and r.json()["ok"] is True
    cfg = json.loads((tmp_path / "config.json").read_text())
    assert cfg["paused_sessions"] == ["weekend_day"]
    r2 = client.post("/api/bot/session_pause",
                     json={"session": "weekend_day", "action": "bogus"})
    assert r2.status_code == 400


# ── kill-switch: stop.sh CLI fallback + page injection ────────────────
#
# stop.sh exists so there is still a kill-switch when the web UI is down.
# It must write byte-compatible control.json to what bot_control_write
# produces, or the bot would ignore it.

import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


def _run_stop(bot_dir, *args):
    return subprocess.run([str(_REPO / "stop.sh"), *args],
                          env={"PATH": "/usr/bin:/bin", "BOT_DIR": str(bot_dir)},
                          capture_output=True, text=True, timeout=20)


def test_stop_sh_writes_same_shape_as_bot_control_write(tmp_path):
    py_dir, sh_dir = tmp_path / "py", tmp_path / "sh"
    py_dir.mkdir(); sh_dir.mkdir()
    bot_control_write(py_dir, "pause")
    r = _run_stop(sh_dir)
    assert r.returncode == 0, r.stderr
    assert json.loads((sh_dir / "control.json").read_text()) \
        == json.loads((py_dir / "control.json").read_text())


def test_stop_sh_resume_and_nonce_increments(tmp_path):
    assert _run_stop(tmp_path).returncode == 0
    assert json.loads((tmp_path / "control.json").read_text())["nonce"] == 1
    assert _run_stop(tmp_path, "resume").returncode == 0
    c = json.loads((tmp_path / "control.json").read_text())
    assert c == {"nonce": 2, "cmd": "resume"}


def test_stop_sh_rejects_unknown_command(tmp_path):
    r = _run_stop(tmp_path, "fire_the_missiles")
    assert r.returncode != 0
    assert not (tmp_path / "control.json").exists()


def test_stop_sh_leaves_no_temp_file(tmp_path):
    _run_stop(tmp_path)
    assert not list(tmp_path.glob("*.tmp"))


def test_kill_switch_is_injected_into_both_pages():
    """A missing kill-switch must fail loudly at import, not silently at
    2am: both pages must actually carry the component, not just the
    marker comments."""
    from bot_page import BOT_HTML
    from web import _TRADE_HTML
    for html in (BOT_HTML, _TRADE_HTML):
        assert "stopBtn" in html and "stopBanner" in html
        assert "/*STOP_CSS*/" not in html      # marker consumed
        assert "<!--STOP_BAR-->" not in html
        assert "//STOP_JS" not in html


def test_stop_sh_recovers_from_a_corrupt_control_file(tmp_path):
    """A truncated control.json used to reset the nonce to 1, which the bot
    silently ignores (read_control needs nonce > last_control_nonce) while
    the script reports success. Floor it against the consumed nonce."""
    (tmp_path / "control.json").write_text('{"nonce": 4')       # truncated
    (tmp_path / "bot_state.json").write_text(json.dumps({"last_control_nonce": 67}))
    assert _run_stop(tmp_path).returncode == 0
    assert json.loads((tmp_path / "control.json").read_text())["nonce"] == 68


# ── /api/bot/series: slim chart data (2026-08-02 charts design) ───────
#
# The status payload sends trades[-50:] because full rows would push a
# 5s-polled endpoint past half a megabyte. Charts need all 680 trades but
# only four columns, so they get their own endpoint.

from web import bot_series_payload


def _trade(ts, pnl, reason="target", entry_ts=None):
    return {"status": "closed", "exit_ts": ts, "net_pnl": pnl,
            "exit_reason": reason, "entry_ts": entry_ts if entry_ts else ts - 60,
            "ticker": "T1", "side": "YES", "qty": 3, "entry_price": 0.5,
            "exit_price": 0.6, "fees": 0.02, "mode": "paper",
            "entry_sig": {"a": 1}, "exit_sig": {"b": 2}}


def _write_trades(d, rows):
    (d / "bot_trades.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows))


def test_series_returns_every_closed_trade_not_just_fifty(tmp_path):
    _write_trades(tmp_path, [_trade(1000.0 + i, 0.1) for i in range(300)])
    out = bot_series_payload(tmp_path)
    assert len(out["trades"]) == 300, "charts must see the whole history"


def test_series_carries_only_chart_columns(tmp_path):
    _write_trades(tmp_path, [_trade(1000.0, 1.25, "stretch")])
    row = bot_series_payload(tmp_path)["trades"][0]
    assert set(row) == {"t", "p", "r", "s"}
    assert row["t"] == 1000.0 and row["p"] == 1.25 and row["r"] == "stretch"


def test_series_resolves_session_server_side(tmp_path):
    """Session is derived with bot_core.session_tag rather than
    reimplemented in JS -- sessions are the organizing concept of this bot
    and a second definition would eventually drift from the first."""
    wd_night = 1784592000.0            # Tue 00:00Z
    wd_day = wd_night + 14 * 3600      # Tue 14:00Z
    _write_trades(tmp_path, [_trade(wd_night + 60, 1.0, entry_ts=wd_night),
                             _trade(wd_day + 60, 1.0, entry_ts=wd_day)])
    got = [r["s"] for r in bot_series_payload(tmp_path)["trades"]]
    assert got == ["weekday_night", "weekday_day"]


def test_series_skips_unparseable_and_open_rows(tmp_path):
    p = tmp_path / "bot_trades.jsonl"
    p.write_text(json.dumps(_trade(1000.0, 1.0)) + "\n"
                 + "{not json\n"
                 + json.dumps({"status": "open", "ticker": "T2"}) + "\n"
                 + json.dumps(_trade(2000.0, 2.0)) + "\n")
    out = bot_series_payload(tmp_path)
    assert [r["p"] for r in out["trades"]] == [1.0, 2.0]


def test_series_is_ordered_by_exit_time(tmp_path):
    _write_trades(tmp_path, [_trade(3000.0, 1.0), _trade(1000.0, 2.0),
                             _trade(2000.0, 3.0)])
    assert [r["t"] for r in bot_series_payload(tmp_path)["trades"]] \
        == [1000.0, 2000.0, 3000.0]


def test_series_payload_stays_small(tmp_path):
    """The whole point of a separate endpoint: 680 full rows are ~391 KB,
    the same trades as chart columns are an order of magnitude smaller."""
    _write_trades(tmp_path, [_trade(1000.0 + i, 0.1) for i in range(680)])
    assert len(json.dumps(bot_series_payload(tmp_path))) < 80_000


def test_series_empty_when_no_trade_log(tmp_path):
    assert bot_series_payload(tmp_path) == {"trades": []}


def test_no_duplicate_js_declarations_on_the_bot_page():
    """chart_kit and stop_control are concatenated into bot_page's single
    <script> block, so a name either module shares with the page is a
    redeclaration SyntaxError that kills ALL the JS on the page -- charts,
    kill-switch and dashboard alike. This actually happened: chart_kit
    declared `const SESSIONS`, which bot_page.py already defines.
    """
    import re
    from collections import Counter
    from bot_page import BOT_HTML
    body = BOT_HTML.split("<script>")[-1]
    names = re.findall(r"^(?:const|let|function)\s+([A-Za-z_$][\w$]*)",
                       body, re.MULTILINE)
    dupes = {n: c for n, c in Counter(names).items() if c > 1}
    assert not dupes, f"duplicate top-level JS declarations: {dupes}"


def _node_bin():
    import shutil, os
    return (shutil.which("node")
            or next((p for p in [os.path.expanduser("~/.local/node/bin/node")]
                     if os.path.exists(p)), None))


def test_bot_page_script_block_parses():
    """The single strongest guard on this page: bot_page, chart_kit and
    stop_control are concatenated into ONE <script>, so a syntax error in
    any of them silently kills every bit of JS on /bot -- charts,
    kill-switch and dashboard together, with a page that still looks fine.
    Both known breakages (a `const SESSIONS` redeclaration, and a bad
    bracket from a careless rename) were caught only by running a parser.
    """
    import subprocess, tempfile, os
    node = _node_bin()
    if not node:
        import pytest
        pytest.skip("node not available")
    from bot_page import BOT_HTML
    body = BOT_HTML.split("<script>")[-1].split("</script>")[0]
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(body)
        path = f.name
    try:
        r = subprocess.run([node, "--check", path], capture_output=True, text=True)
        assert r.returncode == 0, f"/bot script block does not parse:\n{r.stderr[:800]}"
    finally:
        os.unlink(path)


def test_status_lite_is_state_and_config_only(tmp_path):
    # /trade and the STOP widget only read state (+config for the confirm
    # text); the full payload costs ~58ms and 169KB per poll.
    from web import bot_status_lite
    _seed(tmp_path)
    full, lite = bot_status_payload(tmp_path), bot_status_lite(tmp_path)
    assert set(lite) == {"state", "config"}
    assert lite["state"] == full["state"] and lite["config"] == full["config"]


def test_status_lite_empty_dir_is_safe(tmp_path):
    from web import bot_status_lite
    assert bot_status_lite(tmp_path)["state"] == {}


def test_api_bot_status_lite_query(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import web
    _seed(tmp_path)
    monkeypatch.setattr(web, "_BOT_DIR", tmp_path)
    client = TestClient(web.app)
    lite = client.get("/api/bot/status?lite=1").json()
    assert set(lite) == {"state", "config"} and lite["state"]["day_pnl"] == 1.5
    full = client.get("/api/bot/status").json()
    assert "grades" in full and full["state"]["day_pnl"] == 1.5


def test_status_pollers_use_lite():
    # The 5s STOP poller and /trade's 12s poller must not pull the full payload.
    import stop_control
    from web import _TRADE_HTML
    assert "/api/bot/status?lite=1" in stop_control.STOP_JS
    assert "fj('/api/bot/status?lite=1'" in _TRADE_HTML
    assert "fj('/api/bot/status'," not in _TRADE_HTML


def test_scorecard_endpoint_pending_then_cached(monkeypatch):
    from fastapi.testclient import TestClient
    import web
    client = TestClient(web.app)
    monkeypatch.setattr(web, "_scorecard_cache", {})
    assert client.get("/api/crypto/scorecard").json() == {"status": "pending"}
    monkeypatch.setattr(web, "_scorecard_cache", {"windows": {"7d": {"n": 3}}})
    assert client.get("/api/crypto/scorecard").json()["windows"]["7d"]["n"] == 3


def test_manual_summary_endpoint_reads_local_fills(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import fills_sync
    import web
    monkeypatch.setattr(fills_sync, "FILLS_FILE", tmp_path / "f.jsonl")
    monkeypatch.setattr(fills_sync, "SETTLE_FILE", tmp_path / "s.jsonl")
    monkeypatch.setattr(fills_sync, "BALANCE_FILE", tmp_path / "b.jsonl")
    (tmp_path / "f.jsonl").write_text(json.dumps(
        {"id": "1", "ticker": "T1", "side": "YES", "action": "buy", "qty": 1,
         "price": 0.6, "ts": __import__("time").time() - 60, "fee": 0.02,
         "taker": True, "source": "manual"}) + "\n")
    (tmp_path / "s.jsonl").write_text(json.dumps({"ticker": "T1", "result": "yes"}) + "\n")
    (tmp_path / "b.jsonl").write_text(json.dumps({"ts": 1, "balance": 5.82}) + "\n")
    p = TestClient(web.app).get("/api/manual/summary").json()
    assert p["windows"]["7d"]["markets"] == 1 and p["balance"] == 5.82
    assert p["synced_ts"] is not None


def test_trade_page_has_cost_scorecard_and_your_trades():
    from web import _TRADE_HTML
    for needle in ('id="cost-strip"', 'id="yt-body"', "function renderCost(",
                   "function renderScorecard(", "function renderManual(",
                   "fj('/api/crypto/scorecard',null)", "fj('/api/manual/summary',null)",
                   "· target +"):
        assert needle in _TRADE_HTML, needle
    assert "· edge +" not in _TRADE_HTML     # range arithmetic, not a measured edge


def test_status_payload_memo_skips_unchanged_files(tmp_path, monkeypatch):
    # bot_state.json is rewritten every ~5s (heartbeat) but trades/grades/
    # tuner rarely: re-parsing them each poll was ~55 of the ~58ms.
    import web
    _seed(tmp_path)
    reads = []
    real = web._read_jsonl_tail
    monkeypatch.setattr(web, "_read_jsonl_tail",
                        lambda p, n: reads.append(p.name) or real(p, n))
    first = web.bot_status_payload(tmp_path)
    n_first = len(reads)
    st = json.loads((tmp_path / "bot_state.json").read_text())
    st["heartbeat"] = 999.0                       # only state changes
    (tmp_path / "bot_state.json").write_text(json.dumps(st))
    second = web.bot_status_payload(tmp_path)
    assert len(reads) == n_first                  # nothing re-tailed
    assert second["state"]["heartbeat"] == 999.0  # state always fresh
    assert second["stats"] == first["stats"] and second["trades"] == first["trades"]


def test_status_payload_memo_invalidates_on_trade_append(tmp_path):
    import web
    _seed(tmp_path)
    assert web.bot_status_payload(tmp_path)["stats"]["all_time"]["n"] == 4
    with open(tmp_path / "bot_trades.jsonl", "a") as f:
        f.write("\n" + json.dumps({"ticker": "T9", "net_pnl": 1.0, "status": "closed",
                                   "exit_reason": "flip", "side": "YES", "qty": 1,
                                   "entry_price": .5, "exit_price": .6, "entry_ts": 9,
                                   "exit_ts": 10, "fees": 0.0, "mode": "paper"}))
    assert web.bot_status_payload(tmp_path)["stats"]["all_time"]["n"] == 5


def test_status_payload_memo_rolls_today_block_at_utc_midnight(tmp_path, monkeypatch):
    import web
    _seed(tmp_path)
    calls = []
    real = web._trade_stats
    monkeypatch.setattr(web, "_trade_stats", lambda t: calls.append(1) or real(t))
    monkeypatch.setattr(web, "_utc_day_str", lambda ts: "2026-10-08")
    web.bot_status_payload(tmp_path)
    web.bot_status_payload(tmp_path)
    assert len(calls) == 1                        # same day: memo hit
    monkeypatch.setattr(web, "_utc_day_str", lambda ts: "2026-10-09")
    web.bot_status_payload(tmp_path)
    assert len(calls) == 2                        # new UTC day: today recomputed
