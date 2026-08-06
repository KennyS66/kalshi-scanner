import json

import pytest

from settle_bot import (SETTLE_DIR, DEFAULT_CONFIG, load_config, fresh_state,
                        load_state, save_state, append_jsonl)


def test_default_config_has_the_spec_values():
    assert DEFAULT_CONFIG["entry_threshold"] == 10.0
    assert DEFAULT_CONFIG["min_mins_left"] == 5.0
    assert DEFAULT_CONFIG["max_mins_left"] == 11.0
    assert DEFAULT_CONFIG["qty"] == 1


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


def test_entry_decision_reads_its_bounds_from_config_not_hardcoded():
    """Fails if entry_threshold / min_mins_left / max_mins_left were baked
    into the function body -- a later task tunes these via config alone."""
    # threshold raised: a signal that fires at the default must now be ignored
    assert entry_decision(_sig(sig_combined=15.0),
                          dict(DEFAULT_CONFIG, entry_threshold=20.0)) is None
    # threshold lowered: a signal below the default must now fire
    assert entry_decision(_sig(sig_combined=6.0),
                          dict(DEFAULT_CONFIG, entry_threshold=5.0)) == "YES"
    # window narrowed: a time inside the default window must now be excluded
    assert entry_decision(_sig(sig_combined=15.0, mins_left=6.0),
                          dict(DEFAULT_CONFIG, min_mins_left=7.0)) is None
    assert entry_decision(_sig(sig_combined=15.0, mins_left=10.0),
                          dict(DEFAULT_CONFIG, max_mins_left=9.0)) is None


from settle_bot import limit_price, limit_filled


def test_limit_price_joins_the_bid_never_crosses():
    # yes_ask 0.42, spread 0.01 -> rest at 0.41, strictly below the ask
    assert limit_price(_sig(), "YES") == 0.41
    assert limit_price(_sig(), "NO") == 0.58        # no_ask 0.59 - 0.01
    assert limit_price(_sig(yes_ask=None), "YES") is None


def test_limit_price_clamps_at_one_cent_and_handles_crossed_book():
    assert limit_price(_sig(yes_ask=0.01, spread=0.05), "YES") == 0.01
    # crossed book -> negative spread must not push the limit ABOVE the ask
    assert limit_price(_sig(yes_ask=0.42, spread=-0.03), "YES") == 0.42


def test_limit_fills_only_when_the_ask_reaches_it():
    pend = {"side": "YES", "limit": 0.41, "qty": 1, "placed_ts": 1000.0,
            "entry_sig": {}}
    assert limit_filled(_sig(yes_ask=0.41), pend) is True
    assert limit_filled(_sig(yes_ask=0.40), pend) is True
    assert limit_filled(_sig(yes_ask=0.42), pend) is False
    assert limit_filled(_sig(yes_ask=None), pend) is False
    no_pend = dict(pend, side="NO", limit=0.58)
    assert limit_filled(_sig(no_ask=0.57), no_pend) is True
    assert limit_filled(_sig(no_ask=0.59), no_pend) is False


import bot_broker
from settle_bot import Bot, TRADES_FILE, EVENTS_FILE


def _rows(tmp_path, name):
    p = tmp_path / name
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def _mkbot(tmp_path, sigs):
    it = iter(sigs)
    return Bot(tmp_path, fetch_fn=lambda: next(it, None))


def test_qualifying_signal_rests_a_limit(tmp_path):
    bot = _mkbot(tmp_path, [_sig(sig_combined=15.0)])
    bot.tick(now_ts=1000.0)
    assert list(bot.state["pending"]) == ["M1"]
    assert bot.state["pending"]["M1"]["limit"] == 0.41
    assert bot.state["pending"]["M1"]["qty"] == 1
    assert bot.state["open"] == {}
    assert any(e["action"] == "place" for e in _rows(tmp_path, EVENTS_FILE))


def test_resting_order_fills_when_the_ask_reaches_it(tmp_path):
    sigs = [_sig(sig_combined=15.0),                       # place at 0.41
            _sig(sig_combined=15.0, yes_ask=0.41, mins_left=7.0)]   # fills
    bot = _mkbot(tmp_path, sigs)
    bot.tick(now_ts=1000.0)
    bot.tick(now_ts=1005.0)
    assert bot.state["pending"] == {}
    assert bot.state["open"]["M1"]["entry_price"] == 0.41
    assert bot.state["open"]["M1"]["qty"] == 1
    assert bot.state["open"]["M1"]["fee_total"] == 0.0     # maker fee is zero
    assert any(e["action"] == "enter" for e in _rows(tmp_path, EVENTS_FILE))


def test_unfilled_order_is_cancelled_at_window_exit_never_chased(tmp_path):
    sigs = [_sig(sig_combined=15.0),                              # place
            _sig(sig_combined=15.0, mins_left=4.5, yes_ask=0.42)] # window closed
    bot = _mkbot(tmp_path, sigs)
    bot.tick(now_ts=1000.0)
    bot.tick(now_ts=1005.0)
    assert bot.state["pending"] == {}
    assert bot.state["open"] == {}, "an expired limit was chased into a position"
    assert any(e["action"] == "cancel" for e in _rows(tmp_path, EVENTS_FILE))


def test_one_attempt_per_market(tmp_path):
    sigs = [_sig(sig_combined=15.0), _sig(sig_combined=15.0),
            _sig(sig_combined=15.0)]
    bot = _mkbot(tmp_path, sigs)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert len(_rows(tmp_path, EVENTS_FILE)) == 1     # placed once, not thrice


def test_fill_books_the_resting_limit_not_a_gapped_ask(tmp_path):
    """The market can gap straight through the resting limit -- the fill
    must still book at the limit we posted, never at whatever the ask
    happens to be on the touching tick. Swapping pend["limit"] for
    sig["yes_ask"] in _process_pending would make this fail."""
    sigs = [_sig(sig_combined=15.0),                                    # place at 0.41
            _sig(sig_combined=15.0, yes_ask=0.30, mins_left=7.0)]       # gaps through it
    bot = _mkbot(tmp_path, sigs)
    bot.tick(now_ts=1000.0)
    bot.tick(now_ts=1005.0)
    assert bot.state["open"]["M1"]["entry_price"] == 0.41


def test_cancelled_market_is_never_retried_on_a_fresh_qualifying_signal(tmp_path):
    """_seen's `done` clause -- not just `pending` -- must block a second
    attempt. Drive the order through cancellation, then feed a brand new
    qualifying signal for the same ticker and confirm nothing is placed."""
    sigs = [_sig(sig_combined=15.0),                               # place
            _sig(sig_combined=15.0, mins_left=4.5, yes_ask=0.42),  # window closed -> cancel
            _sig(sig_combined=15.0, mins_left=8.0, ts=2000.0)]     # fresh qualifying signal
    bot = _mkbot(tmp_path, sigs)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert bot.state["pending"] == {}
    assert bot.state["open"] == {}
    actions = [e["action"] for e in _rows(tmp_path, EVENTS_FILE)]
    assert actions == ["place", "cancel"]     # no second place after the cancel


from settle_bot import settle_side, settle_pnl


def test_settle_side_reads_the_sign_of_distance():
    assert settle_side({"distance": 12.5}) == "YES"
    assert settle_side({"distance": -3.0}) == "NO"
    assert settle_side({"distance": 0.0}) == "NO"      # at/below strike = NO


def test_settle_pnl_pays_one_minus_entry_on_a_win():
    pos = {"side": "YES", "qty": 1, "entry_price": 0.41, "fee_total": 0.0}
    assert settle_pnl(pos, "YES") == 0.59
    assert settle_pnl(pos, "NO") == -0.41
    no_pos = {"side": "NO", "qty": 1, "entry_price": 0.58, "fee_total": 0.0}
    assert settle_pnl(no_pos, "NO") == 0.42
    assert settle_pnl(no_pos, "YES") == -0.58


def test_position_resolves_when_its_market_rolls_away(tmp_path):
    """Debounce: it takes absent_ticks_to_resolve (default 3) consecutive
    off-ticker ticks -- not one -- before the market is treated as rolled."""
    m2 = _sig(ticker="M2", mins_left=14.0, sig_combined=0.0)
    sigs = [_sig(sig_combined=15.0),
            _sig(sig_combined=15.0, yes_ask=0.41, mins_left=7.0),   # fill
            _sig(ticker="M1", yes_ask=0.98, mins_left=0.1, distance=25.0,
                 sig_combined=0.0),                                  # near expiry
            m2, m2, m2]     # M1 gone: 3 consecutive off-ticker ticks
    bot = _mkbot(tmp_path, sigs)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert bot.state["open"] == {}
    rows = _rows(tmp_path, TRADES_FILE)
    assert len(rows) == 1
    assert rows[0]["settled"] == "YES" and rows[0]["net_pnl"] == 0.59
    assert rows[0]["status"] == "settled"


def test_failed_journal_write_leaves_the_position_open(tmp_path, monkeypatch):
    """Write-ordering: the durable row lands before the position is forgotten."""
    import settle_bot
    m2 = _sig(ticker="M2", mins_left=14.0, sig_combined=0.0)
    sigs = [_sig(sig_combined=15.0),
            _sig(sig_combined=15.0, yes_ask=0.41, mins_left=7.0),
            _sig(ticker="M1", yes_ask=0.98, mins_left=0.1, distance=25.0,
                 sig_combined=0.0),
            m2, m2, m2]
    bot = _mkbot(tmp_path, sigs)
    bot.tick(now_ts=1000.0)
    bot.tick(now_ts=1005.0)
    assert "M1" in bot.state["open"]

    real = settle_bot.append_jsonl

    def boom(path, row):
        if str(path).endswith(TRADES_FILE):
            raise OSError("disk full")
        return real(path, row)

    monkeypatch.setattr(settle_bot, "append_jsonl", boom)
    bot.tick(now_ts=1010.0)          # near-expiry tick for M1: records last_sig
    bot.tick(now_ts=1015.0)          # off-ticker #1 -- debounce, no resolve yet
    bot.tick(now_ts=1020.0)          # off-ticker #2 -- debounce, no resolve yet
    with pytest.raises(OSError):
        bot.tick(now_ts=1025.0)      # off-ticker #3 crosses threshold -> resolve -> journal write fails
    assert "M1" in bot.state["open"], "position forgotten with no journal row"
    assert _rows(tmp_path, TRADES_FILE) == []


def test_settle_rows_never_touch_the_swing_bot_journal(tmp_path):
    """The contamination guard: data/bot/ feeds the swing bot's live gates."""
    m2 = _sig(ticker="M2", mins_left=14.0, sig_combined=0.0)
    sigs = [_sig(sig_combined=15.0),
            _sig(sig_combined=15.0, yes_ask=0.41, mins_left=7.0),
            _sig(ticker="M1", yes_ask=0.98, mins_left=0.1, distance=25.0,
                 sig_combined=0.0),
            m2, m2, m2]
    bot = _mkbot(tmp_path, sigs)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert _rows(tmp_path, TRADES_FILE)                      # it did write
    assert not (tmp_path / "bot_trades.jsonl").exists()
    assert not (tmp_path / "bot_events.jsonl").exists()


def test_unresolved_when_no_near_expiry_tick_was_ever_seen(tmp_path):
    """A position whose market rolls away without ever ticking close to
    expiry must be booked unresolved, not journaled -- never guessed at."""
    m2 = _sig(ticker="M2", mins_left=14.0, sig_combined=0.0)
    sigs = [_sig(sig_combined=15.0),
            _sig(sig_combined=15.0, yes_ask=0.41, mins_left=7.0),   # fill;
            # last_sig mins_left=7.0 stays well above SETTLE_MINS forever
            m2, m2, m2]    # M1 gone: 3 consecutive off-ticker ticks (debounce)
    bot = _mkbot(tmp_path, sigs)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert "M1" not in bot.state["open"]
    assert bot.state["unresolved"] == 1
    assert bot.state["done"]["M1"] == "unresolved"
    assert _rows(tmp_path, TRADES_FILE) == []


def test_single_off_ticker_tick_does_not_resolve(tmp_path):
    """Debounce: one stray off-ticker tick must not book a live sample as
    unresolved or drop it from the journal -- that would silently bias the
    fill-rate/edge statistics this forward test exists to measure."""
    sigs = [_sig(sig_combined=15.0),
            _sig(sig_combined=15.0, yes_ask=0.41, mins_left=7.0),   # fill
            _sig(ticker="M2", mins_left=14.0, sig_combined=0.0)]    # one stray tick
    bot = _mkbot(tmp_path, sigs)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert "M1" in bot.state["open"]
    assert bot.state["open"]["M1"]["absent_ticks"] == 1
    assert _rows(tmp_path, TRADES_FILE) == []


def test_own_ticker_tick_resets_the_absence_counter(tmp_path):
    """An intervening tick for the position's own market resets the debounce
    counter -- off, off, own, off, off must NOT resolve."""
    m2 = _sig(ticker="M2", mins_left=14.0, sig_combined=0.0)
    own = _sig(ticker="M1", yes_ask=0.41, mins_left=6.0, sig_combined=0.0)
    sigs = [_sig(sig_combined=15.0),
            _sig(sig_combined=15.0, yes_ask=0.41, mins_left=7.0),   # fill
            m2, m2,               # 2 off-ticker ticks
            own,                  # own-ticker tick resets the counter
            m2, m2]               # 2 more off-ticker ticks -- still below 3
    bot = _mkbot(tmp_path, sigs)
    for _ in sigs:
        bot.tick(now_ts=1000.0)
    assert "M1" in bot.state["open"]
    assert bot.state["open"]["M1"]["absent_ticks"] == 2
    assert _rows(tmp_path, TRADES_FILE) == []


def test_reconcile_warns_when_an_entry_has_no_settle(tmp_path):
    for row in [{"ts": 1.0, "ticker": "A", "action": "enter"},
                {"ts": 2.0, "ticker": "A", "action": "settle"},
                {"ts": 3.0, "ticker": "B", "action": "enter"}]:   # B lost
        append_jsonl(tmp_path / EVENTS_FILE, row)
    Bot(tmp_path, fetch_fn=lambda: None)
    flagged = [e for e in _rows(tmp_path, EVENTS_FILE)
               if e["action"] == "reconcile"]
    assert len(flagged) == 1 and "+1" in flagged[0]["reason"]


def test_reconcile_survives_a_truncated_line_mid_file(tmp_path):
    """This box power-cycles and the events file is append-only, so a crash
    mid-write can leave one truncated line sitting between otherwise-valid
    rows. The old implementation read the whole file with a single list
    comprehension: one bad `json.loads` raised, hit the file-level
    `except Exception: return`, and skipped the gap computation entirely --
    permanently blinding the boot tripwire, since a corrupt line is never
    fixed by anything that runs afterward. It must instead skip just that
    line and still warn on the gap the surviving rows show."""
    good = [{"ts": 1.0, "ticker": "A", "action": "enter"},
            {"ts": 2.0, "ticker": "A", "action": "settle"},
            {"ts": 3.0, "ticker": "B", "action": "enter"}]   # B lost -> +1
    lines = [json.dumps(good[0]), json.dumps(good[1]),
             '{"ts": 2.5, "ticker": "X", "acti',   # truncated mid-line
             json.dumps(good[2])]
    (tmp_path / EVENTS_FILE).write_text("\n".join(lines) + "\n")
    Bot(tmp_path, fetch_fn=lambda: None)
    # _rows() itself does not skip malformed lines, so parse leniently here
    # -- the truncated line is still in the file; only _reconcile must
    # tolerate it.
    parsed = []
    for l in (tmp_path / EVENTS_FILE).read_text().splitlines():
        try:
            parsed.append(json.loads(l))
        except Exception:
            continue
    flagged = [e for e in parsed if e["action"] == "reconcile"]
    assert len(flagged) == 1 and "+1" in flagged[0]["reason"]


def test_reconcile_quiet_when_an_open_position_explains_the_gap(tmp_path):
    for row in [{"ts": 1.0, "ticker": "A", "action": "enter"},
                {"ts": 2.0, "ticker": "A", "action": "settle"},
                {"ts": 3.0, "ticker": "B", "action": "enter"}]:
        append_jsonl(tmp_path / EVENTS_FILE, row)
    s = fresh_state()
    s["open"]["B"] = {"side": "YES", "qty": 1, "entry_price": 0.4,
                      "fee_total": 0.0, "entry_ts": 3.0}
    save_state(tmp_path, s)
    Bot(tmp_path, fetch_fn=lambda: None)
    assert [e for e in _rows(tmp_path, EVENTS_FILE)
            if e["action"] == "reconcile"] == []


def test_reconcile_stays_quiet_at_a_steady_baseline(tmp_path):
    for row in [{"ts": 1.0, "ticker": "A", "action": "enter"}]:
        append_jsonl(tmp_path / EVENTS_FILE, row)
    b1 = Bot(tmp_path, fetch_fn=lambda: None)
    save_state(tmp_path, b1.state)
    before = len([e for e in _rows(tmp_path, EVENTS_FILE)
                  if e["action"] == "reconcile"])
    Bot(tmp_path, fetch_fn=lambda: None)
    after = len([e for e in _rows(tmp_path, EVENTS_FILE)
                 if e["action"] == "reconcile"])
    assert after == before, "warned again at an unchanged baseline"


def test_reconcile_warns_when_a_fill_has_no_enter_event(tmp_path):
    """Negative gap: position in state but no corresponding enter event in journal.
    Catches fills whose enter events never landed (the regression swing_bot
    suffered for 13 days undetected)."""
    # Write some events (place, cancel for a different ticker) so the file
    # parses cleanly but contributes no enters/settles
    for row in [{"ts": 1.0, "ticker": "X", "action": "place"},
                {"ts": 2.0, "ticker": "X", "action": "cancel"}]:
        append_jsonl(tmp_path / EVENTS_FILE, row)
    # Create state with an open position (C) whose enter event never landed
    s = fresh_state()
    s["open"]["C"] = {"side": "YES", "qty": 1, "entry_price": 0.45,
                      "fee_total": 0.0, "entry_ts": 5.0}
    save_state(tmp_path, s)
    # Gap = 0 enters - 0 settles - 1 open = -1
    # Baseline = 0, so -1 != 0 -> should warn with negative sign
    Bot(tmp_path, fetch_fn=lambda: None)
    flagged = [e for e in _rows(tmp_path, EVENTS_FILE)
               if e["action"] == "reconcile"]
    assert len(flagged) == 1 and "-1" in flagged[0]["reason"]


def test_run_survives_a_tick_that_raises(tmp_path, monkeypatch):
    """A bad tick must log an error event and keep the loop alive."""
    bot = Bot(tmp_path, fetch_fn=lambda: None)
    calls = {"n": 0}

    def boom(now_ts=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        raise KeyboardInterrupt      # end the loop on the second pass

    monkeypatch.setattr(bot, "tick", boom)
    bot.cfg["poll_secs"] = 0        # cfg is a plain dict -- set the key, do
                                    # NOT monkeypatch.setattr a dict method
    with pytest.raises(KeyboardInterrupt):
        bot.run()
    assert any(e["action"] == "error" for e in _rows(tmp_path, EVENTS_FILE))
