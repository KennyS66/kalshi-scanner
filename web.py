"""Web dashboard for kalshi-scanner — /whales BTC signals + /crypto full dashboard."""
from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import re
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

_scanner = None
_alpha_engine = None

# BTC spot price history — filled by background poller every 5s
_btc_spot_history: collections.deque = collections.deque(maxlen=40)
_btc_spot_lock = threading.Lock()
# Kalshi floor_strike per ticker — fetched once per market
_market_floor_strike: dict[str, float] = {}
# Whale flow history per ticker — (ts, yes_pct) pairs
_whale_flow_history: dict[str, collections.deque] = {}
# Signal decision log — last 20 market calls with outcomes
_signal_log: list[dict] = []
_signal_log_lock = threading.Lock()

app = FastAPI(title="kalshi-scanner")

_DATA_DIR = Path("data/whales")

_CRYPTO_PREFIXES = ("KXBTC", "KXETH", "KXSOL", "KXXBT")


def init(scanner, alpha_engine=None) -> None:
    global _scanner, _alpha_engine
    _scanner = scanner
    _alpha_engine = alpha_engine


def _is_crypto(ticker: str) -> bool:
    t = ticker.upper()
    return any(t.startswith(p) for p in _CRYPTO_PREFIXES)


def _get_asset(ticker: str) -> str:
    t = ticker.upper()
    if "ETH" in t:
        return "ETH"
    if "SOL" in t:
        return "SOL"
    return "BTC"


def _extract_strike(ticker: str):
    m = re.search(r'-([TB])(\d{4,7})(?:\.\d+)?', ticker.upper())
    if m:
        return int(m.group(2)), m.group(1)
    return None, None


def _is_15m_or_1h(ticker: str) -> bool:
    t = ticker.upper()
    return "15M" in t or "1H" in t


def _btc_spot_poller_loop(interval: float = 5.0) -> None:
    """Background thread: poll BTC spot from Coinbase every 5s."""
    import urllib.request as ur
    while True:
        time.sleep(interval)
        with contextlib.suppress(Exception):
            req = ur.Request("https://api.coinbase.com/v2/prices/BTC-USD/spot",
                             headers={"User-Agent": "kalshi-scanner/1.0"})
            with ur.urlopen(req, timeout=4) as r:
                data = json.loads(r.read())
                price = float(data["data"]["amount"])
                with _btc_spot_lock:
                    _btc_spot_history.append((time.time(), price))


def _outcome_checker_loop(interval: float = 30.0) -> None:
    """Background thread: check if past signal tickers have finalized on Kalshi."""
    import urllib.request as ur
    while True:
        time.sleep(interval)
        with _signal_log_lock:
            pending = [s for s in _signal_log if s.get("outcome") is None]
        for sig in pending:
            with contextlib.suppress(Exception):
                ticker = sig["ticker"]
                req = ur.Request(
                    f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}",
                    headers={"User-Agent": "kalshi-scanner/1.0"},
                )
                with ur.urlopen(req, timeout=4) as r:
                    mkt = json.loads(r.read()).get("market", {})
                    if mkt.get("status") == "finalized":
                        result = mkt.get("result", "").upper()
                        if result in ("YES", "NO"):
                            with _signal_log_lock:
                                sig["outcome"] = result
                                sig["correct"] = (sig["direction"] == result)


def _pick_writer_loop(interval: int = 30) -> None:
    while True:
        time.sleep(interval)
        if _scanner is not None:
            with contextlib.suppress(Exception):
                from sink import write_btc_picks
                with _btc_spot_lock:
                    hist = list(_btc_spot_history)
                write_btc_picks(_scanner, spot_history=hist)


def _calibration_loop(interval: int = 600) -> None:
    """Every 10 min, refresh outcomes and refit calibration weights from picks_log."""
    while True:
        time.sleep(interval)
        with contextlib.suppress(Exception):
            from calibration import fit
            fit()


def start_background(port: int = 9050) -> threading.Thread:
    threading.Thread(target=_pick_writer_loop, daemon=True).start()
    threading.Thread(target=_btc_spot_poller_loop, daemon=True).start()
    threading.Thread(target=_outcome_checker_loop, daemon=True).start()
    threading.Thread(target=_calibration_loop, daemon=True).start()
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="error")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    return t


def _read_pick(path: Path) -> dict | None:
    if not path.exists():
        return None
    with contextlib.suppress(Exception):
        data = json.loads(path.read_text())
        age_s = time.time() - (data.get("ts_ms", 0) / 1000)
        data["age_s"] = round(age_s)
        return data
    return None


def _whale_rows(limit: int = 200) -> list[dict]:
    if _scanner is None:
        return []
    alerts = list(_scanner.whale_alerts)[:limit]
    if not alerts:
        return []
    notionals = [a.notional for a in alerts]
    avg = sum(notionals) / len(notionals)
    std = (sum((n - avg) ** 2 for n in notionals) / len(notionals)) ** 0.5 if len(notionals) > 1 else 1
    rows = []
    for alert in alerts:
        z = (alert.notional - avg) / std if std > 0 else 0
        rows.append({
            "type": "whale",
            "ticker": alert.ticker,
            "taker_side": alert.side,
            "notional_usd": round(alert.notional, 2),
            "ts_ms": alert.timestamp.timestamp() * 1000 if alert.timestamp else None,
            "z_score": round(z, 2),
        })
    return rows


@app.get("/api/crypto/spot")
async def api_crypto_spot() -> JSONResponse:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            br, er = await asyncio.gather(
                client.get("https://api.coinbase.com/v2/prices/BTC-USD/spot"),
                client.get("https://api.coinbase.com/v2/prices/ETH-USD/spot"),
            )
        return JSONResponse({
            "btc": float(br.json()["data"]["amount"]),
            "eth": float(er.json()["data"]["amount"]),
        })
    except Exception:
        return JSONResponse({"btc": None, "eth": None})


@app.get("/api/crypto/strikes")
async def api_crypto_strikes() -> JSONResponse:
    if _scanner is None:
        return JSONResponse({"rows": []})
    spot_resp = await api_crypto_spot()
    spot = json.loads(spot_resp.body)
    btc_spot = spot.get("btc") or 0
    eth_spot = spot.get("eth") or 0

    rows = []
    for ticker, snap in list(_scanner.market_snapshots.items()):
        if not _is_crypto(ticker):
            continue
        strike, stype = _extract_strike(ticker)
        if strike is None:
            continue
        asset = _get_asset(ticker)
        s = btc_spot if asset == "BTC" else eth_spot if asset == "ETH" else 0
        if not s:
            continue
        diff = s - strike
        price = snap.last_price or snap.yes_price or 0
        rows.append({
            "ticker": ticker,
            "asset": asset,
            "strike": strike,
            "spot": s,
            "diff": round(diff, 2),
            "itm": diff > 0,
            "stype": stype,
            "price": round(price, 4),
            "volume": snap.trade_volume,
            "whale_count": snap.recent_whale_count,
            "buy_pressure": round(snap.buy_pressure, 1),
        })
    rows.sort(key=lambda r: (r["asset"], abs(r["diff"])))
    return JSONResponse({"rows": rows, "btc_spot": btc_spot, "eth_spot": eth_spot})


def _next_15m_expiry() -> "tuple[str, float]":
    """Return (ticker_suffix, unix_ts) for the next BTC 15m expiry (ET-based)."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    now = dt.datetime.now(ET)
    # Round up to next 15-min boundary in ET
    next_min = (now.minute // 15 + 1) * 15
    exp = now.replace(second=0, microsecond=0, minute=0) + dt.timedelta(minutes=next_min)
    months = {1:"JAN",2:"FEB",3:"MAR",4:"APR",5:"MAY",6:"JUN",
              7:"JUL",8:"AUG",9:"SEP",10:"OCT",11:"NOV",12:"DEC"}
    suffix = f"{str(exp.year)[2:]}{months[exp.month]}{exp.day:02d}{exp.hour:02d}{exp.minute:02d}-{exp.minute:02d}"
    return suffix, exp.timestamp()


@app.get("/api/crypto/signal")
async def api_crypto_signal() -> JSONResponse:
    """Analyze active BTC 15m market: blends whale flow + spot-vs-strike + momentum."""
    if _scanner is None:
        return JSONResponse({"status": "no_data"})

    import datetime as dt
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
    _months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
               "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}

    def _ticker_expiry(t):
        m = re.search(r'(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})', t)
        if not m:
            return None
        try:
            return dt.datetime(2000+int(m.group(1)), _months[m.group(2)], int(m.group(3)),
                               int(m.group(4)), int(m.group(5)), tzinfo=_ET)
        except Exception:
            return None

    # Find active (unsettled) BTC 15m market — prefer soonest future expiry
    active = None
    active_exp = None
    for ticker, snap in list(_scanner.market_snapshots.items()):
        if "KXBTC15M" not in ticker.upper():
            continue
        price = snap.last_price or snap.yes_price or 0
        if not (0.01 < price < 0.99):
            continue
        exp = _ticker_expiry(ticker)
        if exp is None:
            continue
        if (exp.timestamp() - time.time()) < -120:
            continue  # expired more than 2 minutes ago
        if active is None or exp < active_exp:
            active = (ticker, snap)
            active_exp = exp

    # Fallback: scanner hasn't seen this market yet — fetch directly from Kalshi
    _direct_mkt: dict | None = None
    if not active:
        with contextlib.suppress(Exception):
            next_suffix, next_ts = _next_15m_expiry()
            _dticker = f"KXBTC15M-{next_suffix}"
            _mins_rem = (next_ts - time.time()) / 60
            if 0 < _mins_rem <= 15:
                async with httpx.AsyncClient(timeout=4) as client:
                    r = await client.get(
                        f"https://api.elections.kalshi.com/trade-api/v2/markets/{_dticker}"
                    )
                    mkt = r.json().get("market", {})
                    if mkt.get("status") == "active":
                        _direct_mkt = {
                            "ticker": _dticker,
                            "price": float(mkt.get("last_price_dollars") or 0.5),
                            "floor_strike": float(mkt["floor_strike"]) if mkt.get("floor_strike") else None,
                            "yes_ask": float(mkt.get("yes_ask_dollars") or 0),
                            "no_ask": float(mkt.get("no_ask_dollars") or 0),
                        }
                        if _direct_mkt["floor_strike"]:
                            _market_floor_strike[_dticker] = _direct_mkt["floor_strike"]

    if not active and _direct_mkt is None:
        try:
            next_suffix, next_ts = _next_15m_expiry()
            next_ticker = f"KXBTC15M-{next_suffix}"
            mins_to_open = round((next_ts - time.time()) / 60, 1)
            return JSONResponse({
                "status": "between_markets",
                "next_ticker": next_ticker,
                "mins_to_open": mins_to_open,
            })
        except Exception:
            return JSONResponse({"status": "no_active_market"})

    if active is not None:
        ticker, snap = active
        price = snap.last_price or snap.yes_price or 0.5
    else:
        ticker, snap = _direct_mkt["ticker"], None
        price = _direct_mkt["price"]

    # Tally whale YES vs NO on this ticker
    # Weights: time-decayed (8-min half-life) + aggressiveness (ask-side takers = 1.4x)
    import math as _math
    _decay = _math.log(2) / (8 * 60)
    _now_ts = time.time()
    yes_c = no_c = 0
    yes_not = no_not = 0.0   # raw notional for display
    yes_w = no_w = 0.0       # time-decayed + aggression-weighted notional for signal
    for a in list(_scanner.whale_alerts):
        if a.ticker != ticker:
            continue
        age_s = max(0.0, _now_ts - (a.timestamp.timestamp() if a.timestamp else _now_ts))
        tw = _math.exp(-_decay * age_s)
        aggr = 1.4 if getattr(a, "taker_side", None) == "ask" else 1.0
        w = a.notional * tw * aggr
        if a.side == "yes":
            yes_c += a.contracts
            yes_not += a.notional
            yes_w += w
        else:
            no_c += a.contracts
            no_not += a.notional
            no_w += w

    total_not = yes_not + no_not
    has_whale_data = (yes_w + no_w) > 0
    if has_whale_data:
        # Use time-decayed + aggression-weighted ratio for the signal
        yes_pct = yes_w / (yes_w + no_w)
    else:
        # No whale trades yet — fall back to all-trade buy pressure if available
        _bp = snap.buy_pressure if snap else 0
        _tv = snap.trade_volume if snap else 0
        if _tv > 0:
            yes_pct = (_bp / _tv + 1) / 2  # map [-1,1] → [0,1]
        else:
            yes_pct = 0.5  # truly no data

    # Minutes left
    mins_left = None
    mx = re.search(r'(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})', ticker)
    if mx:
        exp_dt = dt.datetime(2000+int(mx.group(1)), _months[mx.group(2)], int(mx.group(3)),
                             int(mx.group(4)), int(mx.group(5)), tzinfo=_ET)
        mins_left = round((exp_dt.timestamp() - time.time()) / 60, 1)

    # ── Floor strike (fetch from Kalshi once per market) ────────────────
    if ticker not in _market_floor_strike:
        with contextlib.suppress(Exception):
            async with httpx.AsyncClient(timeout=4) as client:
                r = await client.get(
                    f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}"
                )
                mkt = r.json().get("market", {})
                fs = mkt.get("floor_strike")
                if fs is not None:
                    _market_floor_strike[ticker] = float(fs)

    floor_strike = _market_floor_strike.get(ticker)

    # ── BTC spot + momentum from poller history ──────────────────────────
    with _btc_spot_lock:
        hist = list(_btc_spot_history)

    spot = hist[-1][1] if hist else None
    momentum = None  # $/min, positive = BTC rising

    if len(hist) >= 2:
        recent = [(t, p) for t, p in hist if time.time() - t < 90]
        if len(recent) >= 2:
            dt_span = recent[-1][0] - recent[0][0]
            dp_span = recent[-1][1] - recent[0][1]
            if dt_span > 1:
                momentum = round((dp_span / dt_span) * 60, 2)  # $/min

    # ── Distance from strike ─────────────────────────────────────────────
    distance = None
    if spot is not None and floor_strike is not None:
        distance = round(spot - floor_strike, 2)

    # ── Whale flow trend (is conviction accelerating or fading?) ─────────
    if ticker not in _whale_flow_history:
        _whale_flow_history[ticker] = collections.deque(maxlen=40)
    _whale_flow_history[ticker].append((time.time(), yes_pct))

    whale_trend = 0.0  # yes_pct change per minute; positive = getting more bullish
    wh_hist = list(_whale_flow_history[ticker])
    if len(wh_hist) >= 2:
        recent_wh = [(t, p) for t, p in wh_hist if time.time() - t < 180]
        if len(recent_wh) >= 2:
            dt_wh = recent_wh[-1][0] - recent_wh[0][0]
            dp_wh = recent_wh[-1][1] - recent_wh[0][1]
            if dt_wh > 1:
                whale_trend = (dp_wh / dt_wh) * 60

    # ── BTC realized volatility ($/min) — to scale distance signal ───────
    import math
    btc_vol_per_min = 50.0  # default fallback
    if len(hist) >= 3:
        recent_h = [(t, p) for t, p in hist if time.time() - t < 300]
        if len(recent_h) >= 3:
            moves = [abs(recent_h[i+1][1] - recent_h[i][1]) /
                     max((recent_h[i+1][0] - recent_h[i][0]) / 60.0, 0.01)
                     for i in range(len(recent_h) - 1)]
            if moves:
                btc_vol_per_min = sum(moves) / len(moves)

    # ── Three component signals, each -1 to +1 ───────────────────────────
    # Whale: flow (70%) + trend acceleration (30%)
    whale_flow_sig = (yes_pct - 0.5) * 2
    whale_trend_sig = max(-1.0, min(1.0, whale_trend * 4))  # 0.25/min → full
    whale_signal = whale_flow_sig * 0.70 + whale_trend_sig * 0.30

    # Spot: distance normalized by expected BTC range (vol × √mins_remaining)
    mins_remaining = max(mins_left or 15.0, 0.5)
    spot_signal = 0.0
    if distance is not None:
        expected_range = btc_vol_per_min * math.sqrt(mins_remaining)
        spot_signal = max(-1.0, min(1.0, distance / max(expected_range, 1.0)))

    # Momentum: BTC $/min; scaled by vol so context-aware
    momentum_signal = 0.0
    if momentum is not None:
        momentum_signal = max(-1.0, min(1.0, momentum / max(btc_vol_per_min, 1.0)))

    # ── Bid/ask spread — market maker certainty indicator ─────────────────
    yes_ask = (snap.yes_price if snap else _direct_mkt["yes_ask"]) or 0
    no_ask = (snap.no_price if snap else _direct_mkt["no_ask"]) or 0
    spread = round(yes_ask + no_ask - 1.0, 4) if yes_ask and no_ask else None

    # ── Time-weighted blend ───────────────────────────────────────────────
    if mins_remaining < 2:
        w = (0.15, 0.70, 0.15)
    elif mins_remaining < 5:
        w = (0.25, 0.50, 0.25)
    elif mins_remaining < 9:
        w = (0.35, 0.35, 0.30)
    else:
        w = (0.45, 0.20, 0.35)

    if floor_strike is None or spot is None:
        w_total = w[0] + w[2] or 1.0
        combined = (whale_signal * w[0] + momentum_signal * w[2]) / w_total
    else:
        combined = whale_signal * w[0] + spot_signal * w[1] + momentum_signal * w[2]

    direction = "YES" if combined >= 0 else "NO"
    confidence = round(min(abs(combined), 1.0) * 100)

    # ── Log signal (once per new ticker) ─────────────────────────────────
    with _signal_log_lock:
        if not _signal_log or _signal_log[-1]["ticker"] != ticker:
            _signal_log.append({
                "ticker": ticker,
                "direction": direction,
                "conf": confidence,
                "yes_pct": round(yes_pct * 100, 1),
                "has_whale_data": has_whale_data,
                "price": round(price, 4),
                "distance": distance,
                "momentum": momentum,
                "ts": time.time(),
                "outcome": None,
                "correct": None,
            })
            if len(_signal_log) > 20:
                _signal_log.pop(0)

    return JSONResponse({
        "status": "ok",
        "ticker": ticker,
        "direction": direction,
        "confidence": confidence,
        "price": round(price, 4),
        # Whale / flow component
        "yes_pct": round(yes_pct * 100, 1),
        "has_whale_data": has_whale_data,
        "yes_contracts": round(yes_c),
        "no_contracts": round(no_c),
        "yes_notional": round(yes_not),
        "no_notional": round(no_not),
        "whale_count": snap.recent_whale_count if snap else 0,
        "buy_pressure": round(snap.buy_pressure if snap else 0),
        "whale_trend": round(whale_trend * 100, 1),
        # Spot / strike component
        "spot": round(spot, 2) if spot is not None else None,
        "floor_strike": round(floor_strike, 2) if floor_strike is not None else None,
        "distance": distance,
        "btc_vol_per_min": round(btc_vol_per_min, 1),
        # Bid/ask spread
        "spread": spread,
        # Momentum component
        "momentum": momentum,
        # Raw signal values (-100 to +100)
        "sig_whale": round(whale_signal * 100),
        "sig_spot": round(spot_signal * 100),
        "sig_momentum": round(momentum_signal * 100),
        "sig_combined": round(combined * 100),
        "mins_left": mins_left,
        "ts": time.time(),
    })


@app.get("/api/analyze")
async def api_analyze() -> JSONResponse:
    """Call Claude Opus 4.7 with adaptive thinking to analyze the current BTC 15m signal."""
    import os
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return JSONResponse({"error": "ANTHROPIC_API_KEY not set"}, status_code=503)

    signal_resp = await api_crypto_signal()
    signal = json.loads(signal_resp.body)

    if signal.get("status") != "ok":
        return JSONResponse({"error": "no active market", "signal_status": signal.get("status")})

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    prompt = f"""You are analyzing a live Kalshi BTC 15-minute prediction market. The question is: will BTC close ABOVE the strike price at expiry?

Current signal data:
- Ticker: {signal['ticker']}
- Minutes left: {signal['mins_left']}
- BTC spot: ${signal.get('spot', 'N/A')}
- Strike (floor): ${signal.get('floor_strike', 'N/A')}
- Distance (spot - strike): ${signal.get('distance', 'N/A')} (positive = YES winning, negative = NO winning)
- Momentum: {signal.get('momentum', 'N/A')} $/min (positive = BTC rising)
- BTC volatility: ±${signal.get('btc_vol_per_min', 'N/A')}/min
- YES price: {round((signal['price'] or 0) * 100, 1)}¢
- Whale flow: {signal['yes_pct']}% YES ({signal['yes_contracts']} YES contracts vs {signal['no_contracts']} NO contracts)
- Has whale data: {signal['has_whale_data']}
- Whale trend: {signal.get('whale_trend', 0)} (positive = flow shifting YES)
- Spread (vig): {round((signal.get('spread') or 0) * 100, 1)}¢
- Signal components (−100 to +100): whale={signal['sig_whale']}, spot={signal['sig_spot']}, momentum={signal['sig_momentum']}, combined={signal['sig_combined']}
- Scanner confidence: {signal['confidence']}% {signal['direction']}

Think carefully about:
1. With {signal.get('mins_left', '?')} minutes left, can BTC move enough to cross the strike?
2. Is the whale flow meaningful or noise?
3. What's the risk/reward at current prices?
4. Entry, hold, or exit recommendation?

Be concise. Give a clear trade recommendation with reasoning."""

    try:
        response = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=600,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": prompt}],
        )
        thinking_text = ""
        answer_text = ""
        for block in response.content:
            if block.type == "thinking":
                thinking_text = getattr(block, "thinking", "") or ""
            elif block.type == "text":
                answer_text = block.text
        return JSONResponse({
            "status": "ok",
            "ticker": signal["ticker"],
            "confidence": signal["confidence"],
            "direction": signal["direction"],
            "mins_left": signal["mins_left"],
            "analysis": answer_text,
            "thinking_summary": thinking_text[:400] if thinking_text else None,
            "signal": signal,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/crypto/history")
async def api_crypto_history() -> JSONResponse:
    with _signal_log_lock:
        rows = list(_signal_log)
    return JSONResponse({"rows": rows})


@app.get("/api/crypto/updown")
async def api_crypto_updown() -> JSONResponse:
    if _scanner is None:
        return JSONResponse({"rows": []})
    rows = []
    for ticker, snap in list(_scanner.market_snapshots.items()):
        if not _is_crypto(ticker) or not _is_15m_or_1h(ticker):
            continue
        price = snap.last_price or snap.yes_price or 0
        if not (0.01 < price < 0.99):
            continue  # skip settled markets at the rails
        rows.append({
            "ticker": ticker,
            "title": snap.title or ticker,
            "price": round(price, 4),
            "direction": "YES" if snap.buy_pressure >= 0 else "NO",
            "volume": snap.trade_volume,
            "whale_count": snap.recent_whale_count,
            "buy_pressure": round(snap.buy_pressure, 1),
        })
    rows.sort(key=lambda r: r["volume"], reverse=True)
    return JSONResponse({"rows": rows[:20]})


@app.get("/api/crypto/signals")
async def api_crypto_signals() -> JSONResponse:
    if _alpha_engine is None:
        return JSONResponse({"rows": []})
    with contextlib.suppress(Exception):
        sigs = _alpha_engine.get_top_signals(80)
        rows = []
        for s in sigs:
            if not _is_crypto(s.ticker) and "btc_" not in s.signal_type and "ladder" not in s.signal_type:
                continue
            rows.append({
                "ticker": s.ticker,
                "title": s.title,
                "type": s.signal_type,
                "direction": s.direction,
                "strength": s.strength,
                "edge_pct": s.edge_pct,
                "kalshi_price": s.kalshi_price,
                "fair_value": s.fair_value,
                "detail": s.detail,
            })
        return JSONResponse({"rows": rows[:30]})
    return JSONResponse({"rows": []})


@app.get("/api/crypto/whales")
async def api_crypto_whales() -> JSONResponse:
    if _scanner is None:
        return JSONResponse({"rows": []})
    spot_resp = await api_crypto_spot()
    spot = json.loads(spot_resp.body)
    btc_spot = spot.get("btc") or 0

    rows = []
    for alert in list(_scanner.whale_alerts):
        if not _is_crypto(alert.ticker):
            continue
        strike, _ = _extract_strike(alert.ticker)
        vs_spot = round(btc_spot - strike, 0) if strike and btc_spot else None
        rows.append({
            "ticker": alert.ticker,
            "side": alert.side,
            "contracts": alert.contracts,
            "price": alert.price,
            "notional": round(alert.notional, 2),
            "ts_ms": alert.timestamp.timestamp() * 1000 if alert.timestamp else None,
            "vs_spot": vs_spot,
        })
    return JSONResponse({"rows": rows[:80]})


@app.get("/crypto", response_class=HTMLResponse)
async def crypto_page() -> str:
    return _CRYPTO_HTML


@app.get("/trade", response_class=HTMLResponse)
async def trade_page() -> str:
    return _TRADE_HTML


@app.get("/api/whales")
async def api_whales(limit: int = 60) -> JSONResponse:
    return JSONResponse({"rows": _whale_rows(limit)})


@app.get("/api/btc")
async def api_btc() -> JSONResponse:
    return JSONResponse({
        "btc_15m": _read_pick(_DATA_DIR / "btc_15m_pick.json"),
        "btc_d":   _read_pick(_DATA_DIR / "btc_d_pick.json"),
    })


@app.get("/whales", response_class=HTMLResponse)
async def whales_page() -> str:
    return _WHALES_HTML


_TRADE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>kalshi · trade caller</title>
<style>
:root {
  --bg:#0d1117; --bg2:#161b22; --bg3:#21262d; --fg:#e6edf3; --mute:#7d8590;
  --border:#30363d; --green:#3fb950; --red:#f85149; --yellow:#d29922; --blue:#58a6ff;
}
* { box-sizing:border-box; margin:0; padding:0; }
body {
  font-family:ui-monospace,"SF Mono","Fira Code",monospace;
  background:var(--bg); color:var(--fg);
  min-height:100vh; display:flex; flex-direction:column;
}

/* ── header ── */
header {
  padding:10px 24px; border-bottom:1px solid var(--border); background:var(--bg2);
  display:flex; align-items:center; justify-content:space-between;
}
.logo { font-size:13px; font-weight:700; color:var(--blue); }
.nav { display:flex; gap:16px; }
.nav a { color:var(--mute); font-size:11px; text-decoration:none; }
.nav a:hover { color:var(--blue); }
.clock { color:var(--mute); font-size:12px; }

/* ── main call box ── */
#call-box {
  margin:28px auto; width:100%; max-width:720px; padding:0 20px;
  display:flex; flex-direction:column; gap:0;
}

.mkt-label {
  font-size:11px; color:var(--mute); letter-spacing:0.5px; margin-bottom:4px;
}
.mkt-ticker {
  font-size:14px; color:var(--blue); margin-bottom:16px;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
}

/* big call card */
.call-card {
  border:2px solid var(--border); border-radius:12px; padding:28px 32px;
  background:var(--bg2); transition:border-color 0.3s, background 0.3s;
}
.call-card.yes { border-color:#2d5a3d; background:#0d1f12; }
.call-card.no  { border-color:#5a2a2a; background:#1a0d0d; }
.call-card.waiting { border-color:var(--border); background:var(--bg2); }

.call-direction {
  font-size:72px; font-weight:900; letter-spacing:-2px; line-height:1;
  margin-bottom:12px;
}
.call-direction.yes { color:var(--green); }
.call-direction.no  { color:var(--red); }
.call-direction.waiting { color:var(--mute); font-size:36px; margin-bottom:0; }

.call-action {
  font-size:22px; font-weight:700; margin-bottom:20px; color:var(--fg);
}

.call-meta {
  display:flex; flex-wrap:wrap; gap:24px; margin-bottom:20px;
}
.meta-item { display:flex; flex-direction:column; gap:2px; }
.meta-label { font-size:10px; color:var(--mute); text-transform:uppercase; letter-spacing:0.5px; }
.meta-value { font-size:18px; font-weight:700; font-variant-numeric:tabular-nums; }
.meta-value.yes { color:var(--green); }
.meta-value.no  { color:var(--red); }
.meta-value.neu { color:var(--yellow); }

/* confidence bar */
.conf-wrap { display:flex; align-items:center; gap:12px; margin-bottom:16px; }
.conf-label { font-size:11px; color:var(--mute); white-space:nowrap; }
.conf-track {
  flex:1; height:8px; background:var(--bg3); border-radius:4px; overflow:hidden;
}
.conf-fill {
  height:100%; border-radius:4px; transition:width 0.4s;
}
.conf-fill.yes { background:var(--green); }
.conf-fill.no  { background:var(--red); }
.conf-pct { font-size:14px; font-weight:700; min-width:40px; text-align:right; }

/* component row */
.components {
  display:flex; gap:10px; flex-wrap:wrap;
}
.comp-chip {
  padding:4px 12px; border-radius:20px; font-size:12px; font-weight:700; border:1px solid;
}
.comp-chip.bull { background:#0d2018; color:var(--green); border-color:#2d5a3d; }
.comp-chip.bear { background:#1f0d0d; color:var(--red); border-color:#5a2a2a; }
.comp-chip.neut { background:var(--bg3); color:var(--mute); border-color:var(--border); }

/* ── round history ── */
#history-section {
  max-width:720px; width:100%; margin:0 auto 28px; padding:0 20px;
}
.hist-title {
  font-size:11px; color:var(--mute); text-transform:uppercase; letter-spacing:0.5px;
  margin-bottom:10px; display:flex; align-items:center; justify-content:space-between;
}
.hist-grid {
  display:flex; flex-wrap:wrap; gap:8px;
}
.hcard {
  border:1px solid var(--border); border-radius:8px; padding:10px 14px;
  background:var(--bg2); min-width:130px; display:flex; flex-direction:column; gap:4px;
}
.hcard.correct  { border-color:#2d5a3d; background:#0d1f12; }
.hcard.wrong    { border-color:#5a2a2a; background:#1a0d0d; }
.hcard.pending  { opacity:0.6; }
.hcard-dir { font-size:16px; font-weight:900; }
.hcard-dir.yes { color:var(--green); }
.hcard-dir.no  { color:var(--red); }
.hcard-conf { font-size:11px; color:var(--mute); }
.hcard-out { font-size:13px; font-weight:700; }
.hcard-out.ok  { color:var(--green); }
.hcard-out.bad { color:var(--red); }
.hcard-out.pending { color:var(--mute); font-style:italic; }
.hcard-time { font-size:10px; color:var(--mute); }

/* ── score pill ── */
.score-pill {
  display:inline-block; padding:2px 10px; border-radius:12px;
  font-size:12px; font-weight:700; border:1px solid var(--border);
  background:var(--bg3);
}
</style>
</head>
<body>

<header>
  <span class="logo">kalshi · trade caller</span>
  <nav class="nav">
    <a href="/crypto">→ full dashboard</a>
    <a href="/whales">→ whales</a>
  </nav>
  <span class="clock" id="clock">--:--:--</span>
</header>

<div id="call-box">
  <div class="mkt-label">CURRENT MARKET</div>
  <div class="mkt-ticker" id="mkt-ticker">loading…</div>

  <div class="call-card waiting" id="call-card">
    <div class="call-direction waiting" id="call-dir">—</div>
    <div class="call-action" id="call-action" style="color:var(--mute)">waiting for data…</div>

    <div class="call-meta" id="call-meta"></div>

    <div class="conf-wrap">
      <span class="conf-label">Confidence</span>
      <div class="conf-track"><div class="conf-fill" id="conf-fill" style="width:0%"></div></div>
      <span class="conf-pct" id="conf-pct">—</span>
    </div>

    <div class="components" id="call-comps"></div>
  </div>
</div>

<div id="history-section">
  <div class="hist-title">
    <span>ROUND HISTORY</span>
    <span id="score-label"></span>
  </div>
  <div class="hist-grid" id="hist-grid">
    <span style="color:var(--mute);font-size:11px">history appears after first round…</span>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);

function tick() { $('clock').textContent = new Date().toISOString().slice(11,19)+' UTC'; }
tick(); setInterval(tick, 1000);

function chip(label, val) {
  if(val == null) return '';
  const cls = val > 8 ? 'bull' : val < -8 ? 'bear' : 'neut';
  const arrow = val > 8 ? '▲' : val < -8 ? '▼' : '▶';
  return `<span class="comp-chip ${cls}">${arrow} ${label} ${val > 0 ? '+' : ''}${val}</span>`;
}

function metaItem(label, value, cls='') {
  return `<div class="meta-item">
    <span class="meta-label">${label}</span>
    <span class="meta-value ${cls}">${value}</span>
  </div>`;
}

function fmt$(n) { return n == null ? '—' : '$' + Math.round(n).toLocaleString(); }

async function pollSignal() {
  try {
    const s = await fetch('/api/crypto/signal').then(r => r.json());
    const card = $('call-card');

    if(s.status === 'between_markets' || s.status === 'no_active_market') {
      card.className = 'call-card waiting';
      $('call-dir').className = 'call-direction waiting';
      $('call-dir').textContent = '—';
      const mins = s.mins_to_open != null ? ` · opens in ${s.mins_to_open.toFixed(1)}m` : '';
      $('call-action').textContent = 'Waiting for next round' + mins;
      $('call-action').style.color = 'var(--mute)';
      $('mkt-ticker').textContent = s.next_ticker || 'between markets';
      $('call-meta').innerHTML = '';
      $('call-comps').innerHTML = '';
      $('conf-fill').style.width = '0%';
      $('conf-pct').textContent = '—';
      return;
    }
    if(s.status !== 'ok') return;

    const isUp = s.direction === 'YES';
    const dirCls = isUp ? 'yes' : 'no';
    card.className = 'call-card ' + dirCls;
    $('call-dir').className = 'call-direction ' + dirCls;
    $('call-dir').textContent = isUp ? '▲ YES' : '▼ NO';

    const price = isUp ? s.price : (1 - s.price);
    const flowPct = isUp ? s.yes_pct : (100 - s.yes_pct);
    const flowSrc = s.has_whale_data ? 'whale' : 'retail';
    $('call-action').style.color = 'var(--fg)';
    $('call-action').textContent = `BUY ${s.direction} at ${(price * 100).toFixed(1)}¢`;

    // Spot vs strike info
    let spotNote = '';
    if(s.spot != null && s.floor_strike != null) {
      const dist = Math.round(s.distance);
      const sign = dist >= 0 ? '+$' : '−$';
      spotNote = `spot ${fmt$(s.spot)} vs strike ${fmt$(s.floor_strike)} (${sign}${Math.abs(dist).toLocaleString()})`;
    }

    const minsStr = s.mins_left != null ? s.mins_left.toFixed(1) + 'm' : '—';
    const spreadStr = s.spread != null ? (s.spread * 100).toFixed(1) + '¢ vig' : '—';

    $('call-meta').innerHTML =
      metaItem('Price', (price * 100).toFixed(1) + '¢', dirCls) +
      metaItem('Flow', flowPct.toFixed(1) + '% ' + s.direction + ' (' + flowSrc + ')', dirCls) +
      metaItem('Time Left', minsStr, 'neu') +
      metaItem('Spread', spreadStr, s.spread > 0.06 ? 'no' : 'neu') +
      (spotNote ? `<div class="meta-item" style="flex-basis:100%">
        <span class="meta-label">Spot vs Strike</span>
        <span class="meta-value" style="font-size:14px;color:var(--mute)">${spotNote}</span>
      </div>` : '');

    const conf = s.confidence;
    $('conf-fill').style.width = conf + '%';
    $('conf-fill').className = 'conf-fill ' + dirCls;
    $('conf-pct').textContent = conf + '%';
    $('conf-pct').style.color = conf >= 60 ? (isUp ? 'var(--green)' : 'var(--red)') : 'var(--mute)';

    $('call-comps').innerHTML =
      chip('Whale', s.sig_whale) +
      chip('Spot',  s.sig_spot) +
      chip('Momo',  s.sig_momentum) +
      (s.sig_combined != null
        ? `<span class="comp-chip ${s.sig_combined > 8 ? 'bull' : s.sig_combined < -8 ? 'bear' : 'neut'}" style="font-size:13px;padding:4px 14px">NET ${s.sig_combined > 0 ? '+' : ''}${s.sig_combined}</span>`
        : '');

    $('mkt-ticker').textContent = s.ticker;
  } catch(e) { console.error('signal poll error', e); }
}

async function pollHistory() {
  try {
    const { rows } = await fetch('/api/crypto/history').then(r => r.json());
    if(!rows || !rows.length) return;

    const wins = rows.filter(r => r.correct === true).length;
    const settled = rows.filter(r => r.outcome != null).length;
    const scoreHtml = settled > 0
      ? `<span class="score-pill">${wins}/${settled} (${Math.round(wins/settled*100)}%)</span>`
      : '';
    $('score-label').innerHTML = scoreHtml;

    $('hist-grid').innerHTML = rows.slice().reverse().map(r => {
      const isUp = r.direction === 'YES';
      const dirCls = isUp ? 'yes' : 'no';
      const dirLabel = isUp ? '▲ YES' : '▼ NO';
      const ts = r.ts ? new Date(r.ts * 1000).toISOString().slice(11, 16) : '?';
      const label = r.ticker ? r.ticker.split('-').slice(-2).join('-') : '';

      let outHtml, outCls;
      if(r.outcome) {
        const ok = r.correct;
        outHtml = ok ? `✓ ${r.outcome}` : `✗ ${r.outcome}`;
        outCls = ok ? 'ok' : 'bad';
      } else {
        outHtml = 'pending…';
        outCls = 'pending';
      }
      const cardCls = r.outcome ? (r.correct ? 'correct' : 'wrong') : 'pending';

      return `<div class="hcard ${cardCls}">
        <span class="hcard-dir ${dirCls}">${dirLabel}</span>
        <span class="hcard-conf">${r.conf != null ? r.conf + '% conf' : ''}</span>
        <span class="hcard-out ${outCls}">${outHtml}</span>
        <span class="hcard-time">${label} · ${ts} UTC</span>
      </div>`;
    }).join('');
  } catch(e) { console.error('history poll error', e); }
}

pollSignal();   setInterval(pollSignal,   4000);
pollHistory();  setInterval(pollHistory,  10000);
</script>
</body>
</html>"""

_CRYPTO_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>kalshi-scanner · crypto dashboard</title>
<style>
:root { --bg:#0d1117; --bg2:#161b22; --bg3:#21262d; --fg:#e6edf3; --mute:#7d8590;
        --border:#30363d; --green:#3fb950; --red:#f85149; --yellow:#d29922;
        --blue:#58a6ff; --orange:#f0883e; --purple:#bc8cff; }
* { box-sizing:border-box; margin:0; padding:0; }
body { font-family:ui-monospace,"SF Mono","Fira Code",monospace; background:var(--bg); color:var(--fg); font-size:13px; min-height:100vh; }

header { padding:10px 20px; border-bottom:1px solid var(--border); background:var(--bg2);
         display:flex; align-items:center; gap:20px; }
.logo { font-size:14px; font-weight:700; color:var(--blue); margin-right:8px; }
.spot-btc { color:var(--orange); font-weight:700; font-size:14px; }
.spot-eth { color:var(--purple); font-weight:700; font-size:14px; }
.clock { color:var(--mute); font-size:12px; margin-left:auto; }
.nav-link { color:var(--mute); font-size:11px; text-decoration:none; }
.nav-link:hover { color:var(--blue); }

/* ── signal banner ── */
.signal-banner { padding:10px 20px; display:flex; align-items:center; gap:16px; border-bottom:2px solid var(--border);
                 background:var(--bg2); transition:background 0.4s; }
.signal-banner.up   { border-bottom-color:var(--green); background:#0d1f10; }
.signal-banner.down { border-bottom-color:var(--red);   background:#1f0d0d; }
.signal-banner.flash { animation: flashpulse 0.6s ease-out; }
@keyframes flashpulse { 0%{opacity:0.2} 50%{opacity:1} 100%{opacity:1} }

.sig-direction { font-size:28px; font-weight:900; letter-spacing:1px; line-height:1; }
.sig-direction.up   { color:var(--green); }
.sig-direction.down { color:var(--red); }

.sig-conf { font-size:11px; color:var(--mute); }
.sig-conf span { font-weight:700; }
.conf-bar-wrap { width:80px; height:6px; background:#1c2128; border-radius:3px; display:inline-block; vertical-align:middle; margin-left:4px; }
.conf-bar      { height:6px; border-radius:3px; background:var(--green); }
.conf-bar.down { background:var(--red); }

.sig-stats { display:flex; gap:12px; flex-wrap:wrap; font-size:12px; }
.sig-stat  { display:flex; flex-direction:column; gap:1px; }
.sig-stat .k { font-size:10px; color:var(--mute); text-transform:uppercase; }
.sig-stat .v { font-weight:700; font-variant-numeric:tabular-nums; }

.sig-components { display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
.sig-comp { padding:3px 8px; border-radius:4px; font-size:11px; font-weight:700;
            border:1px solid var(--border); white-space:nowrap; }
.sig-comp.bull { background:#0d1f10; color:var(--green); border-color:#2d5a3d; }
.sig-comp.bear { background:#1f0d0d; color:var(--red);   border-color:#5a2a2a; }
.sig-comp.neut { background:var(--bg3); color:var(--mute); }
.sig-ticker-label { font-size:11px; color:var(--mute); margin-left:auto; }

/* ── signal history strip ── */
.history-strip { display:flex; gap:8px; padding:6px 16px; overflow-x:auto;
                 border-bottom:1px solid var(--border); background:var(--bg);
                 min-height:62px; align-items:center; }
.hist-card { flex-shrink:0; padding:5px 10px; border-radius:6px; min-width:100px;
             border:1px solid var(--border); background:var(--bg2); font-size:11px;
             display:flex; flex-direction:column; gap:2px; cursor:default; }
.hist-card.correct { border-color:var(--green); background:#0a1a0c; }
.hist-card.wrong   { border-color:var(--red);   background:#1a0a0a; }
.hist-card.pending { opacity:0.65; }
.sig-reset-badge  { font-size:10px; padding:2px 7px; border-radius:10px; background:#1a3a2a; color:var(--green);
                    border:1px solid #2d5a3d; white-space:nowrap; }
.sig-reset-badge.t1 { background:#3a2e0a; color:var(--yellow); border-color:#5a4a10; }

.layout { display:grid; grid-template-columns:1fr 1fr; gap:12px; padding:12px; height:calc(100vh - 45px - 64px - 62px); }
.col { display:flex; flex-direction:column; gap:12px; min-height:0; }

.card { background:var(--bg2); border:1px solid var(--border); border-radius:8px; overflow:hidden; display:flex; flex-direction:column; min-height:0; }
.card.grow { flex:1; min-height:0; }
.card-header { padding:7px 12px; border-bottom:1px solid var(--border); background:var(--bg3);
               display:flex; align-items:center; justify-content:space-between; flex-shrink:0; }
.card-title { font-size:11px; text-transform:uppercase; letter-spacing:0.7px; color:var(--mute); }
.card-meta { font-size:11px; color:var(--mute); }
.card-body { padding:0; overflow-y:auto; flex:1; min-height:0; }

/* ── strike ladder ── */
.strike-asset-row { padding:5px 10px; background:var(--bg3); color:var(--fg); font-weight:700; font-size:12px;
                    display:flex; align-items:center; gap:8px; border-bottom:1px solid var(--border); position:sticky; top:0; z-index:1; }
.strike-row { display:grid; grid-template-columns:36px 76px 72px 40px 48px 56px 36px 72px;
              gap:4px; padding:4px 10px; border-bottom:1px solid #1c2128; align-items:center; font-size:12px; }
.strike-row:last-child { border-bottom:none; }
.strike-row:hover { background:#1c2128; }
.itm  { color:var(--green); font-weight:700; font-size:11px; }
.otm  { color:var(--red);   font-weight:700; font-size:11px; }
.stype-t { color:var(--blue); }
.stype-b { color:var(--purple); }
.diff-pos { color:var(--green); }
.diff-neg { color:var(--red); }

/* ── up/down table ── */
.ud-row { display:grid; grid-template-columns:42px 1fr 48px 64px 36px 72px;
          gap:4px; padding:4px 10px; border-bottom:1px solid #1c2128; align-items:center; font-size:12px; }
.ud-row:last-child { border-bottom:none; }
.ud-row:hover { background:#1c2128; }

/* ── alpha signals ── */
.sig-row { display:grid; grid-template-columns:64px 1fr 52px 44px 50px 40px;
           gap:6px; padding:5px 10px; border-bottom:1px solid #1c2128; align-items:center; font-size:12px; }
.sig-row:last-child { border-bottom:none; }
.sig-row:hover { background:#1c2128; }
.type-badge { padding:2px 6px; border-radius:3px; font-size:10px; font-weight:700; letter-spacing:0.3px; white-space:nowrap; text-align:center; }
.type-flow    { background:#2a1a3a; color:var(--purple); border:1px solid #4a2a6a; }
.type-mom     { background:#3a2a0a; color:var(--yellow); border:1px solid #5a4a10; }
.type-arb     { background:#2d1b1b; color:var(--red);    border:1px solid #5a2a2a; }
.type-btcarb  { background:#1a2a3a; color:var(--blue);   border:1px solid #2a4a6a; }
.type-sweep   { background:#1a3a2a; color:var(--green);  border:1px solid #2d5a3d; }
.type-odds    { background:#1a2a1a; color:#39d353;       border:1px solid #2a4a2a; }
.dir-yes { color:var(--green); font-weight:700; }
.dir-no  { color:var(--red);   font-weight:700; }
.str-bar { display:inline-block; height:6px; border-radius:3px; background:var(--blue); vertical-align:middle; }

/* ── whale feed ── */
.wh-row { display:grid; grid-template-columns:52px 1fr 36px 50px 56px 68px 72px;
          gap:4px; padding:4px 10px; border-bottom:1px solid #1c2128; align-items:center; font-size:12px; }
.wh-row:last-child { border-bottom:none; }
.wh-row:hover { background:#1c2128; }
.wh-row.big { border-left:2px solid var(--yellow); background:#1a1600; }

/* ── shared ── */
.yes { color:var(--green); font-weight:700; }
.no  { color:var(--red);   font-weight:700; }
.dim { color:var(--mute); }
.pos { color:var(--green); }
.neg { color:var(--red); }
.num { font-variant-numeric:tabular-nums; }
.ticker { color:var(--blue); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.trunc  { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.empty { padding:12px 10px; color:var(--mute); font-size:12px; }

.flow-bar-wrap { width:60px; height:6px; background:#1c2128; border-radius:3px; display:inline-block; vertical-align:middle; }
.flow-bar { height:6px; border-radius:3px; }
.flow-yes { background:var(--green); }
.flow-no  { background:var(--red); }

footer { text-align:center; padding:8px; color:var(--mute); font-size:11px; border-top:1px solid var(--border); }
</style>
</head>
<body>
<header>
  <span class="logo">kalshi-scanner</span>
  <span id="spot-btc" class="spot-btc">BTC $—</span>
  <span id="spot-eth" class="spot-eth">ETH $—</span>
  <a href="/whales" class="nav-link">→ whales</a>
  <span class="clock" id="clock">--:--:--</span>
</header>

<div class="signal-banner" id="signal-banner">
  <div class="sig-direction" id="sig-dir">—</div>
  <div style="min-width:220px">
    <div style="font-size:13px;font-weight:700;margin-bottom:3px" id="sig-label">waiting for market data…</div>
    <div class="sig-conf">
      Confidence <span id="sig-conf-val">—</span>
      <span class="conf-bar-wrap"><div class="conf-bar" id="conf-bar" style="width:0%"></div></span>
    </div>
  </div>
  <div class="sig-stats" id="sig-stats"></div>
  <div class="sig-components" id="sig-components"></div>
  <span class="sig-ticker-label" id="sig-ticker"></span>
  <span id="sig-badge" style="display:none" class="sig-reset-badge">NEW MARKET</span>
</div>

<div class="history-strip" id="history-strip"><span style="color:var(--mute);font-size:11px">signal history loads after first market…</span></div>

<div class="layout">
  <div class="col">
    <div class="card grow" style="flex:2">
      <div class="card-header">
        <span class="card-title">Strike Ladder</span>
        <span class="card-meta" id="strike-meta">—</span>
      </div>
      <div class="card-body" id="strikes"><div class="empty">loading…</div></div>
    </div>
    <div class="card" style="flex:1;min-height:160px">
      <div class="card-header">
        <span class="card-title">15m / 1h Up-Down Markets</span>
        <span class="card-meta" id="ud-meta">—</span>
      </div>
      <div class="card-body" id="updown"><div class="empty">loading…</div></div>
    </div>
  </div>
  <div class="col">
    <div class="card" style="flex:1;min-height:180px">
      <div class="card-header">
        <span class="card-title">Crypto Alpha Signals</span>
        <span class="card-meta" id="sig-meta">—</span>
      </div>
      <div class="card-body" id="signals"><div class="empty">loading…</div></div>
    </div>
    <div class="card grow" style="flex:2">
      <div class="card-header">
        <span class="card-title">Crypto Whale Prints</span>
        <span class="card-meta" id="wh-meta">—</span>
      </div>
      <div class="card-body" id="cwhales"><div class="empty">loading…</div></div>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);

function fmt$(n, dec=0) { return n == null ? '—' : '$' + n.toLocaleString(undefined,{minimumFractionDigits:dec,maximumFractionDigits:dec}); }
function fmtP(n) { return n == null ? '—' : (n*100).toFixed(1)+'¢'; }
function fmtN(n) { if(n==null)return'—'; if(n>=1000)return(n/1000).toFixed(1)+'K'; return Math.round(n).toString(); }
function fmtDiff(d) {
  if(d==null)return'—';
  const s = d>=0 ? `+$${d.toLocaleString(undefined,{maximumFractionDigits:0})}` : `-$${Math.abs(d).toLocaleString(undefined,{maximumFractionDigits:0})}`;
  return `<span class="${d>=0?'diff-pos':'diff-neg'}">${s}</span>`;
}
function flowBar(bp, maxBP) {
  const pct = maxBP > 0 ? Math.min(Math.abs(bp)/maxBP, 1)*100 : 0;
  const cls = bp >= 0 ? 'flow-yes' : 'flow-no';
  return `<div class="flow-bar-wrap"><div class="flow-bar ${cls}" style="width:${pct.toFixed(0)}%"></div></div>`;
}
function typeBadge(t) {
  const map = {
    flow_divergence: ['FLOW','type-flow'],
    momentum:        ['MNTM','type-mom'],
    arb:             ['ARB', 'type-arb'],
    btc_strike_arb:  ['SARB','type-btcarb'],
    btc_ladder_sweep:['SWEP','type-sweep'],
    odds_edge:       ['ODDS','type-odds'],
  };
  const [label, cls] = map[t] || [t.slice(0,4).toUpperCase(), 'type-flow'];
  return `<span class="type-badge ${cls}">${label}</span>`;
}
function shortTicker(t, n=22) { return t.length>n ? '…'+t.slice(-(n-1)) : t; }

// ── Strike Ladder ────────────────────────────────────────────────────
function renderStrikes(rows, btc_spot, eth_spot) {
  if(!rows||!rows.length){$('strikes').innerHTML='<div class="empty">no crypto strike markets yet</div>';return;}
  $('strike-meta').textContent = rows.length + ' markets';
  let html = '';
  let curAsset = null;
  for(const r of rows) {
    if(r.asset !== curAsset) {
      curAsset = r.asset;
      const sp = r.asset === 'BTC' ? btc_spot : eth_spot;
      html += `<div class="strike-asset-row">
        <span style="color:var(--${r.asset==='BTC'?'orange':'purple'})">${r.asset}</span>
        <span class="dim">spot</span>
        <span style="font-variant-numeric:tabular-nums">${sp ? fmt$(sp) : '—'}</span>
      </div>`;
    }
    const itmLabel = r.itm ? '<span class="itm">ITM</span>' : '<span class="otm">OTM</span>';
    const stCls = r.stype === 'T' ? 'stype-t' : 'stype-b';
    const stLabel = r.stype === 'T' ? '▲' : '▼';
    const whaleStyle = r.whale_count >= 5 ? 'color:var(--red);font-weight:700' : r.whale_count >= 2 ? 'color:var(--yellow)' : 'color:var(--mute)';
    const bpCls = r.buy_pressure > 0 ? 'pos' : r.buy_pressure < 0 ? 'neg' : 'dim';
    const bpPfx = r.buy_pressure > 0 ? '+' : '';
    html += `<div class="strike-row">
      <span class="${stCls}">${stLabel}</span>
      <span class="num">${fmt$(r.strike)}</span>
      ${fmtDiff(r.diff)}
      ${itmLabel}
      <span class="num">${r.price ? fmtP(r.price) : '<span class="dim">—</span>'}</span>
      <span class="dim num">${fmtN(r.volume)}</span>
      <span style="${whaleStyle}">${r.whale_count}</span>
      <span class="${bpCls} num">${bpPfx}${fmtN(r.buy_pressure)}</span>
    </div>`;
  }
  $('strikes').innerHTML = html;
}

// ── Up/Down Markets ──────────────────────────────────────────────────
function renderUpDown(rows) {
  if(!rows||!rows.length){$('updown').innerHTML='<div class="empty">no active 15m/1h markets right now</div>';return;}
  $('ud-meta').textContent = rows.length + ' active';
  const maxBP = Math.max(...rows.map(r=>Math.abs(r.buy_pressure)),1);
  $('updown').innerHTML = rows.map(r => {
    const dir = r.direction === 'YES'
      ? '<span class="yes" style="font-size:11px;font-weight:800">▲UP</span>'
      : '<span class="no"  style="font-size:11px;font-weight:800">▼DN</span>';
    return `<div class="ud-row">
      ${dir}
      <span class="ticker trunc" title="${r.ticker}">${shortTicker(r.title||r.ticker,32)}</span>
      <span class="num dim">${r.price ? fmtP(r.price) : '—'}</span>
      <span class="num dim">${fmtN(r.volume)}</span>
      <span style="${r.whale_count>=3?'color:var(--yellow)':'color:var(--mute)'}">${r.whale_count}w</span>
      ${flowBar(r.buy_pressure, maxBP)}
    </div>`;
  }).join('');
}

// ── Alpha Signals ────────────────────────────────────────────────────
function renderSignals(rows) {
  if(!rows||!rows.length){$('signals').innerHTML='<div class="empty">no crypto signals yet</div>';return;}
  $('sig-meta').textContent = rows.length + ' signals';
  $('signals').innerHTML = rows.map(r => {
    const dirCls = r.direction==='yes'?'dir-yes':'dir-no';
    const dirLabel = r.direction==='yes'?'▲YES':'▼NO';
    const barW = Math.round(r.strength*40);
    const edgeSign = r.fair_value > r.kalshi_price ? '+' : '';
    const edgeCents = Math.round((r.fair_value - r.kalshi_price)*100);
    const edgeCls = edgeCents > 0 ? 'pos' : 'neg';
    return `<div class="sig-row" title="${r.detail||''}">
      ${typeBadge(r.type)}
      <span class="trunc dim" title="${r.title||r.ticker}">${shortTicker(r.title||r.ticker,28)}</span>
      <span class="${dirCls}">${dirLabel}</span>
      <span class="num dim">${fmtP(r.kalshi_price)}</span>
      <span class="num ${edgeCls}">${edgeSign}${edgeCents}¢</span>
      <div style="display:flex;align-items:center"><div class="str-bar" style="width:${barW}px"></div></div>
    </div>`;
  }).join('');
}

// ── Crypto Whale Feed ────────────────────────────────────────────────
function renderCWhales(rows) {
  if(!rows||!rows.length){$('cwhales').innerHTML='<div class="empty">no crypto whale prints yet</div>';return;}
  $('wh-meta').textContent = rows.length + ' prints';
  $('cwhales').innerHTML = rows.map(r => {
    const ts = r.ts_ms ? new Date(r.ts_ms).toISOString().slice(11,19) : '?';
    const side = r.side==='yes'?'<span class="yes">YES</span>':'<span class="no">NO</span>';
    const parts = r.ticker.split('-');
    const label = parts.slice(1).join('-') || r.ticker;
    const big = r.notional >= 500;
    const vs = r.vs_spot != null
      ? `<span class="${r.vs_spot>=0?'pos':'neg'}">${r.vs_spot>=0?'+$':'−$'}${Math.abs(r.vs_spot).toLocaleString()}</span>`
      : '<span class="dim">—</span>';
    return `<div class="wh-row${big?' big':''}">
      <span class="dim">${ts}</span>
      <span class="ticker trunc" title="${r.ticker}">${label}</span>
      ${side}
      <span class="num dim">${fmtN(r.contracts)}</span>
      <span class="num dim">${fmtP(r.price)}</span>
      <span class="num" style="color:var(--yellow)">$${Math.round(r.notional)}</span>
      ${vs}
    </div>`;
  }).join('');
}

// ── Signal history strip ─────────────────────────────────────────────
function renderHistory(rows) {
  if(!rows||!rows.length) return;
  const strip = $('history-strip');
  strip.innerHTML = rows.slice().reverse().map(r => {
    const isUp = r.direction === 'YES';
    const dirCls = isUp ? 'yes' : 'no';
    const dir = isUp ? '▲ YES' : '▼ NO';
    let outHtml = '<span class="dim" style="font-size:10px">pending…</span>';
    let cardCls = 'hist-card pending';
    if(r.outcome) {
      const ok = r.correct;
      outHtml = ok
        ? `<span class="pos" style="font-size:10px;font-weight:700">✓ ${r.outcome}</span>`
        : `<span class="neg" style="font-size:10px;font-weight:700">✗ ${r.outcome}</span>`;
      cardCls = 'hist-card ' + (ok ? 'correct' : 'wrong');
    }
    const ts = r.ts ? new Date(r.ts*1000).toISOString().slice(11,16) : '?';
    const label = r.ticker ? r.ticker.split('-').slice(-2).join('-') : '';
    const conf = r.conf != null ? r.conf + '%' : '?';
    return `<div class="${cardCls}" title="${r.ticker}">
      <span class="${dirCls}" style="font-weight:800;font-size:12px">${dir}</span>
      <span class="dim" style="font-size:10px">${conf} conf</span>
      <span class="dim" style="font-size:10px">${label}</span>
      ${outHtml}
      <span class="dim" style="font-size:9px">${ts} UTC</span>
    </div>`;
  }).join('');
}

// ── Sound alert ──────────────────────────────────────────────────────
let _audioCtx = null;
function playAlert(isUp) {
  try {
    if(!_audioCtx) _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = _audioCtx.createOscillator();
    const gain = _audioCtx.createGain();
    osc.connect(gain);
    gain.connect(_audioCtx.destination);
    osc.frequency.value = isUp ? 880 : 440;
    osc.type = 'sine';
    gain.gain.setValueAtTime(0.25, _audioCtx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, _audioCtx.currentTime + 0.6);
    osc.start(_audioCtx.currentTime);
    osc.stop(_audioCtx.currentTime + 0.6);
    // Second tone for strong signals
    const osc2 = _audioCtx.createOscillator();
    const gain2 = _audioCtx.createGain();
    osc2.connect(gain2);
    gain2.connect(_audioCtx.destination);
    osc2.frequency.value = isUp ? 1100 : 330;
    osc2.type = 'sine';
    gain2.gain.setValueAtTime(0, _audioCtx.currentTime);
    gain2.gain.setValueAtTime(0.15, _audioCtx.currentTime + 0.2);
    gain2.gain.exponentialRampToValueAtTime(0.001, _audioCtx.currentTime + 0.8);
    osc2.start(_audioCtx.currentTime + 0.2);
    osc2.stop(_audioCtx.currentTime + 0.8);
  } catch(e) { /* audio not available */ }
}

// ── Main refresh ─────────────────────────────────────────────────────
async function refresh() {
  try {
    const [spotR, strikesR, udR, sigsR, whR, histR] = await Promise.all([
      fetch('/api/crypto/spot').then(r=>r.json()),
      fetch('/api/crypto/strikes').then(r=>r.json()),
      fetch('/api/crypto/updown').then(r=>r.json()),
      fetch('/api/crypto/signals').then(r=>r.json()),
      fetch('/api/crypto/whales').then(r=>r.json()),
      fetch('/api/crypto/history').then(r=>r.json()),
    ]);

    if(spotR.btc) $('spot-btc').textContent = 'BTC ' + fmt$(spotR.btc);
    if(spotR.eth) $('spot-eth').textContent = 'ETH ' + fmt$(spotR.eth);

    renderStrikes(strikesR.rows, strikesR.btc_spot, strikesR.eth_spot);
    renderUpDown(udR.rows);
    renderSignals(sigsR.rows);
    renderCWhales(whR.rows);
    renderHistory(histR.rows);
  } catch(e) { console.error('refresh error', e); }
}

function tick() { $('clock').textContent = new Date().toISOString().slice(11,19)+' UTC'; }
tick(); setInterval(tick,1000);
refresh(); setInterval(refresh, 3000);

// ── Signal banner ────────────────────────────────────────────────────
let _lastTicker = null;
let _t1Timer = null;
let _lastAlertTicker = null;
let _lastConfAbove50 = false;

function fmt$2(n) { return n==null?'—':'$'+Math.round(n).toLocaleString(); }
function sigComp(label, val, suffix='') {
  if(val==null) return '';
  const cls = val > 8 ? 'bull' : val < -8 ? 'bear' : 'neut';
  const arrow = val > 8 ? '▲' : val < -8 ? '▼' : '▶';
  return `<span class="sig-comp ${cls}">${arrow} ${label}${suffix}</span>`;
}

function renderSignalBanner(s, isT1=false) {
  if(!s || s.status !== 'ok') return;
  const banner = $('signal-banner');
  const isUp = s.direction === 'YES';
  const dir = isUp ? '▲ UP' : '▼ DOWN';
  const dirCls = isUp ? 'up' : 'down';

  $('sig-dir').textContent = dir;
  $('sig-dir').className = 'sig-direction ' + dirCls;
  banner.className = 'signal-banner ' + dirCls + ' flash';
  setTimeout(() => banner.classList.remove('flash'), 700);

  // Label line: show buy side + spot vs strike if available
  const edgeCents = Math.round(Math.abs((isUp ? s.yes_pct/100 : (100-s.yes_pct)/100) - s.price) * 100);
  let spotStr = '';
  if(s.spot != null && s.floor_strike != null) {
    const dist = s.distance;
    const sign = dist >= 0 ? '+' : '';
    const distCls = dist >= 0 ? 'pos' : 'neg';
    spotStr = ` · spot ${fmt$2(s.spot)} vs ${fmt$2(s.floor_strike)} (<span class="${distCls}">${sign}$${Math.round(Math.abs(dist)).toLocaleString()}</span>)`;
  }
  const flowSrc = s.has_whale_data ? 'whales' : 'retail flow';
  $('sig-label').innerHTML = (isUp
    ? `BUY YES — ${flowSrc} ${s.yes_pct}% YES at ${(s.price*100).toFixed(1)}¢`
    : `BUY NO  — ${flowSrc} ${(100-s.yes_pct).toFixed(1)}% NO at ${((1-s.price)*100).toFixed(1)}¢`)
    + ` (edge ~${edgeCents}¢)` + spotStr;

  $('sig-conf-val').textContent = s.confidence + '%';
  const bar = $('conf-bar');
  bar.style.width = s.confidence + '%';
  bar.className = 'conf-bar' + (isUp ? '' : ' down');

  const minsStr = s.mins_left != null
    ? (s.mins_left < 0 ? 'expired' : s.mins_left.toFixed(1) + 'm left')
    : '';

  // Stats row: whale details
  const trendStr = s.whale_trend != null && Math.abs(s.whale_trend) > 2
    ? ` <span class="${s.whale_trend>0?'pos':'neg'}" style="font-size:10px">${s.whale_trend>0?'↑':'↓'}${Math.abs(s.whale_trend).toFixed(0)}</span>`
    : '';
  const spreadStr = s.spread != null
    ? `<span class="${s.spread>0.05?'neg':s.spread<0?'pos':'dim'}">${(s.spread*100).toFixed(1)}¢</span>`
    : '—';
  const volStr = s.btc_vol_per_min != null ? `±$${Math.round(s.btc_vol_per_min)}/min` : '';
  const flowLabel = s.has_whale_data ? 'Whale flow' : 'Retail flow';
  $('sig-stats').innerHTML = `
    <div class="sig-stat"><span class="k">${flowLabel}</span><span class="v" style="color:${isUp?'var(--green)':'var(--red)'}">${s.yes_pct}% YES${trendStr}${s.has_whale_data?'':' <span class="dim" style="font-size:10px">(no whales)</span>'}</span></div>
    <div class="sig-stat"><span class="k">YES / NO</span><span class="v"><span class="pos">${s.yes_contracts.toLocaleString()}</span> / <span class="neg">${s.no_contracts.toLocaleString()}</span></span></div>
    <div class="sig-stat"><span class="k">Whales</span><span class="v">${s.whale_count}</span></div>
    ${s.momentum!=null ? `<div class="sig-stat"><span class="k">Momo</span><span class="v ${s.momentum>=0?'pos':'neg'}">${s.momentum>=0?'+':''}${s.momentum.toFixed(0)}/min</span></div>` : ''}
    ${volStr ? `<div class="sig-stat"><span class="k">BTC vol</span><span class="v dim">${volStr}</span></div>` : ''}
    <div class="sig-stat"><span class="k">Spread</span><span class="v">${spreadStr}</span></div>
    ${minsStr ? `<div class="sig-stat"><span class="k">Expires</span><span class="v dim">${minsStr}</span></div>` : ''}
  `;

  // Signal component badges
  $('sig-components').innerHTML =
    sigComp('Whale', s.sig_whale) +
    sigComp('Spot', s.sig_spot) +
    sigComp('Momo', s.sig_momentum) +
    (s.sig_combined != null ? `<span class="sig-comp ${s.sig_combined>8?'bull':s.sig_combined<-8?'bear':'neut'}" style="font-size:12px;padding:3px 10px">NET ${s.sig_combined>0?'+':''}${s.sig_combined}</span>` : '');

  const parts = s.ticker.split('-');
  $('sig-ticker').textContent = parts.slice(1).join('-') || s.ticker;

  const badge = $('sig-badge');
  if(isT1) {
    badge.textContent = 'T+1 MIN UPDATE';
    badge.className = 'sig-reset-badge t1';
    badge.style.display = '';
    setTimeout(() => { badge.style.display = 'none'; }, 8000);
  }
}

async function pollSignal() {
  try {
    const s = await fetch('/api/crypto/signal').then(r=>r.json());

    if(s.status === 'between_markets' || s.status === 'no_active_market') {
      const banner = $('signal-banner');
      banner.className = 'signal-banner';
      $('sig-dir').textContent = '—';
      $('sig-dir').className = 'sig-direction';
      const mins = s.mins_to_open != null ? ` (opens in ${s.mins_to_open}m)` : '';
      $('sig-label').textContent = `Waiting for next 15m candle${mins}`;
      $('sig-stats').innerHTML = s.next_ticker ? `<div class="sig-stat"><span class="k">Next</span><span class="v dim">${s.next_ticker}</span></div>` : '';
      $('sig-ticker').textContent = '';
      $('sig-badge').style.display = 'none';
      return;
    }
    if(s.status !== 'ok') return;

    const isNew = _lastTicker !== null && s.ticker !== _lastTicker;

    if(isNew) {
      // New market detected — flash banner + schedule T+1 update
      const badge = $('sig-badge');
      badge.textContent = 'NEW MARKET';
      badge.className = 'sig-reset-badge';
      badge.style.display = '';
      setTimeout(() => { badge.style.display = 'none'; }, 8000);

      if(_t1Timer) clearTimeout(_t1Timer);
      _t1Timer = setTimeout(async () => {
        const s2 = await fetch('/api/crypto/signal').then(r=>r.json());
        renderSignalBanner(s2, true);
      }, 60000);
    }

    renderSignalBanner(s);
    _lastTicker = s.ticker;

    // Sound alert when confidence ≥ 50 and it's a new market or just crossed threshold
    const confAbove50 = s.confidence >= 50;
    if(confAbove50 && (s.ticker !== _lastAlertTicker || !_lastConfAbove50)) {
      playAlert(s.direction === 'YES');
      _lastAlertTicker = s.ticker;
    }
    _lastConfAbove50 = confAbove50;
  } catch(e) { console.error('signal poll error', e); }
}

pollSignal();
setInterval(pollSignal, 5000);
</script>
</body>
</html>"""

_WHALES_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>daedalus — BTC signals</title>
<style>
:root { --bg:#0d1117; --bg2:#161b22; --bg3:#21262d; --fg:#e6edf3; --mute:#7d8590;
        --border:#30363d; --green:#3fb950; --red:#f85149; --yellow:#d29922; --blue:#58a6ff; }
* { box-sizing:border-box; margin:0; padding:0; }
body { font-family:ui-monospace,"SF Mono","Fira Code",monospace; background:var(--bg); color:var(--fg); font-size:13px; }

header { padding:10px 20px; border-bottom:1px solid var(--border); background:var(--bg2);
         display:flex; align-items:center; justify-content:space-between; }
.logo { font-size:14px; font-weight:700; color:var(--blue); }
.clock { color:var(--mute); font-size:12px; }

.wrap { max-width:1000px; margin:16px auto; padding:0 16px; display:flex; flex-direction:column; gap:16px; }

.signals-grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
.card { background:var(--bg2); border:1px solid var(--border); border-radius:8px; overflow:hidden; }
.card-header { padding:8px 14px; border-bottom:1px solid var(--border); background:var(--bg3);
               display:flex; align-items:center; justify-content:space-between; }
.card-title { font-size:11px; text-transform:uppercase; letter-spacing:0.7px; color:var(--mute); }
.card-age { font-size:11px; color:var(--mute); }
.card-age.stale { color:var(--red); }
.card-body { padding:10px 14px; }

.sig-row { display:grid; grid-template-columns:22px auto 1fr 52px 72px 56px 58px;
           gap:6px; align-items:center; padding:7px 0; border-bottom:1px solid #1c2128; }
.sig-row:last-child { border-bottom:none; }
.sig-row.no-quote { opacity:0.45; }
.rank { color:var(--mute); font-size:11px; }

.dir-pill { padding:3px 9px; border-radius:4px; font-size:12px; font-weight:800;
            letter-spacing:0.3px; white-space:nowrap; }
.dir-yes { background:#1a3a2a; color:var(--green); border:1px solid #2d5a3d; }
.dir-no  { background:#2d1b1b; color:var(--red);   border:1px solid #5a2a2a; }

.sig-ticker { color:var(--blue); font-size:12px; overflow:hidden; text-overflow:ellipsis;
              white-space:nowrap; min-width:0; }
.sig-mid { text-align:right; font-variant-numeric:tabular-nums; font-size:13px; font-weight:600; }
.sig-mid.no-q { color:var(--mute); font-size:11px; }
.sig-flow { text-align:right; color:var(--yellow); font-size:12px; font-variant-numeric:tabular-nums; }
.sig-wc { text-align:right; color:var(--mute); font-size:11px; }

.expiry-ok     { color:var(--mute); font-size:11px; }
.expiry-soon   { color:var(--yellow); font-size:11px; font-weight:700; }
.expiry-urgent { color:var(--red);    font-size:11px; font-weight:700; }

.copy-btn { background:var(--bg3); border:1px solid var(--border); color:var(--mute);
            padding:2px 7px; border-radius:4px; font-size:10px; cursor:pointer;
            font-family:inherit; white-space:nowrap; }
.copy-btn:hover { border-color:var(--blue); color:var(--blue); }
.copy-btn.copied { border-color:var(--green); color:var(--green); }

.feed-row { display:grid; grid-template-columns:58px 1fr 42px 68px 60px 52px;
            gap:6px; padding:4px 0; border-bottom:1px solid #1c2128; align-items:center; }
.feed-row:last-child { border-bottom:none; }
.feed-row:hover { background:#161b22; }
.feed-row.hi { background:#131a0e; border-left:2px solid var(--green); padding-left:4px; }
.feed-row.hi.neg-hi { background:#160e0e; border-left-color:var(--red); }
.dim  { color:var(--mute); }
.pos  { color:var(--green); }
.neg  { color:var(--red); }
.yes  { color:var(--green); font-weight:700; }
.no   { color:var(--red);   font-weight:700; }
.fticker { color:var(--blue); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.amt  { text-align:right; color:var(--yellow); }
.zsc  { text-align:right; font-size:12px; }

footer { text-align:center; padding:10px; color:var(--mute); font-size:11px;
         border-top:1px solid var(--border); margin-top:4px; }
</style>
</head>
<body>
<header>
  <span class="logo">kalshi-scanner · BTC signals</span>
  <span class="clock" id="clock">--:--:--</span>
</header>
<div class="wrap">
  <div class="signals-grid">
    <div class="card">
      <div class="card-header">
        <span class="card-title">BTC 15-min — top signals</span>
        <span class="card-age" id="age-15m">—</span>
      </div>
      <div class="card-body" id="body-15m"><span class="dim">loading…</span></div>
    </div>
    <div class="card">
      <div class="card-header">
        <span class="card-title">BTC Daily — top signals</span>
        <span class="card-age" id="age-d">—</span>
      </div>
      <div class="card-body" id="body-d"><span class="dim">loading…</span></div>
    </div>
  </div>
  <div class="card">
    <div class="card-header">
      <span class="card-title">BTC whale prints — today</span>
      <span class="card-age" id="feed-count"></span>
    </div>
    <div class="card-body" id="feed"><span class="dim">loading…</span></div>
  </div>
</div>
<footer>auto-refresh 2s &middot; copy button copies full ticker to clipboard &middot; greyed row = no quote available</footer>

<script>
function copyTicker(btn, ticker) {
  navigator.clipboard.writeText(ticker).then(() => {
    btn.textContent = 'copied!';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = 'copy'; btn.classList.remove('copied'); }, 1500);
  });
}

function parseExpiry15m(ticker) {
  const m = ticker.match(/(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})-/);
  if (!m) return null;
  const months = {JAN:0,FEB:1,MAR:2,APR:3,MAY:4,JUN:5,JUL:6,AUG:7,SEP:8,OCT:9,NOV:10,DEC:11};
  // Ticker times are US Eastern — convert ET to UTC by building a local date string
  const etStr = `20${m[1]}-${String(months[m[2]]+1).padStart(2,'0')}-${m[3].padStart(2,'0')}T${m[4]}:${m[5]}:00`;
  // Use Intl to get ET offset then adjust
  const etDate = new Date(etStr + ' GMT-0400'); // EDT (May = summer, UTC-4)
  return etDate;
}

function expiryStr(ticker, is15m) {
  if (!is15m) return '';
  const exp = parseExpiry15m(ticker);
  if (!exp) return '';
  const mins = (exp - Date.now()) / 60000;
  if (mins < 0)  return '<span class="expiry-ok dim">expired</span>';
  if (mins < 3)  return `<span class="expiry-urgent">${mins.toFixed(0)}m</span>`;
  if (mins < 8)  return `<span class="expiry-soon">${mins.toFixed(0)}m</span>`;
  return `<span class="expiry-ok">${mins.toFixed(0)}m</span>`;
}

function renderSignals(bodyId, ageId, data, is15m) {
  const ageEl = document.getElementById(ageId);
  const bodyEl = document.getElementById(bodyId);
  if (!data) { bodyEl.innerHTML = '<span class="dim">no data</span>'; ageEl.textContent = '—'; return; }
  const age = data.age_s ?? 0;
  ageEl.textContent = age + 's ago';
  ageEl.className = 'card-age' + (age > 120 ? ' stale' : '');
  if (!data.markets || !data.markets.length) { bodyEl.innerHTML = '<span class="dim">no markets</span>'; return; }

  bodyEl.innerHTML = data.markets.map((m, i) => {
    const t = m.ticker || '';
    const hasQ = m.mid != null;
    const dirCls = m.direction === 'YES' ? 'dir-yes' : 'dir-no';
    const dirLabel = m.direction === 'YES' ? '▲ YES' : '▼ NO';
    const midHtml = hasQ
      ? `<span class="sig-mid">${(m.mid*100).toFixed(1)}¢</span>`
      : `<span class="sig-mid no-q">no quote</span>`;
    const flow = Math.abs(m.net_notional ?? m.score ?? 0);
    const wc = m.whale_count ?? 0;
    const parts = t.split('-');
    const label = is15m ? parts.slice(1).join('-') : parts.slice(2).join('-') || t;
    const rowCls = hasQ ? 'sig-row' : 'sig-row no-quote';
    return `<div class="${rowCls}">
      <span class="rank">#${i+1}</span>
      <span class="dir-pill ${dirCls}">${dirLabel}</span>
      <span class="sig-ticker" title="${t}">${label}</span>
      ${midHtml}
      <span class="sig-flow">$${flow.toFixed(0)}</span>
      <span class="sig-wc">${wc}w ${expiryStr(t, is15m)}</span>
      <button class="copy-btn" onclick="copyTicker(this,'${t}')">copy</button>
    </div>`;
  }).join('');
}

function renderFeed(rows) {
  const el = document.getElementById('feed');
  const countEl = document.getElementById('feed-count');
  const btc = (rows||[]).filter(r => (r.ticker||'').startsWith('KXBTC'));
  countEl.textContent = btc.length ? `${btc.length} prints today` : '';
  if (!btc.length) { el.innerHTML = '<span class="dim">no BTC whale prints yet today</span>'; return; }
  el.innerHTML = btc.slice(0,50).map(r => {
    const ts = r.ts_ms ? new Date(r.ts_ms).toISOString().slice(11,19) : '?';
    const side = r.taker_side === 'yes' ? '<span class="yes">YES</span>' : '<span class="no">NO</span>';
    const t = r.ticker || '';
    const label = t.split('-').slice(1).join('-') || t;
    const not = r.notional_usd != null ? '$'+r.notional_usd.toFixed(0) : '—';
    const z = r.z_score != null ? r.z_score.toFixed(1) : '?';
    const absZ = Math.abs(r.z_score||0);
    const zCls = absZ >= 2 ? (r.z_score > 0 ? 'pos' : 'neg') : 'dim';
    const hiCls = absZ >= 3 ? (r.z_score > 0 ? ' hi' : ' hi neg-hi') : '';
    return `<div class="feed-row${hiCls}">
      <span class="dim">${ts}</span>
      <span class="fticker" title="${t}">${label}</span>
      ${side}
      <span class="amt">${not}</span>
      <button class="copy-btn" onclick="copyTicker(this,'${t}')">copy</button>
      <span class="zsc ${zCls}">z=${z}</span>
    </div>`;
  }).join('');
}

async function refresh() {
  try {
    const [{rows}, btc] = await Promise.all([
      fetch('/api/whales?limit=200').then(r=>r.json()),
      fetch('/api/btc').then(r=>r.json()),
    ]);
    renderSignals('body-15m','age-15m', btc.btc_15m, true);
    renderSignals('body-d',  'age-d',   btc.btc_d,   false);
    renderFeed(rows);
  } catch(e) { console.error(e); }
}

function tick() { document.getElementById('clock').textContent = new Date().toISOString().slice(11,19)+' UTC'; }
tick(); setInterval(tick, 1000);
refresh(); setInterval(refresh, 2000);
</script>
</body>
</html>"""
