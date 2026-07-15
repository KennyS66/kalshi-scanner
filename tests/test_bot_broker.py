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


def test_round_trip_pnl_nets_out_fees():
    b = PaperBroker()
    entry = b.buy("YES", 10, SIG)                      # 10 @ 0.52
    exit_ = b.sell("YES", 10, {**SIG, "yes_ask": 0.60, "ts": 1300.0})  # 10 @ 0.58
    expected = (0.58 - 0.52) * 10 - entry["fee_total"] - exit_["fee_total"]
    assert round_trip_pnl(entry, exit_) == pytest.approx(expected)
