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


# ── swing-bot journal loader ──────────────────────────────────────────────

import json as _json

from fill_quality import _load_swing_events


def _events(tmp_path, rows):
    (tmp_path / "bot_events.jsonl").write_text(
        "\n".join(_json.dumps(r) for r in rows))
    return tmp_path


def test_swing_loader_parses_the_place_reason_format(tmp_path):
    d = _events(tmp_path, [{"ts": 1, "ticker": "T1", "action": "place",
                            "reason": "YES x36 limit @ 0.160 (patient)"}])
    placed, filled = _load_swing_events(d)
    assert placed["T1"]["side"] == "YES"
    assert placed["T1"]["limit"] == 0.160
    assert filled == set()


def test_swing_loader_marks_a_placement_filled_when_enter_follows(tmp_path):
    d = _events(tmp_path, [
        {"ts": 1, "ticker": "T1", "action": "place",
         "reason": "NO x20 limit @ 0.470 (aggressive)"},
        {"ts": 2, "ticker": "T1", "action": "enter", "reason": "NO x20 @ 0.47"},
    ])
    _, filled = _load_swing_events(d)
    assert filled == {"T1"}


def test_swing_loader_ignores_an_enter_that_predates_the_placement(tmp_path):
    """A ticker can be placed, cancelled and placed again. Crediting the
    earlier entry to the later placement would manufacture a fill."""
    d = _events(tmp_path, [
        {"ts": 1, "ticker": "T1", "action": "enter", "reason": "NO x20 @ 0.47"},
        {"ts": 9, "ticker": "T1", "action": "place",
         "reason": "NO x20 limit @ 0.470 (patient)"},
    ])
    _, filled = _load_swing_events(d)
    assert filled == set()


def test_swing_loader_honours_since_ts(tmp_path):
    d = _events(tmp_path, [
        {"ts": 10, "ticker": "OLD", "action": "place",
         "reason": "YES x1 limit @ 0.100 (patient)"},
        {"ts": 99, "ticker": "NEW", "action": "place",
         "reason": "YES x1 limit @ 0.200 (patient)"},
    ])
    placed, _ = _load_swing_events(d, since_ts=50)
    assert set(placed) == {"NEW"}


def test_swing_loader_skips_unparseable_reasons(tmp_path):
    d = _events(tmp_path, [{"ts": 1, "ticker": "T1", "action": "place",
                            "reason": "flatten all"}])
    placed, _ = _load_swing_events(d)
    assert placed == {}
