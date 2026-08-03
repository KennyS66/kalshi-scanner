import pytest
from backtest_gate import fee
from bot_broker import PaperBroker, round_trip_pnl

SIG = {"yes_ask": 0.52, "no_ask": 0.50, "spread": 0.02, "ts": 1000.0}


def test_buy_yes_fills_at_yes_ask_plus_fee():
    f = PaperBroker().buy("YES", 10, SIG)
    assert f["price"] == 0.52
    assert f["qty"] == 10
    assert f["fee_total"] == pytest.approx(fee(0.52) * 10)
    assert f["ts"] == 1000.0


def test_buy_no_fills_at_no_ask():
    f = PaperBroker().buy("NO", 5, SIG)
    assert f["price"] == 0.50


def test_sell_crosses_the_spread():
    b = PaperBroker()
    assert b.sell("YES", 10, SIG)["price"] == pytest.approx(0.50)  # 0.52 - 0.02
    assert b.sell("NO", 10, SIG)["price"] == pytest.approx(0.48)   # 0.50 - 0.02


def test_sell_price_never_below_one_cent():
    b = PaperBroker()
    low = {"yes_ask": 0.02, "no_ask": 0.02, "spread": 0.05, "ts": 0}
    assert b.sell("YES", 1, low)["price"] == 0.01


def test_sell_clamps_negative_spread_so_price_never_exceeds_ask():
    # A crossed book (yes_ask + no_ask < 1) yields spread < 0; without a
    # clamp the sell price would be computed above the ask (phantom profit).
    b = PaperBroker()
    crossed = {"yes_ask": 0.52, "no_ask": 0.50, "spread": -0.05, "ts": 0}
    assert b.sell("YES", 1, crossed)["price"] == 0.52


def test_round_trip_pnl_nets_out_fees():
    b = PaperBroker()
    entry = b.buy("YES", 10, SIG)                      # 10 @ 0.52
    exit_ = b.sell("YES", 10, {**SIG, "yes_ask": 0.60, "ts": 1300.0})  # 10 @ 0.58
    expected = (0.58 - 0.52) * 10 - entry["fee_total"] - exit_["fee_total"]
    assert round_trip_pnl(entry, exit_) == pytest.approx(expected)


import bot_broker
from bot_broker import fetch_bankroll, live_unlock_ok, LiveBroker, FALLBACK_BANKROLL




def test_fetch_bankroll_returns_none_on_failure(monkeypatch):
    monkeypatch.setattr(bot_broker, "_balance_dollars",
                        lambda: (_ for _ in ()).throw(RuntimeError("api down")))
    assert fetch_bankroll() is None


def test_fetch_bankroll_returns_dollars(monkeypatch):
    monkeypatch.setattr(bot_broker, "_balance_dollars", lambda: 512.33)
    assert fetch_bankroll() == 512.33




def test_live_broker_manual_buy_emits_signal_not_order(tmp_path, monkeypatch):
    import bot_broker
    monkeypatch.setattr(bot_broker, "requests", None)  # would explode if called
    cfg = {"live_sessions_requested": ["weekday_night"], "broker_mode": "manual"}
    env = {"BOT_LIVE": "1"}
    b = bot_broker.LiveBroker(cfg, env, bot_dir=tmp_path)
    fill = b.buy("YES", 1, {"yes_ask": 0.31, "no_ask": 0.71, "ts": 1000.0,
                            "ticker": "T1", "mins_left": 10.0})
    assert fill["price"] == 0.31
    assert fill["qty"] == 1
    assert fill["fee_total"] == 0.0   # nothing was actually filled
    assert fill.get("signal_only") is True
    rows = [_json.loads(l) for l in (tmp_path / "live_signals.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["side"] == "YES" and rows[0]["price"] == 0.31


def test_live_broker_auto_buy_places_market_order(tmp_path, monkeypatch):
    import bot_broker
    import live_broker
    cfg = {"live_sessions_requested": ["weekday_night"], "broker_mode": "auto"}
    env = {"BOT_LIVE": "1"}
    calls = []
    monkeypatch.setattr(live_broker, "place_order", lambda *a, **k: calls.append((a, k)) or
                        {"order_id": "abc", "status": "executed",
                         "yes_price": 31, "no_price": 69})
    b = bot_broker.LiveBroker(cfg, env, bot_dir=tmp_path)
    fill = b.buy("YES", 1, {"yes_ask": 0.31, "no_ask": 0.71, "ts": 1000.0,
                            "ticker": "T1", "mins_left": 10.0})
    assert fill["price"] == 0.31
    assert fill["qty"] == 1
    assert not (tmp_path / "live_signals.jsonl").exists()  # no signal in auto mode
    assert calls[0][0] == ("yes", "buy", "T1", 1, 0.31, "market")


def test_live_broker_auto_buy_error_emits_signal_with_error_and_reraises(tmp_path, monkeypatch):
    import bot_broker
    import live_broker
    cfg = {"live_sessions_requested": ["weekday_night"], "broker_mode": "auto"}
    env = {"BOT_LIVE": "1"}
    def _boom(*a, **k):
        raise RuntimeError("400 insufficient balance")
    monkeypatch.setattr(live_broker, "place_order", _boom)
    b = bot_broker.LiveBroker(cfg, env, bot_dir=tmp_path)
    with pytest.raises(RuntimeError, match="insufficient balance"):
        b.buy("YES", 1, {"yes_ask": 0.31, "no_ask": 0.71, "ts": 1000.0,
                         "ticker": "T1", "mins_left": 10.0})
    rows = [_json.loads(l) for l in (tmp_path / "live_signals.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and "insufficient balance" in rows[0]["error"]



def test_live_broker_fill_with_order_id_never_places_a_second_real_order(tmp_path, monkeypatch):
    """The bug this guards: swing_bot._place_entry places the REAL resting
    limit order up front to get an order_id to poll. Once _process_pending
    confirms it executed, it calls fill(..., maker=True) to finalize the
    accounting -- fill() must NOT place a second real order for a position
    that's already open. Passing order_id is exactly how the caller tells
    fill() "this already happened, just do the bookkeeping." """
    import bot_broker
    import live_broker
    cfg = {"live_sessions_requested": ["weekday_night"], "broker_mode": "auto"}
    env = {"BOT_LIVE": "1"}
    monkeypatch.setattr(live_broker, "place_order", lambda *a, **k:
                        (_ for _ in ()).throw(
                            AssertionError("fill() must not place a new order "
                                           "when order_id is already known")))
    b = bot_broker.LiveBroker(cfg, env, bot_dir=tmp_path)
    fill = b.fill(0.49, 3, 1000.0, maker=True,
                 sig={"ticker": "T1", "side": "YES", "ts": 1784592000.0},
                 order_id="already-placed-123")
    assert fill["price"] == 0.49
    assert fill["qty"] == 3
    assert fill["order_id"] == "already-placed-123"
    assert fill["fee_total"] > 0   # maker fee still computed


def test_live_broker_fill_without_order_id_places_a_new_order(tmp_path, monkeypatch):
    """The complement of the guard above: the chase-to-market path cancels
    the original order first, so fill() is called with order_id=None and
    MUST place a genuinely new order."""
    import bot_broker
    import live_broker
    cfg = {"live_sessions_requested": ["weekday_night"], "broker_mode": "auto"}
    env = {"BOT_LIVE": "1"}
    calls = []
    monkeypatch.setattr(live_broker, "place_order", lambda *a, **k:
                        calls.append(a) or {"order_id": "new-order-456",
                                            "yes_price": 55, "no_price": 45})
    b = bot_broker.LiveBroker(cfg, env, bot_dir=tmp_path)
    fill = b.fill(0.55, 2, 1000.0, maker=False,
                 sig={"ticker": "T1", "side": "YES", "ts": 1784592000.0}, order_id=None)
    assert len(calls) == 1
    assert fill["order_id"] == "new-order-456"


def test_fallback_bankroll_is_500():
    assert FALLBACK_BANKROLL == 500.0


import json as _json


def test_emit_live_signal_appends_one_row(tmp_path):
    from bot_broker import emit_live_signal
    emit_live_signal(tmp_path, "KXBTC15M-26JUL290300-00", "YES", 1, 0.31,
                     "patient", "weekday_night")
    rows = [_json.loads(l) for l in
            (tmp_path / "live_signals.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    r = rows[0]
    assert r["ticker"] == "KXBTC15M-26JUL290300-00"
    assert r["side"] == "YES"
    assert r["qty"] == 1
    assert r["price"] == 0.31
    assert r["tier"] == "patient"
    assert r["pool"] == "weekday_night"
    assert r["error"] is None
    assert isinstance(r["ts"], float)


def test_emit_live_signal_records_error(tmp_path):
    from bot_broker import emit_live_signal
    emit_live_signal(tmp_path, "KXBTC15M-26JUL290300-00", "YES", 1, 0.31,
                     "market", "weekday_night", error="order rejected: insufficient funds")
    rows = [_json.loads(l) for l in
            (tmp_path / "live_signals.jsonl").read_text().splitlines()]
    assert rows[0]["error"] == "order rejected: insufficient funds"


def test_emit_live_signal_appends_multiple_rows(tmp_path):
    from bot_broker import emit_live_signal
    emit_live_signal(tmp_path, "T1", "YES", 1, 0.50, "aggressive", "weekday_night")
    emit_live_signal(tmp_path, "T2", "NO", 2, 0.40, "patient", "weekday_night")
    rows = [_json.loads(l) for l in
            (tmp_path / "live_signals.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["ticker"] == "T1" and rows[1]["ticker"] == "T2"


# ── per-session live toggle (2026-07-29 design) ──────────────────────

def test_live_capability_ok_requires_nonempty_sessions_and_bot_live():
    from bot_broker import live_capability_ok
    ok, reason = live_capability_ok({"live_sessions_requested": ["weekday_night"]}, {"BOT_LIVE": "1"})
    assert ok
    assert not live_capability_ok({"live_sessions_requested": []}, {"BOT_LIVE": "1"})[0]
    assert not live_capability_ok({}, {"BOT_LIVE": "1"})[0]  # key absent entirely
    assert not live_capability_ok({"live_sessions_requested": ["weekday_night"]}, {})[0]
    assert not live_capability_ok({"live_sessions_requested": ["weekday_night"]}, {"BOT_LIVE": "0"})[0]


def test_live_unlock_ok_checks_only_the_named_session():
    from bot_broker import live_unlock_ok
    cfg = {"live_sessions_requested": ["weekday_night"]}
    env = {"BOT_LIVE": "1"}
    assert live_unlock_ok(cfg, env, "weekday_night")[0]
    ok, reason = live_unlock_ok(cfg, env, "weekend_day")
    assert not ok
    assert "weekend_day" in reason
    # other sessions' presence/absence never affects this session's check
    cfg2 = {"live_sessions_requested": ["weekday_night", "weekend_day", "weekend_night"]}
    assert not live_unlock_ok(cfg2, env, "weekday_day")[0]   # still absent, still locked
    assert live_unlock_ok(cfg2, env, "weekend_night")[0]      # present, still unlocked


def test_live_unlock_ok_requires_bot_live_even_if_session_requested():
    from bot_broker import live_unlock_ok
    cfg = {"live_sessions_requested": ["weekday_night"]}
    assert not live_unlock_ok(cfg, {}, "weekday_night")[0]
    assert not live_unlock_ok(cfg, {"BOT_LIVE": "0"}, "weekday_night")[0]


def test_live_unlock_ok_does_not_recheck_trade_history():
    """The no-auto-disable-on-regression guarantee: once a session is in
    live_sessions_requested, it stays unlocked regardless of what its
    trade history would show if recomputed -- because these functions
    never look at trade history at all. This test's real assertion is
    the function signature itself: live_unlock_ok takes no trades
    argument, so there is nothing for a regression to be recomputed
    FROM."""
    from bot_broker import live_unlock_ok
    cfg = {"live_sessions_requested": ["weekday_night"]}
    env = {"BOT_LIVE": "1"}
    ok, reason = live_unlock_ok(cfg, env, "weekday_night")
    assert ok and reason == "unlocked"


def test_live_broker_constructs_with_no_trades_argument(tmp_path):
    from bot_broker import LiveBroker
    cfg = {"live_sessions_requested": ["weekday_night"], "broker_mode": "manual"}
    env = {"BOT_LIVE": "1"}
    b = LiveBroker(cfg, env, bot_dir=tmp_path)
    assert b.mode == "live"
    assert b.broker_mode == "manual"


def test_live_broker_construction_fails_when_no_session_ever_requested(tmp_path):
    from bot_broker import LiveBroker
    import pytest
    with pytest.raises(RuntimeError, match="live trading locked"):
        LiveBroker({"live_sessions_requested": []}, {"BOT_LIVE": "1"}, bot_dir=tmp_path)


def test_live_broker_buy_checks_the_entrys_own_session(tmp_path):
    from bot_broker import LiveBroker
    import pytest
    cfg = {"live_sessions_requested": ["weekday_night"], "broker_mode": "manual"}
    env = {"BOT_LIVE": "1"}
    b = LiveBroker(cfg, env, bot_dir=tmp_path)
    # weekday_night entry (ts falls in weekday_night per bot_core.session_tag) succeeds
    fill = b.buy("YES", 1, {"yes_ask": 0.31, "no_ask": 0.71, "ts": 1784592000.0,  # Tue 00:00Z
                            "ticker": "T1", "mins_left": 10.0})
    assert fill["price"] == 0.31
    # a weekday_day entry (same day, hour 14 -> day session) on the SAME already-constructed
    # broker instance is rejected -- proving the check is per-call, not per-broker
    with pytest.raises(RuntimeError, match="live trading locked"):
        b.buy("YES", 1, {"yes_ask": 0.31, "no_ask": 0.71, "ts": 1784592000.0 + 14 * 3600,
                         "ticker": "T2", "mins_left": 10.0})


def test_live_broker_sell_exempt_from_session_gate(tmp_path):
    """Exits must never be blocked by the session gate: a play entered in a
    live session can legitimately need to close after the clock crosses into
    a non-live session, and blocking the sell traps the position (2026-08-02
    code-review finding #1). The gate exists to stop NEW un-earned exposure,
    not risk reduction."""
    from bot_broker import LiveBroker
    cfg = {"live_sessions_requested": ["weekday_night"], "broker_mode": "manual"}
    env = {"BOT_LIVE": "1"}
    b = LiveBroker(cfg, env, bot_dir=tmp_path)
    weekend_ts = 1784419200.0  # Sun 00:00Z -> weekend_night, not requested
    fill = b.sell("YES", 1, {"yes_ask": 0.50, "no_ask": 0.50, "spread": 0.02,
                             "ts": weekend_ts, "ticker": "T3"})
    assert fill["price"] == 0.48  # ask - spread, same as an unlocked sell


def test_live_broker_fill_exempt_from_session_gate(tmp_path):
    """Entry gating happens at placement time (swing_bot blocks the entry
    before any order exists; buy() double-checks). By fill time the pend has
    already been deleted from state, so a raise here would silently lose the
    entry AND abort the tick — fill must not re-check the session."""
    from bot_broker import LiveBroker
    cfg = {"live_sessions_requested": ["weekday_night"], "broker_mode": "manual"}
    env = {"BOT_LIVE": "1"}
    b = LiveBroker(cfg, env, bot_dir=tmp_path)
    weekend_ts = 1784419200.0
    fill = b.fill(0.50, 1, weekend_ts, maker=True,
                  sig={"ticker": "T3", "side": "YES", "ts": weekend_ts})
    assert fill["price"] == 0.50 and fill["signal_only"]


def test_live_broker_fails_closed_when_session_cannot_be_resolved(tmp_path):
    """A sig with no ts resolves to session_tag -> "unknown", which can
    never appear in live_sessions_requested. The gate must reject it
    rather than fall through to a real order: an entry whose session we
    cannot identify is exactly the one we must not trade live."""
    from bot_broker import LiveBroker
    import pytest
    b = LiveBroker({"live_sessions_requested": ["weekday_night", "weekday_day",
                                                "weekend_day", "weekend_night"],
                    "broker_mode": "auto"}, {"BOT_LIVE": "1"}, bot_dir=tmp_path)
    with pytest.raises(RuntimeError, match="live trading locked"):
        b.buy("YES", 1, {"yes_ask": 0.31, "no_ask": 0.71, "ticker": "T1"})
