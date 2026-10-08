"""A rate-limited trades page must not kill the scan cycle.

2026-10-07/08: a bulk backfill sharing the scanner's IP pushed
/markets/trades into HTTP 429. scan_trades raised on the first failed page,
so whale_alerts never merged and the dashboard loop skipped enrich_markets:
market_snapshots stayed 0 and every live signal row lost its whale data for
~3 hours, with the error drawn only into a Rich panel nobody could see.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scanner import Scanner

NOW_ISO = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _trade(tid, count="10"):
    return {"trade_id": tid, "ticker": "KXBTC15M-A", "count_fp": count,
            "yes_price_dollars": "0.50", "taker_outcome_side": "yes",
            "taker_book_side": "ask", "created_time": NOW_ISO}


def _http_429():
    resp = SimpleNamespace(status_code=429)
    return requests.exceptions.HTTPError("429 Client Error: Too Many Requests", response=resp)


class FlakyAPI:
    """Page 1 succeeds (with a whale), page 2 is rate limited."""

    def __init__(self):
        self.calls = 0

    def get_trades(self, **kw):
        self.calls += 1
        if self.calls == 1:
            return {"trades": [_trade("w1", count="80"), _trade("t2")], "cursor": "c1"}
        raise _http_429()


def test_rate_limited_page_keeps_fetched_trades_and_whales(capsys):
    s = Scanner(api=FlakyAPI(), whale_threshold=50, lookback_minutes=60)
    whales, n = s.scan_trades()                     # must not raise
    assert n == 2 and len(whales) == 1
    assert len(s.whale_alerts) == 1                 # merged despite the error
    assert s.scan_errors == 1 and "429" in s.last_scan_error
    assert "scanner WARNING: trade scan" in capsys.readouterr().err


def test_rate_limited_scan_does_not_skip_unfetched_tape():
    # Advancing last_trade_ts past a failed page would drop those trades for
    # good; keep the old floor so the next cycle refetches (ids dedupe).
    s = Scanner(api=FlakyAPI(), whale_threshold=50, lookback_minutes=60)
    s.scan_trades()
    floor = s.last_trade_ts
    assert floor is not None
    s.api = SimpleNamespace(get_trades=lambda **kw: {
        "trades": [_trade("w1", count="80"), _trade("t3")], "cursor": ""})
    _, n = s.scan_trades()
    assert n == 1                                   # w1 deduped, t3 new
    assert len(s.whale_alerts) == 1                 # whale not double counted
    assert s.last_trade_ts > floor - 1


def test_api_get_retries_429_then_succeeds(monkeypatch):
    import api as api_mod
    sleeps = []
    monkeypatch.setattr(api_mod.time, "sleep", lambda s: sleeps.append(s))
    a = api_mod.KalshiAPI()
    seq = [SimpleNamespace(status_code=429, headers={}),
           SimpleNamespace(status_code=200, headers={},
                           raise_for_status=lambda: None, json=lambda: {"ok": 1})]
    seq[0].raise_for_status = lambda: (_ for _ in ()).throw(_http_429())
    monkeypatch.setattr(a.session, "get", lambda *x, **k: seq.pop(0))
    assert a._get("/markets/trades") == {"ok": 1}
    assert len(sleeps) == 1


def test_api_get_gives_up_after_retries(monkeypatch):
    import api as api_mod
    monkeypatch.setattr(api_mod.time, "sleep", lambda s: None)
    a = api_mod.KalshiAPI()

    def always_429(*x, **k):
        r = SimpleNamespace(status_code=429, headers={})
        r.raise_for_status = lambda: (_ for _ in ()).throw(_http_429())
        return r
    monkeypatch.setattr(a.session, "get", always_429)
    with pytest.raises(requests.exceptions.HTTPError):
        a._get("/markets/trades")
