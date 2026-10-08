import urllib.error

import kalshi_backfill as kb


def test_429_pauses_all_workers_then_retries(monkeypatch):
    clock = [1000.0]
    sleeps = []
    monkeypatch.setattr(kb.time, "time", lambda: clock[0])
    monkeypatch.setattr(kb.time, "sleep", lambda s: (sleeps.append(s), clock.__setitem__(0, clock[0] + s)))
    monkeypatch.setattr(kb, "_last_req", 0.0)
    monkeypatch.setattr(kb, "_pause_until", 0.0)
    calls = []

    class _R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"ok": 1}'

    def fake_urlopen(req, timeout):
        calls.append(req.full_url)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)
        return _R()
    monkeypatch.setattr(kb.urllib.request, "urlopen", fake_urlopen)
    assert kb._get("https://x/markets/trades") == {"ok": 1}
    assert len(calls) == 2
    assert max(sleeps) >= kb.PAUSE_ON_429_S - 1      # the shared pause was honoured
    assert kb._pause_until >= 1000.0 + kb.PAUSE_ON_429_S


def test_default_rate_is_well_under_shared_budget():
    assert 1 / kb.MIN_GAP_S <= 2.0
