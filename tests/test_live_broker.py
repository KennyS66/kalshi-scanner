import pytest
from unittest import mock


def _fake_pk():
    """A real key isn't needed to test request construction/parsing --
    only that place_order/get_order/cancel_order build the right request
    and parse the response, so the actual .sign() call is mocked too."""
    pk = mock.Mock()
    pk.sign.return_value = b"fake-signature-bytes"
    return pk


def test_place_order_posts_signed_request_and_returns_order(monkeypatch):
    import live_broker
    monkeypatch.setattr(live_broker.account, "_load_env",
                        lambda: ("key-id-123", "/fake/path.pem"))
    monkeypatch.setattr(live_broker, "_load_private_key", lambda path: _fake_pk())

    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        r = mock.Mock()
        r.raise_for_status = lambda: None
        r.json = lambda: {"order": {"order_id": "abc-123", "status": "resting"}}
        return r

    monkeypatch.setattr(live_broker.requests, "post", fake_post)

    result = live_broker.place_order("yes", "buy", "KXBTC15M-26JUL290300-00",
                                     1, 0.31, "limit")
    assert result["order_id"] == "abc-123"
    assert captured["url"].endswith("/trade-api/v2/portfolio/orders")
    assert captured["headers"]["KALSHI-ACCESS-KEY"] == "key-id-123"
    assert "KALSHI-ACCESS-SIGNATURE" in captured["headers"]
    body = captured["json"]
    assert body["side"] == "yes"
    assert body["action"] == "buy"
    assert body["ticker"] == "KXBTC15M-26JUL290300-00"
    assert body["count"] == 1
    assert body["type"] == "limit"
    assert body["yes_price"] == 31   # cents, per Kalshi's integer-cents API


def test_get_order_returns_status(monkeypatch):
    import live_broker
    monkeypatch.setattr(live_broker.account, "_load_env",
                        lambda: ("key-id-123", "/fake/path.pem"))
    monkeypatch.setattr(live_broker, "_load_private_key", lambda path: _fake_pk())

    def fake_get(url, headers=None, timeout=None):
        r = mock.Mock()
        r.raise_for_status = lambda: None
        r.json = lambda: {"order": {"order_id": "abc-123", "status": "executed"}}
        return r

    monkeypatch.setattr(live_broker.requests, "get", fake_get)
    result = live_broker.get_order("abc-123")
    assert result["status"] == "executed"


def test_cancel_order_sends_delete(monkeypatch):
    import live_broker
    monkeypatch.setattr(live_broker.account, "_load_env",
                        lambda: ("key-id-123", "/fake/path.pem"))
    monkeypatch.setattr(live_broker, "_load_private_key", lambda path: _fake_pk())

    captured = {}

    def fake_delete(url, headers=None, timeout=None):
        captured["url"] = url
        r = mock.Mock()
        r.raise_for_status = lambda: None
        r.json = lambda: {"order": {"order_id": "abc-123", "status": "canceled"}}
        return r

    monkeypatch.setattr(live_broker.requests, "delete", fake_delete)
    result = live_broker.cancel_order("abc-123")
    assert result["status"] == "canceled"
    assert captured["url"].endswith("/trade-api/v2/portfolio/orders/abc-123")


def test_place_order_raises_on_http_error(monkeypatch):
    import live_broker
    monkeypatch.setattr(live_broker.account, "_load_env",
                        lambda: ("key-id-123", "/fake/path.pem"))
    monkeypatch.setattr(live_broker, "_load_private_key", lambda path: _fake_pk())

    def fake_post(url, headers=None, json=None, timeout=None):
        r = mock.Mock()
        r.raise_for_status = mock.Mock(
            side_effect=Exception("400 Bad Request: insufficient balance"))
        return r

    monkeypatch.setattr(live_broker.requests, "post", fake_post)
    with pytest.raises(Exception, match="insufficient balance"):
        live_broker.place_order("yes", "buy", "T", 1, 0.31, "limit")
