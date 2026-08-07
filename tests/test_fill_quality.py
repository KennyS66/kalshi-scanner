import math

from fill_quality import fill_quality


def _mk(n, side, limit, settled):
    """n markets with a known side/limit and a known settlement."""
    return [{"ticker": f"T{i}-{settled}-{limit}", "side": side, "limit": limit,
             "settled": settled} for i in range(n)]


def test_edge_is_one_minus_limit_on_a_win_and_minus_limit_on_a_loss():
    r = fill_quality(filled=_mk(1, "YES", 0.40, "YES"), unfilled=[])
    assert r["filled"]["edge"] == 0.60
    r = fill_quality(filled=_mk(1, "YES", 0.40, "NO"), unfilled=[])
    assert r["filled"]["edge"] == -0.40


def test_detects_adverse_selection_when_fills_lose_and_misses_win():
    """The 2026-08-07 settle_bot finding in miniature: identical signal and
    limit, but the filled subset loses while the unfilled subset wins."""
    r = fill_quality(filled=_mk(50, "YES", 0.50, "NO"),      # every fill loses
                     unfilled=_mk(50, "YES", 0.50, "YES"))   # every miss wins
    assert r["filled"]["edge"] == -0.50
    assert r["unfilled"]["edge"] == 0.50
    assert r["difference"] == -1.0
    assert r["adverse"] is True


def test_no_adverse_selection_when_both_groups_perform_alike():
    filled = _mk(25, "YES", 0.50, "YES") + _mk(25, "YES", 0.50, "NO")
    unfilled = _mk(25, "YES", 0.50, "YES") + _mk(25, "YES", 0.50, "NO")
    r = fill_quality(filled=filled, unfilled=unfilled)
    assert r["difference"] == 0.0
    assert r["adverse"] is False


def test_win_rate_and_counts_reported():
    r = fill_quality(filled=_mk(3, "NO", 0.30, "NO") + _mk(1, "NO", 0.30, "YES"),
                     unfilled=_mk(2, "NO", 0.30, "YES"))
    assert r["filled"]["n"] == 4 and r["filled"]["win_pct"] == 75.0
    assert r["unfilled"]["n"] == 2 and r["unfilled"]["win_pct"] == 0.0


def test_empty_groups_do_not_raise():
    r = fill_quality(filled=[], unfilled=[])
    assert r["filled"]["n"] == 0 and r["unfilled"]["n"] == 0
    assert r["difference"] is None and r["adverse"] is False
