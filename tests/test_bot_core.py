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


def test_flip_detector_subthreshold_sample_does_not_consume_the_flip():
    # +3 -> -1.5 (sub-threshold, must NOT silently confirm the sign flip) -> -5
    # (mom agreeing) should still fire NO against the ORIGINAL +3 confirmed sign.
    d = FlipDetector(flip_threshold=2.0)
    assert d.update("T1", 3.0, 30.0) is None
    assert d.update("T1", -1.5, -15.0) is None          # sub-threshold, no confirm
    assert d.update("T1", -5.0, -50.0) == "NO"


def test_flip_detector_subthreshold_then_back_to_same_side_fires_nothing():
    d = FlipDetector(flip_threshold=2.0)
    assert d.update("T1", 3.0, 30.0) is None
    assert d.update("T1", -1.5, -15.0) is None          # sub-threshold, no confirm
    assert d.update("T1", 2.5, 25.0) is None            # back to same (confirmed) side


def test_flip_detector_momentum_blocked_flip_does_not_consume():
    # +3 -> -2.5 with mom disagreeing (blocked) -> -2.6 with mom agreeing must
    # still fire NO against the ORIGINAL +3 confirmed sign.
    d = FlipDetector(flip_threshold=2.0)
    assert d.update("T1", 3.0, 30.0) is None
    assert d.update("T1", -2.5, 20.0) is None           # threshold ok, mom disagrees
    assert d.update("T1", -2.6, -26.0) == "NO"


from bot_core import size_contracts, entry_blockers, should_time_exit


def _sig(**over):
    base = {"status": "ok", "ticker": "T", "price": 0.50, "yes_ask": 0.52,
            "no_ask": 0.50, "spread": 0.02, "mins_left": 10.0,
            "whale_trend": 3.0, "momentum": 30.0, "ts": 1000.0}
    base.update(over)
    return base


def test_size_contracts_two_percent_of_bankroll():
    # $500 * 2% = $10 budget; cost/contract = 0.52 + fee(0.52)=0.02 -> 0.54
    assert size_contracts(500.0, 0.52, 0.02) == 18
    assert size_contracts(500.0, 0.52, 0.02) * 0.54 <= 10.0


def test_size_contracts_zero_when_budget_below_one_contract():
    assert size_contracts(10.0, 0.52, 0.02) == 0     # $0.20 budget < $0.54


def test_entry_blockers_clean_signal_is_empty():
    assert entry_blockers(_sig(), dict(DEFAULT_CONFIG), {}, False, False) == []


def test_entry_blockers_each_guard():
    cfg = dict(DEFAULT_CONFIG)
    assert "paused" in entry_blockers(_sig(), cfg, {}, False, True)
    assert "halted" in entry_blockers(_sig(), cfg, {}, True, False)
    assert any("decided" in b for b in entry_blockers(_sig(price=0.97), cfg, {}, False, False))
    assert any("decided" in b for b in entry_blockers(_sig(price=0.03), cfg, {}, False, False))
    assert any("mins_left" in b for b in entry_blockers(_sig(mins_left=3.0), cfg, {}, False, False))
    assert any("already_open" in b for b in entry_blockers(_sig(), cfg, {"T": {}}, False, False))
    full = {f"M{i}": {} for i in range(cfg["max_open_plays"])}
    assert any("max_open" in b for b in entry_blockers(_sig(), cfg, full, False, False))
    assert any("not_ok" in b for b in entry_blockers({"status": "between_markets"}, cfg, {}, False, False))


def test_entry_blockers_missing_quote_is_blocked():
    cfg = dict(DEFAULT_CONFIG)
    sig = _sig()
    del sig["yes_ask"]
    blockers = entry_blockers(sig, cfg, {}, False, False)
    assert any(b.startswith("no_quote") for b in blockers)
    sig2 = _sig()
    del sig2["no_ask"]
    blockers2 = entry_blockers(sig2, cfg, {}, False, False)
    assert any(b.startswith("no_quote") for b in blockers2)


def test_should_time_exit():
    cfg = dict(DEFAULT_CONFIG)
    assert should_time_exit(_sig(mins_left=1.9), cfg) is True
    assert should_time_exit(_sig(mins_left=2.5), cfg) is False


from bot_core import (compute_side_ranges, sell_price_c, should_target_exit,
                      load_offsets)


def test_compute_side_ranges_yes_zero_offsets():
    r = compute_side_ranges(0.50, 70.0, "YES", {})
    assert (r["buy_low"], r["buy_high"]) == (47.0, 52.0)
    assert r["sell_low"] == 62.0          # buy_high + 10
    assert r["sell_high"] == 70.0         # flow fair
    assert r["side"] == "YES"


def test_compute_side_ranges_no_side_and_offsets():
    # NO side: buy price is 1 - price; offsets pull the targets down.
    r = compute_side_ranges(0.30, 40.0, "NO", {"sell_low_offset_c": 8.0,
                                               "sell_high_offset_c": 8.0})
    assert (r["buy_low"], r["buy_high"]) == (67.0, 72.0)
    assert r["sell_low"] == 74.0          # max(72+2, 72+10-8)
    assert r["sell_high"] == 76.0         # max(74+2, 60-8=52) -> floor wins


def test_compute_side_ranges_clamps():
    r = compute_side_ranges(0.02, 99.0, "YES", {})
    assert r["buy_low"] == 1.0            # floor at 1c
    r2 = compute_side_ranges(0.97, 99.0, "YES", {})
    assert r2["buy_high"] == 95.0         # cap at 95c


def test_entry_blockers_range_gate():
    sig = {"status": "ok", "ticker": "M1", "price": 0.50, "yes_ask": 0.60,
           "no_ask": 0.42, "mins_left": 10.0}
    cfg = dict(DEFAULT_CONFIG)
    r = compute_side_ranges(0.50, 70.0, "YES", {})
    b = entry_blockers(sig, cfg, {}, False, False, r)
    assert any("above buy range" in x for x in b)
    sig2 = dict(sig, yes_ask=0.50)
    assert entry_blockers(sig2, cfg, {}, False, False, r) == []
    sig3 = dict(sig, yes_ask=0.40)
    b3 = entry_blockers(sig3, cfg, {}, False, False, r)
    assert any("below buy range" in x for x in b3)
    assert entry_blockers(sig, cfg, {}, False, False, None) == []  # gate off


def test_sell_price_and_target_exit():
    sig = {"yes_ask": 0.66, "no_ask": 0.36, "spread": 0.02}
    assert sell_price_c(sig, "YES") == 64.0
    assert sell_price_c({"yes_ask": None}, "YES") is None
    play = {"side": "YES", "ranges": {"sell_low": 62.0}}
    assert should_target_exit(play, sig) is True
    assert should_target_exit({"side": "YES", "ranges": None}, sig) is False
    assert should_target_exit(
        {"side": "YES", "ranges": {"sell_low": 65.0}}, sig) is False


def test_load_offsets_missing_and_malformed(tmp_path):
    assert load_offsets(tmp_path / "nope.json") == {}
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    assert load_offsets(p) == {}
    p2 = tmp_path / "ok.json"
    p2.write_text('{"yes": {"sell_low_offset_c": 2.5}, "no": {}}')
    assert load_offsets(p2)["yes"]["sell_low_offset_c"] == 2.5


from bot_core import (entry_bucket, bucket_stats, update_bucket_stats,
                      ev_gate_blocker)


def _trade(side="YES", pnl=1.0, ask=0.50, mins=8.0, mom=5.0):
    return {"status": "closed", "side": side, "net_pnl": pnl,
            "entry_sig": {"yes_ask": ask, "no_ask": round(1 - ask, 2),
                          "mins_left": mins, "momentum": mom}}


def test_entry_bucket_bands():
    assert entry_bucket("YES", {"yes_ask": 0.30, "mins_left": 5,
                                "momentum": 3.0}) == "YES|cheap|4-7m|weak"
    assert entry_bucket("YES", {"yes_ask": 0.50, "mins_left": 8,
                                "momentum": -2.0}) == "YES|mid|7-11m|against"
    assert entry_bucket("YES", {"yes_ask": 0.50, "mins_left": 8,
                                "momentum": 12.0}) == "YES|mid|7-11m|strong"
    assert entry_bucket("NO",  {"no_ask": 0.70, "mins_left": 12,
                                "momentum": -9.0}) == "NO|rich|11m+|strong"
    assert entry_bucket("NO",  {"no_ask": 0.70, "mins_left": 12,
                                "momentum": -4.0}) == "NO|rich|11m+|weak"
    assert entry_bucket("NO",  {"no_ask": 0.70, "mins_left": 12,
                                "momentum": 4.0}) == "NO|rich|11m+|against"


def test_entry_bucket_flow_neutral_and_missing_momentum_are_weak():
    # zero or absent momentum reads as weak conviction, not against
    assert entry_bucket("YES", {"yes_ask": 0.50, "mins_left": 8,
                                "momentum": 0.0}).endswith("|weak")
    assert entry_bucket("NO",  {"no_ask": 0.50, "mins_left": 8,
                                "momentum": 0.0}).endswith("|weak")
    assert entry_bucket("YES", {"yes_ask": 0.50, "mins_left": 8}).endswith("|weak")


def test_bucket_stats_and_incremental_update_agree():
    trades = [_trade(pnl=1.0), _trade(pnl=-0.5), _trade(side="NO", pnl=2.0),
              {"status": "open"}]                       # open rows ignored
    agg = bucket_stats(trades)
    inc = {}
    for t in trades[:3]:
        update_bucket_stats(inc, t["side"], t["entry_sig"], t["net_pnl"])
    assert agg == inc
    assert agg["YES|mid|7-11m|weak"] == {"n": 2, "wins": 1, "net": 0.5,
                                         "net_avg": 0.25, "win_pct": 50.0}


def test_ev_gate_blocks_only_proven_negative_buckets():
    cfg = dict(DEFAULT_CONFIG)
    sig = {"yes_ask": 0.50, "no_ask": 0.50, "mins_left": 8}
    losing = bucket_stats([_trade(pnl=-0.5) for _ in range(12)])
    assert "ev_gate" in ev_gate_blocker("YES", sig, losing, cfg)
    small = bucket_stats([_trade(pnl=-0.5) for _ in range(11)])
    assert ev_gate_blocker("YES", sig, small, cfg) is None      # under floor
    winning = bucket_stats([_trade(pnl=0.5) for _ in range(20)])
    assert ev_gate_blocker("YES", sig, winning, cfg) is None    # profitable
    other = {"yes_ask": 0.70, "no_ask": 0.30, "mins_left": 8}   # different bucket
    assert ev_gate_blocker("YES", other, losing, cfg) is None
    off = dict(cfg, ev_gate=False)
    assert ev_gate_blocker("YES", sig, losing, off) is None


from bot_core import trade_budget, loss_headroom, size_for_budget


def test_loss_headroom_consumed_by_losses_only():
    cfg = dict(DEFAULT_CONFIG)                     # cap 100
    assert loss_headroom(0.0, cfg) == 100.0
    assert loss_headroom(-40.0, cfg) == 60.0
    assert loss_headroom(+50.0, cfg) == 100.0      # profit never expands it
    assert loss_headroom(-120.0, cfg) == 0.0
    assert loss_headroom(-999.0, dict(cfg, max_loss_usd=0)) == float("inf")


def test_trade_budget_matches_remaining_headroom():
    cfg = dict(DEFAULT_CONFIG)   # risk_pct .02, cap 100, trade_risk_frac .10
    assert trade_budget(500.0, 0.0, cfg) == 10.0       # min(10, 10)
    assert trade_budget(500.0, -50.0, cfg) == 5.0      # headroom 50 -> $5
    assert trade_budget(500.0, -100.0, cfg) == 0.0     # cap hit -> no size
    assert trade_budget(500.0, -50.0, dict(cfg, max_loss_usd=0)) == 10.0


def test_size_for_budget():
    assert size_for_budget(10.0, 0.50) > 0
    assert size_for_budget(0.0, 0.50) == 0


from bot_core import weekend_curfew_blocker

def test_weekend_curfew_blocks_sat_sun_overnight():
    sun_03z = 1784430000.0   # Sun 03:00Z — curfew window
    sun_14z = 1784469600.0   # Sun 14:00Z — after 13Z
    fri_03z = 1784257200.0   # Fri 03:00Z — weekday overnight
    both_on = {"weekend_curfew": True, "overnight_curfew": True}
    we_only = {"weekend_curfew": True, "overnight_curfew": False}
    all_off = {"weekend_curfew": False, "overnight_curfew": False}
    assert "overnight" in weekend_curfew_blocker(sun_03z, both_on)
    assert "overnight" in weekend_curfew_blocker(fri_03z, both_on)   # nights too
    assert weekend_curfew_blocker(sun_14z, both_on) is None          # 13Z+ open
    assert "weekend" in weekend_curfew_blocker(sun_03z, we_only)
    assert weekend_curfew_blocker(fri_03z, we_only) is None
    assert weekend_curfew_blocker(sun_03z, all_off) is None
