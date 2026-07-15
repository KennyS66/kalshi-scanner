import json
from bot_replay import replay


def _row(ticker, ts, wt, mom, yes_ask=0.50, mins_left=10.0, price=0.50):
    return {"ticker": ticker, "ts": ts, "whale_trend": wt, "momentum": mom,
            "price": price, "yes_ask": yes_ask, "no_ask": round(1 - yes_ask + 0.02, 3),
            "spread": 0.02, "mins_left": mins_left, "buy_pressure": 0,
            "status": "ok"}


def test_replay_produces_trades_and_summary(tmp_path, monkeypatch):
    import bot_broker
    monkeypatch.setattr(bot_broker, "_balance_dollars", lambda: 500.0)
    log = tmp_path / "log.jsonl"
    rows = [
        _row("KXBTC15M-A", 1, -3.0, -10),
        _row("KXBTC15M-A", 2, 3.0, 10),                 # flip -> enter YES
        _row("KXBTC15M-A", 3, 3.0, 10, yes_ask=0.60, mins_left=1.5),  # time exit
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows))
    result = replay(log, tmp_path / "out")
    assert result["trades"] == 1
    assert result["net_total"] != 0


def test_replay_never_hits_live_balance_endpoint(tmp_path, monkeypatch):
    """Historical ts values make now_ts - bankroll_ts >= 3600 constantly; replay
    must pin bankroll offline instead of calling the real signed balance GET.
    fetch_bankroll() swallows exceptions from _balance_dollars (BaseException
    catch), so the load-bearing assertion is a call counter, not the raise
    itself — otherwise this test would pass even without the fix."""
    import bot_broker
    calls = []

    def _boom():
        calls.append(1)
        raise AssertionError("network hit")

    monkeypatch.setattr(bot_broker, "_balance_dollars", _boom)
    log = tmp_path / "log.jsonl"
    base = 1_700_000_000
    rows = [
        _row("KXBTC15M-A", base, -3.0, -10),
        _row("KXBTC15M-A", base + 7200, 3.0, 10),                 # flip -> enter YES
        _row("KXBTC15M-A", base + 14400, 3.0, 10, yes_ask=0.60, mins_left=1.5),
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows))
    result = replay(log, tmp_path / "out", bankroll=500.0)
    assert calls == []
    assert result["trades"] == 1
