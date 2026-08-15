"""The REST glue wsrig depends on, asserted against the real KalshiAPI.

The wsrig tests use fake clients that accept any kwarg, so nothing there can
catch a parameter that never reaches the wire. These tests drive the actual
`get_markets` through `requests`' own URL preparation instead.

`api.py` is shared with the live scanner and swing bot, so the last test here
pins the default call byte for byte.
"""
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import requests

from api import KalshiAPI


class _Resp:
    @staticmethod
    def raise_for_status():
        return None

    @staticmethod
    def json():
        return {"markets": []}


class _RecordingSession:
    """Prepares the request the way requests really does, and records the URL.

    Asserting on the params dict would not prove anything: `status=None` is
    present in that dict and is dropped later, during preparation.
    """

    def __init__(self):
        self.url = None

    def get(self, url, params=None, headers=None, timeout=None):
        self.url = requests.Request("GET", url, params=params).prepare().url
        return _Resp()


def _query(**kwargs):
    api = KalshiAPI()
    session = _RecordingSession()
    api.session = session
    api.get_markets(**kwargs)
    return parse_qs(urlparse(session.url).query)


def test_series_ticker_is_sent_when_given():
    """Without it the listing never contains KXBTC15M at all."""
    assert _query(series_ticker="KXBTC15M")["series_ticker"] == ["KXBTC15M"]


def test_status_none_sends_no_status_filter_at_all():
    """run_tracker relies on this: any status filter hides the pre-open
    ("initialized") markets the lookahead exists to catch."""
    assert "status" not in _query(status=None, series_ticker="KXBTC15M")


def test_the_close_time_window_is_sent_when_given():
    q = _query(status=None, series_ticker="KXBTC15M",
               min_close_ts=1786766907, max_close_ts=1786768107)
    assert q["min_close_ts"] == ["1786766907"]
    assert q["max_close_ts"] == ["1786768107"]


def test_a_zero_min_close_ts_is_still_sent():
    """Guard against an `if min_close_ts:` truthiness check — 0 is a real epoch
    bound and dropping it would silently widen the window to everything."""
    assert _query(min_close_ts=0)["min_close_ts"] == ["0"]


def test_the_new_parameters_are_absent_unless_asked_for():
    """Strictly additive: the live scanner's existing calls must be unchanged."""
    q = _query()
    assert q == {"limit": ["200"], "status": ["open"]}
