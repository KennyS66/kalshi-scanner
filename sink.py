"""Writes BTC pick files consumed by the /whales web dashboard."""
from __future__ import annotations

import json
import time
from pathlib import Path

_DATA_DIR = Path("data/whales")


def _is_btc_15m(ticker: str) -> bool:
    return "KXBTC15M" in ticker.upper() or ("KXBTC" in ticker.upper() and "15M" in ticker.upper())


def _is_btc_daily(ticker: str) -> bool:
    t = ticker.upper()
    return t.startswith("KXBTC") and "15M" not in t and "ETH" not in t


def _snap_to_market(snap) -> dict:
    price = snap.last_price or snap.yes_price or None
    direction = "YES" if snap.buy_pressure >= 0 else "NO"
    net_notional = abs(snap.buy_pressure) * (price or 0.5)
    return {
        "ticker": snap.ticker,
        "direction": direction,
        "mid": round(price, 4) if price else None,
        "net_notional": round(net_notional, 2),
        "whale_count": snap.recent_whale_count,
        "score": round(snap.score * 10000),
    }


def _rank(snap) -> float:
    return snap.recent_whale_count * 2.0 + snap.recent_whale_volume * 0.001 + abs(snap.buy_pressure) * 0.1


def write_btc_picks(scanner) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    ts_ms = int(time.time() * 1000)
    snaps = list(scanner.market_snapshots.values())

    snaps_15m = sorted(
        [s for s in snaps if _is_btc_15m(s.ticker)],
        key=_rank, reverse=True,
    )
    snaps_d = sorted(
        [s for s in snaps if _is_btc_daily(s.ticker)],
        key=_rank, reverse=True,
    )

    (_DATA_DIR / "btc_15m_pick.json").write_text(json.dumps({
        "ts_ms": ts_ms,
        "markets": [_snap_to_market(s) for s in snaps_15m[:15]],
    }))
    (_DATA_DIR / "btc_d_pick.json").write_text(json.dumps({
        "ts_ms": ts_ms,
        "markets": [_snap_to_market(s) for s in snaps_d[:15]],
    }))
