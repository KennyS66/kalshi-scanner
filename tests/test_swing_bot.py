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
    # curfews off: fixture sig timestamps are epoch-small (hour 00Z) and these
    # tests exercise strategy mechanics, not the session-curfew overlay
    cfg_p = tmp_path / "config.json"
    if not cfg_p.exists():
        cfg_p.write_text(json.dumps({"overnight_curfew": False,
                                     "weekend_curfew": False}))
    else:
        cfg = json.loads(cfg_p.read_text())
        cfg.setdefault("overnight_curfew", False)
        cfg.setdefault("weekend_curfew", False)
        cfg_p.write_text(json.dumps(cfg))
    # offsets_file under tmp_path (absent -> zero offsets) so tests never
    # read the machine's live banner_offsets.json
    return Bot(tmp_path, fetch_fn=lambda: next(it, None),
               offsets_file=tmp_path / "banner_offsets.json",
               loop_log=tmp_path / "loop_log.jsonl")


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


def test_paper_bankroll_override_sizes_trades_and_skips_balance_fetch(tmp_path, monkeypatch):
    import json as _json
    calls = []
    monkeypatch.setattr(bot_broker, "_balance_dollars",
                        lambda: calls.append(1) or 123.0)
    (tmp_path / "config.json").write_text(_json.dumps(
        {"paper_bankroll": 400.0,
         "overnight_curfew": False, "weekend_curfew": False}))
    sigs = [_sig(), _sig(whale_trend=3.0, momentum=30.0, ts=1005.0)]
    it = iter(sigs)
    # now_ts far past bankroll_ts=0 so the hourly guard WOULD fetch the live
    # balance (123.0) if the paper_bankroll override didn't short-circuit it.
    bot = Bot(tmp_path, fetch_fn=lambda: next(it, None),
              offsets_file=tmp_path / "banner_offsets.json",
              loop_log=tmp_path / "loop_log.jsonl")
    bot.tick(now_ts=2_000_000_000.0)
    bot.tick(now_ts=2_000_000_005.0)
    assert calls == []                                   # no live balance fetch
    assert bot.state["bankroll"] == 400.0                # from config, not 123/500
    # $400 * 2% = $8 budget; yes_ask 0.52 + fee 0.02 = 0.54 -> 14 contracts
    assert bot.state["open_plays"]["M1"]["qty"] == 14


def test_target_exit_at_calibrated_sell_low(tmp_path, monkeypatch):
    # Zero offsets: buy range 47-52, sell_low = buy_high + 10 = 62.
    sigs = [
        _sig(),
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0),     # enter YES @ 0.52
        _sig(whale_trend=3.5, momentum=30.0, yes_ask=0.60, ts=1010.0),  # 58 < 62
        _sig(whale_trend=3.5, momentum=30.0, yes_ask=0.66, ts=1015.0),  # 64 >= 62
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    trades = _rows(tmp_path, TRADES_FILE)
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "target"
    assert trades[0]["exit_price"] == 0.64          # 0.66 - 0.02 spread
    assert bot.state["open_plays"] == {}


def test_offsets_file_tightens_target(tmp_path, monkeypatch):
    # sell_low_offset 8c from the graded history: sell_low = max(54, 52+10-8) = 54.
    # min_edge off: 54c target on a 52c ask is deliberately thin — this test
    # checks offset plumbing, and the thin-edge gate would (correctly) block it.
    import json as _json
    (tmp_path / "config.json").write_text(_json.dumps({"min_edge_c": None}))
    (tmp_path / "banner_offsets.json").write_text(_json.dumps(
        {"yes": {"sell_low_offset_c": 8.0}, "no": {}}))
    sigs = [
        _sig(),
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0),                # enter
        _sig(whale_trend=3.5, momentum=30.0, yes_ask=0.57, ts=1010.0),  # 55 >= 54
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    trades = _rows(tmp_path, TRADES_FILE)
    assert len(trades) == 1 and trades[0]["exit_reason"] == "target"
    assert trades[0]["entry_sig"] is not None


def test_flip_outside_buy_range_is_skipped(tmp_path, monkeypatch):
    # Ask 60c with price 0.50 -> buy range 47-52 -> chasing, skip.
    sigs = [
        _sig(),
        _sig(whale_trend=3.0, momentum=30.0, yes_ask=0.60, ts=1005.0),
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert bot.state["open_plays"] == {}
    events = _rows(tmp_path, EVENTS_FILE)
    skips = [e for e in events if e["action"] == "skip"]
    assert skips and any("above buy range" in e["reason"] for e in skips)


def test_use_ranges_false_restores_unGated_entry(tmp_path, monkeypatch):
    import json as _json
    (tmp_path / "config.json").write_text(_json.dumps({"use_ranges": False}))
    sigs = [
        _sig(),
        _sig(whale_trend=3.0, momentum=30.0, yes_ask=0.60, ts=1005.0),
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert "M1" in bot.state["open_plays"]
    assert bot.state["open_plays"]["M1"]["ranges"] is None


def _losing_trade_row(ask=0.52, mins=10.0, mom=30.0):
    # momentum matches _sig()'s strong band so the journal poisons the same
    # bucket the live entry lands in
    return {"status": "closed", "side": "YES", "net_pnl": -0.5,
            "ticker": "OLD", "qty": 1, "entry_price": ask, "exit_price": 0.4,
            "entry_ts": 1.0, "exit_ts": 2.0, "fees": 0.02, "exit_reason": "time",
            "mode": "paper",
            "entry_sig": {"yes_ask": ask, "no_ask": round(1 - ask, 2),
                          "mins_left": mins, "momentum": mom}}


def test_ev_gate_skips_poisoned_bucket_from_journal(tmp_path, monkeypatch):
    # 12 historical losers in YES|mid|7-11m -> boot-loaded gate blocks entry.
    (tmp_path / TRADES_FILE).write_text(
        "\n".join(json.dumps(_losing_trade_row()) for _ in range(12)))
    sigs = [_sig(), _sig(whale_trend=3.0, momentum=30.0, ts=1005.0)]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert bot.state["open_plays"] == {}
    events = _rows(tmp_path, EVENTS_FILE)
    assert any("ev_gate" in e["reason"] for e in events if e["action"] == "skip")


def test_ev_stats_update_on_live_exit(tmp_path, monkeypatch):
    sigs = [
        _sig(),
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0),                # enter
        _sig(whale_trend=-3.0, momentum=-20.0, yes_ask=0.60, ts=1010.0),  # exit
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    assert bot.ev_stats == {}
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert sum(v["n"] for v in bot.ev_stats.values()) == 1


def _mkbot_deadman(tmp_path, sigs, monkeypatch, hb_age_secs):
    import os
    monkeypatch.setattr(bot_broker, "_balance_dollars", lambda: 500.0)
    hb = tmp_path / "loop_log.jsonl"
    hb.write_text('{"hb": 1}\n')
    now = 2_000_000_000.0
    os.utime(hb, (now - hb_age_secs, now - hb_age_secs))
    it = iter(sigs)
    bot = Bot(tmp_path, fetch_fn=lambda: next(it, None),
              offsets_file=tmp_path / "banner_offsets.json", loop_log=hb)
    return bot, now


def test_deadman_pauses_on_stale_loop_heartbeat(tmp_path, monkeypatch):
    sigs = [_sig(), _sig(whale_trend=3.0, momentum=30.0, ts=1005.0)]
    bot, now = _mkbot_deadman(tmp_path, sigs, monkeypatch,
                              hb_age_secs=91 * 60)
    for _ in sigs:
        bot.tick(now_ts=now)
    assert bot.state["paused"] is True
    assert bot.state["paused_by"] == "deadman"
    assert bot.state["open_plays"] == {}          # entry blocked while paused
    events = _rows(tmp_path, EVENTS_FILE)
    assert any("deadman" in e["reason"] for e in events if e["action"] == "pause")


def test_deadman_releases_when_heartbeat_returns(tmp_path, monkeypatch):
    import os
    bot, now = _mkbot_deadman(tmp_path, [_sig()], monkeypatch,
                              hb_age_secs=91 * 60)
    bot.tick(now_ts=now)
    assert bot.state["paused_by"] == "deadman"
    os.utime(bot.loop_log, (now, now))            # heartbeat returns
    bot.tick(now_ts=now)
    assert bot.state["paused"] is False
    assert "paused_by" not in bot.state


def test_deadman_never_releases_manual_pause(tmp_path, monkeypatch):
    bot, now = _mkbot_deadman(tmp_path, [_sig(), _sig()], monkeypatch,
                              hb_age_secs=0)      # fresh heartbeat
    bot.state["paused"] = True                    # manual pause, no paused_by
    bot.tick(now_ts=now)
    assert bot.state["paused"] is True


def test_deadman_ignored_when_no_loop_log(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_broker, "_balance_dollars", lambda: 500.0)
    sigs = [_sig()]
    it = iter(sigs)
    bot = Bot(tmp_path, fetch_fn=lambda: next(it, None),
              offsets_file=tmp_path / "banner_offsets.json",
              loop_log=tmp_path / "never_existed.jsonl")
    bot.tick(now_ts=2_000_000_000.0)
    assert bot.state["paused"] is False


def test_boot_never_replays_preexisting_control(tmp_path, monkeypatch):
    # A control command written before boot must not fire on the new process.
    monkeypatch.setattr(bot_broker, "_balance_dollars", lambda: 500.0)
    (tmp_path / "control.json").write_text(json.dumps({"nonce": 9, "cmd": "resume"}))
    save_state(tmp_path, fresh_state() | {"paused": True,
                                          "last_control_nonce": 7})
    bot = Bot(tmp_path, fetch_fn=lambda: None,
              offsets_file=tmp_path / "banner_offsets.json",
              loop_log=tmp_path / "loop_log.jsonl")
    bot.tick(now_ts=1000.0)
    assert bot.state["paused"] is True            # stale resume NOT replayed
    assert bot.state["last_control_nonce"] == 9
    (tmp_path / "control.json").write_text(json.dumps({"nonce": 10, "cmd": "resume"}))
    bot.tick(now_ts=1005.0)
    assert bot.state["paused"] is False           # fresh command still works


def test_stop_loss_cuts_loser_before_settlement(tmp_path, monkeypatch):
    # Enter YES @ 0.52; sell value halves (26c line) -> exit "stop", not a
    # ride to settlement. yes_ask 0.27, spread 0.02 -> sell 25c <= 26c.
    sigs = [
        _sig(),
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0),
        _sig(whale_trend=3.5, momentum=30.0, yes_ask=0.40, ts=1010.0),  # 38c > 26c
        _sig(whale_trend=3.5, momentum=30.0, yes_ask=0.27, ts=1015.0),  # 25c <= 26c
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    trades = _rows(tmp_path, TRADES_FILE)
    assert len(trades) == 1 and trades[0]["exit_reason"] == "stop"
    assert trades[0]["exit_price"] == 0.25
    assert bot.state["open_plays"] == {}


def test_stop_loss_disabled_by_zero_frac(tmp_path, monkeypatch):
    import json as _json
    (tmp_path / "config.json").write_text(_json.dumps({"stop_loss_frac": 0}))
    sigs = [
        _sig(),
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0),
        _sig(whale_trend=3.5, momentum=30.0, yes_ask=0.27, ts=1010.0),
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert "M1" in bot.state["open_plays"]          # still holding, no stop


def test_max_loss_cap_flattens_blocks_and_survives_day_roll(tmp_path, monkeypatch):
    # Journal already shows -101 total -> boot trips the cap before any entry.
    rows = [dict(_losing_trade_row(), net_pnl=-50.5) for _ in range(2)]
    (tmp_path / TRADES_FILE).write_text("\n".join(json.dumps(r) for r in rows))
    sigs = [_sig(), _sig(whale_trend=3.0, momentum=30.0, ts=1005.0)]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    assert bot.state["total_pnl"] == -101.0
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert bot.state["loss_capped"] is True
    assert bot.state["open_plays"] == {}
    events = _rows(tmp_path, EVENTS_FILE)
    assert any("MAX LOSS CAP" in e["reason"] for e in events)
    skips = [e for e in events if e["action"] == "skip"]
    assert any("max_loss_cap" in e["reason"] for e in skips)
    # Day roll resets the day stop but NOT the cap
    bot.tick(now_ts=1000.0 + 86400 * 30)
    assert bot.state["loss_capped"] is True


def test_max_loss_cap_releases_when_config_raised(tmp_path, monkeypatch):
    import json as _json
    rows = [dict(_losing_trade_row(), net_pnl=-101.0)]
    (tmp_path / TRADES_FILE).write_text("\n".join(json.dumps(r) for r in rows))
    bot = _mkbot(tmp_path, [_sig(), _sig()], monkeypatch)
    bot.tick(now_ts=1000.0)
    assert bot.state["loss_capped"] is True
    (tmp_path / "config.json").write_text(_json.dumps({"max_loss_usd": 200.0}))
    bot.tick(now_ts=1005.0)                        # hot-reload raises the cap
    assert "loss_capped" not in bot.state


def test_sizing_shrinks_with_consumed_loss_budget(tmp_path, monkeypatch):
    # total_pnl -50 -> headroom 50 -> budget $5 -> 9 contracts at 0.52+fee
    rows = [dict(_losing_trade_row(), net_pnl=-50.0)]
    (tmp_path / TRADES_FILE).write_text(json.dumps(rows[0]))
    sigs = [_sig(), _sig(whale_trend=3.0, momentum=30.0, ts=1005.0)]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    qty = bot.state["open_plays"]["M1"]["qty"]
    assert 0 < qty <= 9                            # vs 18 with full headroom


def test_flip_exit_off_holds_through_opposite_flip(tmp_path, monkeypatch):
    import json as _json
    (tmp_path / "config.json").write_text(_json.dumps({"flip_exit": False}))
    sigs = [
        _sig(),
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0),                  # enter YES
        _sig(whale_trend=-3.0, momentum=-20.0, ts=1010.0),                # opp flip: HOLD
        _sig(whale_trend=-3.5, momentum=-20.0, yes_ask=0.66, ts=1015.0),  # target 64>=62
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    trades = _rows(tmp_path, TRADES_FILE)
    assert len(trades) == 1 and trades[0]["exit_reason"] == "target"


def test_momentum_cap_blocks_late_entries(tmp_path, monkeypatch):
    import json as _json
    (tmp_path / "config.json").write_text(_json.dumps({"max_entry_momentum": 25.0}))
    sigs = [_sig(), _sig(whale_trend=3.0, momentum=30.0, ts=1005.0)]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert bot.state["open_plays"] == {}
    events = _rows(tmp_path, EVENTS_FILE)
    assert any("late entry" in e["reason"] for e in events if e["action"] == "skip")


def test_per_market_entry_cap(tmp_path, monkeypatch):
    import json as _json
    (tmp_path / "config.json").write_text(_json.dumps({"max_entries_per_market": 2}))
    flip_up = dict(whale_trend=3.0, momentum=30.0)
    flip_dn = dict(whale_trend=-3.0, momentum=-30.0)
    # After every exit the detector forgets the ticker, so each new flip
    # needs a reseed sample first.
    sigs = [
        _sig(),                                   # seed (down)
        _sig(**flip_up, ts=1005.0),               # entry 1 (YES)
        _sig(**flip_dn, ts=1010.0),               # opp flip -> exit + forget
        _sig(**flip_dn, ts=1015.0),               # reseed (down)
        _sig(**flip_up, ts=1020.0),               # entry 2 (YES)
        _sig(**flip_dn, ts=1025.0),               # opp flip -> exit + forget
        _sig(**flip_dn, ts=1030.0),               # reseed (down)
        _sig(**flip_up, ts=1035.0),               # entry 3 -> BLOCKED
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert bot.state["market_entries"]["M1"] == 2
    assert bot.state["open_plays"] == {}
    events = _rows(tmp_path, EVENTS_FILE)
    assert any("whipsaw guard" in e["reason"] for e in events if e["action"] == "skip")
