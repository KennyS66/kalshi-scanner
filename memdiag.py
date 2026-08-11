#!/usr/bin/env python3
"""Memory diagnostics for the scanner process — retention vs. arena ratchet.

Written 2026-08-10, during the third investigation into the scanner's RSS
climbing to 1.8GB+ over a day. The first two burned time on the wrong
question, so this module exists to answer the right one first.

THE TRAP THIS EXISTS TO AVOID: `RssAnon` / `Private_Dirty` being 100% of RSS
looks like proof that Python objects are being retained. It is not. Memory
that has been freed by Python but not returned to the OS by glibc is *also*
anonymous and private-dirty. The two are indistinguishable from outside the
process, and they need completely different fixes:

    retention  -> find the container that grows; prune it
    ratchet    -> allocation churn fragmenting the heap; the fix is
                  malloc_trim / MALLOC_ARENA_MAX / not allocating the
                  burst in the first place. No container hunt will ever
                  find anything, because nothing is being retained.

`malloc_trim(0)` is the discriminator: it asks glibc to hand freed memory
back to the OS. If RSS drops sharply, the memory was already free and the
answer is "ratchet". If it barely moves, something really is holding it and
the type histogram says what.

A harness reproduction on this repo's own allocation profile (4MB read +
~30k json.loads, as /api/crypto/candles does) ratcheted +11.6MB, of which
gc.collect() returned 0.0MB and malloc_trim returned 11.5MB — 99%.
"""
from __future__ import annotations

import ctypes
import gc
from collections import Counter, deque

# Types worth a census: the sized containers a cache is ever built from.
SIZED = (dict, list, set, frozenset, tuple, bytearray, deque)

# Fraction of RSS that must come back for the drop to mean anything. Well
# above measurement noise on a live process (which allocates during the two
# reads), well below the ~99% a real ratchet returns.
RATCHET_MIN_RETURNED = 0.10


def rss_mb() -> float:
    """Resident set size of THIS process, in MB."""
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024.0
    return 0.0


def type_histogram(objects, top: int = 25) -> list[tuple[str, int]]:
    """(type_name, count) for the live objects, most numerous first.

    Takes the object list rather than calling gc.get_objects() itself so it
    stays pure and testable.
    """
    return Counter(type(o).__name__ for o in objects).most_common(top)


def container_sizes(namespace: dict) -> dict[str, int]:
    """len() of every sized, non-dunder value in a module namespace.

    The census that says which module-level cache is growing. Anything
    whose len() raises is skipped rather than taking the whole report down
    with it — a diagnostic that crashes when the process is sick is worse
    than none.
    """
    # Materialise first: this reads a live module's globals() while 15 other
    # threads mutate them, and iterating the dict directly raises
    # "dictionary changed size during iteration" — precisely when the
    # process is unhealthy and the report matters most. For a plain dict
    # list() is atomic under the GIL; the retry covers anything exotic, and
    # a partial census beats an exception either way.
    items = []
    for _ in range(3):
        try:
            items = list(namespace.items())
            break
        except RuntimeError:
            continue

    out = {}
    for name, value in items:
        if name.startswith("__"):
            continue
        if not isinstance(value, SIZED):
            continue
        try:
            out[name] = len(value)
        except Exception:
            continue
    return out


def verdict(rss_before: float, rss_after: float | None) -> str | None:
    """"ratchet" | "retention" | None, from RSS either side of malloc_trim.

    A negative delta (RSS grew between the reads, which a live process can
    do) is not a ratchet.
    """
    if rss_before is None or rss_after is None:
        return None
    returned = rss_before - rss_after
    if returned <= 0:
        return "retention"
    return "ratchet" if returned / rss_before > RATCHET_MIN_RETURNED else "retention"


def malloc_trim() -> bool:
    """Ask glibc to return free heap to the OS. False where unavailable."""
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
        return True
    except Exception:
        return False


def snapshot(namespaces: dict[str, dict] | None = None,
             trim: bool = False, top: int = 25) -> dict:
    """Full memory report. `trim=True` runs the discriminator.

    `namespaces` maps a label to a module's globals() for the container
    census, e.g. {"web": vars(web), "scanner": vars(scanner)}.
    """
    objects = gc.get_objects()
    report = {
        "rss_mb": round(rss_mb(), 1),
        "gc_objects": len(objects),
        "gc_counts": gc.get_count(),
        "gc_collected_now": gc.collect(),
        "types": type_histogram(objects, top=top),
        "containers": {label: container_sizes(ns)
                       for label, ns in (namespaces or {}).items()},
    }
    del objects
    if trim:
        before = rss_mb()
        report["trim"] = {
            "ran": malloc_trim(),
            "rss_before_mb": round(before, 1),
        }
        after = rss_mb() if report["trim"]["ran"] else None
        report["trim"]["rss_after_mb"] = round(after, 1) if after else None
        report["trim"]["returned_mb"] = round(before - after, 1) if after else None
        report["trim"]["verdict"] = verdict(before, after)
    return report
