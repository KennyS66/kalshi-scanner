import math
import pytest

from hypothesis_gate import (hypothesis, assess, adjusted_bar, predicate_hash,
                             Registry)


# ── pre-registration is enforced ─────────────────────────────────────────

def test_hypothesis_requires_a_band():
    """You cannot see the result and then decide what counts as good."""
    with pytest.raises(ValueError, match="band"):
        @hypothesis(name="no band", window=(5.0, 11.0))
        def f(row):
            return "YES"


def test_hypothesis_records_its_declaration():
    @hypothesis(name="declared", band=(0.02, 0.05), window=(5.0, 11.0))
    def f(row):
        return "YES" if row.get("x", 0) > 0 else None
    assert f.hypothesis_name == "declared"
    assert f.band == (0.02, 0.05)
    assert f.window == (5.0, 11.0)
    assert f({"x": 1}) == "YES" and f({"x": 0}) is None


# ── the multiple-comparisons penalty ─────────────────────────────────────

def test_adjusted_bar_rises_with_comparison_count():
    """After 25 tests a t of 2 is expected by chance. bot_tuner's whole
    failure mode was best-of-N selection with no such correction."""
    assert adjusted_bar(1) == pytest.approx(1.96, abs=0.01)
    assert adjusted_bar(25) == pytest.approx(3.09, abs=0.02)
    assert adjusted_bar(50) > adjusted_bar(25) > adjusted_bar(1)


def test_adjusted_bar_handles_zero_comparisons():
    assert adjusted_bar(0) == pytest.approx(1.96, abs=0.01)


# ── the protocol ─────────────────────────────────────────────────────────

def _samples(edges, day="01-01"):
    return [{"ts": float(i), "day": day, "edge_taker": e, "edge_maker": e}
            for i, e in enumerate(edges)]


def test_assess_dies_when_a_window_is_negative():
    """3/3 windows is the bar that rejected the stop-loss and the taker
    variant of settle_bot."""
    import random
    rnd = random.Random(1)
    edges = ([0.05 + rnd.gauss(0, 0.02) for _ in range(60)]
             + [0.05 + rnd.gauss(0, 0.02) for _ in range(60)]
             + [-0.05 + rnd.gauss(0, 0.02) for _ in range(60)])
    r = assess(_samples(edges), band=(0.0, 0.5), comparisons=1)
    assert r["verdict"] == "DIED"
    assert "windows" in r["failed"]
    assert r["windows_positive"] == 2


def test_assess_dies_when_t_is_below_the_adjusted_bar():
    edges = [0.01, -0.01] * 60          # mean ~0, t ~0
    r = assess(_samples(edges), band=(0.0, 0.5), comparisons=1)
    assert r["verdict"] == "DIED" and "t" in r["failed"]


def test_assess_dies_when_the_edge_is_outside_its_declared_band():
    """Outside the band means the implementation does not match the rule that
    was measured -- a bug, not a new finding."""
    import random
    rnd = random.Random(2)
    edges = [0.40 + rnd.gauss(0, 0.02) for _ in range(180)]
    r = assess(_samples(edges), band=(0.02, 0.05), comparisons=1)
    assert r["verdict"] == "DIED" and "band" in r["failed"]


def test_assess_dies_when_two_good_days_carry_it():
    days = []
    for d in range(10):
        day = f"01-{d:02d}"
        edges = ([0.60] * 12 if d < 2
                 else [-0.02 + (0.001 if i % 2 else -0.001) for i in range(12)])
        days += [{"ts": float(d * 100 + i), "day": day,
                  "edge_taker": e, "edge_maker": e} for i, e in enumerate(edges)]
    r = assess(days, band=(0.0, 0.9), comparisons=1)
    assert r["verdict"] == "DIED" and "drop2" in r["failed"]


def test_assess_survives_a_genuinely_robust_edge():
    edges = [0.05, 0.03, 0.04, 0.06] * 60
    days = [{"ts": float(i), "day": f"01-{i % 12:02d}",
             "edge_taker": e, "edge_maker": e} for i, e in enumerate(edges)]
    r = assess(days, band=(0.02, 0.08), comparisons=1)
    assert r["verdict"] == "SURVIVED" and r["failed"] == []


def test_assess_flags_maker_only_survivors_as_fill_dependent():
    """settle_bot cleared on the maker basis and died live to adverse
    selection. A maker-only pass is a warning, not a pass."""
    import random
    rnd = random.Random(3)
    s = [{"ts": float(i), "day": f"01-{i % 12:02d}",
          "edge_taker": -0.01 + rnd.gauss(0, 0.30),
          "edge_maker": 0.05 + rnd.gauss(0, 0.02)} for i in range(400)]
    r = assess(s, band=(0.0, 0.9), comparisons=1)
    assert r["verdict"] == "DIED"
    assert r["fill_dependent"] is True


def test_assess_needs_enough_samples():
    r = assess(_samples([0.05] * 5), band=(0.0, 0.5), comparisons=1)
    assert r["verdict"] == "DIED" and "samples" in r["failed"]


# ── the registry ─────────────────────────────────────────────────────────

def test_registry_counts_comparisons(tmp_path):
    reg = Registry(tmp_path / "registry.jsonl")
    assert reg.comparisons() == 0
    reg.record({"name": "a", "hash": "h1", "verdict": "DIED"})
    reg.record({"name": "b", "hash": "h2", "verdict": "DIED"})
    assert reg.comparisons() == 2


def test_registry_refuses_to_retest_a_dead_predicate(tmp_path):
    """Disproofs should stop being re-derived."""
    reg = Registry(tmp_path / "registry.jsonl")
    reg.record({"name": "stop-loss 0.5", "hash": "deadbeef", "verdict": "DIED"})
    assert reg.is_dead("deadbeef") is True
    assert reg.is_dead("neverseen") is False


def test_registry_does_not_block_a_previous_survivor(tmp_path):
    reg = Registry(tmp_path / "registry.jsonl")
    reg.record({"name": "good", "hash": "cafe", "verdict": "SURVIVED"})
    assert reg.is_dead("cafe") is False


def test_predicate_hash_is_stable_and_content_addressed():
    def a(row):
        return "YES" if row.get("x", 0) > 0 else None
    def b(row):
        return "YES" if row.get("x", 0) > 0 else None
    def c(row):
        return "NO" if row.get("x", 0) > 0 else None
    assert predicate_hash(a) == predicate_hash(b)
    assert predicate_hash(a) != predicate_hash(c)


# ── latency: the lesson the spec predated ────────────────────────────────

def test_assess_flags_latency_dependent_edges_and_kills_them():
    """The momentum rule scored +0.0567 (t=4.49, 3/3) at the signal tick and
    decayed to nothing by 20s — half gone in 5s, against a 5.1s tick cadence.
    A harness blind to action delay would have certified it."""
    import random
    rnd = random.Random(5)
    s = [{"ts": float(i), "day": f"01-{i % 12:02d}",
          "edge_taker": -0.005 + rnd.gauss(0, 0.30),   # acting late
          "edge_taker_instant": 0.06 + rnd.gauss(0, 0.02),  # the tick promised
          "edge_maker": -0.005 + rnd.gauss(0, 0.30)} for i in range(400)]
    r = assess(s, band=(0.0, 0.9), comparisons=1)
    assert r["verdict"] == "DIED"
    assert r["latency_dependent"] is True


def test_assess_does_not_flag_latency_when_the_edge_survives_the_delay():
    import random
    rnd = random.Random(11)
    s = [{"ts": float(i), "day": f"01-{i % 12:02d}",
          "edge_taker": 0.05 + rnd.gauss(0, 0.02),
          "edge_taker_instant": 0.055 + rnd.gauss(0, 0.02),
          "edge_maker": 0.05 + rnd.gauss(0, 0.02)} for i in range(400)]
    r = assess(s, band=(0.02, 0.08), comparisons=1)
    assert r["verdict"] == "SURVIVED" and r["latency_dependent"] is False


def test_declared_hypotheses_are_visible_through_the_module_namespace():
    """Run as a script, hypothesis_gate is __main__ while hypotheses.py
    imports it AGAIN as a module — so decorators register into a different
    namespace than a naive CLI would read, and --list silently shows nothing.
    The CLI must consult the module object, not its own globals."""
    import hypothesis_gate as hg
    import hypotheses  # noqa: F401 — importing registers them
    assert len(hg._REGISTERED) >= 5
    assert "NULL control: always YES" in hg._REGISTERED


def test_flags_fire_when_the_verdict_flips_not_only_when_the_edge_goes_negative():
    """Caught by the real acceptance run: 'follow momentum' scored +0.0573
    instant (clears) and +0.0233 delayed (fails) — unambiguously latency-
    dependent, yet a sign-based flag stayed silent because +0.0233 > 0. The
    flag must mean 'this passes only under the optimistic assumption'."""
    import random
    rnd = random.Random(7)
    s = [{"ts": float(i), "day": f"01-{i % 12:02d}",
          "edge_taker": 0.004 + rnd.gauss(0, 0.30),
          "edge_taker_instant": 0.06 + rnd.gauss(0, 0.05),
          "edge_maker": 0.06 + rnd.gauss(0, 0.05)} for i in range(300)]
    r = assess(s, band=(0.0, 0.9), comparisons=1)
    assert r["verdict"] == "DIED"
    assert r["latency_dependent"] is True
    assert r["fill_dependent"] is True


def test_degenerate_constant_input_cannot_manufacture_significance():
    """Constant edges leave se at ~1e-20 from float error, not 0. A naive
    truthiness guard let t reach 1e16 and reported SURVIVED."""
    s = [{"ts": float(i), "day": f"01-{i % 12:02d}",
          "edge_taker": 0.004, "edge_taker_instant": 0.004,
          "edge_maker": 0.004} for i in range(300)]
    r = assess(s, band=(0.0, 0.9), comparisons=1)
    assert r["verdict"] == "DIED" and "degenerate" in r["failed"]
    assert abs(r["t"]) < 1.0
