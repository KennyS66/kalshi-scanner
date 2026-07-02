"""Writes BTC pick files consumed by the /whales web dashboard.

Picks are scored using spot-aware probability + multi-feature ranking.
Each pick is appended to picks_log.jsonl so the calibration loop can
fit weights against realised outcomes over time.
"""
from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path

from alpha import _ewma_whale_flow, _ticker_mins_left, _normal_cdf

_DATA_DIR = Path("data/whales")
_PICKS_LOG = _DATA_DIR / "picks_log.jsonl"
_CALIBRATION_FILE = _DATA_DIR / "calibration.json"

# Hand-picked defaults; overridden by data/whales/calibration.json once enough
# outcome history accumulates. Tuned conservatively so the spot-based base
# rate dominates and whale flow is a moderate adjustment.
_DEFAULT_WEIGHTS = {
    "base_weight": 0.65,    # how much weight the spot-implied prob gets
    "flow_weight": 0.35,    # how much weight the EWMA flow prob gets
    "ladder_weight": 0.15,  # extra nudge when sibling strikes agree
    # per-minute BTC vol — annualised ≈ 55% → per-minute ≈ 0.55 / sqrt(525600)
    "sigma_per_min": 0.00076,
    "flow_half_life_min": 2.0,   # 15m binaries decay fast
}


def _is_btc_15m(ticker: str) -> bool:
    t = ticker.upper()
    return "KXBTC15M" in t or ("KXBTC" in t and "15M" in t)


def _is_btc_daily(ticker: str) -> bool:
    t = ticker.upper()
    return t.startswith("KXBTC") and "15M" not in t and "ETH" not in t


_STRIKE_RE = re.compile(r"-([TB])(\d+(?:\.\d+)?)")


def _strike_and_dir(ticker: str):
    """Return (strike_float, 'T'|'B') for tickers like KXBTCD-...-T75899.99.

    Returns (None, None) when ticker has no T/B-prefixed strike — caller
    should fall back to snap.floor_strike (15m markets, which use API-only
    strikes since the ticker only carries the close-time minute).
    """
    m = _STRIKE_RE.search(ticker.upper())
    if not m:
        return None, None
    return float(m.group(2)), m.group(1)


def _resolve_strike(snap):
    """(strike, direction_char). Prefers ticker-encoded strike (daily T/B),
    falls back to snap.floor_strike (15m markets — always above-strike)."""
    strike, dchar = _strike_and_dir(snap.ticker)
    if strike is not None:
        return strike, dchar
    fs = getattr(snap, "floor_strike", None)
    if fs:
        return float(fs), "T"  # 15m "BTC up" markets are always above-strike
    return None, None


def _mins_left(snap):
    """Prefer snap.close_ts; fall back to ticker-encoded TTE."""
    close_ts = getattr(snap, "close_ts", None)
    if close_ts:
        return max(0.0, (close_ts - time.time()) / 60.0)
    return _ticker_mins_left(snap.ticker)


def _load_weights() -> dict:
    if _CALIBRATION_FILE.exists():
        try:
            data = json.loads(_CALIBRATION_FILE.read_text())
            # Only accept calibration if it has the expected keys
            if all(k in data for k in ("base_weight", "flow_weight")):
                merged = dict(_DEFAULT_WEIGHTS)
                merged.update({k: v for k, v in data.items() if k in _DEFAULT_WEIGHTS})
                return merged
        except Exception:
            pass
    return dict(_DEFAULT_WEIGHTS)


def _vol_per_min_estimate(spot_history, fallback: float) -> float:
    """Realised per-minute σ from recent spot ticks, σ = std(log returns) / √Δt.

    spot_history is a list[(ts_s, price)] like web._btc_spot_history.
    Falls back to the configured default when there isn't enough data.
    """
    if not spot_history or len(spot_history) < 6:
        return fallback
    log_rets = []
    for (t0, p0), (t1, p1) in zip(spot_history, spot_history[1:]):
        if p0 <= 0 or p1 <= 0 or t1 <= t0:
            continue
        dt_min = (t1 - t0) / 60.0
        if dt_min <= 0:
            continue
        # Normalise each log-return to a per-minute equivalent
        log_rets.append(math.log(p1 / p0) / math.sqrt(dt_min))
    if len(log_rets) < 4:
        return fallback
    mean = sum(log_rets) / len(log_rets)
    var = sum((r - mean) ** 2 for r in log_rets) / (len(log_rets) - 1)
    sigma = math.sqrt(max(var, 0.0))
    # 200s of 5-second polls systematically under-estimates 15-min realized vol
    # (vol-of-vol is high on short windows), so floor at the prior. Cap blocks
    # network glitches / single huge ticks from blowing up the base prob.
    return max(fallback, min(sigma, 0.01))


def _base_prob(spot, strike, direction_char, mins_left, sigma_per_min) -> float | None:
    """P(strike side wins) under log-normal BTC with given per-minute σ."""
    if not spot or not strike or mins_left is None or mins_left <= 0:
        return None
    sigma_t = sigma_per_min * math.sqrt(mins_left)
    if sigma_t <= 0:
        return None
    # Use log-space: log(spot/strike) vs σ_t
    z = math.log(spot / strike) / sigma_t
    p_above = 1.0 - _normal_cdf(-z)  # = Φ(z)
    if direction_char == "T":
        return max(0.001, min(0.999, p_above))
    if direction_char == "B":
        return max(0.001, min(0.999, 1.0 - p_above))
    return None


def _flow_prob(scanner, ticker, half_life_min) -> float | None:
    """P(YES) implied by time-decayed, aggressor-weighted whale flow."""
    yes_w, no_w = _ewma_whale_flow(scanner.whale_alerts, ticker, half_life_min)
    total = yes_w + no_w
    if total <= 0:
        return None
    return yes_w / total


def _concordance(scanner, ticker, direction_char, is_15m) -> float:
    """How many sibling 15m strikes agree on direction? 0..1."""
    if not is_15m:
        return 0.0
    base_ticker = ticker.upper()
    # Sibling = same expiry suffix
    parts = base_ticker.split("-")
    if len(parts) < 3:
        return 0.0
    expiry = parts[-1]
    same_dir = 0
    total = 0
    for t, snap in list(scanner.market_snapshots.items()):
        tu = t.upper()
        if not _is_btc_15m(tu):
            continue
        if not tu.endswith(expiry):
            continue
        _, d = _strike_and_dir(tu)
        if d != direction_char:
            continue
        bp = snap.buy_pressure or 0
        if bp == 0:
            continue
        total += 1
        # 'T' (above) market with YES flow ↔ bullish on BTC
        # 'B' (below) market with YES flow ↔ bearish on BTC
        # Agreement = same flow sign across sibling strikes of same type
        if (direction_char == "T" and bp > 0) or (direction_char == "B" and bp > 0):
            same_dir += 1
        elif (direction_char == "T" and bp < 0) or (direction_char == "B" and bp < 0):
            same_dir -= 1
    if total < 2:
        return 0.0
    return max(-1.0, min(1.0, same_dir / total))


def _btc_direction(snap, spot, mins_left, scanner, weights):
    """Return dict with direction, prob, base_prob, flow_prob, components."""
    strike, dchar = _resolve_strike(snap)
    base_p = _base_prob(spot, strike, dchar, mins_left,
                        weights["sigma_per_min"])
    flow_p = _flow_prob(scanner, snap.ticker, weights["flow_half_life_min"])

    # Blend. Each component is a P(YES). Missing components fall through.
    parts: list[tuple[float, float]] = []
    if base_p is not None:
        parts.append((weights["base_weight"], base_p))
    if flow_p is not None:
        parts.append((weights["flow_weight"], flow_p))

    if not parts:
        # No spot, no flow — fall back to the old sign-of-flow heuristic
        prob = 0.55 if (snap.buy_pressure or 0) >= 0 else 0.45
    else:
        w_sum = sum(w for w, _ in parts)
        prob = sum(w * p for w, p in parts) / w_sum

    # Ladder concordance nudge — only on 15m where sibling strikes are meaningful
    concord = _concordance(scanner, snap.ticker, dchar, _is_btc_15m(snap.ticker))
    if concord != 0:
        prob += concord * weights["ladder_weight"] * 0.1  # max ±1.5¢ nudge
        prob = max(0.001, min(0.999, prob))

    direction = "YES" if prob >= 0.5 else "NO"
    return {
        "direction": direction,
        "prob": round(prob, 4),
        "base_prob": round(base_p, 4) if base_p is not None else None,
        "flow_prob": round(flow_p, 4) if flow_p is not None else None,
        "concord": round(concord, 3),
        "strike": strike,
        "strike_type": dchar,
        "mins_left": round(mins_left, 2) if mins_left is not None else None,
    }


def _btc_score(snap, decision: dict) -> float:
    """Multi-feature rank. Higher = more confident, more liquid, more whale-backed."""
    prob = decision["prob"]
    confidence = abs(prob - 0.5) * 2.0  # 0..1
    # Liquidity (sqrt-dampened — 100k vol shouldn't dominate 10k)
    liq = math.sqrt(max(0.0, snap.trade_volume or 0)) / 50.0
    liq = min(liq, 1.0)
    # Whale weight — keep close to old _rank semantics so existing dashboards
    # still look reasonable
    whale = (snap.recent_whale_count or 0) * 2.0 + (snap.recent_whale_volume or 0) * 0.001
    # Recency — shorter time to expiry = higher info
    mins_left = decision.get("mins_left")
    recency = 1.0
    if mins_left is not None and mins_left > 0:
        recency = max(0.5, min(2.0, 10.0 / mins_left))
    # Ladder concordance bonus
    concord_bonus = abs(decision.get("concord") or 0) * 5.0
    return round(
        whale * recency
        + confidence * 30.0
        + liq * 5.0
        + concord_bonus,
        3,
    )


def _snap_to_market(snap, decision: dict, score: float) -> dict:
    price = snap.last_price or snap.yes_price or None
    net_notional = abs(snap.buy_pressure or 0) * (price or 0.5)
    return {
        "ticker": snap.ticker,
        "direction": decision["direction"],
        "mid": round(price, 4) if price else None,
        "net_notional": round(net_notional, 2),
        "whale_count": snap.recent_whale_count,
        "score": round(score * 100),
        # New fields — additive, GUI ignores until updated
        "prob": decision["prob"],
        "base_prob": decision["base_prob"],
        "flow_prob": decision["flow_prob"],
        "confidence": round(abs(decision["prob"] - 0.5) * 200, 1),  # 0..100
        "strike": decision.get("strike"),
        "strike_type": decision.get("strike_type"),
        "mins_left": decision.get("mins_left"),
        "concord": decision.get("concord"),
    }


def _log_pick(entry: dict) -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        with _PICKS_LOG.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _rank_for_sort(snap, decision, weights) -> float:
    """Score used to choose the top-N markets to publish."""
    return _btc_score(snap, decision)


def write_btc_picks(scanner, spot_history=None) -> None:
    """Write top 15m and daily BTC picks for the web GUI.

    spot_history: optional list[(ts_s, price)] from the web spot poller.
    Without it, base-rate probability is skipped and direction falls back to
    EWMA flow alone.
    """
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    ts_s = time.time()
    ts_ms = int(ts_s * 1000)
    snaps = list(scanner.market_snapshots.values())
    weights = _load_weights()

    spot = None
    if spot_history:
        spot = spot_history[-1][1] if spot_history else None
    weights["sigma_per_min"] = _vol_per_min_estimate(
        spot_history, weights["sigma_per_min"]
    )

    def decide(snap):
        mins_left = _mins_left(snap)
        decision = _btc_direction(snap, spot, mins_left, scanner, weights)
        score = _btc_score(snap, decision)
        return decision, score

    snaps_15m = [(s, *decide(s)) for s in snaps if _is_btc_15m(s.ticker)]
    snaps_d = [(s, *decide(s)) for s in snaps if _is_btc_daily(s.ticker)]

    snaps_15m.sort(key=lambda x: x[2], reverse=True)
    snaps_d.sort(key=lambda x: x[2], reverse=True)

    out_15m = [_snap_to_market(s, d, sc) for s, d, sc in snaps_15m[:15]]
    out_d = [_snap_to_market(s, d, sc) for s, d, sc in snaps_d[:15]]

    (_DATA_DIR / "btc_15m_pick.json").write_text(json.dumps({
        "ts_ms": ts_ms,
        "spot": spot,
        "sigma_per_min": round(weights["sigma_per_min"], 6),
        "markets": out_15m,
    }))
    (_DATA_DIR / "btc_d_pick.json").write_text(json.dumps({
        "ts_ms": ts_ms,
        "spot": spot,
        "sigma_per_min": round(weights["sigma_per_min"], 6),
        "markets": out_d,
    }))

    # Persist top picks so the calibration loop has something to score against
    for snap, decision, score in snaps_15m[:5] + snaps_d[:3]:
        _log_pick({
            "ts": round(ts_s, 1),
            "ticker": snap.ticker,
            "direction": decision["direction"],
            "prob": decision["prob"],
            "base_prob": decision["base_prob"],
            "flow_prob": decision["flow_prob"],
            "concord": decision["concord"],
            "spot": spot,
            "strike": decision["strike"],
            "strike_type": decision["strike_type"],
            "mins_left": decision["mins_left"],
            "score": round(score, 2),
            "mid": snap.last_price or snap.yes_price,
        })
