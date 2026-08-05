import json

from settle_bot import (SETTLE_DIR, DEFAULT_CONFIG, load_config, fresh_state,
                        load_state, save_state, append_jsonl)


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


def test_settle_dir_is_isolated_from_the_swing_bot_journal():
    """settle_bot must never write into data/bot/ -- that directory feeds
    bot_core.session_gate_stats, the gate that authorizes live trading for
    the OTHER strategy."""
    parts = SETTLE_DIR.parts
    assert parts[-2:] == ("data", "settle")
    assert "bot" not in parts[-1:]


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
