import json
from bot_core import DEFAULT_CONFIG, load_config, FlipDetector


def test_default_config_keys():
    for k in ("flip_threshold", "min_entry_mins", "exit_mins", "risk_pct",
              "day_stop_pct", "max_open_plays", "decided_lo", "decided_hi",
              "poll_secs", "mode", "live_requested"):
        assert k in DEFAULT_CONFIG
    assert DEFAULT_CONFIG["mode"] == "paper"
    assert DEFAULT_CONFIG["live_requested"] is False


def test_load_config_merges_file_over_defaults(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"flip_threshold": 3.5}))
    cfg = load_config(p)
    assert cfg["flip_threshold"] == 3.5
    assert cfg["exit_mins"] == DEFAULT_CONFIG["exit_mins"]


def test_load_config_missing_or_corrupt_file_gives_defaults(tmp_path):
    assert load_config(tmp_path / "nope.json") == DEFAULT_CONFIG
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_config(bad) == DEFAULT_CONFIG


def test_flip_detector_fires_on_sign_flip_with_momentum_agreement():
    d = FlipDetector(flip_threshold=2.0)
    assert d.update("T1", -3.0, -10.0) is None          # first sample seeds, never fires
    assert d.update("T1", 2.5, 20.0) == "YES"           # - -> + flip, |2.5|>=2, mom agrees
    assert d.update("T1", 3.0, 25.0) is None            # same sign, no flip
    assert d.update("T1", -2.2, -5.0) == "NO"           # + -> - flip


def test_flip_detector_requires_threshold_and_momentum():
    d = FlipDetector(flip_threshold=2.0)
    d.update("T1", -3.0, -10.0)
    assert d.update("T1", 1.0, 20.0) is None            # |1.0| < threshold
    d2 = FlipDetector(flip_threshold=2.0)
    d2.update("T1", -3.0, -10.0)
    assert d2.update("T1", 2.5, -20.0) is None          # momentum disagrees


def test_flip_detector_zero_prev_never_flips():
    d = FlipDetector(flip_threshold=2.0)
    d.update("T1", 0.0, 0.0)
    assert d.update("T1", 2.5, 10.0) is None            # 0 has no sign; seeds instead


def test_flip_detector_tracks_tickers_independently_and_forgets():
    d = FlipDetector(flip_threshold=2.0)
    d.update("A", -3.0, -1.0)
    d.update("B", 3.0, 1.0)
    assert d.update("A", 2.5, 1.0) == "YES"
    assert d.update("B", -2.5, -1.0) == "NO"
    d.forget("A")
    assert d.update("A", -2.5, -1.0) is None            # reseeded after forget
