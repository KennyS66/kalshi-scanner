"""Tests for memdiag — the instrument, so it can't lie to us.

The 2026-08-09 leak hunt burned a day on a container audit because RssAnon
private-dirty was mistaken for proof of retention. It isn't: freed-but-
unreturned glibc arenas look identical. These tests pin the two things the
report has to get right — the retention/ratchet discriminator, and the
container census — so the next investigation starts from data.
"""
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import memdiag


def test_type_histogram_counts_by_type_name_descending():
    objs = [1, 2, 3, "a", "b", {}, [], []]
    hist = memdiag.type_histogram(objs, top=3)
    assert hist[0] == ("int", 3)
    assert dict(hist)["str"] == 2
    assert len(hist) == 3


def test_type_histogram_top_limits_output():
    assert len(memdiag.type_histogram([1, "a", {}, [], set()], top=2)) == 2


def test_container_sizes_reports_len_of_sized_globals_only():
    ns = {"_cache": {"a": 1, "b": 2}, "_hist": deque([1, 2, 3]),
          "_rows": [1], "_n": 42, "_fn": len, "__builtins__": {}}
    sizes = memdiag.container_sizes(ns)
    assert sizes == {"_cache": 2, "_hist": 3, "_rows": 1}


def test_container_sizes_skips_dunder_and_unsized():
    ns = {"__name__": "web", "_x": object(), "_ok": [1, 2]}
    assert memdiag.container_sizes(ns) == {"_ok": 2}


def test_container_sizes_survives_a_len_that_raises():
    class Hostile:
        def __len__(self):
            raise RuntimeError("boom")
    assert memdiag.container_sizes({"_bad": Hostile(), "_good": [1]}) == {"_good": 1}


def test_container_sizes_never_raises_on_mutation_during_iteration():
    """Contract: a diagnostic must not blow up on the sick process it inspects.

    web.py's globals are mutated by 15 threads while this reads them. A
    partial census is fine; an exception is not.
    """
    class Mutating(dict):
        def items(self):
            for k, v in super().items():
                self[f"_new{k}"] = [1]      # a poller adding a cache mid-read
                yield k, v

    assert isinstance(memdiag.container_sizes(Mutating({"_c": [1]})), dict)


def test_container_sizes_reads_a_plain_dict_completely():
    """The retry must not cost us the normal case: plain dicts census fully."""
    ns = {f"_c{i}": [i] for i in range(50)}
    assert len(memdiag.container_sizes(ns)) == 50


def test_verdict_ratchet_when_trim_returns_most_of_the_growth():
    assert memdiag.verdict(rss_before=1800.0, rss_after=900.0) == "ratchet"


def test_verdict_retention_when_trim_returns_little():
    assert memdiag.verdict(rss_before=1800.0, rss_after=1790.0) == "retention"


def test_verdict_needs_both_readings():
    assert memdiag.verdict(1800.0, None) is None


def test_verdict_boundary_is_ten_percent():
    # Exactly 10% returned is not yet a ratchet; just over it is.
    assert memdiag.verdict(1000.0, 900.0) == "retention"
    assert memdiag.verdict(1000.0, 899.0) == "ratchet"


def test_verdict_ignores_a_negative_delta():
    """RSS can rise between the two reads on a live process; that is not a ratchet."""
    assert memdiag.verdict(1000.0, 1010.0) == "retention"


def test_rss_mb_reads_the_live_process():
    v = memdiag.rss_mb()
    assert isinstance(v, float) and v > 0
