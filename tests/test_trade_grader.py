import pytest

from trade_grader import (
    verdict_for, infer_settlement, hold_path_stats, expiry_of,
)


def tick(ts, spot=64000.0, strike=63950.0, price=0.5, ticker="T-1"):
    return {"ts": ts, "spot": spot, "floor_strike": strike,
            "price": price, "ticker": ticker}


@pytest.mark.parametrize("reason,side,settled,expected", [
    ("stop",   "YES", "YES", "whipsaw_stop"),
    ("stop",   "YES", "NO",  "good_stop"),
    ("target", "NO",  "NO",  "clean_win"),
    ("target", "NO",  "YES", "lucky_exit"),
    ("deadman","YES", "YES", "left_money"),
    ("flip",   "NO",  "YES", "good_exit"),
    ("stop",   "YES", "unknown", "ungraded"),
])
def test_verdict_table(reason, side, settled, expected):
    assert verdict_for(reason, side, settled) == expected


def test_expiry_from_entry_sig():
    t = {"entry_ts": 1000.0, "entry_sig": {"mins_left": 10.0}}
    assert expiry_of(t) == 1600.0


def test_settlement_strike_basis_yes_and_no():
    # last tick 30s before expiry -> inside SETTLE_WINDOW_S -> strike basis
    up = [tick(900), tick(970, spot=64000.0, strike=63950.0)]
    assert infer_settlement(up, expiry=1000.0) == ("YES", "strike")
    dn = [tick(900), tick(970, spot=63900.0, strike=63950.0)]
    assert infer_settlement(dn, expiry=1000.0) == ("NO", "strike")


def test_settlement_price_fallback_when_no_late_tick():
    # last tick 300s before expiry, but market already decided
    decided = [tick(700, price=0.97)]
    assert infer_settlement(decided, expiry=1000.0) == ("YES", "price")
    decided_no = [tick(700, price=0.03)]
    assert infer_settlement(decided_no, expiry=1000.0) == ("NO", "price")
    undecided = [tick(700, price=0.5)]
    assert infer_settlement(undecided, expiry=1000.0) == ("unknown", "none")
    assert infer_settlement([], expiry=1000.0) == ("unknown", "none")


def test_settlement_ignores_ticks_after_expiry():
    ticks = [tick(970, spot=64000.0, strike=63950.0),
             tick(1050, spot=60000.0, strike=63950.0)]  # next market's data
    assert infer_settlement(ticks, expiry=1000.0) == ("YES", "strike")


def test_hold_path_stats_yes_side():
    ticks = [tick(100, price=0.50), tick(160, price=0.62), tick(220, price=0.41)]
    mfe, mae = hold_path_stats(ticks, "YES", entry_price=0.50,
                               entry_ts=100, exit_ts=220)
    assert mfe == pytest.approx(0.12)
    assert mae == pytest.approx(0.09)


def test_hold_path_stats_no_side_uses_inverted_price():
    # NO side price = 1 - price
    ticks = [tick(100, price=0.50), tick(160, price=0.30), tick(220, price=0.70)]
    mfe, mae = hold_path_stats(ticks, "NO", entry_price=0.50,
                               entry_ts=100, exit_ts=220)
    assert mfe == pytest.approx(0.20)   # 1-0.30=0.70 vs 0.50
    assert mae == pytest.approx(0.20)   # 1-0.70=0.30 vs 0.50


def test_hold_path_stats_no_ticks_in_window():
    assert hold_path_stats([], "YES", 0.5, 100, 200) == (None, None)
    outside = [tick(50), tick(300)]
    assert hold_path_stats(outside, "YES", 0.5, 100, 200) == (None, None)
