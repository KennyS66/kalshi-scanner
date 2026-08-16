"""`_seen_trade_ids` retention: tied to the re-fetch horizon, not 24 hours.

`_seen_trade_ids` maps trade_id -> epoch-first-seen and exists for exactly one
purpose: skip trades a previous scan already processed. It was pruned with a
hardcoded 86400s (24h) cutoff. Measured on the live scanner 2026-08-14, pid
10068 (steady state, confirmed twice 45s apart):

    _seen_trade_ids   9,013,616 entries
    bytes/entry       159 (36-char UUID key + float value + dict overhead)
    that dict         1,373 MB
    process RSS       1,631 MB      -> 84% of the whole process

The same dict caused the CPU: rebuilding it costs ~2.9s and the rebuild runs
every scan cycle (nominal 5s), i.e. ~58% of a core against 61% observed.

24h is ~24x beyond anything reachable. `scan_trades` asks the API for
`min_ts = self.last_trade_ts or (now - lookback_minutes*60)`, so it never
requests trades older than `lookback_minutes` (60 by default) and normally
only reaches back to the previous scan's start. Dedup therefore cannot need
more history than the lookback window.

It hid for so long because gc_objects sat flat at ~225k across the entire
growth curve: gc.get_objects() only tracks containers, so nine million str
keys are invisible to it and every object-counting diagnostic showed a
healthy process.
"""
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scanner as scanner_mod
from scanner import Scanner


class EmptyAPI:
    """No trades. The prune sits after the paging loop and runs
    unconditionally, so an empty tape still exercises it -- which isolates
    retention from ingestion."""

    def get_trades(self, **kw):
        return {"trades": [], "cursor": ""}


def _scanner(lookback_minutes=60, api=None):
    return Scanner(api=api or EmptyAPI(), lookback_minutes=lookback_minutes)


def test_entry_older_than_the_lookback_window_is_dropped():
    """The load-bearing case. A 2h-old id is far inside the old 86400s window
    but well past a 60min re-fetch horizon, so it is dead weight."""
    s = _scanner(lookback_minutes=60)
    s._seen_trade_ids["old"] = time.time() - 7200
    s.scan_trades()
    assert "old" not in s._seen_trade_ids


def test_entry_inside_the_lookback_window_survives():
    """The other half: pruning must not eat ids the next scan could re-fetch."""
    s = _scanner(lookback_minutes=60)
    s._seen_trade_ids["recent"] = time.time() - 600
    s.scan_trades()
    assert "recent" in s._seen_trade_ids


def test_retention_derives_from_lookback_and_not_from_86400():
    s = _scanner(lookback_minutes=60)
    assert s._seen_id_retention_s() == 3600
    assert s._seen_id_retention_s() != 86400
    assert _scanner(lookback_minutes=240)._seen_id_retention_s() == 14400


def test_a_larger_lookback_widens_the_window():
    """Retention has to track the horizon in BOTH directions. A 4h lookback
    can genuinely re-fetch 4h back, so a 2h-old id must still be remembered --
    a fixed small constant would be as wrong as the fixed large one."""
    s = _scanner(lookback_minutes=240)
    s._seen_trade_ids["old"] = time.time() - 7200
    s.scan_trades()
    assert "old" in s._seen_trade_ids


def test_floor_applies_when_lookback_is_small():
    """`--lookback 1` must not shrink dedup to 60s. The scan loop only has to
    be briefly slow for trades to be re-processed as new."""
    s = _scanner(lookback_minutes=1)
    assert s._seen_id_retention_s() == scanner_mod.SEEN_ID_RETENTION_FLOOR_S

    s._seen_trade_ids["within_floor"] = time.time() - 300   # 5min >> 1min
    s.scan_trades()
    assert "within_floor" in s._seen_trade_ids


def test_the_floor_is_a_floor_and_not_a_second_hardcoded_day():
    """The floor bounds the small end only; it must never reintroduce the leak."""
    assert scanner_mod.SEEN_ID_RETENTION_FLOOR_S == 900

    s = _scanner(lookback_minutes=1)
    s._seen_trade_ids["past_floor"] = time.time() - 1800    # 30min > 15min floor
    s.scan_trades()
    assert "past_floor" not in s._seen_trade_ids


def test_only_the_window_survives_a_24h_spread():
    """Shape of the measured impact. Ids arrive at a steady rate, so a 60min
    window keeps ~1/24 of what the 24h window kept: live that is 9,013,616 ->
    ~375k, i.e. 1,373 MB -> ~57 MB at the measured 159 bytes/entry."""
    s = _scanner(lookback_minutes=60)
    now = time.time()
    total = 24_000                                  # one id per 3.6s of a 24h span
    for i in range(total):
        s._seen_trade_ids[f"t{i}"] = now - (86400 * i / total)

    s.scan_trades()

    kept = len(s._seen_trade_ids)
    assert 950 <= kept <= 1050, f"kept {kept}, expected ~1/24 of {total}"


def test_dedup_still_skips_ids_seen_on_a_previous_scan():
    """Shrinking the window must not break what the dict is FOR."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    class SameTradeAPI:
        def get_trades(self, **kw):
            return {"trades": [{"trade_id": "T1", "ticker": "KXBTC15M-A",
                                "count_fp": "100", "yes_price_dollars": "0.50",
                                "taker_outcome_side": "yes",
                                "created_time": now_iso}], "cursor": ""}

    s = Scanner(api=SameTradeAPI(), whale_threshold=50, lookback_minutes=60)
    assert s.scan_trades()[1] == 1        # first scan: genuinely new
    assert s.scan_trades()[1] == 0        # second scan: deduped, not re-counted
