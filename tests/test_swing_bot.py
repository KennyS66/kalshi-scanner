import json
import time
from swing_bot import (fresh_state, load_state, save_state, read_control,
                       roll_day_if_needed, append_jsonl)


def test_state_roundtrip_atomic(tmp_path):
    s = fresh_state()
    s["day_pnl"] = -3.21
    s["open_plays"]["T1"] = {"side": "YES", "qty": 5}
    save_state(tmp_path, s)
    assert load_state(tmp_path) == s
    assert not list(tmp_path.glob("*.tmp"))            # no temp litter


def test_load_state_missing_gives_fresh(tmp_path):
    s = load_state(tmp_path)
    assert s == fresh_state() | {"day": s["day"]}


def test_read_control_only_fires_on_new_nonce(tmp_path):
    (tmp_path / "control.json").write_text(json.dumps({"nonce": 5, "cmd": "pause"}))
    cmd, nonce = read_control(tmp_path, last_nonce=4)
    assert (cmd, nonce) == ("pause", 5)
    cmd, nonce = read_control(tmp_path, last_nonce=5)   # already handled
    assert cmd is None and nonce == 5
    cmd, nonce = read_control(tmp_path / "nope", last_nonce=0)  # missing file
    assert cmd is None


def test_roll_day_resets_pnl_and_halt():
    s = fresh_state()
    s.update({"day": "2020-01-01", "day_pnl": -50.0, "halted": True})
    assert roll_day_if_needed(s, time.time()) is True
    assert s["day_pnl"] == 0.0 and s["halted"] is False
    assert roll_day_if_needed(s, time.time()) is False  # same day now


def test_append_jsonl(tmp_path):
    p = tmp_path / "x.jsonl"
    append_jsonl(p, {"a": 1})
    append_jsonl(p, {"b": 2})
    rows = [json.loads(l) for l in p.read_text().splitlines()]
    assert rows == [{"a": 1}, {"b": 2}]


import bot_broker
from swing_bot import Bot, TRADES_FILE, EVENTS_FILE, STATE_FILE


def _sig(**over):
    base = {"status": "ok", "ticker": "M1", "price": 0.50, "yes_ask": 0.52,
            "no_ask": 0.50, "spread": 0.02, "mins_left": 10.0,
            "whale_trend": -3.0, "momentum": -30.0, "buy_pressure": -5000,
            "ts": 1000.0}
    base.update(over)
    return base


def _mkbot(tmp_path, sigs, monkeypatch, bankroll=500.0):
    monkeypatch.setattr(bot_broker, "_balance_dollars", lambda: bankroll)
    it = iter(sigs)
    return Bot(tmp_path, fetch_fn=lambda: next(it, None))


def _rows(tmp_path, name):
    import json
    p = tmp_path / name
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_full_round_trip_flip_entry_and_flip_exit(tmp_path, monkeypatch):
    sigs = [
        _sig(),                                              # seeds detector
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0),     # flip -> enter YES
        _sig(whale_trend=4.0, momentum=20.0, yes_ask=0.60, ts=1010.0),  # hold
        _sig(whale_trend=-3.0, momentum=-20.0, yes_ask=0.60, ts=1015.0),  # flip -> exit
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    trades = _rows(tmp_path, TRADES_FILE)
    assert len(trades) == 1
    t = trades[0]
    assert t["side"] == "YES" and t["status"] == "closed"
    assert t["entry_price"] == 0.52 and t["exit_price"] == 0.58  # 0.60 - spread
    assert t["exit_reason"] == "flip"
    assert bot.state["open_plays"] == {}
    assert bot.state["day_pnl"] == t["net_pnl"]


def test_time_exit_at_two_minutes(tmp_path, monkeypatch):
    sigs = [
        _sig(),
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0),     # enter YES
        _sig(whale_trend=3.5, momentum=30.0, mins_left=1.5, ts=1010.0),  # time exit
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    trades = _rows(tmp_path, TRADES_FILE)
    assert len(trades) == 1 and trades[0]["exit_reason"] == "time"


def test_rolled_market_exits_at_last_seen_price(tmp_path, monkeypatch):
    sigs = [
        _sig(),
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0),               # enter M1
        _sig(ticker="M2", whale_trend=-1.0, momentum=-1.0, ts=1900.0), # M1 gone
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    trades = _rows(tmp_path, TRADES_FILE)
    assert len(trades) == 1 and trades[0]["exit_reason"] == "rolled"


def test_day_stop_halts_entries(tmp_path, monkeypatch):
    sigs = [_sig(), _sig(whale_trend=3.0, momentum=30.0, ts=1005.0)]
    bot = _mkbot(tmp_path, sigs, monkeypatch, bankroll=500.0)
    bot.state["day_pnl"] = -51.0                       # beyond 10% of 500
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert bot.state["halted"] is True
    assert bot.state["open_plays"] == {}
    events = _rows(tmp_path, EVENTS_FILE)
    assert any(e["action"] == "halt" for e in events)


def test_pause_control_blocks_entry_and_flatten_closes(tmp_path, monkeypatch):
    import json
    sigs = [
        _sig(),
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0),   # enter
        _sig(whale_trend=3.5, momentum=30.0, ts=1010.0),   # hold (flatten arrives)
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    bot.tick(); bot.tick()
    assert len(bot.state["open_plays"]) == 1
    (tmp_path / "control.json").write_text(json.dumps({"nonce": 1, "cmd": "flatten"}))
    bot.tick()
    assert bot.state["open_plays"] == {}
    trades = _rows(tmp_path, TRADES_FILE)
    assert trades[-1]["exit_reason"] == "flatten"


def test_skip_events_logged_with_reasons(tmp_path, monkeypatch):
    sigs = [_sig(mins_left=3.0), _sig(mins_left=3.0, whale_trend=3.0, momentum=30.0, ts=1005.0)]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    events = _rows(tmp_path, EVENTS_FILE)
    skips = [e for e in events if e["action"] == "skip"]
    assert skips and any("mins_left" in e["reason"] for e in skips)


def test_time_exit_survives_askless_replay_row(tmp_path, monkeypatch):
    # Historical replay rows can be status="ok" with mins_left but without
    # yes_ask/no_ask. The time exit must still fire, falling back to the
    # play's last quoted prices (from entry) instead of raising a KeyError.
    sigs = [
        _sig(),
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0),     # enter YES @ 0.52
        _sig(whale_trend=3.5, momentum=30.0, mins_left=1.5, ts=1010.0,
             yes_ask=None, no_ask=None),                      # askless, time exit
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    trades = _rows(tmp_path, TRADES_FILE)
    assert len(trades) == 1
    t = trades[0]
    assert t["exit_reason"] == "time"
    assert t["exit_price"] == 0.50   # fell back to entry's last_sig: 0.52 - spread 0.02
    assert bot.state["open_plays"] == {}


def test_feed_down_event_after_three_failures(tmp_path, monkeypatch):
    bot = _mkbot(tmp_path, [None, None, None, None], monkeypatch)
    for _ in range(4):
        bot.tick(now_ts=1000.0)
    events = _rows(tmp_path, EVENTS_FILE)
    assert sum(1 for e in events if e["action"] == "feed_down") == 1  # fires once
