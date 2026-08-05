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
