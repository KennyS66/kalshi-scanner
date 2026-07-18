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


_MON = 1784592000.0   # 2026-07-20 12:00Z (Monday)
_SAT = 1784419200.0   # 2026-07-18 12:00Z (Saturday)


def _trades(n, avg, n_weekend=None):
    """n closed trades; half weekend/half weekday unless n_weekend given."""
    if n_weekend is None:
        n_weekend = n // 2
    return [{"net_pnl": avg, "status": "closed",
             "exit_ts": _SAT if i < n_weekend else _MON}
            for i in range(n)]


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
    ok, _ = live_unlock_ok(_trades(200, 0.01), cfg_on, env_on)
    assert ok
    assert not live_unlock_ok(_trades(199, 0.01), cfg_on, env_on)[0]     # a band < 100
    assert not live_unlock_ok(_trades(200, -0.01), cfg_on, env_on)[0]    # negative EV
    assert not live_unlock_ok(_trades(200, 0.01), {"live_requested": False}, env_on)[0]


def test_live_unlock_requires_100_weekday_and_100_weekend():
    cfg_on, env_on = {"live_requested": True}, {"BOT_LIVE": "1"}
    # 150 weekend + 50 weekday: plenty total, weekday sample short
    ok, why = live_unlock_ok(_trades(200, 0.01, n_weekend=150), cfg_on, env_on)
    assert not ok and "weekday" in why
    # 150 weekday + 50 weekend: weekend sample short
    ok, why = live_unlock_ok(_trades(200, 0.01, n_weekend=50), cfg_on, env_on)
    assert not ok and "weekend" in why
    assert not live_unlock_ok(_trades(100, 0.01), cfg_on, {})[0]         # no BOT_LIVE


def test_live_broker_locked_raises():
    import pytest
    with pytest.raises(RuntimeError, match="live trading locked"):
        LiveBroker(_trades(3, 0.01), {"live_requested": True}, {})


def test_fallback_bankroll_is_500():
    assert FALLBACK_BANKROLL == 500.0
