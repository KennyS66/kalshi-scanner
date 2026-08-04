import os
import json
import time
from unittest.mock import Mock

import pytest

from swing_bot import (fresh_state, load_state, save_state, read_control,
                       roll_day_if_needed, append_jsonl)


def test_state_roundtrip_atomic(tmp_path):
    s = fresh_state()
    s["pools"]["weekday_night"]["day_pnl"] = -3.21
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
    s["day"] = "2020-01-01"
    s["pools"]["weekday_night"]["day_pnl"] = -50.0
    s["pools"]["weekday_night"]["halted"] = True
    s["pools"]["weekend_day"]["day_pnl"] = -7.0     # every pool resets together
    s["pools"]["weekend_day"]["halted"] = True
    assert roll_day_if_needed(s, time.time()) is True
    for p in s["pools"].values():
        assert p["day_pnl"] == 0.0 and p["halted"] is False
    assert roll_day_if_needed(s, time.time()) is False  # same day now


def test_migrate_pools_backfills_total_and_todays_day_pnl():
    from swing_bot import _migrate_pools
    trades = [
        {"status": "closed", "net_pnl": -10.0, "entry_ts": 1.0, "exit_ts": 100.0,
         "entry_sig": {}},                                     # weekday_night, today
        {"status": "closed", "net_pnl": 5.0, "entry_ts": 1.0, "exit_ts": 200.0,
         "entry_sig": {}},                                     # weekday_night, today
        {"status": "closed", "net_pnl": -50.0, "entry_ts": 1.0,
         "exit_ts": -86400.0, "entry_sig": {}},                 # weekday_night, NOT today
        {"status": "open"},                                     # ignored
    ]
    pools = _migrate_pools(trades, paper_bankroll=400.0, today="1970-01-01")
    assert pools["weekday_night"]["bankroll"] == 100.0          # 400/4
    assert pools["weekday_night"]["total_pnl"] == -55.0         # all 3 closed rows
    assert pools["weekday_night"]["day_pnl"] == -5.0            # only today's 2 rows
    assert pools["weekday_night"]["day_high"] == 0.0            # peaked at 0 (first leg -10, never positive)
    assert pools["weekday_day"]["total_pnl"] == 0.0
    assert pools["weekday_day"]["bankroll"] == 100.0


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
    cfg = json.loads(cfg_p.read_text()) if cfg_p.exists() else {}
    # legacy-mechanics defaults: curfews and scale-out off unless a test
    # opts in via its own config.json written before _mkbot
    cfg.setdefault("overnight_curfew", False)
    cfg.setdefault("weekend_curfew", False)
    cfg.setdefault("scale_out", False)
    # legacy-mechanics default: immediate market fills, same as before
    # limit-order entries existed -- tests exercising the resting-limit
    # state machine itself opt in via their own config.json.
    cfg.setdefault("limit_entries", False)
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
    assert bot.state["pools"]["weekday_night"]["day_pnl"] == t["net_pnl"]


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
    bot.state["pools"]["weekday_night"]["day_pnl"] = -51.0   # beyond 10% of $125 pool
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert bot.state["pools"]["weekday_night"]["halted"] is True
    assert bot.state["open_plays"] == {}
    events = _rows(tmp_path, EVENTS_FILE)
    assert any(e["action"] == "halt" for e in events)


def test_pool_halt_blocks_only_that_pools_entries(tmp_path, monkeypatch):
    import json as _json
    (tmp_path / "config.json").write_text(_json.dumps(
        {"overnight_curfew": False, "weekend_curfew": False}))
    sigs = [
        _sig(ts=1000.0),                                              # seed weekday_night (M1)
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0),               # flip -> M1 blocked
        _sig(ticker="M2", ts=47800.0),                                 # seed weekday_day (M2, 13:03Z)
        _sig(ticker="M2", whale_trend=3.0, momentum=30.0, ts=47805.0), # flip -> M2 should enter
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    bot.state["pools"]["weekday_night"]["halted"] = True
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert "M1" not in bot.state["open_plays"]
    assert "M2" in bot.state["open_plays"]
    assert bot.state["pools"]["weekday_day"]["halted"] is False
    events = _rows(tmp_path, EVENTS_FILE)
    skips = [e for e in events if e["action"] == "skip" and e["ticker"] == "M1"]
    assert any("halted" in e["reason"] for e in skips)


def test_loss_capped_pool_blocks_only_that_pools_entries(tmp_path, monkeypatch):
    import json as _json
    (tmp_path / "config.json").write_text(_json.dumps(
        {"overnight_curfew": False, "weekend_curfew": False}))
    sigs = [
        _sig(ts=1000.0),
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0),               # M1, weekday_night
        _sig(ticker="M2", ts=47800.0),
        _sig(ticker="M2", whale_trend=3.0, momentum=30.0, ts=47805.0), # M2, weekday_day
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    bot.state["pools"]["weekday_night"]["loss_capped"] = True
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert "M1" not in bot.state["open_plays"]
    assert "M2" in bot.state["open_plays"]


def test_day_stop_halt_flattens_only_that_pools_plays(tmp_path, monkeypatch):
    # The live feed only ever tracks one active 15m market at a time — any
    # open play whose ticker doesn't match the tick's fetched sig gets
    # exited as "rolled" (see test_rolled_market_exits_at_last_seen_price).
    # So two *concurrently open* plays across two different pools can't be
    # produced by feeding sequential single-ticker sigs through tick(); seed
    # them directly instead (same play shape _enter builds, including the
    # "pool" field Task 2 routes exits through) and drive the feed with
    # sigs=[None] so _manage never runs — only the risk checks tick() always
    # runs before the fetch, isolating exactly what this test is about.
    bot = _mkbot(tmp_path, [None], monkeypatch)
    m1_sig = _sig(ticker="M1", ts=1005.0)
    m2_sig = _sig(ticker="M2", ts=47805.0)
    bot.state["open_plays"]["M1"] = {
        "side": "YES", "qty": 2, "entry": bot.broker.buy("YES", 2, m1_sig),
        "ranges": None, "entry_sig": m1_sig, "last_sig": m1_sig,
        "pool": "weekday_night"}
    bot.state["open_plays"]["M2"] = {
        "side": "YES", "qty": 2, "entry": bot.broker.buy("YES", 2, m2_sig),
        "ranges": None, "entry_sig": m2_sig, "last_sig": m2_sig,
        "pool": "weekday_day"}
    bot.state["pools"]["weekday_night"]["day_pnl"] = -51.0   # trip only this pool's day stop
    bot.tick(now_ts=1000.0)
    assert "M1" not in bot.state["open_plays"]                # flattened
    assert "M2" in bot.state["open_plays"]                     # untouched
    assert bot.state["pools"]["weekday_night"]["halted"] is True
    assert bot.state["pools"]["weekday_day"]["halted"] is False


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
        {"paper_bankroll": 400.0, "limit_entries": False,
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
    assert bot.state["pools"]["weekday_night"]["bankroll"] == 100.0  # 400/4, not 400
    # $100 pool * 2% = $2 budget; yes_ask 0.52 + fee 0.02 = 0.54 -> 3 contracts
    assert bot.state["open_plays"]["M1"]["qty"] == 3


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
            "entry_ts": 1.0, "exit_ts": 50000.0,   # 13:53Z — inside the gate's
            "fees": 0.02, "exit_reason": "time",   # daytime learning window
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
    # The deadman guards live money only (paper collects unattended by
    # design), so exercise it in live mode. Overriding the broker's mode
    # keeps this focused on the deadman without real live credentials.
    bot.broker.mode = "live"
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
    assert bot.state["pools"]["weekday_night"]["total_pnl"] == -101.0
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert bot.state["pools"]["weekday_night"]["loss_capped"] is True
    assert bot.state["open_plays"] == {}
    events = _rows(tmp_path, EVENTS_FILE)
    assert any("MAX LOSS CAP" in e["reason"] for e in events)
    skips = [e for e in events if e["action"] == "skip"]
    assert any("max_loss_cap" in e["reason"] for e in skips)
    # Day roll resets the day stop but NOT the cap
    bot.tick(now_ts=1000.0 + 86400 * 30)
    assert bot.state["pools"]["weekday_night"]["loss_capped"] is True


def test_max_loss_cap_releases_when_config_raised(tmp_path, monkeypatch):
    import json as _json
    rows = [dict(_losing_trade_row(), net_pnl=-101.0)]
    (tmp_path / TRADES_FILE).write_text("\n".join(json.dumps(r) for r in rows))
    bot = _mkbot(tmp_path, [_sig(), _sig()], monkeypatch)
    bot.tick(now_ts=1000.0)
    assert bot.state["pools"]["weekday_night"]["loss_capped"] is True
    (tmp_path / "config.json").write_text(_json.dumps({"max_loss_usd": 200.0}))
    bot.tick(now_ts=1005.0)                        # hot-reload raises the cap
    assert "loss_capped" not in bot.state["pools"]["weekday_night"]


def test_sizing_shrinks_with_consumed_loss_budget(tmp_path, monkeypatch):
    # $125 pool (500/4) -> base budget 125*.02=$2.50 -> 4 contracts w/ full headroom.
    # total_pnl -91 -> headroom 9 -> trade_risk_frac .10 -> $0.90 budget -> 1
    # contract: the loss-budget constraint now binds instead of the bankroll one.
    # exit_ts is pushed off fresh_state()'s default "day" (_utc_day(0.0)) so
    # _migrate_pools's today-only day_pnl/day_high backfill doesn't also
    # count this loss toward day_pnl -- that would trip the (unrelated)
    # day-stop halt and block the entry outright, masking the loss-budget
    # sizing effect this test is isolating. Only total_pnl (all-time, always
    # backfilled) should be exercised here.
    rows = [dict(_losing_trade_row(), net_pnl=-91.0, exit_ts=90000.0)]
    (tmp_path / TRADES_FILE).write_text(json.dumps(rows[0]))
    sigs = [_sig(), _sig(whale_trend=3.0, momentum=30.0, ts=1005.0)]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    qty = bot.state["open_plays"]["M1"]["qty"]
    assert 0 < qty <= 1                             # vs 4 with full headroom


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


def test_scale_out_banks_half_then_runner_rides_to_stretch(tmp_path, monkeypatch):
    import json as _json
    (tmp_path / "config.json").write_text(_json.dumps(
        {"scale_out": True, "min_edge_c": None,
         "overnight_curfew": False, "weekend_curfew": False}))
    sigs = [
        _sig(),
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0),                 # enter x4 @ .52 ($125 pool * 2% = $2.50)
        _sig(whale_trend=3.5, momentum=20.0, yes_ask=0.66, ts=1010.0),   # sell 64c >= 62 -> scale half
        _sig(whale_trend=3.5, momentum=20.0, yes_ask=0.74, ts=1015.0),   # sell 72c >= 64 -> stretch
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    trades = _rows(tmp_path, TRADES_FILE)
    assert [t["exit_reason"] for t in trades] == ["target_half", "stretch"]
    half, runner = trades
    assert half["qty"] == 2 and runner["qty"] == 2
    assert half["exit_price"] == 0.64 and runner["exit_price"] == 0.72
    assert bot.state["open_plays"] == {}
    # both legs realized into the pool's day pnl; ev gate saw ONE combined sample
    assert bot.state["pools"]["weekday_night"]["day_pnl"] == pytest.approx(
        half["net_pnl"] + runner["net_pnl"])
    bucket = [v for v in bot.ev_stats.values()]
    assert len(bucket) == 1 and bucket[0]["n"] == 1
    assert bucket[0]["net"] == pytest.approx(half["net_pnl"] + runner["net_pnl"])


def test_scale_out_qty_one_exits_full_at_target(tmp_path, monkeypatch):
    import json as _json
    (tmp_path / "config.json").write_text(_json.dumps(
        {"scale_out": True, "min_edge_c": None, "paper_bankroll": 120.0,
         "overnight_curfew": False, "weekend_curfew": False}))
    sigs = [
        _sig(),
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0),                 # tiny budget -> 1 contract
        _sig(whale_trend=3.5, momentum=20.0, yes_ask=0.66, ts=1010.0),
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    trades = _rows(tmp_path, TRADES_FILE)
    assert len(trades) == 1 and trades[0]["exit_reason"] == "target"
    assert trades[0]["qty"] == 1


def test_scale_out_odd_qty_fee_split_conserves_total(tmp_path, monkeypatch):
    # qty=3 -> half=3//2=1, runner=2. The two legs' fees must sum back to
    # the original entry fee exactly (entry["fee_total"] is decremented by
    # subtraction, not recomputed independently, so this is a conservation
    # check, not a rounding-leak hunt -- but the existing scale-out test
    # only covers an even qty=4 split, so this is the first direct check
    # that an odd split doesn't silently drop or double-count a fraction
    # of a cent).
    import json as _json
    from backtest_gate import fee
    (tmp_path / "config.json").write_text(_json.dumps(
        {"scale_out": True, "min_edge_c": None, "paper_bankroll": 340.0,
         "overnight_curfew": False, "weekend_curfew": False}))
    sigs = [
        _sig(),
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0),                 # enter x3 @ .52
        _sig(whale_trend=3.5, momentum=20.0, yes_ask=0.66, ts=1010.0),   # sell 64c -> scale half
        _sig(whale_trend=3.5, momentum=20.0, yes_ask=0.74, ts=1015.0),   # sell 72c -> stretch
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    trades = _rows(tmp_path, TRADES_FILE)
    assert [t["exit_reason"] for t in trades] == ["target_half", "stretch"]
    half, runner = trades
    assert half["qty"] == 1 and runner["qty"] == 2
    original_entry_fee = round(fee(0.52) * 3, 4)
    entry_fee_half = round(original_entry_fee * 1 / 3, 4)
    entry_fee_runner = round(original_entry_fee - entry_fee_half, 4)
    assert half["fees"] == pytest.approx(entry_fee_half + fee(half["exit_price"]) * 1, abs=0.0001)
    assert runner["fees"] == pytest.approx(entry_fee_runner + fee(runner["exit_price"]) * 2, abs=0.0001)
    # the two legs' entry-side fee components alone must reconstruct the
    # original entry fee to the cent -- the actual conservation guarantee.
    assert entry_fee_half + entry_fee_runner == pytest.approx(original_entry_fee, abs=0.0001)


def test_profit_lock_halts_on_giveback_and_halves_size(tmp_path, monkeypatch):
    import json as _json
    (tmp_path / "config.json").write_text(_json.dumps(
        {"min_edge_c": None, "scale_out": False,
         "overnight_curfew": False, "weekend_curfew": False}))
    sigs = [_sig(), _sig(whale_trend=3.0, momentum=30.0, ts=1005.0)]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    # armed: day peaked at +20, still healthy -> entries allowed at HALF size
    bot.state["pools"]["weekday_night"]["day_pnl"] = 20.0
    bot.tick(now_ts=1000.0)                    # seeds detector, sets day_high
    assert bot.state["pools"]["weekday_night"]["day_high"] == 20.0
    bot.tick(now_ts=1000.0)                    # flip -> enter at half budget
    play = list(bot.state["open_plays"].values())[0]
    # $125 pool * 2% = $2.50 full budget -> half $1.25 -> qty 2 @ .52+.02 fee
    assert play["qty"] == 2
    # giveback: drop below 50% of the 20 peak -> halt, profit banked
    bot.state["pools"]["weekday_night"]["day_pnl"] = 9.5
    bot.tick(now_ts=1000.0)
    assert bot.state["pools"]["weekday_night"]["halted"] is True
    events = _rows(tmp_path, EVENTS_FILE)
    assert any("profit_lock" in e["reason"] for e in events if e["action"] == "halt")


def test_profit_lock_not_armed_below_threshold():
    from swing_bot import fresh_state, roll_day_if_needed
    s = fresh_state()
    s["day"] = "2020-01-01"
    s["pools"]["weekday_night"]["day_pnl"] = 5.0
    s["pools"]["weekday_night"]["day_high"] = 20.0
    roll_day_if_needed(s, time.time())
    assert s["pools"]["weekday_night"]["day_high"] == 0.0  # watermark resets each day


# ── resting limit-order entries (2026-07-21: aggressive/patient tiers) ─────

def _limit_cfg(tmp_path, **over):
    import json as _json
    cfg = {"limit_entries": True, "overnight_curfew": False, "weekend_curfew": False}
    cfg.update(over)
    (tmp_path / "config.json").write_text(_json.dumps(cfg))


def test_flip_with_limit_entries_places_pending_not_immediate_fill(tmp_path, monkeypatch):
    _limit_cfg(tmp_path)
    sigs = [
        _sig(ts=1000.0),
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0, mins_left=10.0),  # patient tier
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert bot.state["open_plays"] == {}
    pend = bot.state["pending_entries"]["M1"]
    assert pend["side"] == "YES"
    assert pend["tier"] == "patient"
    assert pend["limit_price"] == pytest.approx(0.52 - 0.01)   # yes_ask - patient offset
    events = _rows(tmp_path, EVENTS_FILE)
    assert any(e["action"] == "place" for e in events)


def test_flip_under_five_minutes_skips_the_limit_and_fills_immediately(tmp_path, monkeypatch):
    _limit_cfg(tmp_path)
    sigs = [
        _sig(ts=1000.0, mins_left=4.5),
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0, mins_left=4.5),  # too close for a limit
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert bot.state["pending_entries"] == {}
    assert "M1" in bot.state["open_plays"]
    assert bot.state["open_plays"]["M1"]["entry"]["price"] == 0.52  # market ask


def test_flip_at_cheap_price_skips_the_limit_and_fills_immediately(tmp_path, monkeypatch):
    # replay evidence: cheap entries (<35c) chase more and lose more when
    # they do -- entry_tier's price gate routes them straight to market
    # even with plenty of time left, same fallback path as the <5min case.
    _limit_cfg(tmp_path)
    sigs = [
        _sig(ts=1000.0, price=0.30, yes_ask=0.30, no_ask=0.72),
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0, mins_left=10.0,
             price=0.30, yes_ask=0.30, no_ask=0.72),               # plenty of time, cheap price
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert bot.state["pending_entries"] == {}
    assert "M1" in bot.state["open_plays"]
    assert bot.state["open_plays"]["M1"]["entry"]["price"] == 0.30  # market ask


def test_pending_entry_fills_at_limit_price_with_maker_fee(tmp_path, monkeypatch):
    from backtest_gate import maker_fee
    _limit_cfg(tmp_path)
    sigs = [
        _sig(ts=1000.0),
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0, mins_left=10.0),           # pending @ 0.51
        _sig(whale_trend=3.0, momentum=5.0, ts=1010.0, yes_ask=0.51, mins_left=9.9),  # ask reaches limit
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert bot.state["pending_entries"] == {}
    play = bot.state["open_plays"]["M1"]
    assert play["entry"]["price"] == 0.51
    assert play["entry"]["maker"] is True
    assert play["entry"]["fee_total"] == pytest.approx(
        maker_fee(0.51) * play["qty"], abs=0.001)
    events = _rows(tmp_path, EVENTS_FILE)
    fills = [e for e in events if e["action"] == "enter"]
    assert any("limit filled" in e["reason"] for e in fills)


def test_pending_entry_chases_to_market_after_timeout(tmp_path, monkeypatch):
    from backtest_gate import fee
    _limit_cfg(tmp_path, limit_fill_timeout_secs=20)
    sigs = [
        _sig(ts=1000.0),
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0, mins_left=10.0),  # pending @ 0.51, placed_ts=1005
        # 25s later (> 20s timeout), ask never dropped to the limit
        _sig(whale_trend=3.0, momentum=5.0, ts=1030.0, yes_ask=0.55, mins_left=9.5),
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert bot.state["pending_entries"] == {}
    play = bot.state["open_plays"]["M1"]
    assert play["entry"]["price"] == 0.55       # chased at the current ask, not the limit
    assert play["entry"]["maker"] is False
    assert play["entry"]["fee_total"] == pytest.approx(fee(0.55) * play["qty"], abs=0.001)
    events = _rows(tmp_path, EVENTS_FILE)
    fills = [e for e in events if e["action"] == "enter"]
    assert any("chased to market" in e["reason"] for e in fills)


def test_pending_entry_not_yet_due_stays_pending(tmp_path, monkeypatch):
    _limit_cfg(tmp_path, limit_fill_timeout_secs=60)
    sigs = [
        _sig(ts=1000.0),
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0, mins_left=10.0),  # pending @ 0.51
        # only 10s later, ask hasn't reached the limit, timeout not elapsed
        _sig(whale_trend=3.0, momentum=5.0, ts=1015.0, yes_ask=0.55, mins_left=9.8),
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert "M1" in bot.state["pending_entries"]
    assert bot.state["open_plays"] == {}


def test_pending_entry_cancelled_on_opposite_flip(tmp_path, monkeypatch):
    _limit_cfg(tmp_path)
    sigs = [
        _sig(ts=1000.0),
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0, mins_left=10.0),  # flip YES -> pending
        _sig(ts=1010.0, mins_left=9.9),                                    # flips back to NO
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert bot.state["pending_entries"] == {}
    assert bot.state["open_plays"] == {}
    events = _rows(tmp_path, EVENTS_FILE)
    assert any(e["action"] == "cancel" and "opposite flip" in e["reason"] for e in events)


def test_pending_entry_cancelled_when_market_rolls(tmp_path, monkeypatch):
    _limit_cfg(tmp_path)
    sigs = [
        _sig(ts=1000.0),
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0, mins_left=10.0),  # pending on M1
        _sig(ticker="M2", ts=1010.0),                                      # new market entirely
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert bot.state["pending_entries"] == {}
    assert "M1" not in bot.state["open_plays"]
    events = _rows(tmp_path, EVENTS_FILE)
    assert any(e["action"] == "cancel" and "rolled" in e["reason"] for e in events)


def test_flatten_cancels_pending_entries_in_that_pool_only(tmp_path, monkeypatch):
    _limit_cfg(tmp_path)
    sigs = [
        _sig(ts=1000.0),
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0, mins_left=10.0),  # pending on M1, weekday_night
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert "M1" in bot.state["pending_entries"]
    bot._flatten("halt", pool="weekend_day")   # different pool -- must not touch it
    assert "M1" in bot.state["pending_entries"]
    bot._flatten("halt", pool="weekday_night")
    assert bot.state["pending_entries"] == {}
    events = _rows(tmp_path, EVENTS_FILE)
    assert any(e["action"] == "cancel" and "halt" in e["reason"] for e in events)


def test_pending_entry_survives_same_ticker_askless_tick(tmp_path, monkeypatch):
    # Replay rows are routinely status="ok" for the live ticker but missing
    # yes_ask/no_ask. _process_pending must treat that as "can't evaluate
    # this tick, wait" -- exactly like the exit loop already does for open
    # plays -- not as a roll. Folding the ask-presence check into the roll
    # condition was a real bug: it cancelled resting orders on their own
    # market's quiet ticks, well before a genuine ticker change.
    _limit_cfg(tmp_path)
    sigs = [
        _sig(ts=1000.0),
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0, mins_left=10.0),  # pending @ 0.51
        _sig(whale_trend=3.0, momentum=30.0, ts=1010.0, mins_left=9.9,
             yes_ask=None, no_ask=None),                                   # same ticker, no quote
        _sig(whale_trend=3.0, momentum=30.0, ts=1015.0, mins_left=9.8,
             yes_ask=0.49, no_ask=0.51),                                   # quote returns, touches limit
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert bot.state["pending_entries"] == {}
    assert "M1" in bot.state["open_plays"]
    events = _rows(tmp_path, EVENTS_FILE)
    assert not any(e["action"] == "cancel" for e in events)
    fills = [e for e in events if e["action"] == "enter"]
    assert any("limit filled" in e["reason"] for e in fills)


def test_bot_constructs_live_broker_when_mode_is_live_and_unlocked(tmp_path, monkeypatch):
    import json as _json
    import bot_broker
    # seed a fully-unlocked trade history (reuse test_bot_broker's helper shape
    # inline here since swing_bot tests don't import test_bot_broker)
    from bot_core import POOL_NAMES, session_tag
    ts_by_pool = {"weekday_day": 1784592000.0 + 14 * 3600,
                  "weekday_night": 1784592000.0,
                  "weekend_day": 1784419200.0 + 14 * 3600,
                  "weekend_night": 1784419200.0}
    trades = []
    for pool, ts in ts_by_pool.items():
        for _ in range(100):
            trades.append({"status": "closed", "net_pnl": 0.01, "entry_ts": ts,
                           "entry_sig": {"ts": ts}})
    (tmp_path / "bot_trades.jsonl").write_text(
        "\n".join(_json.dumps(t) for t in trades))
    (tmp_path / "config.json").write_text(_json.dumps(
        {"mode": "live", "live_sessions_requested": ["weekday_day", "weekday_night",
                                    "weekend_day", "weekend_night"], "broker_mode": "manual",
         "overnight_curfew": False, "weekend_curfew": False}))
    monkeypatch.setenv("BOT_LIVE", "1")
    bot = Bot(tmp_path, fetch_fn=lambda: None,
              offsets_file=tmp_path / "banner_offsets.json",
              loop_log=tmp_path / "loop_log.jsonl")
    assert bot.broker.mode == "live"
    assert bot.broker.broker_mode == "manual"


def test_bot_falls_back_to_paper_broker_when_mode_paper(tmp_path, monkeypatch):
    bot = _mkbot(tmp_path, [_sig()], monkeypatch)
    assert bot.broker.mode == "paper"


def test_bot_raises_loudly_when_mode_live_but_gate_not_unlocked(tmp_path, monkeypatch):
    import json as _json
    (tmp_path / "config.json").write_text(_json.dumps(
        {"mode": "live", "overnight_curfew": False, "weekend_curfew": False}))
    with pytest.raises(RuntimeError, match="live trading locked"):
        Bot(tmp_path, fetch_fn=lambda: None,
            offsets_file=tmp_path / "banner_offsets.json",
            loop_log=tmp_path / "loop_log.jsonl")


def test_live_mode_entries_use_flat_live_qty_not_pool_budget_formula(tmp_path, monkeypatch):
    import json as _json
    ts_by_pool = {"weekday_day": 1784592000.0 + 14 * 3600,
                  "weekday_night": 1784592000.0,
                  "weekend_day": 1784419200.0 + 14 * 3600,
                  "weekend_night": 1784419200.0}
    trades = []
    for pool, ts in ts_by_pool.items():
        for _ in range(100):
            trades.append({"status": "closed", "net_pnl": 0.01, "entry_ts": ts,
                           "entry_sig": {"ts": ts}})
    (tmp_path / "bot_trades.jsonl").write_text(
        "\n".join(_json.dumps(t) for t in trades))
    (tmp_path / "config.json").write_text(_json.dumps(
        {"mode": "live", "live_sessions_requested": ["weekday_day", "weekday_night",
                                    "weekend_day", "weekend_night"], "broker_mode": "manual",
         "overnight_curfew": False, "weekend_curfew": False,
         "limit_entries": False,   # exercise _enter's sizing directly
         "live_qty": 1, "paper_bankroll": 500.0}))
    monkeypatch.setenv("BOT_LIVE", "1")
    bot = Bot(tmp_path, fetch_fn=lambda: None,
              offsets_file=tmp_path / "banner_offsets.json",
              loop_log=tmp_path / "loop_log.jsonl")
    sig = _sig(whale_trend=3.0, momentum=30.0, ts=ts_by_pool["weekday_night"],
              mins_left=10.0, yes_ask=0.50)
    # paper_bankroll=500 -> pool bankroll 125 -> the OLD %-of-pool formula
    # would size this at several contracts (as it does in every existing
    # paper test using the same bankroll); live mode must use live_qty=1
    # regardless of that budget.
    bot._enter("YES", sig)
    assert bot.state["open_plays"]["M1"]["qty"] == 1


def test_paper_mode_entries_still_use_pool_budget_formula(tmp_path, monkeypatch):
    # regression guard: this task's sizing change must be live-mode-only --
    # paper's existing %-of-pool sizing (already covered extensively by
    # other tests in this file) must not change.
    sigs = [_sig(), _sig(whale_trend=3.0, momentum=30.0, ts=1005.0)]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert bot.state["open_plays"]["M1"]["qty"] > 1   # unchanged paper sizing


def test_process_pending_polls_real_order_status_in_auto_mode(tmp_path, monkeypatch):
    import json as _json
    import live_broker
    from bot_core import POOL_NAMES
    ts_by_pool = {"weekday_day": 1784592000.0 + 14 * 3600,
                  "weekday_night": 1784592000.0,
                  "weekend_day": 1784419200.0 + 14 * 3600,
                  "weekend_night": 1784419200.0}
    trades = []
    for pool, ts in ts_by_pool.items():
        for _ in range(100):
            trades.append({"status": "closed", "net_pnl": 0.01, "entry_ts": ts,
                           "entry_sig": {"ts": ts}})
    (tmp_path / "bot_trades.jsonl").write_text(
        "\n".join(_json.dumps(t) for t in trades))
    (tmp_path / "config.json").write_text(_json.dumps(
        {"mode": "live", "live_sessions_requested": ["weekday_day", "weekday_night",
                                    "weekend_day", "weekend_night"], "broker_mode": "auto",
         "overnight_curfew": False, "weekend_curfew": False,
         "limit_entries": True}))
    monkeypatch.setenv("BOT_LIVE", "1")
    monkeypatch.setattr(live_broker, "place_order", lambda *a, **k:
                        {"order_id": "ord-1", "status": "resting",
                         "yes_price": 49, "no_price": 51})
    monkeypatch.setattr(live_broker, "get_order", lambda order_id:
                        {"order_id": order_id, "status": "executed",
                         "yes_price": 49, "no_price": 51})
    bot = Bot(tmp_path, fetch_fn=lambda: None,
              offsets_file=tmp_path / "banner_offsets.json",
              loop_log=tmp_path / "loop_log.jsonl")
    sig = _sig(whale_trend=3.0, momentum=30.0, ts=ts_by_pool["weekday_night"],
              mins_left=10.0, yes_ask=0.50)
    bot._place_entry("YES", sig)
    assert "M1" in bot.state["pending_entries"]
    assert bot.state["pending_entries"]["M1"]["order_id"] == "ord-1"
    next_sig = _sig(whale_trend=3.0, momentum=30.0,
                    ts=ts_by_pool["weekday_night"] + 5, mins_left=9.9, yes_ask=0.50)
    bot._process_pending(next_sig)
    assert "M1" not in bot.state["pending_entries"]
    assert "M1" in bot.state["open_plays"]
    assert bot.state["open_plays"]["M1"]["entry"]["price"] == 0.49


def _live_auto_bot(tmp_path, monkeypatch, balance_sequence):
    import json as _json
    import bot_broker
    ts_by_pool = {"weekday_day": 1784592000.0 + 14 * 3600,
                  "weekday_night": 1784592000.0,
                  "weekend_day": 1784419200.0 + 14 * 3600,
                  "weekend_night": 1784419200.0}
    trades = []
    for pool, ts in ts_by_pool.items():
        for _ in range(100):
            trades.append({"status": "closed", "net_pnl": 0.01, "entry_ts": ts,
                           "entry_sig": {"ts": ts}})
    (tmp_path / "bot_trades.jsonl").write_text(
        "\n".join(_json.dumps(t) for t in trades))
    (tmp_path / "config.json").write_text(_json.dumps(
        {"mode": "live", "live_sessions_requested": ["weekday_day", "weekday_night",
                                    "weekend_day", "weekend_night"], "broker_mode": "auto",
         "overnight_curfew": False, "weekend_curfew": False,
         "live_hard_stop_usd": -8.0, "live_daily_soft_stop_usd": -3.0}))
    monkeypatch.setenv("BOT_LIVE", "1")
    it = iter(balance_sequence)
    monkeypatch.setattr(bot_broker, "_balance_dollars", lambda: next(it))
    return Bot(tmp_path, fetch_fn=lambda: None,
              offsets_file=tmp_path / "banner_offsets.json",
              loop_log=tmp_path / "loop_log.jsonl")


def test_live_stop_baseline_set_on_first_check_then_not_overwritten(tmp_path, monkeypatch):
    bot = _live_auto_bot(tmp_path, monkeypatch, [20.02, 19.50, 19.00])
    bot._check_live_stop()
    assert bot.state["live_baseline_balance"] == 20.02
    bot._check_live_stop()
    assert bot.state["live_baseline_balance"] == 20.02   # not re-baselined


def test_live_stop_halts_all_pools_at_hard_stop(tmp_path, monkeypatch):
    bot = _live_auto_bot(tmp_path, monkeypatch, [20.02, 12.00])
    bot._check_live_stop()   # baseline = 20.02
    bot._check_live_stop()   # 12.00 - 20.02 = -8.02 <= -8.0 hard stop
    for p in bot.state["pools"].values():
        assert p["halted"] is True
    events = _rows(tmp_path, EVENTS_FILE)
    assert any(e["action"] == "halt" and "live_hard_stop" in e["reason"] for e in events)


def test_live_stop_no_halt_above_the_line(tmp_path, monkeypatch):
    bot = _live_auto_bot(tmp_path, monkeypatch, [20.02, 17.50])
    bot._check_live_stop()
    bot._check_live_stop()   # 17.50 - 20.02 = -2.52, above both stops
    for p in bot.state["pools"].values():
        assert p["halted"] is False


def test_live_stop_inactive_in_manual_mode(tmp_path, monkeypatch):
    """live+manual is the literal combination named in the brief and the
    plan's global constraint: a human reads the dashboard before placing
    anything, so _check_live_stop must never touch the real balance API.

    Uses a Mock + assert_not_called() rather than a side-effecting
    exception: _check_live_stop wraps its balance call in a broad
    `except BaseException: return`, so an exception raised by the
    monkeypatched function to "prove" it was called would be silently
    swallowed by that handler even if the guard were broken -- the test
    would still pass with 'live_baseline_balance' not in bot.state, since
    that's also the observable outcome when the call happens but fails.
    assert_not_called() checks the fact of invocation directly and can't
    be defeated that way."""
    import json as _json
    import bot_broker
    ts_by_pool = {"weekday_day": 1784592000.0 + 14 * 3600,
                  "weekday_night": 1784592000.0,
                  "weekend_day": 1784419200.0 + 14 * 3600,
                  "weekend_night": 1784419200.0}
    trades = []
    for pool, ts in ts_by_pool.items():
        for _ in range(100):
            trades.append({"status": "closed", "net_pnl": 0.01, "entry_ts": ts,
                           "entry_sig": {"ts": ts}})
    (tmp_path / "bot_trades.jsonl").write_text(
        "\n".join(_json.dumps(t) for t in trades))
    (tmp_path / "config.json").write_text(_json.dumps(
        {"mode": "live", "live_sessions_requested": ["weekday_day", "weekday_night",
                                    "weekend_day", "weekend_night"], "broker_mode": "manual",
         "overnight_curfew": False, "weekend_curfew": False,
         "live_hard_stop_usd": -8.0, "live_daily_soft_stop_usd": -3.0}))
    monkeypatch.setenv("BOT_LIVE", "1")
    mock_balance = Mock(side_effect=AssertionError("should not be called"))
    monkeypatch.setattr(bot_broker, "_balance_dollars", mock_balance)
    bot = Bot(tmp_path, fetch_fn=lambda: None,
              offsets_file=tmp_path / "banner_offsets.json",
              loop_log=tmp_path / "loop_log.jsonl")
    assert bot.broker.mode == "live" and bot.broker.broker_mode == "manual"
    bot._check_live_stop()   # manual mode -- must be a no-op, must not call balance
    mock_balance.assert_not_called()
    assert "live_baseline_balance" not in bot.state


def test_live_stop_inactive_in_paper_mode(tmp_path, monkeypatch):
    """paper mode has no real balance to check, so _check_live_stop must
    never call the balance API here either. Same Mock/assert_not_called
    mechanism as the manual-mode case above, for the same reason: an
    exception-based tripwire would be silently caught by
    _check_live_stop's own `except BaseException: return` and prove
    nothing about whether the guard actually fired."""
    import json as _json
    import bot_broker
    ts = 1784592000.0
    trades = [{"status": "closed", "net_pnl": 0.01, "entry_ts": ts,
              "entry_sig": {"ts": ts}} for _ in range(100)]
    (tmp_path / "bot_trades.jsonl").write_text(
        "\n".join(_json.dumps(t) for t in trades * 4))  # not actually unlocked, doesn't matter here
    (tmp_path / "config.json").write_text(_json.dumps(
        {"mode": "paper", "overnight_curfew": False, "weekend_curfew": False}))
    mock_balance = Mock(side_effect=AssertionError("should not be called"))
    monkeypatch.setattr(bot_broker, "_balance_dollars", mock_balance)
    bot = Bot(tmp_path, fetch_fn=lambda: None,
              offsets_file=tmp_path / "banner_offsets.json",
              loop_log=tmp_path / "loop_log.jsonl")
    bot._check_live_stop()   # paper mode -- must be a no-op, must not call balance
    mock_balance.assert_not_called()
    assert "live_baseline_balance" not in bot.state


def test_auto_order_error_halts_only_that_pool_not_the_whole_bot(tmp_path, monkeypatch):
    import json as _json
    import live_broker
    ts_by_pool = {"weekday_day": 1784592000.0 + 14 * 3600,
                  "weekday_night": 1784592000.0,
                  "weekend_day": 1784419200.0 + 14 * 3600,
                  "weekend_night": 1784419200.0}
    trades = []
    for pool, ts in ts_by_pool.items():
        for _ in range(100):
            trades.append({"status": "closed", "net_pnl": 0.01, "entry_ts": ts,
                           "entry_sig": {"ts": ts}})
    (tmp_path / "bot_trades.jsonl").write_text(
        "\n".join(_json.dumps(t) for t in trades))
    (tmp_path / "config.json").write_text(_json.dumps(
        {"mode": "live", "live_sessions_requested": ["weekday_day", "weekday_night",
                                    "weekend_day", "weekend_night"], "broker_mode": "auto",
         "overnight_curfew": False, "weekend_curfew": False,
         "limit_entries": True}))
    monkeypatch.setenv("BOT_LIVE", "1")
    def _boom(*a, **k):
        raise RuntimeError("400 insufficient balance")
    monkeypatch.setattr(live_broker, "place_order", _boom)
    bot = Bot(tmp_path, fetch_fn=lambda: None,
              offsets_file=tmp_path / "banner_offsets.json",
              loop_log=tmp_path / "loop_log.jsonl")
    sig = _sig(whale_trend=3.0, momentum=30.0, ts=ts_by_pool["weekday_night"],
              mins_left=10.0, yes_ask=0.50)
    bot._place_entry("YES", sig)   # must not raise out of the caller
    assert bot.state["pools"]["weekday_night"]["halted"] is True
    assert bot.state["pools"]["weekday_day"]["halted"] is False   # other pools unaffected
    assert "M1" not in bot.state["pending_entries"]
    events = _rows(tmp_path, EVENTS_FILE)
    assert any(e["action"] == "halt" and "order_error" in e["reason"] for e in events)


def test_auto_order_error_in_enter_fallback_halts_pool_not_bot(tmp_path, monkeypatch):
    """_enter is the immediate-market-fill fallback _place_entry uses when
    there's too little time left to wait on a resting limit (entry_tier
    returns None below ENTRY_TIER_MIN_MINS). It's named in the task-7 brief
    alongside _process_pending/_fill_pending because it makes its own
    self.broker.buy call in the live+auto path (via LiveBroker._auto_fill
    -> live_broker.place_order) that can raise just like the limit path
    does -- same halt-not-crash contract applies here."""
    import json as _json
    import live_broker
    ts_by_pool = {"weekday_day": 1784592000.0 + 14 * 3600,
                  "weekday_night": 1784592000.0,
                  "weekend_day": 1784419200.0 + 14 * 3600,
                  "weekend_night": 1784419200.0}
    trades = []
    for pool, ts in ts_by_pool.items():
        for _ in range(100):
            trades.append({"status": "closed", "net_pnl": 0.01, "entry_ts": ts,
                           "entry_sig": {"ts": ts}})
    (tmp_path / "bot_trades.jsonl").write_text(
        "\n".join(_json.dumps(t) for t in trades))
    (tmp_path / "config.json").write_text(_json.dumps(
        {"mode": "live", "live_sessions_requested": ["weekday_day", "weekday_night",
                                    "weekend_day", "weekend_night"], "broker_mode": "auto",
         "overnight_curfew": False, "weekend_curfew": False,
         "limit_entries": True}))
    monkeypatch.setenv("BOT_LIVE", "1")
    def _boom(*a, **k):
        raise RuntimeError("400 insufficient balance")
    monkeypatch.setattr(live_broker, "place_order", _boom)
    bot = Bot(tmp_path, fetch_fn=lambda: None,
              offsets_file=tmp_path / "banner_offsets.json",
              loop_log=tmp_path / "loop_log.jsonl")
    # mins_left below ENTRY_TIER_MIN_MINS (5.0) -> entry_tier returns None
    # -> _place_entry falls back to _enter's immediate market buy
    sig = _sig(whale_trend=3.0, momentum=30.0, ts=ts_by_pool["weekday_night"],
              mins_left=2.0, yes_ask=0.50)
    bot._place_entry("YES", sig)   # must not raise out of the caller
    assert bot.state["pools"]["weekday_night"]["halted"] is True
    assert bot.state["pools"]["weekday_day"]["halted"] is False
    assert "M1" not in bot.state["open_plays"]
    events = _rows(tmp_path, EVENTS_FILE)
    assert any(e["action"] == "halt" and "order_error" in e["reason"] for e in events)


# ── task 7b: exit-side (sell) backoff-retry, no halt ────────────────────

def _mk_open_play(pool="weekday_night", qty=4, entry_price=0.50, entry_ts=1000.0):
    """A minimal open play, built directly rather than through _enter, so
    these tests can drive _scale_out/_exit in isolation without going
    through the full tick()/entry-gate machinery."""
    return {
        "side": "YES", "qty": qty,
        "entry": {"price": entry_price, "fee_total": 0.02, "qty": qty, "ts": entry_ts},
        "ranges": None,
        "entry_sig": {"ts": entry_ts},
        "last_sig": _sig(ts=entry_ts),
        "pool": pool,
    }


def test_scale_out_sell_error_backs_off_without_halting_pool(tmp_path, monkeypatch):
    import live_broker
    bot = _live_auto_bot(tmp_path, monkeypatch, [20.02, 19.50])
    play = _mk_open_play()
    bot.state["open_plays"]["M1"] = play

    def _boom(*a, **k):
        raise RuntimeError("500 gateway timeout")
    monkeypatch.setattr(live_broker, "place_order", _boom)
    sig = _sig(ticker="M1", ts=1050.0, yes_ask=0.60, no_ask=0.40)
    bot._scale_out("M1", play, sig)
    assert "M1" in bot.state["open_plays"]          # play stays open, not removed
    assert play["sell_error_ts"] == 1050.0
    assert bot.state["pools"]["weekday_night"]["halted"] is False   # NOT halted
    events = _rows(tmp_path, EVENTS_FILE)
    assert any(e["action"] == "skip" and "backing off" in e["reason"] for e in events)


def test_exit_sell_error_backs_off_without_halting_pool(tmp_path, monkeypatch):
    import live_broker
    bot = _live_auto_bot(tmp_path, monkeypatch, [20.02, 19.50])
    play = _mk_open_play()
    bot.state["open_plays"]["M1"] = play

    def _boom(*a, **k):
        raise RuntimeError("500 gateway timeout")
    monkeypatch.setattr(live_broker, "place_order", _boom)
    sig = _sig(ticker="M1", ts=1050.0, yes_ask=0.60, no_ask=0.40)
    bot._exit("M1", play, sig, "target")
    assert "M1" in bot.state["open_plays"]          # NOT removed -- sell failed
    assert play["sell_error_ts"] == 1050.0
    assert bot.state["pools"]["weekday_night"]["halted"] is False   # NOT halted
    events = _rows(tmp_path, EVENTS_FILE)
    assert any(e["action"] == "skip" and "backing off" in e["reason"] for e in events)


def test_sell_backoff_blocks_retry_within_window_then_allows_after(tmp_path, monkeypatch):
    bot = _live_auto_bot(tmp_path, monkeypatch, [20.02, 19.50])
    play = _mk_open_play()
    play["sell_error_ts"] = 1000.0   # seed a recent failed-sell timestamp
    bot.state["open_plays"]["M1"] = play

    mock_sell = Mock(side_effect=AssertionError("sell must not be called during backoff"))
    monkeypatch.setattr(bot.broker, "sell", mock_sell)
    sig_within = _sig(ticker="M1", ts=1010.0, yes_ask=0.60, no_ask=0.40)  # +10s < 30s backoff
    bot._exit("M1", play, sig_within, "target")
    mock_sell.assert_not_called()
    assert "M1" in bot.state["open_plays"]          # early return -- untouched

    mock_sell_after = Mock(return_value={"price": 0.55, "qty": play["qty"],
                                         "fee_total": 0.02, "ts": 1035.0, "maker": False})
    monkeypatch.setattr(bot.broker, "sell", mock_sell_after)
    sig_after = _sig(ticker="M1", ts=1035.0, yes_ask=0.60, no_ask=0.40)   # +35s >= 30s backoff
    bot._exit("M1", play, sig_after, "target")
    mock_sell_after.assert_called_once()
    assert "M1" not in bot.state["open_plays"]      # exit succeeded this time


def test_paper_mode_sell_still_works_unaffected(tmp_path, monkeypatch):
    """Regression guard for task 7b: the live+auto backoff guard added to
    _scale_out/_exit must be a complete no-op in paper mode -- normal exits
    still succeed and no sell_error_ts bookkeeping ever appears."""
    bot = _mkbot(tmp_path, [_sig()], monkeypatch)
    play = _mk_open_play()
    bot.state["open_plays"]["M1"] = play
    sig = _sig(ticker="M1", ts=1050.0, yes_ask=0.60, no_ask=0.40)
    bot._exit("M1", play, sig, "target")
    assert "M1" not in bot.state["open_plays"]      # exit succeeded normally
    assert "sell_error_ts" not in play              # guard field never touched
    trades = _rows(tmp_path, TRADES_FILE)
    assert len(trades) == 1 and trades[0]["exit_reason"] == "target"


# ── flatten must not orphan real resting orders in auto mode ───────────

def test_flatten_paper_pending_entry_never_calls_live_broker(tmp_path, monkeypatch):
    """Regression guard: a paper/manual pending entry (no order_id) must
    behave exactly as before this fix -- plain del + cancel event, with
    live_broker.cancel_order never even called. Complements
    test_flatten_cancels_pending_entries_in_that_pool_only, which checks
    this path's outcome but not the absence of a live_broker call."""
    import live_broker
    _limit_cfg(tmp_path)
    sigs = [
        _sig(ts=1000.0),
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0, mins_left=10.0),  # pending on M1
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert "M1" in bot.state["pending_entries"]
    assert "order_id" not in bot.state["pending_entries"]["M1"]
    mock_cancel = Mock()
    monkeypatch.setattr(live_broker, "cancel_order", mock_cancel)
    bot._flatten("flatten")
    mock_cancel.assert_not_called()
    assert bot.state["pending_entries"] == {}
    events = _rows(tmp_path, EVENTS_FILE)
    assert any(e["action"] == "cancel" and "flatten" in e["reason"] for e in events)


def test_flatten_cancels_live_resting_order_for_pending_entry(tmp_path, monkeypatch):
    """The bug this guards: in live+auto mode a pending entry carries a
    real resting limit order on Kalshi (order_id set by _place_entry).
    _flatten must cancel that order on the exchange before dropping the
    entry from local state -- otherwise it stays live on the book and can
    fill later into a position nothing here is tracking."""
    import live_broker
    bot = _live_auto_bot(tmp_path, monkeypatch, [20.02, 19.50])
    bot.state["pending_entries"]["M1"] = {
        "side": "YES", "qty": 1, "limit_price": 0.45, "tier": "patient",
        "placed_ts": 1000.0, "ranges": None, "entry_sig": {"ts": 1000.0},
        "pool": "weekday_night", "order_id": "ord-1"}
    mock_cancel = Mock()
    monkeypatch.setattr(live_broker, "cancel_order", mock_cancel)
    bot._flatten("flatten")
    mock_cancel.assert_called_once_with("ord-1")
    assert bot.state["pending_entries"] == {}
    assert bot.state["pools"]["weekday_night"]["halted"] is False   # cancel ok, no halt
    events = _rows(tmp_path, EVENTS_FILE)
    assert any(e["action"] == "cancel" and "flatten" in e["reason"] for e in events)


def test_flatten_halts_pool_on_cancel_failure_but_still_processes_other_entries(tmp_path, monkeypatch):
    """A failed cancel must not be silently swallowed -- halt that entry's
    pool, same as the order_error pattern used elsewhere -- and must not
    abort cleanup of the rest of the flatten loop. Two pending entries in
    two different pools, both raising on cancel: both must end up halted
    and removed, and cancel_order must be attempted for both."""
    import live_broker
    bot = _live_auto_bot(tmp_path, monkeypatch, [20.02, 19.50])
    bot.state["pending_entries"]["M1"] = {
        "side": "YES", "qty": 1, "limit_price": 0.45, "tier": "patient",
        "placed_ts": 1000.0, "ranges": None, "entry_sig": {"ts": 1000.0},
        "pool": "weekday_night", "order_id": "ord-1"}
    bot.state["pending_entries"]["M2"] = {
        "side": "YES", "qty": 1, "limit_price": 0.45, "tier": "patient",
        "placed_ts": 1000.0, "ranges": None, "entry_sig": {"ts": 1000.0},
        "pool": "weekday_day", "order_id": "ord-2"}

    def _boom(order_id):
        raise RuntimeError("500 gateway timeout")
    mock_cancel = Mock(side_effect=_boom)
    monkeypatch.setattr(live_broker, "cancel_order", mock_cancel)
    bot._flatten("flatten")   # must not raise out of the caller
    assert mock_cancel.call_count == 2
    assert bot.state["pending_entries"] == {}
    assert bot.state["pools"]["weekday_night"]["halted"] is True
    assert bot.state["pools"]["weekday_day"]["halted"] is True
    events = _rows(tmp_path, EVENTS_FILE)
    halt_events = [e for e in events if e["action"] == "halt" and "order_error" in e["reason"]]
    assert len(halt_events) == 2


# ── per-session pause (GUI per-pool resume/pause) ────────────────────

_WD_NIGHT_TS = 1784592000.0            # Tue 00:00Z -> weekday_night
_WD_DAY_TS = 1784592000.0 + 14 * 3600  # Tue 14:00Z -> weekday_day


def _flip_pair(ts):
    """Two sigs whose whale_trend flips sign -- enough to trigger an entry."""
    return [_sig(ts=ts, whale_trend=3.0, momentum=30.0),
            _sig(ts=ts + 5, whale_trend=-3.0, momentum=-30.0)]


def test_paused_session_blocks_only_that_sessions_entries(tmp_path, monkeypatch):
    """A session named in cfg["paused_sessions"] takes no new entries, while
    every other session keeps trading -- the whole point of a per-pool
    pause as opposed to the existing global one."""
    (tmp_path / "config.json").write_text(json.dumps(
        {"paused_sessions": ["weekday_night"],
         "overnight_curfew": False, "weekend_curfew": False}))
    bot = _mkbot(tmp_path, _flip_pair(_WD_NIGHT_TS), monkeypatch)
    for _ in range(2):
        bot.tick()
    assert bot.state["open_plays"] == {}, "paused session must not open a play"
    reasons = [e["reason"] for e in _rows(tmp_path, EVENTS_FILE)
               if e["action"] == "skip"]
    assert any("weekday_night paused" in r for r in reasons), reasons


def test_unpaused_session_still_enters_while_another_is_paused(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps(
        {"paused_sessions": ["weekday_night"],
         "overnight_curfew": False, "weekend_curfew": False}))
    bot = _mkbot(tmp_path, _flip_pair(_WD_DAY_TS), monkeypatch)
    for _ in range(2):
        bot.tick()
    assert bot.state["open_plays"], "weekday_day is not paused and must still enter"


def test_session_pause_survives_a_day_roll(tmp_path, monkeypatch):
    """Pausing lives in config.json, not state, so roll_day_if_needed --
    which resets day_pnl/halted for every pool -- must not silently
    un-pause a session the user deliberately paused."""
    from swing_bot import roll_day_if_needed
    (tmp_path / "config.json").write_text(json.dumps(
        {"paused_sessions": ["weekday_night"],
         "overnight_curfew": False, "weekend_curfew": False}))
    bot = _mkbot(tmp_path, _flip_pair(_WD_NIGHT_TS), monkeypatch)
    bot.state["day"] = "1999-01-01"
    assert roll_day_if_needed(bot.state, _WD_NIGHT_TS) is True
    assert bot.cfg["paused_sessions"] == ["weekday_night"]


# ── live-manual per-session gating (2026-08-02 code-review finding #1) ──
#
# With mode=live + broker_mode=manual and only SOME sessions toggled live
# (the actual Monday plan: weekday_night only), the bot must decline
# non-live-session entries per the 2026-07-29 spec ("simply decline ...
# entries individually, not refuse to run at all") -- NOT raise an
# uncaught RuntimeError that aborts the tick before heartbeat/state save.


def _mk_live_manual_bot(tmp_path, sigs, monkeypatch, extra_cfg=None):
    monkeypatch.setenv("BOT_LIVE", "1")
    cfg = {"mode": "live", "broker_mode": "manual",
           "live_sessions_requested": ["weekday_night"],
           "overnight_curfew": False, "weekend_curfew": False,
           "scale_out": False, "limit_entries": False}
    cfg.update(extra_cfg or {})
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    return _mkbot(tmp_path, sigs, monkeypatch)


def test_live_manual_entry_in_nonlive_session_skips_not_raises(tmp_path, monkeypatch):
    bot = _mk_live_manual_bot(tmp_path, _flip_pair(_WD_DAY_TS), monkeypatch)
    for _ in range(2):
        bot.tick()          # must not raise
    assert bot.state["open_plays"] == {}
    assert bot.state["pending_entries"] == {}
    reasons = [e["reason"] for e in _rows(tmp_path, EVENTS_FILE)
               if e["action"] == "skip"]
    assert any("weekday_day not live" in r for r in reasons), reasons


def test_live_manual_entry_in_live_session_still_enters(tmp_path, monkeypatch):
    bot = _mk_live_manual_bot(tmp_path, _flip_pair(_WD_NIGHT_TS), monkeypatch)
    for _ in range(2):
        bot.tick()
    assert "M1" in bot.state["open_plays"], "the live session itself must still enter"


def test_live_manual_exit_across_session_boundary_still_closes(tmp_path, monkeypatch):
    """A weekday_night play still open when the clock crosses 13Z must exit
    normally in weekday_day -- the old behavior raised on sell every tick
    forever, so the play could never close and the deadman froze the bot."""
    sigs = _flip_pair(_WD_NIGHT_TS) + [
        _sig(ts=_WD_DAY_TS, whale_trend=-3.0, momentum=-30.0, mins_left=1.5)]
    bot = _mk_live_manual_bot(tmp_path, sigs, monkeypatch)
    for _ in range(3):
        bot.tick()          # must not raise
    assert bot.state["open_plays"] == {}
    trades = _rows(tmp_path, TRADES_FILE)
    assert len(trades) == 1 and trades[0]["status"] == "closed"


def test_tick_persists_heartbeat_even_when_manage_raises(tmp_path, monkeypatch):
    """Any exception inside the tick must not skip the heartbeat/state
    save -- a silently-stale heartbeat is how the old bug froze the bot
    while the process looked alive (code-review finding #3)."""
    bot = _mkbot(tmp_path, [_sig()], monkeypatch)
    monkeypatch.setattr(bot, "_manage", Mock(side_effect=RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        bot.tick(now_ts=4242.0)
    saved = json.loads((tmp_path / STATE_FILE).read_text())
    assert saved["heartbeat"] == 4242.0


# ── STOP must also stop resting limit orders (2026-08-02 review, crit #1) ──
#
# _process_pending runs before the paused entry-blocker, so a resting limit
# order could still fill -- or chase to MARKET on timeout -- after the user
# hit STOP. The kill-switch confirm promises "new entries stop immediately",
# so a pause must cancel resting entries, not let them become positions.


def _pending_bot(tmp_path, monkeypatch, extra_cfg=None):
    cfg = {"limit_entries": True, "overnight_curfew": False,
           "weekend_curfew": False, "scale_out": False}
    cfg.update(extra_cfg or {})
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    sigs = [_sig(ts=_WD_NIGHT_TS, whale_trend=3.0, momentum=30.0),
            _sig(ts=_WD_NIGHT_TS + 5, whale_trend=-3.0, momentum=-30.0),
            _sig(ts=_WD_NIGHT_TS + 10, whale_trend=-3.5, momentum=-30.0)]
    return _mkbot(tmp_path, sigs, monkeypatch)


def test_pause_cancels_a_resting_entry_instead_of_filling_it(tmp_path, monkeypatch):
    bot = _pending_bot(tmp_path, monkeypatch)
    for _ in range(2):
        bot.tick()
    assert bot.state["pending_entries"], "precondition: a limit order is resting"
    bot.state["paused"] = True
    bot.tick()
    assert bot.state["pending_entries"] == {}, "paused: resting order must be cancelled"
    assert bot.state["open_plays"] == {}, "paused: it must not become a position"
    reasons = [e["reason"] for e in _rows(tmp_path, EVENTS_FILE)
               if e["action"] == "cancel"]
    assert any("paused" in r for r in reasons), reasons


def test_paused_session_also_cancels_its_resting_entry(tmp_path, monkeypatch):
    """The per-session pause makes the same promise as the global one."""
    bot = _pending_bot(tmp_path, monkeypatch)
    for _ in range(2):
        bot.tick()
    assert bot.state["pending_entries"]
    (tmp_path / "config.json").write_text(json.dumps(
        {"limit_entries": True, "overnight_curfew": False,
         "weekend_curfew": False, "scale_out": False,
         "paused_sessions": ["weekday_night"]}))
    bot.tick()
    assert bot.state["pending_entries"] == {}
    assert bot.state["open_plays"] == {}


def test_unpaused_resting_entry_still_fills_normally(tmp_path, monkeypatch):
    """The guard must not break the ordinary path it sits in front of."""
    bot = _pending_bot(tmp_path, monkeypatch)
    for _ in range(2):
        bot.tick()
    pend = dict(next(iter(bot.state["pending_entries"].values())))
    bot.fetch = lambda: _sig(ts=_WD_NIGHT_TS + 10, whale_trend=-3.5, momentum=-30.0,
                             yes_ask=pend["limit_price"], no_ask=pend["limit_price"])
    bot.tick()
    assert bot.state["pending_entries"] == {}
    assert bot.state["open_plays"], "an unpaused resting order must still fill"


# ── maker_only: never pay the taker fee on entries (2026-08-03) ────────
#
# Confirmed against real account fills: Kalshi charges makers ZERO and
# takers ~0.07*p*(1-p)/contract. Kenny: "bot should only be doing maker
# orders." Entries have two taker paths -- the too-close-to-expiry
# market fallback, and chase-to-market when a resting limit times out.


def _maker_cfg(**over):
    cfg = {"maker_only": True, "limit_entries": True, "overnight_curfew": False,
           "weekend_curfew": False, "scale_out": False}
    cfg.update(over)
    return cfg


def test_maker_only_skips_entries_too_close_to_expiry_instead_of_taking(tmp_path, monkeypatch):
    """entry_tier returns None near expiry and the bot market-fills. Under
    maker_only that trade is skipped: paying the taker fee is not an
    option, so no-trade is the only maker-consistent outcome."""
    (tmp_path / "config.json").write_text(json.dumps(_maker_cfg()))
    # 4.5 clears min_entry_mins (4.0) but is under ENTRY_TIER_MIN_MINS (5.0),
    # so entry_tier returns None -- exactly the market-fallback path.
    sigs = [_sig(ts=_WD_NIGHT_TS, whale_trend=3.0, momentum=30.0, mins_left=4.5),
            _sig(ts=_WD_NIGHT_TS + 5, whale_trend=-3.0, momentum=-30.0, mins_left=4.5)]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick()
    assert bot.state["open_plays"] == {} and bot.state["pending_entries"] == {}
    reasons = [e["reason"] for e in _rows(tmp_path, EVENTS_FILE) if e["action"] == "skip"]
    assert any("maker_only" in r for r in reasons), reasons


def test_maker_only_cancels_a_timed_out_limit_instead_of_chasing(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps(
        _maker_cfg(limit_fill_timeout_secs=10)))
    sigs = [_sig(ts=_WD_NIGHT_TS, whale_trend=3.0, momentum=30.0),
            _sig(ts=_WD_NIGHT_TS + 5, whale_trend=-3.0, momentum=-30.0),
            _sig(ts=_WD_NIGHT_TS + 60, whale_trend=-3.5, momentum=-30.0)]  # past timeout
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick()
    assert bot.state["open_plays"] == {}, "must not chase to market"
    assert bot.state["pending_entries"] == {}
    reasons = [e["reason"] for e in _rows(tmp_path, EVENTS_FILE) if e["action"] == "cancel"]
    assert any("maker_only" in r for r in reasons), reasons


def test_taker_paths_still_work_when_maker_only_is_off(tmp_path, monkeypatch):
    """The flag must be opt-in: default behaviour is unchanged."""
    (tmp_path / "config.json").write_text(json.dumps(
        _maker_cfg(maker_only=False, limit_entries=False)))
    sigs = [_sig(ts=_WD_NIGHT_TS, whale_trend=3.0, momentum=30.0, mins_left=4.5),
            _sig(ts=_WD_NIGHT_TS + 5, whale_trend=-3.0, momentum=-30.0, mins_left=4.5)]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    for _ in sigs:
        bot.tick()
    assert bot.state["open_plays"], "market entry must still work with the flag off"


# ── deadman applies to live money only (2026-08-03) ───────────────────
#
# "No unsupervised trading" is about real money. In paper mode there is
# nothing to protect, and collecting unattended paper data is the entire
# point of paper mode -- the 680-trade history was gathered that way.
# Scoping it to live also means the guard comes BACK automatically when
# mode flips to live, with no config to remember to restore.


def _deadman_bot(tmp_path, monkeypatch, mode):
    (tmp_path / "config.json").write_text(json.dumps(
        {"loop_deadman_mins": 45,
         "overnight_curfew": False, "weekend_curfew": False}))
    stale = tmp_path / "loop_log.jsonl"
    stale.write_text("{}\n")
    os.utime(stale, (time.time() - 9999, time.time() - 9999))   # very stale
    bot = _mkbot(tmp_path, [_sig(ts=_WD_NIGHT_TS)], monkeypatch)
    bot.loop_log = stale
    bot.broker.mode = mode
    return bot


def test_deadman_does_not_pause_a_paper_bot(tmp_path, monkeypatch):
    bot = _deadman_bot(tmp_path, monkeypatch, "paper")
    bot.tick()
    assert bot.state["paused"] is False, "paper must keep collecting unattended"


def test_deadman_still_pauses_a_live_bot(tmp_path, monkeypatch):
    bot = _deadman_bot(tmp_path, monkeypatch, "live")
    bot.tick()
    assert bot.state["paused"] is True
    assert bot.state.get("paused_by") == "deadman"


import swing_bot


def test_failed_trade_row_write_leaves_no_half_applied_exit(tmp_path, monkeypatch):
    """A trade-log write failure must not book P&L for a play it then forgets.

    The July 2026 orphan (KXBTC15M-26JUL210515-15) went exactly this way:
    _exit credited the pool and deleted the play, then the append raised, so
    the trade vanished from bot_trades.jsonl while its P&L stayed booked.
    """
    sigs = [
        _sig(),                                              # seeds detector
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0),     # flip -> enter YES
        _sig(whale_trend=4.0, momentum=20.0, yes_ask=0.60, ts=1010.0),  # hold
        _sig(whale_trend=-3.0, momentum=-20.0, yes_ask=0.60, ts=1015.0),  # flip -> exit
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    bot.tick(now_ts=1000.0)
    bot.tick(now_ts=1000.0)
    bot.tick(now_ts=1000.0)
    assert bot.state["open_plays"], "expected an open play before the exit tick"
    pnl_before = bot.state["pools"]["weekday_night"]["total_pnl"]

    real_append = swing_bot.append_jsonl

    def fail_trades_file(path, row):
        if str(path).endswith(TRADES_FILE):
            raise OSError("simulated disk failure writing the trade row")
        return real_append(path, row)

    monkeypatch.setattr(swing_bot, "append_jsonl", fail_trades_file)

    with pytest.raises(OSError):
        bot.tick(now_ts=1000.0)

    assert _rows(tmp_path, TRADES_FILE) == [], "no trade row should have landed"
    assert bot.state["pools"]["weekday_night"]["total_pnl"] == pnl_before, \
        "P&L was booked for a trade that never reached the log"
    assert bot.state["open_plays"], \
        "the play was forgotten even though its exit was never recorded"


def test_exit_retry_after_failed_row_write_does_not_sell_twice(tmp_path, monkeypatch):
    """Writing the trade row first means a failed exit retries next tick.

    The sell already hit the broker, so the retry must reuse that fill rather
    than dumping the position a second time -- harmless on paper, a real
    double-sell on live money.
    """
    sigs = [
        _sig(),                                                          # seed
        _sig(whale_trend=3.0, momentum=30.0, ts=1005.0),                 # enter YES
        _sig(whale_trend=3.5, momentum=30.0, mins_left=1.5, ts=1010.0),  # time exit
        _sig(whale_trend=3.5, momentum=30.0, mins_left=1.4, ts=1015.0),  # retry
    ]
    bot = _mkbot(tmp_path, sigs, monkeypatch)
    bot.tick(now_ts=1000.0)
    bot.tick(now_ts=1000.0)
    assert bot.state["open_plays"], "expected an open play before the exit tick"

    sells = []
    real_sell = bot.broker.sell

    def counting_sell(side, qty, sig):
        sells.append((side, qty))
        return real_sell(side, qty, sig)

    monkeypatch.setattr(bot.broker, "sell", counting_sell)

    real_append = swing_bot.append_jsonl

    def fail_trades_file(path, row):
        if str(path).endswith(TRADES_FILE):
            raise OSError("simulated disk failure writing the trade row")
        return real_append(path, row)

    monkeypatch.setattr(swing_bot, "append_jsonl", fail_trades_file)
    with pytest.raises(OSError):
        bot.tick(now_ts=1000.0)          # sell lands, trade row does not

    monkeypatch.setattr(swing_bot, "append_jsonl", real_append)
    bot.tick(now_ts=1000.0)              # retry: row lands this time

    assert bot.state["open_plays"] == {}, "retry should have completed the exit"
    assert len(_rows(tmp_path, TRADES_FILE)) == 1
    assert len(sells) == 1, f"position was sold {len(sells)} times, expected 1"


def test_startup_flags_entries_that_never_produced_an_exit(tmp_path, monkeypatch):
    """Boot-time reconciliation: enters - exits must equal the open plays.

    This is the check that would have surfaced the 2026-07-21 orphan the day
    it happened instead of thirteen days later.
    """
    for row in [{"ts": 1.0, "ticker": "A", "action": "enter"},
                {"ts": 2.0, "ticker": "A", "action": "exit"},
                {"ts": 3.0, "ticker": "B", "action": "enter"}]:   # B: never exited
        append_jsonl(tmp_path / EVENTS_FILE, row)

    bot = _mkbot(tmp_path, [], monkeypatch)

    flagged = [e for e in _rows(tmp_path, EVENTS_FILE) if e["action"] == "reconcile"]
    assert len(flagged) == 1, "boot should flag the unaccounted-for entry"
    assert "1" in flagged[0]["reason"]
    assert bot.state["open_plays"] == {}


def test_startup_stays_quiet_when_an_open_play_accounts_for_the_gap(tmp_path, monkeypatch):
    """An entry with no exit is fine while the play is still open."""
    for row in [{"ts": 1.0, "ticker": "A", "action": "enter"},
                {"ts": 2.0, "ticker": "A", "action": "exit"},
                {"ts": 3.0, "ticker": "B", "action": "enter"}]:
        append_jsonl(tmp_path / EVENTS_FILE, row)
    s = fresh_state()
    s["open_plays"]["B"] = {"side": "YES", "qty": 5,
                            "entry": {"price": 0.5, "qty": 5, "fee_total": 0.1,
                                      "ts": 3.0}}
    save_state(tmp_path, s)

    _mkbot(tmp_path, [], monkeypatch)

    flagged = [e for e in _rows(tmp_path, EVENTS_FILE) if e["action"] == "reconcile"]
    assert flagged == [], "the open play accounts for the missing exit"


def test_startup_reconcile_warns_once_then_only_when_the_gap_grows(tmp_path, monkeypatch):
    """A known-bad history must not warn on every boot.

    The 2026-07-21 orphan is a permanent +1. If that fired every restart, the
    next orphan would be indistinguishable from the standing noise -- which is
    the whole point of the check.
    """
    for row in [{"ts": 1.0, "ticker": "A", "action": "enter"},
                {"ts": 2.0, "ticker": "A", "action": "exit"},
                {"ts": 3.0, "ticker": "B", "action": "enter"}]:   # orphan #1
        append_jsonl(tmp_path / EVENTS_FILE, row)

    def flagged():
        return [e for e in _rows(tmp_path, EVENTS_FILE) if e["action"] == "reconcile"]

    bot = _mkbot(tmp_path, [], monkeypatch)
    assert len(flagged()) == 1, "first sighting of the gap should warn"
    save_state(tmp_path, bot.state)          # as tick()'s finally would

    bot = _mkbot(tmp_path, [], monkeypatch)
    assert len(flagged()) == 1, "steady state must stay quiet"
    save_state(tmp_path, bot.state)

    append_jsonl(tmp_path / EVENTS_FILE,
                 {"ts": 4.0, "ticker": "C", "action": "enter"})   # orphan #2
    _mkbot(tmp_path, [], monkeypatch)
    assert len(flagged()) == 2, "a NEW orphan must warn"
    assert "+2" in flagged()[1]["reason"]


def _fail_on_trades_file(monkeypatch):
    """Make append_jsonl blow up for the trade journal only."""
    real_append = swing_bot.append_jsonl

    def boom(path, row):
        if str(path).endswith(TRADES_FILE):
            raise OSError("simulated disk failure writing the trade row")
        return real_append(path, row)

    monkeypatch.setattr(swing_bot, "append_jsonl", boom)
    return real_append


def test_failed_scale_row_write_does_not_credit_the_pool(tmp_path, monkeypatch):
    """Same ordering defect _exit had, in the scale-out path."""
    bot = _mkbot(tmp_path, [], monkeypatch)
    play = _mk_open_play(qty=4)
    bot.state["open_plays"]["M1"] = play
    before = bot.state["pools"]["weekday_night"]["total_pnl"]

    _fail_on_trades_file(monkeypatch)
    sig = _sig(ticker="M1", ts=1050.0, yes_ask=0.60, no_ask=0.40)
    with pytest.raises(OSError):
        bot._scale_out("M1", play, sig)

    assert _rows(tmp_path, TRADES_FILE) == [], "no trade row should have landed"
    assert bot.state["pools"]["weekday_night"]["total_pnl"] == before, \
        "pool was credited for a banked leg that never reached the log"


def test_scale_out_retry_after_failed_row_write_banks_exactly_once(tmp_path, monkeypatch):
    """The consequence that makes this worse than the _exit defect.

    Crediting the pool before entry["qty"] -= half and play["scaled"] run
    leaves the play looking unscaled at full size, so the next tick banks the
    same half again -- a double-count on top of a double-sell.
    """
    bot = _mkbot(tmp_path, [], monkeypatch)
    play = _mk_open_play(qty=4)
    bot.state["open_plays"]["M1"] = play
    before = bot.state["pools"]["weekday_night"]["total_pnl"]

    sells = []
    real_sell = bot.broker.sell

    def counting_sell(side, qty, sig):
        sells.append((side, qty))
        return real_sell(side, qty, sig)

    monkeypatch.setattr(bot.broker, "sell", counting_sell)

    real_append = _fail_on_trades_file(monkeypatch)
    sig = _sig(ticker="M1", ts=1050.0, yes_ask=0.60, no_ask=0.40)
    with pytest.raises(OSError):
        bot._scale_out("M1", play, sig)

    monkeypatch.setattr(swing_bot, "append_jsonl", real_append)
    bot._scale_out("M1", play, sig)                     # retry

    rows = _rows(tmp_path, TRADES_FILE)
    assert len(rows) == 1, f"banked {len(rows)} times, expected 1"
    assert bot.state["pools"]["weekday_night"]["total_pnl"] == \
        pytest.approx(before + rows[0]["net_pnl"]), "pool credited more than once"
    assert len(sells) == 1, f"sold {len(sells)} times, expected 1"
    assert play["qty"] == 2 and play["scaled"]
