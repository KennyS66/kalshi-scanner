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
