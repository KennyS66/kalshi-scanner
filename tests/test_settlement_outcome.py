import json

import settlement as st


def row(ts, spot, strike=100.0, mins_left=None, distance=None):
    return {"ts": ts, "spot": spot, "floor_strike": strike,
            "mins_left": mins_left, "distance": distance}


def test_estimate_side_averages_final_minute():
    rows = [row(945, 120), row(970, 110), row(998, 95)]   # mean 108.3 > 100
    assert st.estimate_side(rows, 1000) == "YES"
    assert st.estimate_side([row(945, 90), row(998, 105)], 1000) == "NO"


def test_estimate_side_refuses_to_guess_without_final_minute():
    assert st.estimate_side([row(900, 150)], 1000) is None
    assert st.estimate_side([row(1010, 150)], 1000) is None   # after expiry


def test_outcome_order_official_then_spot60_then_legacy():
    rows = [row(970, 120, mins_left=0.5, distance=-5)]       # expiry = 1000
    assert st.outcome("T", rows, {"T": "NO"}) == "NO"
    assert st.outcome("T", rows, {}) == "YES"                 # spot60
    stale = [row(700, 120, mins_left=5.0, distance=-5)]       # no final minute
    assert st.outcome("T", stale, {}) == "NO"                  # legacy sign
    assert st.outcome("T", [], {}) is None


def test_official_results_cache_first_and_offline_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "CACHE", tmp_path / "res.json")
    calls = []

    def fake_fetch(tickers):
        calls.append(list(tickers))
        return {t: "YES" for t in tickers if t != "PENDING"}

    monkeypatch.setattr(st, "_fetch", fake_fetch)
    assert st.official_results(["A", "PENDING"]) == {"A": "YES"}
    assert json.loads((tmp_path / "res.json").read_text()) == {"A": "YES"}
    assert st.official_results(["A"]) == {"A": "YES"}
    assert calls == [["A", "PENDING"]]            # A served from cache

    def boom(tickers):
        raise OSError("offline")

    monkeypatch.setattr(st, "_fetch", boom)
    assert st.official_results(["A", "B"]) == {"A": "YES"}   # degrade, no raise
    assert st.official_results(["B"], fetch=False) == {}
