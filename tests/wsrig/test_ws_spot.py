import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from wsrig.ws_spot import parse_ticker


def test_parses_a_ticker_message():
    r = parse_ticker({"type": "ticker", "product_id": "BTC-USD",
                      "price": "63416.52", "time": "2026-08-13T22:00:00.123456Z"})
    assert r["k"] == "spot"
    assert r["p"] == 63416.52
    assert isinstance(r["tx"], float)
    assert r["tx"] > 1_700_000_000        # parsed to an epoch, not left as a string


def test_ignores_non_ticker_messages():
    assert parse_ticker({"type": "subscriptions", "channels": []}) is None
    assert parse_ticker({"type": "heartbeat"}) is None


def test_ignores_a_malformed_price():
    assert parse_ticker({"type": "ticker", "product_id": "BTC-USD",
                         "price": "not-a-number"}) is None
    assert parse_ticker({"type": "ticker", "product_id": "BTC-USD"}) is None


def test_missing_exchange_time_yields_null_not_a_guess():
    """Never substitute local time for exchange time — that would hide feed lag,
    which is precisely what this rig measures."""
    r = parse_ticker({"type": "ticker", "product_id": "BTC-USD", "price": "1.0"})
    assert r["tx"] is None


def test_records_the_product_so_eth_cannot_be_mistaken_for_btc():
    r = parse_ticker({"type": "ticker", "product_id": "ETH-USD", "price": "1884.0"})
    assert r["sym"] == "ETH-USD"
