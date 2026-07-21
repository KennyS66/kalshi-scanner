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


def test_live_broker_locked_raises():
    import pytest
    with pytest.raises(RuntimeError, match="live trading locked"):
        LiveBroker(_session_trades("weekday_day", 3, 0.01), {"live_requested": True}, {})


def test_fallback_bankroll_is_500():
    assert FALLBACK_BANKROLL == 500.0
