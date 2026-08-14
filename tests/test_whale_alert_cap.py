"""The whale_alerts buffer: aged out, newest-first, and count-capped.

`whale_alerts` was bounded only by a 4-hour time window, with no cap on
count. Measured on the live scanner 2026-08-11 via /api/debug/memory:

    whale_alerts   665,889
    gc_objects     741,802

90% of every live Python object in the process, at RSS 1553MB against a
fresh process's 86MB. It is also rescanned in full, per ticker, per cycle
by `_ewma_whale_flow` (alpha.py) and the flow block in web.py.

Retaining 4h is arithmetically pointless for those consumers: the half
lives are 10 and 8 minutes, so a 4h-old alert carries a weight of ~6e-8.
Nothing else needs the depth either -- every consumer takes the head
(`rows[:80]`, `[:200]`, `[:50]`, `[:limit]`).

Hence a count cap. The trap it has to avoid is dropping the WRONG end: a
cap keeping the oldest 150k satisfies any length assertion while
destroying every decayed signal in the system, so ordering is asserted
explicitly here.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scanner as scanner_mod
from scanner import Scanner, WhaleAlert

NOW = datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc)


def _alert(age_min, ticker="KXBTC15M-A", contracts=100.0):
    return WhaleAlert(ticker=ticker, contracts=contracts, price=0.5, side="yes",
                      taker_side="ask", timestamp=NOW - timedelta(minutes=age_min))


def _scanner():
    return Scanner(api=None)


def test_ages_out_alerts_beyond_the_window():
    s = _scanner()
    s.whale_alerts = [_alert(300)]                    # 5h old
    out = s._merge_whale_alerts([_alert(1)], now=NOW.timestamp())
    assert len(out) == 1
    assert out[0].timestamp == NOW - timedelta(minutes=1)


def test_keeps_alerts_inside_the_window():
    s = _scanner()
    s.whale_alerts = [_alert(200)]                    # 3h20m, inside 4h
    assert len(s._merge_whale_alerts([], now=NOW.timestamp())) == 1


def test_drops_alerts_with_no_timestamp():
    s = _scanner()
    bad = _alert(1)
    bad.timestamp = None
    assert s._merge_whale_alerts([bad], now=NOW.timestamp()) == []


def test_count_cap_bounds_the_buffer():
    s = _scanner()
    s.whale_alerts = [_alert(10) for _ in range(5000)]
    out = s._merge_whale_alerts([], now=NOW.timestamp(), max_alerts=100)
    assert len(out) == 100


def test_cap_keeps_the_NEWEST_and_drops_the_oldest():
    """The ordering trap. Every consumer takes the head of this list and
    every analytical one decays at a <=10-minute half-life, so a cap that
    kept the oldest would pass a length check and still destroy the
    signals."""
    s = _scanner()
    s.whale_alerts = [_alert(m, ticker=f"OLD-{m}") for m in range(100, 200)]
    new = [_alert(1, ticker="NEWEST")]
    out = s._merge_whale_alerts(new, now=NOW.timestamp(), max_alerts=10)

    assert len(out) == 10
    assert out[0].ticker == "NEWEST"
    assert not any(a.ticker == "OLD-199" for a in out), "dropped the wrong end"


def test_newest_first_ordering_is_preserved():
    """web.py `[:80]`, alpha.py `[:200]` and main.py `[:50]` all assume the
    head is the most recent."""
    s = _scanner()
    s.whale_alerts = [_alert(30, ticker="OLDER")]
    out = s._merge_whale_alerts([_alert(1, ticker="NEWER")], now=NOW.timestamp())
    assert [a.ticker for a in out] == ["NEWER", "OLDER"]


def test_cap_does_not_bind_in_normal_operation():
    """Live count on a quiet evening was ~17,877. The shipped default must
    leave that untouched."""
    s = _scanner()
    s.whale_alerts = [_alert(10) for _ in range(17_877)]
    assert len(s._merge_whale_alerts([], now=NOW.timestamp())) == 17_877


def test_default_cap_holds_under_the_measured_burst_rate():
    """Sustained arrivals at the observed burst rate must stay bounded.

    665,889 alerts inside a 4h window is ~166k/hour. Simulated here as
    repeated merges so the buffer never exceeds the shipped default.
    """
    s = _scanner()
    per_merge = 5000
    for cycle in range(80):                    # 400k arrivals
        s.whale_alerts = s._merge_whale_alerts(
            [_alert(0.01) for _ in range(per_merge)], now=NOW.timestamp())
        assert len(s.whale_alerts) <= scanner_mod.MAX_WHALE_ALERTS

    assert len(s.whale_alerts) == scanner_mod.MAX_WHALE_ALERTS


def test_scan_trades_applies_the_cap_on_the_production_path():
    """The cap has to run where production runs it -- inside scan_trades --
    not merely be available as a helper nobody calls."""
    class FakeAPI:
        def get_trades(self, **kw):
            return {"trades": [{"trade_id": f"t{i}", "ticker": "KXBTC15M-A",
                                "count_fp": "100", "yes_price_dollars": "0.50",
                                "taker_outcome_side": "yes",
                                "created_time": NOW.strftime("%Y-%m-%dT%H:%M:%SZ")}
                               for i in range(500)], "cursor": ""}

    s = Scanner(api=FakeAPI(), whale_threshold=50)
    s.whale_alerts = [_alert(10) for _ in range(1000)]
    scanner_mod.MAX_WHALE_ALERTS, saved = 200, scanner_mod.MAX_WHALE_ALERTS
    try:
        s.scan_trades()
        assert len(s.whale_alerts) <= 200
    finally:
        scanner_mod.MAX_WHALE_ALERTS = saved
