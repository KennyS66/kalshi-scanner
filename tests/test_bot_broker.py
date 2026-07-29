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


_TUE_NIGHT = 1784592000.0            # 2026-07-21 00:00Z Tue -> weekday_night
_TUE_DAY = _TUE_NIGHT + 14 * 3600     # 2026-07-21 14:00Z Tue -> weekday_day
_SUN_NIGHT = 1784419200.0            # 2026-07-19 00:00Z Sun -> weekend_night
_SUN_DAY = _SUN_NIGHT + 14 * 3600     # 2026-07-19 14:00Z Sun -> weekend_day
_SESSION_TS = {"weekday_day": _TUE_DAY, "weekday_night": _TUE_NIGHT,
               "weekend_day": _SUN_DAY, "weekend_night": _SUN_NIGHT}


def _session_trades(session, n, avg):
    ts = _SESSION_TS[session]
    return [{"net_pnl": avg, "status": "closed", "entry_ts": ts} for _ in range(n)]


def _all_sessions_trades(n=100, avg=0.01, overrides=None):
    """100 trades at +0.01 avg in each of the 4 sessions by default --
    overrides={session: (n, avg)} to make specific sessions fall short."""
    overrides = overrides or {}
    out = []
    for s in _SESSION_TS:
        sn, savg = overrides.get(s, (n, avg))
        out += _session_trades(s, sn, savg)
    return out


def test_fetch_bankroll_returns_none_on_failure(monkeypatch):
    monkeypatch.setattr(bot_broker, "_balance_dollars",
                        lambda: (_ for _ in ()).throw(RuntimeError("api down")))
    assert fetch_bankroll() is None


def test_fetch_bankroll_returns_dollars(monkeypatch):
    monkeypatch.setattr(bot_broker, "_balance_dollars", lambda: 512.33)
    assert fetch_bankroll() == 512.33


def test_live_unlock_requires_all_four_conditions():
    cfg_on = {"live_requested": True}
    env_on = {"BOT_LIVE": "1"}
    ok, _ = live_unlock_ok(_all_sessions_trades(), cfg_on, env_on)
    assert ok
    # one session a trade short of the floor
    short = _all_sessions_trades(overrides={"weekday_day": (99, 0.01)})
    assert not live_unlock_ok(short, cfg_on, env_on)[0]
    # one session net-negative
    neg = _all_sessions_trades(overrides={"weekend_night": (100, -0.01)})
    assert not live_unlock_ok(neg, cfg_on, env_on)[0]
    assert not live_unlock_ok(_all_sessions_trades(), {"live_requested": False}, env_on)[0]


def test_live_unlock_requires_100_and_positive_avg_in_every_session():
    """Per Kenny 2026-07-21: the bar is 100 settled + positive net avg in
    EACH of the 4 sessions independently -- a strong weekday can't mask a
    losing weekend, or vice versa."""
    cfg_on, env_on = {"live_requested": True}, {"BOT_LIVE": "1"}
    # every session short by name in the reason
    for session in _SESSION_TS:
        trades = _all_sessions_trades(overrides={session: (50, 0.01)})
        ok, why = live_unlock_ok(trades, cfg_on, env_on)
        assert not ok and session in why, (session, why)
    # a session with 100 trades but a negative average is named too
    trades = _all_sessions_trades(overrides={"weekend_day": (100, -0.5)})
    ok, why = live_unlock_ok(trades, cfg_on, env_on)
    assert not ok and "weekend_day" in why and "net avg" in why
    # a globally-positive blended average no longer masks one bad session --
    # weekday_day carries huge profit, weekend_night is a small loss
    trades = _all_sessions_trades(overrides={
        "weekday_day": (100, 5.0), "weekend_night": (100, -0.01)})
    assert not live_unlock_ok(trades, cfg_on, env_on)[0]
    assert not live_unlock_ok(_all_sessions_trades(), cfg_on, {})[0]  # no BOT_LIVE


def test_live_broker_manual_buy_emits_signal_not_order(tmp_path, monkeypatch):
    import bot_broker
    monkeypatch.setattr(bot_broker, "requests", None)  # would explode if called
    cfg = {"live_requested": True, "broker_mode": "manual"}
    env = {"BOT_LIVE": "1"}
    b = bot_broker.LiveBroker(_all_sessions_trades(), cfg, env, bot_dir=tmp_path)
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
    cfg = {"live_requested": True, "broker_mode": "auto"}
    env = {"BOT_LIVE": "1"}
    calls = []
    monkeypatch.setattr(live_broker, "place_order", lambda *a, **k: calls.append((a, k)) or
                        {"order_id": "abc", "status": "executed",
                         "yes_price": 31, "no_price": 69})
    b = bot_broker.LiveBroker(_all_sessions_trades(), cfg, env, bot_dir=tmp_path)
    fill = b.buy("YES", 1, {"yes_ask": 0.31, "no_ask": 0.71, "ts": 1000.0,
                            "ticker": "T1", "mins_left": 10.0})
    assert fill["price"] == 0.31
    assert fill["qty"] == 1
    assert not (tmp_path / "live_signals.jsonl").exists()  # no signal in auto mode
    assert calls[0][0] == ("yes", "buy", "T1", 1, 0.31, "market")


def test_live_broker_auto_buy_error_emits_signal_with_error_and_reraises(tmp_path, monkeypatch):
    import bot_broker
    import live_broker
    cfg = {"live_requested": True, "broker_mode": "auto"}
    env = {"BOT_LIVE": "1"}
    def _boom(*a, **k):
        raise RuntimeError("400 insufficient balance")
    monkeypatch.setattr(live_broker, "place_order", _boom)
    b = bot_broker.LiveBroker(_all_sessions_trades(), cfg, env, bot_dir=tmp_path)
    with pytest.raises(RuntimeError, match="insufficient balance"):
        b.buy("YES", 1, {"yes_ask": 0.31, "no_ask": 0.71, "ts": 1000.0,
                         "ticker": "T1", "mins_left": 10.0})
    rows = [_json.loads(l) for l in (tmp_path / "live_signals.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and "insufficient balance" in rows[0]["error"]


def test_live_broker_still_locked_when_unlock_fails():
    with pytest.raises(RuntimeError, match="live trading locked"):
        bot_broker.LiveBroker(_session_trades("weekday_day", 3, 0.01),
                              {"live_requested": True}, {})


def test_live_broker_fill_with_order_id_never_places_a_second_real_order(tmp_path, monkeypatch):
    """The bug this guards: swing_bot._place_entry places the REAL resting
    limit order up front to get an order_id to poll. Once _process_pending
    confirms it executed, it calls fill(..., maker=True) to finalize the
    accounting -- fill() must NOT place a second real order for a position
    that's already open. Passing order_id is exactly how the caller tells
    fill() "this already happened, just do the bookkeeping." """
    import bot_broker
    import live_broker
    cfg = {"live_requested": True, "broker_mode": "auto"}
    env = {"BOT_LIVE": "1"}
    monkeypatch.setattr(live_broker, "place_order", lambda *a, **k:
                        (_ for _ in ()).throw(
                            AssertionError("fill() must not place a new order "
                                           "when order_id is already known")))
    b = bot_broker.LiveBroker(_all_sessions_trades(), cfg, env, bot_dir=tmp_path)
    fill = b.fill(0.49, 3, 1000.0, maker=True,
                 sig={"ticker": "T1", "side": "YES"}, order_id="already-placed-123")
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
    cfg = {"live_requested": True, "broker_mode": "auto"}
    env = {"BOT_LIVE": "1"}
    calls = []
    monkeypatch.setattr(live_broker, "place_order", lambda *a, **k:
                        calls.append(a) or {"order_id": "new-order-456",
                                            "yes_price": 55, "no_price": 45})
    b = bot_broker.LiveBroker(_all_sessions_trades(), cfg, env, bot_dir=tmp_path)
    fill = b.fill(0.55, 2, 1000.0, maker=False,
                 sig={"ticker": "T1", "side": "YES"}, order_id=None)
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
