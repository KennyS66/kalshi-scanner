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

import base64
import os

import httpx
import requests as _requests
import uvicorn
from cryptography.hazmat.primitives import hashes as _hashes, serialization as _serialization
from cryptography.hazmat.primitives.asymmetric import padding as _padding
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

_scanner = None
_alpha_engine = None

# BTC/ETH spot price history — filled by background poller every 5s
_btc_spot_history: collections.deque = collections.deque(maxlen=40)
_eth_spot_latest: float | None = None   # single latest ETH price
_btc_spot_lock = threading.Lock()
# Spot poller health — lets /api/crypto/spot report staleness instead of
# serving a frozen last-known price with no warning (the collector is flaky).
_spot_last_ok_ts: float = 0.0
_spot_consec_errors: int = 0
_SPOT_ERR_LOG_THRESHOLD = 3
# Kalshi floor_strike per ticker — fetched once per market
_market_floor_strike: dict[str, float] = {}
_market_floor_strike_inflight: set = set()  # prevent concurrent duplicate fetches
_floor_strike_absent: set = set()  # tickers Kalshi reports with null floor_strike — don't refetch
# Whale flow history per ticker — (ts, yes_pct) pairs
_whale_flow_history: dict[str, collections.deque] = {}
# Signal decision log — last 20 market calls with outcomes
_signal_log: list[dict] = []
_signal_log_lock = threading.Lock()

app = FastAPI(title="kalshi-scanner")

_DATA_DIR = Path("data/whales")

# Durable, full-resolution signal feature log (for backtesting the actual
# flush-bounce / NO entry conditions, which need flush_score / buy_pressure /
# sig_* — fields the older picks_log never captured). Throttled per ticker so
# dashboard polling doesn't bloat the file; one row every few seconds is plenty.
_SIGNAL_FEATURE_LOG = _DATA_DIR / "signal_feature_log.jsonl"
_feature_log_lock = threading.Lock()
_feature_log_last_ts: dict[str, float] = {}
_FEATURE_LOG_MIN_GAP_S = 4.0

_LOOP_LOG_FILE = _DATA_DIR / "loop_log.jsonl"
_loop_log_lock = threading.Lock()


def _log_signal_features(row: dict) -> None:
    """Append a full signal snapshot, throttled to one row per ticker per few seconds."""
    ticker = row.get("ticker")
    now = row.get("ts") or time.time()
    try:
        with _feature_log_lock:
            last = _feature_log_last_ts.get(ticker, 0.0)
            if now - last < _FEATURE_LOG_MIN_GAP_S:
                return
            _feature_log_last_ts[ticker] = now
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            with _SIGNAL_FEATURE_LOG.open("a") as f:
                f.write(json.dumps(row) + "\n")
    except Exception:
        pass

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


def _floor_strike_poller_loop() -> None:
    """Background thread: fetch floor_strike for the active market every 10s."""
    import urllib.request as ur
    while True:
        time.sleep(10)
        if _scanner is None:
            continue
        try:
            live = [t for t in list(_scanner.market_snapshots.keys())
                    if "KXBTC15M" in t.upper()]
        except Exception:
            continue
        pending = [t for t in live
                   if _market_floor_strike.get(t) is None
                   and t not in _floor_strike_absent
                   and t not in _market_floor_strike_inflight]
        for ticker in pending:
            # suppress per ticker: one failed fetch must not skip the rest
            with contextlib.suppress(Exception):
                req = ur.Request(
                    f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}",
                    headers={"User-Agent": "kalshi-scanner/1.0"},
                )
                with ur.urlopen(req, timeout=8) as resp:
                    mkt = json.loads(resp.read()).get("market", {})
                    fs = mkt.get("floor_strike")
                    if fs is not None:
                        _market_floor_strike[ticker] = float(fs)
                    else:
                        _floor_strike_absent.add(ticker)
                    _market_floor_strike_inflight.discard(ticker)
        # Prune per-ticker state for expired markets (a new 15m ticker every
        # 15 min otherwise grows these until restart).
        if len(_market_floor_strike) > 200:
            keep = set(live)
            for d in (_market_floor_strike, _whale_flow_history, _feature_log_last_ts):
                for k in [k for k in d if k not in keep]:
                    d.pop(k, None)
            _floor_strike_absent.intersection_update(keep)


def _btc_spot_poller_loop(interval: float = 5.0) -> None:
    """Background thread: poll BTC+ETH spot from Coinbase every 5s. Caches results so
    the /api/crypto/spot route never needs to make its own outbound HTTP calls."""
    global _eth_spot_latest, _spot_last_ok_ts, _spot_consec_errors
    import urllib.request as ur
    while True:
        try:
            req = ur.Request("https://api.exchange.coinbase.com/products/BTC-USD/ticker",
                             headers={"User-Agent": "kalshi-scanner/1.0"})
            with ur.urlopen(req, timeout=4) as r:
                data = json.loads(r.read())
                price = float(data["price"])
                with _btc_spot_lock:
                    _btc_spot_history.append((time.time(), price))
            if _spot_consec_errors >= _SPOT_ERR_LOG_THRESHOLD:
                print(f"[spot-poller] recovered after {_spot_consec_errors} consecutive failures", flush=True)
            _spot_consec_errors = 0
            _spot_last_ok_ts = time.time()
        except Exception as e:
            _spot_consec_errors += 1
            if _spot_consec_errors == _SPOT_ERR_LOG_THRESHOLD:
                print(f"[spot-poller] BTC spot failing ({type(e).__name__}: {e}); "
                      f"last success {time.time() - _spot_last_ok_ts:.0f}s ago", flush=True)
        with contextlib.suppress(Exception):
            req = ur.Request("https://api.exchange.coinbase.com/products/ETH-USD/ticker",
                             headers={"User-Agent": "kalshi-scanner/1.0"})
            with ur.urlopen(req, timeout=4) as r:
                data = json.loads(r.read())
                _eth_spot_latest = float(data["price"])
        time.sleep(interval)


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


# ── Kalshi account poller ─────────────────────────────────────────────────────
_KALSHI_HOST  = "https://api.elections.kalshi.com"
_KALSHI_ENV   = Path.home() / ".kalshi" / "trading.env"
_account_cache: dict = {"balance": None, "positions": [], "fills": [], "ts": 0, "error": None,
                        "fills_enabled": True, "fills_key_configured": False,
                        "total_realized_pnl": 0.0, "total_unrealized_pnl": 0.0}
_account_lock = threading.Lock()
_fills_enabled = True

_TRADE_LOG_FILE = _DATA_DIR / "trade_log.jsonl"
_seen_fill_ids: set = set()


def _load_seen_fill_ids() -> set:
    ids = set()
    if _TRADE_LOG_FILE.exists():
        for line in _TRADE_LOG_FILE.read_text().splitlines():
            with contextlib.suppress(Exception):
                ids.add(json.loads(line)["fill_id"])
    return ids


def _append_trade_log(new_fills: list) -> None:
    if not new_fills:
        return
    with open(_TRADE_LOG_FILE, "a") as f:
        for rec in new_fills:
            f.write(json.dumps(rec) + "\n")


def _read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if _KALSHI_ENV.exists():
        for line in _KALSHI_ENV.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def _kalshi_creds():
    env = _read_env()
    kid = env.get("KALSHI_API_KEY_ID") or os.environ.get("KALSHI_API_KEY_ID", "")
    kp  = env.get("KALSHI_PRIVATE_KEY_PATH") or os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    return kid or None, kp or None


def _kalshi_fills_creds():
    """Separate fills key (KALSHI_FILLS_KEY_ID + KALSHI_FILLS_KEY_PATH in trading.env).
    Falls back to None/None if not configured — caller will skip fills or use main key."""
    env = _read_env()
    kid = env.get("KALSHI_FILLS_KEY_ID") or os.environ.get("KALSHI_FILLS_KEY_ID", "")
    kp  = env.get("KALSHI_FILLS_KEY_PATH") or os.environ.get("KALSHI_FILLS_KEY_PATH", "")
    return kid or None, kp or None


def _kalshi_get(kid: str, pk, path: str) -> dict:
    ts = str(int(time.time() * 1000))
    # Kalshi signs the bare path only — query params must be stripped before
    # signing (they still go out on the actual request URL below).
    sign_path = path.split("?", 1)[0]
    sig = pk.sign(
        f"{ts}GET{sign_path}".encode(),
        _padding.PSS(mgf=_padding.MGF1(_hashes.SHA256()), salt_length=_padding.PSS.MAX_LENGTH),
        _hashes.SHA256(),
    )
    headers = {
        "KALSHI-ACCESS-KEY": kid,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
    }
    r = _requests.get(_KALSHI_HOST + path, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()


def _account_poller_loop(interval: float = 20.0) -> None:
    global _fills_enabled
    # Account/positions/fills always come from ~/.kalshi/trading.env — this is
    # the user's real manual-trading account, deliberately separate from
    # whatever key the scanner uses for market data (e.g. daedalus-mm's).
    # Retry credential/key loading forever instead of exiting: the thread is
    # started once at boot, and this box powers on with ~/.kalshi sometimes
    # briefly unavailable — a permanent bail here means /api/account serves an
    # error until the whole scanner is restarted.
    while True:
        kid, kp = _kalshi_creds()
        if not kid or not kp:
            with _account_lock:
                _account_cache["error"] = "no credentials (~/.kalshi/trading.env)"
            time.sleep(60)
            continue
        try:
            with open(kp, "rb") as f:
                pk = _serialization.load_pem_private_key(f.read(), password=None)
        except Exception as e:
            with _account_lock:
                _account_cache["error"] = f"key error: {e}"
            time.sleep(60)
            continue
        break

    _seen_fill_ids.update(_load_seen_fill_ids())

    # Load fills-specific key (optional; falls back to main key if not configured)
    fills_kid, fills_kp = _kalshi_fills_creds()
    fills_pk = None
    fills_key_ok = False
    if fills_kid and fills_kp:
        try:
            with open(fills_kp, "rb") as f:
                fills_pk = _serialization.load_pem_private_key(f.read(), password=None)
            fills_key_ok = True
        except Exception as e:
            fills_kid = None
    with _account_lock:
        _account_cache["fills_key_configured"] = fills_key_ok

    while True:
        errors = []
        update: dict = {"ts": time.time(), "error": None}
        update["fills_enabled"] = _fills_enabled
        update["fills_key_configured"] = fills_key_ok

        # Balance
        try:
            bal = _kalshi_get(kid, pk, "/trade-api/v2/portfolio/balance")
            update["balance"] = bal.get("balance_dollars") or round((bal.get("balance") or 0) / 100, 2)
        except Exception as e:
            errors.append(f"balance: {e}")

        # Positions
        try:
            pos = _kalshi_get(kid, pk, "/trade-api/v2/portfolio/positions")
            positions = []
            for p in pos.get("market_positions", []):
                qty = float(p.get("position_fp") or 0)
                if qty == 0:
                    continue
                side = "yes" if qty > 0 else "no"
                ticker = p.get("ticker", "")
                exposure = round(float(p.get("market_exposure_dollars") or 0), 4)
                last_price = None
                unrealized_pnl = None
                try:
                    mkt = _requests.get(
                        f"{_KALSHI_HOST}/trade-api/v2/markets/{ticker}", timeout=5
                    ).json().get("market", {})
                    lp = mkt.get("last_price_dollars")
                    # No recent trade → leave PnL unmarked; marking against 0
                    # shows a fake 100% loss (and values the NO side at $1.00).
                    if lp:
                        yes_price = float(lp)
                        last_price = yes_price if side == "yes" else round(1 - yes_price, 4)
                        market_value = abs(qty) * last_price
                        unrealized_pnl = round(market_value - exposure, 4)
                except Exception:
                    pass
                positions.append({
                    "ticker":         ticker,
                    "side":           side,
                    "qty":            abs(qty),
                    "exposure":       exposure,
                    "realized_pnl":   round(float(p.get("realized_pnl_dollars") or 0), 4),
                    "unrealized_pnl": unrealized_pnl,
                    "last_price":     last_price,
                })
            update["positions"] = positions
            update["total_realized_pnl"] = round(sum(p["realized_pnl"] for p in positions), 4)
            update["total_unrealized_pnl"] = round(
                sum(p["unrealized_pnl"] for p in positions if p["unrealized_pnl"] is not None), 4
            )
        except Exception as e:
            errors.append(f"positions: {e}")

        # Fills — use fills-specific key if configured; skip entirely if disabled
        if _fills_enabled:
            f_kid = fills_kid if fills_key_ok else kid
            f_pk  = fills_pk  if fills_key_ok else pk
            try:
                try:
                    fls = _kalshi_get(f_kid, f_pk, "/trade-api/v2/portfolio/fills?limit=20")
                except Exception as e:
                    # Fills-specific key lacks permission — retry with the main
                    # (working) key rather than leaving fills permanently empty.
                    if "401" in str(e) and f_kid is not kid:
                        fls = _kalshi_get(kid, pk, "/trade-api/v2/portfolio/fills?limit=20")
                    else:
                        raise
                fills = []
                new_journal_recs = []
                for f in fls.get("fills", []):
                    side = f.get("side", "")
                    price_key = "yes_price_dollars" if side == "yes" else "no_price_dollars"
                    fill_id = f.get("fill_id", "")
                    rec = {
                        "fill_id": fill_id,
                        "ticker": f.get("ticker", ""),
                        "side":   side,
                        "qty":    float(f.get("count_fp") or 0),
                        "price":  round(float(f.get(price_key) or 0), 4),
                        "ts":     f.get("created_time", ""),
                        "action": f.get("action", "buy"),
                    }
                    fills.append(rec)
                    if fill_id and fill_id not in _seen_fill_ids:
                        _seen_fill_ids.add(fill_id)
                        new_journal_recs.append(rec)
                _append_trade_log(new_journal_recs)
                update["fills"] = fills[:20]
                update.pop("fills_note", None)
            except Exception as e:
                err_str = str(e)
                if "401" not in err_str:
                    errors.append(f"fills: {e}")
                else:
                    update["fills_note"] = "fills key lacks permission (401)"
        else:
            update["fills"] = []
            update["fills_note"] = "fills polling disabled"

        if errors:
            update["error"] = " | ".join(errors)
        with _account_lock:
            _account_cache.update(update)

        time.sleep(interval)


def start_background(port: int = 9050) -> threading.Thread:
    threading.Thread(target=_pick_writer_loop, daemon=True).start()
    threading.Thread(target=_floor_strike_poller_loop, daemon=True).start()
    threading.Thread(target=_btc_spot_poller_loop, daemon=True).start()
    threading.Thread(target=_outcome_checker_loop, daemon=True).start()
    threading.Thread(target=_calibration_loop, daemon=True).start()
    threading.Thread(target=_next_market_poller_loop, daemon=True).start()
    threading.Thread(target=_account_poller_loop, daemon=True).start()
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


@app.get("/api/debug/strike")
async def api_debug_strike() -> JSONResponse:
    return JSONResponse({"_market_floor_strike": {k: v for k, v in _market_floor_strike.items()}, "inflight": list(_market_floor_strike_inflight)})

@app.get("/api/crypto/spot")
async def api_crypto_spot() -> JSONResponse:
    # Serve from the in-memory cache maintained by the background poller (runs every 5s).
    # NEVER make outbound HTTP calls per-request — that blocks the event loop.
    # If cache is cold (first few seconds after start), return null immediately;
    # the poller will populate it within 5 seconds.
    hist = list(_btc_spot_history)
    btc = hist[-1][1] if hist else None
    eth = _eth_spot_latest
    ts = hist[-1][0] if hist else None
    age = round(time.time() - ts, 1) if ts else None
    return JSONResponse({"btc": btc, "eth": eth, "ts": ts, "age_s": age,
                         "stale": bool(age is None or age > 15)})


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

    # Find active (unsettled) BTC 15m market — prefer soonest FUTURE expiry.
    # Two-pass: first pass only considers markets that haven't expired yet;
    # second pass allows the 2-min grace window. This ensures a newly opened
    # market is always preferred over the one that just expired.
    active = None
    active_exp = None
    _now_ts = time.time()
    for _grace_ok in (False, True):
        for ticker, snap in list(_scanner.market_snapshots.items()):
            if "KXBTC15M" not in ticker.upper():
                continue
            price = snap.last_price or snap.yes_price or 0
            if not (0.01 < price < 0.99):
                continue
            exp = _ticker_expiry(ticker)
            if exp is None:
                continue
            age_s = _now_ts - exp.timestamp()
            if age_s > 120:
                continue  # expired more than 2 minutes ago
            if age_s > 0 and not _grace_ok:
                continue  # skip expired markets in first pass
            if active is None or exp < active_exp:
                active = (ticker, snap)
                active_exp = exp
        if active:
            break

    # Fallback: scanner hasn't seen this market yet — fetch directly from Kalshi.
    # Uses _next_market_cache (real scheduled times) instead of clock arithmetic.
    _direct_mkt: dict | None = None
    if not active:
        nm = _next_market_cache
        nm_fresh = nm and time.time() - nm.get("ts", 0) < _NEXT_MARKET_CACHE_TTL
        if nm_fresh:
            _dticker = nm["ticker"]
            _open_ts = nm["open_ts"]
            _close_ts = nm["close_ts"]
            _market_is_open = _open_ts <= time.time() < _close_ts
        else:
            # Cold start: no cache yet — fall back to clock arithmetic briefly
            _dticker = _market_is_open = None
            try:
                next_suffix, next_ts = _next_15m_expiry()
                _dticker = f"KXBTC15M-{next_suffix}"
                _market_is_open = 0 < (next_ts - time.time()) / 60 <= 15
            except Exception:
                pass

        if _dticker and _market_is_open:
            with contextlib.suppress(Exception):
                _cached = _between_markets_cache.get(_dticker)
                if _cached and time.time() - _cached["ts"] < _BETWEEN_MARKETS_TTL:
                    _direct_mkt = _cached["resp"]
                else:
                    async def _bg_fetch_between(dt=_dticker):
                        import urllib.request as _ur
                        try:
                            def _do():
                                req = _ur.Request(
                                    f"https://api.elections.kalshi.com/trade-api/v2/markets/{dt}",
                                    headers={"User-Agent": "kalshi-scanner/1.0"},
                                )
                                with _ur.urlopen(req, timeout=3) as _r:
                                    return json.loads(_r.read())
                            _resp = await asyncio.get_running_loop().run_in_executor(None, _do)
                            mkt = _resp.get("market", {})
                            if mkt.get("status") in ("active", "open"):
                                _between_markets_cache[dt] = {"resp": {
                                    "ticker": dt,
                                    "price": float(mkt.get("last_price_dollars") or 0.5),
                                    "floor_strike": float(mkt["floor_strike"]) if mkt.get("floor_strike") else None,
                                    "yes_ask": float(mkt.get("yes_ask_dollars") or 0),
                                    "no_ask": float(mkt.get("no_ask_dollars") or 0),
                                }, "ts": time.time()}
                                # Also kick the next-market cache refresh so it
                                # advances past this ticker on the next poll.
                                # Blocking urlopen (up to 6s) — must run off the event loop,
                                # same as the market fetch above.
                                await asyncio.get_running_loop().run_in_executor(
                                    None, _refresh_next_market_cache
                                )
                        except Exception:
                            pass
                    asyncio.create_task(_bg_fetch_between())

    if not active and _direct_mkt is None:
        # Return the real next market time from cache, or fall back to arithmetic
        nm = _next_market_cache
        nm_fresh = nm and time.time() - nm.get("ts", 0) < _NEXT_MARKET_CACHE_TTL
        if nm_fresh:
            next_ticker = nm["ticker"]
            mins_to_open = round((nm["open_ts"] - time.time()) / 60, 1)
        else:
            try:
                next_suffix, next_ts = _next_15m_expiry()
                next_ticker = f"KXBTC15M-{next_suffix}"
                mins_to_open = round((next_ts - time.time()) / 60, 1)
            except Exception:
                return JSONResponse({"status": "no_active_market"})
        return JSONResponse({
            "status": "between_markets",
            "next_ticker": next_ticker,
            "mins_to_open": max(0.0, mins_to_open),
        })

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

    # ── Floor strike (fire-and-forget background fetch) ─────────────────
    # Never await the Kalshi call inline — that blocks the route handler.
    # Instead kick off an asyncio Task; the signal route returns immediately
    # with floor_strike=None on the first poll, then hits the cache on retry.
    if _market_floor_strike.get(ticker) is None and ticker not in _market_floor_strike_inflight:
        _market_floor_strike_inflight.add(ticker)
        async def _bg_fetch_fs(t=ticker):
            import urllib.request as _ur
            try:
                def _do():
                    req = _ur.Request(
                        f"https://api.elections.kalshi.com/trade-api/v2/markets/{t}",
                        headers={"User-Agent": "kalshi-scanner/1.0"},
                    )
                    with _ur.urlopen(req, timeout=3) as _r:
                        return json.loads(_r.read())
                _resp = await asyncio.get_running_loop().run_in_executor(None, _do)
                fs = _resp.get("market", {}).get("floor_strike")
                if fs is not None:
                    _market_floor_strike[t] = float(fs)
            except Exception:
                pass
            finally:
                _market_floor_strike_inflight.discard(t)
        asyncio.create_task(_bg_fetch_fs())

    floor_strike = _market_floor_strike.get(ticker)

    # ── BTC spot + momentum from poller history ──────────────────────────
    # Read without lock — deque is GIL-safe for list() copy; avoids blocking
    # the async event loop on a threading.Lock that background threads may hold.
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

    # ── Flush detector ───────────────────────────────────────────────────
    # When spot is below strike but buy pressure is strongly positive,
    # the market is absorbing the drop — suppress aggressive NO calls.
    is_flush = False
    flush_score = 0
    _bp = snap.buy_pressure if snap else 0
    if (distance is not None and distance < -5 and
            _bp > 5000 and mins_remaining > 3):
        flush_score = min(100, int(_bp / 300))
        if flush_score >= 20:
            is_flush = True
            combined = max(combined, -0.15)  # floor — weaken NO, don't force YES

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

    payload = {
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
        # Bid/ask spread + individual asks
        "spread": spread,
        "yes_ask": round(yes_ask, 4),
        "no_ask": round(no_ask, 4),
        # Momentum component
        "momentum": momentum,
        # Flush detector
        "is_flush": is_flush,
        "flush_score": flush_score,
        # Raw signal values (-100 to +100)
        "sig_whale": round(whale_signal * 100),
        "sig_spot": round(spot_signal * 100),
        "sig_momentum": round(momentum_signal * 100),
        "sig_combined": round(combined * 100),
        "mins_left": mins_left,
        "ts": time.time(),
        # Age of the spot price feeding this payload — "ts" above is when the
        # payload was built, which makes a frozen spot look fresh otherwise.
        "spot_age_s": round(time.time() - _spot_last_ok_ts, 1) if _spot_last_ok_ts else None,
    }

    # Persist the full feature snapshot so the real entry signals become
    # backtestable later (joined against settled outcomes per ticker).
    # Off the event loop: the append hits a multi-MB jsonl and a slow disk
    # would stall every route.
    asyncio.get_running_loop().run_in_executor(None, _log_signal_features, payload)

    return JSONResponse(payload)



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
        # scan() runs 8 detectors synchronously over all market snapshots (~140ms) —
        # offload it so it doesn't block the single event loop for every poller.
        sigs = await asyncio.get_running_loop().run_in_executor(
            None, _alpha_engine.get_top_signals, 80
        )
        rows = []
        for s in sigs:
            is_15m_btc = "KXBTC15M" in s.ticker.upper()
            is_btc_context = (
                "KXBTC" in s.ticker.upper()
                or "btc_" in s.signal_type
                or "ladder" in s.signal_type
                or "btcarb" in s.signal_type
            )
            if not is_15m_btc and not is_btc_context:
                continue
            # Non-15m BTC signals: only surface when they have real signal strength
            if not is_15m_btc and is_btc_context:
                strength = s.strength or 0
                edge = abs(s.edge_pct or 0)
                if strength < 0.5 and edge < 5:
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
                "context": not is_15m_btc,
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
        if strike is None:
            strike = _market_floor_strike.get(alert.ticker)
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


_between_markets_cache: dict = {}   # {"resp": dict, "ts": float}
_BETWEEN_MARKETS_TTL = 20.0         # re-hit Kalshi at most once per 20s

# Cache for the REAL next scheduled KXBTC15M market (polled every 5 min)
_next_market_cache: dict = {}  # {"ticker", "open_ts", "close_ts", "ts"}
_NEXT_MARKET_CACHE_TTL = 300.0


def _refresh_next_market_cache() -> None:
    """Fetch the earliest upcoming KXBTC15M market from Kalshi and cache open/close times.

    Kalshi's unfiltered /markets listing sorts newest-created first, so an
    unfiltered query returns only far-future not-yet-open markets — never the
    currently active one. Query status=open first (the live market, if any);
    fall back to the unfiltered listing only if nothing is open right now.
    """
    import urllib.request as _ur
    from datetime import datetime, timezone

    def _fetch(url: str) -> list[dict]:
        req = _ur.Request(url, headers={"User-Agent": "kalshi-scanner/1.0"})
        with _ur.urlopen(req, timeout=6) as r:
            data = json.loads(r.read())
        mkts = [m for m in data.get("markets", []) if m.get("close_time")]
        mkts.sort(key=lambda m: m["close_time"])
        return mkts

    try:
        mkts = _fetch(
            "https://api.elections.kalshi.com/trade-api/v2/markets"
            "?series_ticker=KXBTC15M&status=open&limit=5"
        )
        if not mkts:
            mkts = _fetch(
                "https://api.elections.kalshi.com/trade-api/v2/markets"
                "?series_ticker=KXBTC15M&limit=10"
            )
        for m in mkts:
            close_ts = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp()
            open_ts = close_ts - 900  # 15 min before close
            # Use the market currently open, or the next one to open.
            # Rebind atomically rather than .update() in place: this runs from
            # two threads while the event loop reads several keys — an in-place
            # update can expose the new ticker with the old open/close times
            # right at the market-roll boundary.
            if time.time() < close_ts:
                global _next_market_cache
                _next_market_cache = {
                    "ticker": m["ticker"],
                    "open_ts": open_ts,
                    "close_ts": close_ts,
                    "ts": time.time(),
                }
                return
    except Exception:
        pass


def _next_market_poller_loop() -> None:
    while True:
        _refresh_next_market_cache()
        time.sleep(300)

_BANNER_OFFSETS_FILE = _DATA_DIR / "banner_offsets.json"
_BANNER_TARGETS_FILE = _DATA_DIR / "banner_targets.jsonl"
_BANNER_CURRENT_FILE = _DATA_DIR / "banner_current.json"
_DAILY_THESIS_FILE = _DATA_DIR / "daily_thesis.jsonl"


@app.get("/api/crypto/banner_current")
async def api_banner_current() -> JSONResponse:
    try:
        return JSONResponse(json.loads(_BANNER_CURRENT_FILE.read_text()))
    except Exception:
        return JSONResponse(None)


_INTRADAY_REGIME_FILE = _DATA_DIR / "intraday_regime.jsonl"


def _latest_regime_today() -> dict | None:
    """Most recent intraday regime entry for today (UTC), or None."""
    try:
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows = _read_jsonl_cached(_INTRADAY_REGIME_FILE)
        for row in reversed(rows):
            if row.get("date") == today:
                return row
    except Exception:
        pass
    return None


@app.get("/api/crypto/intraday_regime")
async def api_intraday_regime() -> JSONResponse:
    return JSONResponse(_latest_regime_today())


@app.get("/api/crypto/daily_thesis")
async def api_daily_thesis() -> JSONResponse:
    try:
        rows = [
            json.loads(l)
            for l in _DAILY_THESIS_FILE.read_text().splitlines()
            if l.strip()
        ]
        if rows:
            out = dict(rows[-1])
            # Attach the live intraday regime overlay so the dashboards'
            # thesis bar reflects what the loop is actually watching, not
            # just the frozen morning call.
            out["regime"] = _latest_regime_today()
            return JSONResponse(out)
    except Exception:
        pass
    return JSONResponse({"bias": None, "level": None, "conviction": None, "date": None,
                         "regime": _latest_regime_today()})


@app.get("/api/crypto/banner_offsets")
async def api_banner_offsets() -> JSONResponse:
    try:
        return JSONResponse(json.loads(_BANNER_OFFSETS_FILE.read_text()))
    except Exception:
        return JSONResponse({
            "sell_low_offset_c": 0.0,
            "sell_high_offset_c": 0.0,
            "low_hit_rate": None,
            "high_hit_rate": None,
            "n": 0,
        })


# Parsed-jsonl cache keyed on (mtime, size): banner_targets.jsonl is >1MB and
# the dashboards poll it every few seconds — re-parsing it per request inside
# an async handler stalls the event loop (this stalled /spot in the past).
_jsonl_cache: dict[str, tuple[tuple[float, int], list]] = {}
_jsonl_cache_lock = threading.Lock()


def _read_jsonl_cached(path: Path) -> list:
    st = path.stat()
    key = (st.st_mtime, st.st_size)
    with _jsonl_cache_lock:
        hit = _jsonl_cache.get(str(path))
        if hit and hit[0] == key:
            return hit[1]
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    with _jsonl_cache_lock:
        _jsonl_cache[str(path)] = (key, rows)
    return rows


@app.get("/api/crypto/banner_history")
async def api_banner_history(limit: int = 20) -> JSONResponse:
    try:
        loop = asyncio.get_running_loop()
        all_rows = await loop.run_in_executor(None, _read_jsonl_cached, _BANNER_TARGETS_FILE)
        # aggregate stats over the full history
        entered   = [r for r in all_rows if r.get("buy_touched")]
        wins      = [r for r in entered  if r.get("low_hit")]
        stretches = [r for r in entered  if r.get("high_hit")]
        stats = {
            "total":    len(all_rows),
            "entered":  len(entered),
            "wins":     len(wins),
            "stretches": len(stretches),
            "win_pct":  round(len(wins) / len(entered) * 100, 1) if entered else 0,
            "str_pct":  round(len(stretches) / len(entered) * 100, 1) if entered else 0,
        }
        recent = list(all_rows[-limit:])
        recent.reverse()  # newest first
        return JSONResponse({"rows": recent, "stats": stats})
    except Exception:
        return JSONResponse({"rows": [], "stats": {}})


@app.get("/api/loop_log")
async def api_loop_log_get(limit: int = 200) -> JSONResponse:
    try:
        loop = asyncio.get_running_loop()
        all_rows = await loop.run_in_executor(None, _read_jsonl_cached, _LOOP_LOG_FILE)
        rows = list(all_rows[-limit:])
        rows.reverse()
        # Both keys: /trade reads .entries, /crypto reads .rows.
        return JSONResponse({"entries": rows, "rows": rows})
    except Exception:
        return JSONResponse({"entries": [], "rows": []})


def _append_loop_log(entry: dict) -> None:
    with _loop_log_lock:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        with _LOOP_LOG_FILE.open("a") as f:
            f.write(json.dumps(entry) + "\n")


@app.post("/api/loop_log")
async def api_loop_log_post(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        entry = {
            "ts": body.get("ts", time.time()),
            "type": str(body.get("type", "NOTE")),
            "spot": body.get("spot"),
            "msg": str(body.get("msg", "")),
        }
        await asyncio.get_running_loop().run_in_executor(None, _append_loop_log, entry)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


# ── swing bot (paper-first; see docs/superpowers/specs/2026-07-15-…) ──
_BOT_DIR = Path(__file__).parent / "data" / "bot"


def _read_jsonl_tail(path, limit=50):
    try:
        lines = Path(path).read_text().splitlines()[-limit:]
    except Exception:
        return []
    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except Exception:
            continue  # skip a single torn/garbage line, not the whole file
    return rows


def _utc_day_str(ts: float) -> str:
    import datetime as _dt
    return _dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")


def _trade_stats(trades):
    closed = [t for t in trades if t.get("status") == "closed"]

    def block(rows):
        n = len(rows)
        wins = sum(1 for t in rows if t["net_pnl"] > 0)
        total = round(sum(t["net_pnl"] for t in rows), 4)
        return {"n": n, "win_pct": round(100 * wins / n, 1) if n else 0.0,
                "net_total": total,
                "net_avg": round(total / n, 4) if n else 0.0}
    by_reason = {}
    for t in closed:
        by_reason.setdefault(t.get("exit_reason", "?"), []).append(t)
    today_str = _utc_day_str(time.time())
    today_rows = [t for t in closed
                  if t.get("exit_ts") is not None and _utc_day_str(t["exit_ts"]) == today_str]
    by_day = {}
    by_session = {}
    for t in closed:
        if t.get("exit_ts"):
            d0 = _utc_day_str(t["exit_ts"])
            by_day[d0] = round(by_day.get(d0, 0.0) + t["net_pnl"], 2)
            g = time.gmtime(t["exit_ts"])
            sess = ("asia" if g.tm_hour < 7 else "europe" if g.tm_hour < 13
                    else "us" if g.tm_hour < 21 else "late")
            key = f"{'we' if g.tm_wday >= 5 else 'wd'}|{sess}"
            by_session.setdefault(key, []).append(t)
    return {"all_time": block(closed),
            "today": block(today_rows),
            "by_exit_reason": {k: block(v) for k, v in by_reason.items()},
            "by_day": dict(sorted(by_day.items())[-14:]),
            "by_session": {k: block(v) for k, v in sorted(by_session.items())}}


def _grade_summary(grades: list) -> dict:
    """Roll up trade_grader.py verdicts for the /bot screen."""
    verdicts: dict = {}
    for g in grades:
        v = g.get("verdict", "?")
        verdicts[v] = verdicts.get(v, 0) + 1
    scored = [g for g in grades if g.get("delta_vs_held") is not None]
    return {
        "n": len(grades),
        "verdicts": verdicts,
        # actual exit P&L minus hold-to-settlement P&L, summed (gross):
        # positive = the exit engine beat holding to expiry
        "exit_edge_usd": round(sum(g["delta_vs_held"] for g in scored), 2),
        "stops_saved_usd": round(sum(g["delta_vs_held"] for g in scored
                                     if g.get("exit_reason") == "stop"), 2),
        "gaps": sum(1 for g in grades if g.get("data_gap")),
    }


def bot_status_payload(bot_dir=None) -> dict:
    d = Path(bot_dir) if bot_dir else _BOT_DIR
    try:
        state = json.loads((d / "bot_state.json").read_text())
    except Exception:
        state = {}
    trades = _read_jsonl_tail(d / "bot_trades.jsonl", 1000)
    from bot_broker import live_capability_ok
    from bot_core import load_config
    cfg = load_config(d / "config.json")
    ok, reason = live_capability_ok(cfg, dict(os.environ))
    from bot_core import bucket_stats, compute_findings, pool_by_date_stats
    try:
        tuner = json.loads((d / "tuner_report.json").read_text())
        tuner.pop("results", None)   # full sweep table is large; GUI shows summary
    except Exception:
        tuner = None
    grades = _read_jsonl_tail(d / "bot_trade_grades.jsonl", 1000)
    ev_buckets = bucket_stats(trades)
    return {"state": state, "stats": _trade_stats(trades),
            "trades": trades[-50:],
            "events": _read_jsonl_tail(d / "bot_events.jsonl", 50),
            "unlock": {"ok": ok, "reason": reason}, "config": cfg,
            "ev_buckets": ev_buckets, "tuner": tuner,
            "grades": grades[-200:], "grade_summary": _grade_summary(grades),
            "gate": _gate_split(trades),
            "pool_by_date": pool_by_date_stats(trades),
            "findings": compute_findings(trades, state, cfg, ev_buckets)}


def candles_from_log(path, mins: int, hours: int, now: float | None = None) -> list:
    """OHLC candles bucketed from the feature log's ~6s spot ticks.

    Tail-reads the last few MB so the growing log never slows the endpoint."""
    now = time.time() if now is None else now
    span = mins * 60
    cutoff = now - hours * 3600
    buckets: dict = {}
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            start = max(0, f.tell() - 4_000_000)
            f.seek(start)
            chunk = f.read().decode(errors="replace")
    except OSError:
        return []
    lines = chunk.splitlines()
    if start > 0:
        lines = lines[1:]   # mid-file seek: first line is a partial record
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        ts, spot = row.get("ts"), row.get("spot")
        if ts is None or spot is None or ts < cutoff:
            continue
        b = int(ts // span) * span
        c = buckets.get(b)
        if c is None:
            buckets[b] = {"t": b, "o": spot, "h": spot, "l": spot, "c": spot}
        else:
            c["h"] = max(c["h"], spot)
            c["l"] = min(c["l"], spot)
            c["c"] = spot
    return [buckets[k] for k in sorted(buckets)]


@app.get("/api/crypto/candles")
async def api_crypto_candles(mins: int = 15, hours: int = 8) -> JSONResponse:
    mins = max(1, min(60, mins))
    hours = max(1, min(48, hours))
    return JSONResponse({"mins": mins,
                         "candles": candles_from_log(
                             _DATA_DIR / "signal_feature_log.jsonl", mins, hours)})


def _gate_split(trades: list) -> dict:
    """Per-session live-unlock gate stats for display -- same
    bot_core.session_gate_stats bot_broker.live_unlock_ok itself gates on,
    so this can't go stale relative to the actual unlock rule again (it
    previously duplicated the aggregation as a 100-weekday + 100-weekend
    COMBINED total, a materially easier bar than the real per-session-
    independent one adopted 2026-07-21 -- see dual-100-trade-gate memory)."""
    from bot_core import session_gate_stats
    return {"sessions": session_gate_stats(trades)}


def bot_control_write(bot_dir, cmd: str) -> int:
    if cmd not in ("pause", "resume", "flatten"):
        raise ValueError(f"unknown bot command: {cmd}")
    d = Path(bot_dir) if bot_dir else _BOT_DIR
    d.mkdir(parents=True, exist_ok=True)
    ctl = d / "control.json"
    try:
        nonce = int(json.loads(ctl.read_text()).get("nonce", 0))
    except Exception:
        nonce = 0
    nonce += 1
    tmp = d / "control.json.tmp"
    tmp.write_text(json.dumps({"nonce": nonce, "cmd": cmd}))
    os.replace(tmp, ctl)
    return nonce


@app.get("/api/bot/status")
async def api_bot_status() -> JSONResponse:
    return JSONResponse(bot_status_payload())


@app.post("/api/bot/control")
async def api_bot_control(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    try:
        nonce = bot_control_write(_BOT_DIR, body.get("cmd", ""))
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "nonce": nonce})


@app.get("/bot", response_class=HTMLResponse)
async def bot_screen() -> str:
    from bot_page import BOT_HTML
    return BOT_HTML


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


@app.get("/api/account")
async def api_account() -> JSONResponse:
    with _account_lock:
        return JSONResponse(dict(_account_cache))


@app.post("/api/fills_toggle")
async def fills_toggle() -> JSONResponse:
    global _fills_enabled
    _fills_enabled = not _fills_enabled
    with _account_lock:
        _account_cache["fills_enabled"] = _fills_enabled
    return JSONResponse({"fills_enabled": _fills_enabled})


# 2026-07-28: Kenny approved a $20 manual sample-live-test on weekday_night
# only (see dual-100-trade-gate / trading-account-setup memory) -- balance
# confirmed $20.02 at approval time, no open positions. PnL is measured as
# balance-since-start rather than summing fills, since the account fills
# poller only keeps the most recent 20 (portfolio/fills?limit=20) and older
# ones roll off well before the test's 30-40 fill target; balance is the one
# number that can't drift out from under a rolling window.
LIVE_TEST_START_TS = 1785301930.0
LIVE_TEST_START_BALANCE = 20.02
LIVE_TEST_HARD_STOP = -8.0
LIVE_TEST_DAILY_SOFT_STOP = -3.0


@app.get("/api/live_test")
async def api_live_test() -> JSONResponse:
    import datetime as _dt
    with _account_lock:
        cache = dict(_account_cache)
    bal_raw = cache.get("balance")
    try:
        balance = float(bal_raw) if bal_raw is not None else None
    except (TypeError, ValueError):
        balance = None
    pnl = round(balance - LIVE_TEST_START_BALANCE, 4) if balance is not None else None

    fills = cache.get("fills") or []
    since_start = []
    for f in fills:
        ts = f.get("ts")
        try:
            f_ts = _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            continue
        if f_ts >= LIVE_TEST_START_TS:
            since_start.append(f)
    since_start.sort(key=lambda f: f.get("ts") or "")

    status = "no_data" if balance is None else \
        "hard_stop_hit" if pnl <= LIVE_TEST_HARD_STOP else "active"

    return JSONResponse({
        "start_ts": LIVE_TEST_START_TS,
        "start_balance": LIVE_TEST_START_BALANCE,
        "hard_stop": LIVE_TEST_HARD_STOP,
        "daily_soft_stop": LIVE_TEST_DAILY_SOFT_STOP,
        "balance": balance,
        "pnl": pnl,
        "fills_since_start": since_start,
        "status": status,
        "error": cache.get("error"),
    })


@app.get("/api/live_signals")
async def api_live_signals() -> JSONResponse:
    path = _BOT_DIR / "live_signals.jsonl"
    # _read_jsonl_tail parses each line independently and skips torn/garbage
    # lines individually, instead of discarding the whole batch on one bad
    # line; it also returns [] when the file doesn't exist.
    return JSONResponse({"signals": _read_jsonl_tail(path, 50)})


_TRADE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>kalshi · scanner + trade</title>
<style>
:root {
  --bg:#0b0f17; --bg2:#121826; --bg3:#1b2334; --fg:#e8edf4; --mute:#8b96a8;
  --border:#263044; --hair:#1a2232;
  --green:#3fd68c; --red:#ff5c64; --yellow:#ffc53d;
  --blue:#5ca8ff; --orange:#ff9f45; --purple:#c29bff;
  --green-bg:rgba(63,214,140,.08);  --green-bd:rgba(63,214,140,.38);
  --red-bg:rgba(255,92,100,.08);    --red-bd:rgba(255,92,100,.38);
  --yellow-bg:rgba(255,197,61,.08); --yellow-bd:rgba(255,197,61,.38);
  --blue-bg:rgba(92,168,255,.09);   --blue-bd:rgba(92,168,255,.35);
  --orange-bg:rgba(255,159,69,.10); --purple-bg:rgba(194,155,255,.10);
  --mono:ui-monospace,"SF Mono","Fira Code",monospace;
  --sans:ui-sans-serif,system-ui,"Segoe UI",sans-serif;
  --radius:10px;
  --card-shadow:inset 0 1px 0 rgba(255,255,255,.035), 0 6px 20px rgba(0,0,0,.30);
}
* { box-sizing:border-box; margin:0; padding:0; }
::-webkit-scrollbar { width:9px; height:9px; }
::-webkit-scrollbar-thumb { background:var(--bg3); border-radius:5px; border:2px solid var(--bg); }
::-webkit-scrollbar-thumb:hover { background:var(--border); }
body {
  font-family:var(--mono);
  background:
    radial-gradient(1100px 460px at 75% -12%, rgba(92,168,255,.055), transparent 65%),
    radial-gradient(900px 420px at -10% 110%, rgba(63,214,140,.035), transparent 60%),
    var(--bg);
  background-attachment:fixed;
  color:var(--fg);
  min-height:100vh; font-size:13px;
}
button:focus-visible, a:focus-visible { outline:2px solid var(--blue); outline-offset:2px; }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation:none !important; transition:none !important; }
}

/* ── header ── */
header {
  padding:11px 20px; border-bottom:1px solid var(--border);
  background:rgba(18,24,38,.92); backdrop-filter:blur(6px);
  display:flex; align-items:center; gap:16px; flex-wrap:wrap;
  position:sticky; top:0; z-index:20;
}
.logo { font-size:14px; font-weight:900; color:var(--fg); letter-spacing:-0.5px; }
.logo::before { content:""; display:inline-block; width:8px; height:8px; border-radius:50%;
                background:var(--green); margin-right:8px; vertical-align:baseline;
                box-shadow:0 0 8px var(--green); animation:livedot 2.4s ease-in-out infinite; }
@keyframes livedot { 0%,100%{opacity:1} 50%{opacity:.35} }
.spot-btc { color:var(--orange); font-weight:800; font-size:15px; font-variant-numeric:tabular-nums; }
.spot-eth { color:var(--purple); font-weight:700; font-size:13px; font-variant-numeric:tabular-nums; }
.clock { color:var(--mute); font-size:12px; margin-left:auto; font-variant-numeric:tabular-nums; }
.nav-link { color:var(--mute); font-size:11px; text-decoration:none;
            padding:4px 10px; border:1px solid transparent; border-radius:6px; transition:all .18s; }
.nav-link:hover { color:var(--blue); border-color:var(--blue-bd); background:var(--blue-bg); }

/* ── thesis bar ── */
.thesis-bar {
  padding:8px 20px; border-bottom:2px solid var(--border);
  display:flex; align-items:center; gap:16px; flex-wrap:wrap;
}
.thesis-bar.up   { background:linear-gradient(90deg, var(--green-bg), transparent 70%); border-bottom-color:var(--green-bd); }
.thesis-bar.down { background:linear-gradient(90deg, var(--red-bg), transparent 70%); border-bottom-color:var(--red-bd); }
.thesis-bar.wait { background:linear-gradient(90deg, var(--yellow-bg), transparent 70%); border-bottom-color:var(--yellow-bd); }
.thesis-bar-bias  { font-size:21px; font-weight:900; line-height:1; letter-spacing:-.5px; }
.thesis-bar-bias.up   { color:var(--green); text-shadow:0 0 18px rgba(63,214,140,.35); }
.thesis-bar-bias.down { color:var(--red);   text-shadow:0 0 18px rgba(255,92,100,.35); }
.thesis-bar-bias.wait { color:var(--yellow);text-shadow:0 0 18px rgba(255,197,61,.30); }
.thesis-bar-meta  { font-size:12px; color:var(--mute); }
.thesis-bar-spot  { margin-left:auto; font-size:13px; font-weight:700; font-variant-numeric:tabular-nums;
                    padding:3px 10px; border-radius:6px; border:1px solid var(--border); background:var(--bg2); }
.thesis-bar-spot.above { color:var(--green); border-color:var(--green-bd); background:var(--green-bg); }
.thesis-bar-spot.below { color:var(--red);   border-color:var(--red-bd);   background:var(--red-bg); }
.thesis-bar-note  { font-size:11px; color:var(--mute); flex-basis:100%; opacity:.85;
                    font-family:var(--sans); letter-spacing:.1px;
                    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }

/* ── flush alert ── */
.flush-strip {
  padding:10px 20px; border-bottom:1px solid var(--yellow-bd);
  background:var(--yellow-bg); color:var(--yellow); font-size:13px; font-weight:800;
  display:flex; align-items:center; gap:12px;
}
.flush-score { font-size:22px; font-weight:900; text-shadow:0 0 16px rgba(255,197,61,.4); }

/* ── signal banner (3-col) ── */
.signal-banner {
  display:grid; grid-template-columns:130px 1fr auto;
  border-bottom:3px solid var(--border); background:var(--bg2);
  transition:background 0.3s; min-height:120px;
}
.signal-banner.up   { border-bottom-color:var(--green);
  background:linear-gradient(125deg, rgba(63,214,140,.13), rgba(63,214,140,.02) 55%), var(--bg2); }
.signal-banner.down { border-bottom-color:var(--red);
  background:linear-gradient(125deg, rgba(255,92,100,.13), rgba(255,92,100,.02) 55%), var(--bg2); }
.signal-banner.flash { animation:flashpulse .5s ease-out; }
.signal-banner.thesis-mute    { opacity:.5; filter:grayscale(50%); }
.signal-banner.thesis-counter { outline:2px dashed var(--red); outline-offset:-2px; }
.signal-banner.stale-data     { opacity:.45; filter:grayscale(80%); }
@keyframes flashpulse { 0%{opacity:.1} 40%{opacity:1} 100%{opacity:1} }

.sig-dir-block {
  display:flex; flex-direction:column; align-items:center; justify-content:center;
  padding:10px 0; border-right:1px solid var(--border);
}
.sig-direction { font-size:60px; font-weight:900; line-height:1; letter-spacing:-2px; }
.sig-direction.up   { color:var(--green); text-shadow:0 0 34px rgba(63,214,140,.45); }
.sig-direction.down { color:var(--red);   text-shadow:0 0 34px rgba(255,92,100,.45); }
.sig-direction.waiting { color:var(--mute); font-size:32px; text-shadow:none; }
.sig-conf-pct { font-size:12px; font-weight:700; color:var(--mute); margin-top:4px; font-variant-numeric:tabular-nums; }

.sig-center { display:flex; flex-direction:column; justify-content:center; gap:8px; padding:14px 20px; }
.sig-range-row { display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; }
.sig-range-buy  { font-size:26px; font-weight:900; color:var(--blue); font-variant-numeric:tabular-nums; letter-spacing:-.5px; }
.sig-range-arr  { font-size:16px; color:var(--mute); }
.sig-range-sell { font-size:26px; font-weight:900; color:var(--yellow); font-variant-numeric:tabular-nums; letter-spacing:-.5px; }
.conf-bar-wrap { width:140px; height:7px; background:var(--hair); border-radius:4px; display:inline-block; vertical-align:middle; margin-left:8px; overflow:hidden; }
.conf-bar      { height:7px; border-radius:4px; transition:width .3s;
                 background:linear-gradient(90deg, rgba(63,214,140,.55), var(--green)); }
.conf-bar.down { background:linear-gradient(90deg, rgba(255,92,100,.55), var(--red)); }
.sig-label { font-size:12px; color:var(--fg); line-height:1.5; font-family:var(--sans); }

.sig-right {
  display:flex; flex-direction:column; justify-content:center; gap:8px;
  padding:14px 20px; border-left:1px solid var(--border); min-width:230px;
}
.sig-stats { display:flex; gap:18px; flex-wrap:wrap; font-size:12px; }
.sig-stat  { display:flex; flex-direction:column; gap:2px; }
.sig-stat .k { font-size:10px; color:var(--mute); text-transform:uppercase; letter-spacing:.7px; font-family:var(--sans); font-weight:600; }
.sig-stat .v { font-weight:800; font-variant-numeric:tabular-nums; font-size:13px; }
.sig-components { display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
.sig-comp { padding:2px 9px; border-radius:99px; font-size:11px; font-weight:700;
            border:1px solid var(--border); white-space:nowrap; }
.sig-comp.bull { background:var(--green-bg); color:var(--green); border-color:var(--green-bd); }
.sig-comp.bear { background:var(--red-bg);   color:var(--red);   border-color:var(--red-bd); }
.sig-comp.neut { background:var(--bg3); color:var(--mute); }
.sig-ticker-label { font-size:10px; color:var(--mute); }
.sig-reset-badge  { font-size:10px; padding:2px 9px; border-radius:99px;
                    background:var(--green-bg); color:var(--green); border:1px solid var(--green-bd); }
.sig-reset-badge.t1 { background:var(--yellow-bg); color:var(--yellow); border-color:var(--yellow-bd); }
.trade-badge { font-size:11px; font-weight:900; padding:3px 11px; border-radius:99px;
               letter-spacing:1px; display:inline-block; }
.trade-badge.swing { background:var(--orange-bg); color:var(--orange); border:1px solid rgba(255,159,69,.4); }
.trade-badge.hold  { background:var(--blue-bg);   color:var(--blue);   border:1px solid var(--blue-bd); }

/* ── distance big stat (inside sig-center) ── */
.dist-stat { display:flex; flex-direction:column; gap:3px; }
.dist-stat .dist-val { font-size:30px; font-weight:900; font-variant-numeric:tabular-nums; line-height:1; letter-spacing:-.5px; }
.dist-stat .dist-sub { font-size:11px; color:var(--mute); font-family:var(--sans); }
.dist-stat .dist-val.pos { color:var(--green); }
.dist-stat .dist-val.neg { color:var(--red); }

/* ── position picker ── */
.pos-row {
  padding:10px 20px; background:var(--bg2); border-bottom:1px solid var(--border);
  display:flex; align-items:center; gap:10px;
}
.pos-label { font-size:10px; color:var(--mute); text-transform:uppercase; letter-spacing:.8px; font-family:var(--sans); font-weight:600; }
.pos-btn {
  padding:8px 22px; border-radius:8px; font-size:13px; font-weight:800;
  border:1px solid var(--border); background:var(--bg); color:var(--mute);
  cursor:pointer; font-family:inherit; letter-spacing:.5px; transition:all .15s;
}
.pos-btn:hover { border-color:var(--fg); color:var(--fg); transform:translateY(-1px); }
.pos-btn:active { transform:translateY(0); }
.pos-btn.active-yes  { background:var(--green-bg); border-color:var(--green); color:var(--green); box-shadow:0 0 14px rgba(63,214,140,.18); }
.pos-btn.active-no   { background:var(--red-bg);   border-color:var(--red);   color:var(--red);   box-shadow:0 0 14px rgba(255,92,100,.18); }
.pos-btn.active-flat { background:var(--bg3); border-color:var(--mute); color:var(--fg); }

/* ── guidance bar ── */
.guidance-wrap { padding:10px 20px; border-bottom:1px solid var(--border); }
.guidance-bar {
  padding:13px 18px; border-radius:12px; font-size:15px; font-weight:800;
  border:1px solid var(--border); display:flex; align-items:center; gap:12px;
  box-shadow:var(--card-shadow);
}
.guidance-bar.hold    { border-color:var(--green-bd); background:linear-gradient(110deg, rgba(63,214,140,.14), rgba(63,214,140,.04)); color:var(--green); }
.guidance-bar.caution { border-color:var(--yellow-bd); background:linear-gradient(110deg, rgba(255,197,61,.14), rgba(255,197,61,.04)); color:var(--yellow); }
.guidance-bar.danger  { border-color:var(--red-bd);   background:linear-gradient(110deg, rgba(255,92,100,.16), rgba(255,92,100,.05)); color:var(--red);
                        animation:dangerpulse 1.8s ease-in-out infinite; }
@keyframes dangerpulse { 0%,100%{box-shadow:var(--card-shadow)} 50%{box-shadow:0 0 0 3px rgba(255,92,100,.18), 0 6px 20px rgba(0,0,0,.30)} }
.guidance-bar.neutral { border-color:var(--border); background:var(--bg2); color:var(--mute); }
.guidance-icon { font-size:21px; flex-shrink:0; }

/* ── history strip ── */
.history-strip {
  display:flex; gap:8px; padding:9px 16px; overflow-x:auto;
  border-bottom:1px solid var(--border); background:transparent;
  min-height:70px; align-items:center; flex-shrink:0;
}
.hist-card {
  flex-shrink:0; padding:7px 12px; border-radius:8px; min-width:110px;
  border:1px solid var(--border); background:var(--bg2); font-size:11px;
  display:flex; flex-direction:column; gap:3px; cursor:default; transition:transform .15s;
}
.hist-card:hover { transform:translateY(-2px); }
.hist-card.correct { border-color:var(--green-bd); background:linear-gradient(160deg, var(--green-bg), var(--bg2)); }
.hist-card.wrong   { border-color:var(--red-bd);   background:linear-gradient(160deg, var(--red-bg), var(--bg2)); }
.hist-card.pending { opacity:.6; border-style:dashed; }
.hc-dir   { font-weight:900; font-size:13px; }
.hc-conf  { color:var(--mute); font-size:10px; }
.hc-out   { font-weight:800; font-size:12px; }
.hc-out.ok  { color:var(--green); }
.hc-out.bad { color:var(--red); }
.hc-time  { color:var(--mute); font-size:10px; }

/* ── limit order panel ── */
.limit-wrap { padding:12px 16px; max-height:480px; overflow-y:auto; }
.limit-card { border:1px solid var(--border); border-radius:12px; background:var(--bg2); overflow:hidden; box-shadow:var(--card-shadow); }
.limit-hdr  { padding:8px 14px; background:var(--bg3); border-bottom:1px solid var(--border);
              font-size:10px; text-transform:uppercase; letter-spacing:1px; color:var(--mute);
              font-family:var(--sans); font-weight:600;
              display:flex; justify-content:space-between; }
.limit-grid { display:grid; grid-template-columns:1fr 1fr; }
.limit-col  { padding:12px 16px; }
.limit-col.signal-yes { background:linear-gradient(180deg, var(--green-bg), transparent 80%); }
.limit-col.signal-no  { background:linear-gradient(180deg, var(--red-bg), transparent 80%); }
.limit-col + .limit-col { border-left:1px solid var(--border); }
.limit-col-title { font-size:12px; font-weight:900; letter-spacing:1px; margin-bottom:10px; }
.limit-col-title.yes { color:var(--green); }
.limit-col-title.no  { color:var(--red); }
.limit-row { display:flex; justify-content:space-between; align-items:center;
             padding:5px 6px; margin:0 -6px; border-bottom:1px solid var(--hair); font-size:12px; border-radius:6px; }
.limit-row:last-child { border-bottom:none; }
.limit-tier  { color:var(--mute); font-size:11px; font-family:var(--sans); }
.limit-price { font-weight:800; font-variant-numeric:tabular-nums; font-size:13px; }
.limit-save  { font-size:10px; color:var(--mute); }
.limit-row.market  .limit-price { color:var(--fg); }
.limit-row.aggr    .limit-price { color:var(--blue); }
.limit-row.patient .limit-price { color:var(--yellow); }
.limit-row.best    { background:var(--green-bg); border-bottom-color:transparent; }
.limit-row.best    .limit-price { color:var(--green); }
.limit-row.best    .limit-tier  { color:var(--green); font-weight:700; }
.limit-note { font-size:10px; color:var(--mute); padding-top:8px; line-height:1.55; font-family:var(--sans); }

/* ── panel shared ── */
.panel-hdr { padding:9px 16px; background:var(--bg3); border-bottom:1px solid var(--border);
             font-size:11px; text-transform:uppercase; letter-spacing:1px; color:var(--yellow);
             font-family:var(--sans);
             display:flex; justify-content:space-between; position:sticky; top:0; z-index:1; font-weight:700; }
.panel-body { padding:0; overflow-y:auto; max-height:480px; }

/* ── bot open plays ── */
.bp-tbl { width:100%; border-collapse:collapse; font-size:12px; }
.bp-tbl th,.bp-tbl td { text-align:left; padding:6px 12px; border-bottom:1px solid var(--hair);
        white-space:nowrap; font-variant-numeric:tabular-nums; }
.bp-tbl th { color:var(--mute); font-weight:600; font-family:var(--sans); font-size:10px;
     text-transform:uppercase; letter-spacing:.6px; }
.bp-tbl tr:last-child td { border-bottom:none; }
.bp-side { font-weight:800; }
.bp-side.yes { color:var(--green); } .bp-side.no { color:var(--red); }
.bp-pool { font-size:10px; color:var(--mute); }
.bp-tbl .empty { padding:8px 0; color:var(--mute); font-size:12px; }


/* ── BRS history ── */
.brs-stat-row { display:flex; gap:32px; padding:14px 18px; border-bottom:1px solid var(--border); flex-wrap:wrap; }
.brs-stat { display:flex; flex-direction:column; gap:3px; }
.brs-stat-k { font-size:11px; color:var(--mute); text-transform:uppercase; letter-spacing:.8px; font-family:var(--sans); font-weight:600; }
.brs-stat-v { font-size:44px; font-weight:900; font-variant-numeric:tabular-nums; line-height:1; letter-spacing:-1px; }
.brs-stat-sub { font-size:12px; color:var(--mute); }
.brs-row { display:grid; grid-template-columns:1fr 56px 72px 72px 90px 66px 30px;
           padding:8px 14px; border-bottom:1px solid var(--hair); align-items:center; font-size:13px;
           border-left:3px solid transparent; transition:background .12s; }
.brs-row:last-child { border-bottom:none; }
.brs-row:hover { background:var(--bg3); }
.brs-row.win      { border-left-color:var(--green); }
.brs-row.loss     { border-left-color:var(--red); }
.brs-row.no-entry { border-left-color:var(--border); }

/* ── round history ── */
.hist-wrap { padding:12px 20px; border-top:1px solid var(--border); }
.hist-label { font-size:10px; text-transform:uppercase; letter-spacing:1px; color:var(--mute); margin-bottom:8px; display:flex; align-items:center; gap:10px; }
.hist-grid { display:flex; flex-direction:column; gap:5px; }
.hcard { display:grid; grid-template-columns:80px 1fr 1fr auto; gap:8px; align-items:center;
         padding:7px 10px; border-radius:8px; border:1px solid var(--border);
         background:var(--bg2); font-size:12px; }
.hcard.correct { border-color:var(--green-bd); background:linear-gradient(160deg, var(--green-bg), var(--bg2)); }
.hcard.wrong   { border-color:var(--red-bd);   background:linear-gradient(160deg, var(--red-bg), var(--bg2)); }
.hcard.pending { opacity:.6; border-style:dashed; }
.hcard-dir   { font-weight:800; font-size:13px; }
.hcard-conf  { color:var(--mute); }
.hcard-out   { font-weight:700; }
.hcard-time  { color:var(--mute); font-size:11px; text-align:right; }
.hcard-out.ok  { color:var(--green); }
.hcard-out.bad { color:var(--red); }
.hcard-out.pending { color:var(--mute); }

/* ── loop log ── */
.log-panel { border-top:1px solid var(--border); }
.log-panel-header { padding:7px 20px; background:var(--bg2); border-bottom:1px solid var(--border);
                    display:flex; align-items:center; gap:10px; }
.log-panel-title { font-size:10px; text-transform:uppercase; letter-spacing:1px; color:var(--mute); font-weight:700; }
.log-panel-meta  { font-size:11px; color:var(--mute); margin-left:auto; }
.log-clear-btn   { font-size:10px; padding:3px 10px; border:1px solid var(--border);
                   border-radius:6px; background:transparent; color:var(--mute);
                   cursor:pointer; font-family:inherit; transition:all .15s; }
.log-clear-btn:hover { color:var(--fg); border-color:var(--fg); }
.log-entries     { max-height:320px; overflow-y:auto; }
.log-entry { display:flex; gap:10px; align-items:flex-start; padding:6px 20px;
             border-bottom:1px solid var(--hair); }
.log-entry:last-child { border-bottom:none; }
.log-ts   { color:var(--mute); font-size:11px; white-space:nowrap; flex-shrink:0; }
.log-type { font-size:10px; font-weight:700; padding:1px 8px; border-radius:99px; white-space:nowrap; flex-shrink:0; }
.log-type.BREAK_UP   { background:var(--green-bg); color:var(--green); border:1px solid var(--green-bd); }
.log-type.BREAK_DOWN { background:var(--green-bg); color:var(--green); border:1px solid var(--green-bd); }
.log-type.REJECT     { background:var(--red-bg);   color:var(--red);   border:1px solid var(--red-bd); }
.log-type.CROSS_ABOVE{ background:var(--red-bg);   color:var(--red);   border:1px solid var(--red-bd); }
.log-type.LEVEL_TEST { background:var(--yellow-bg); color:var(--yellow); border:1px solid var(--yellow-bd); }
.log-type.HOLD       { background:var(--blue-bg);  color:var(--blue);  border:1px solid var(--blue-bd); }
.log-type.SIGNAL     { background:var(--purple-bg); color:var(--purple); border:1px solid rgba(194,155,255,.4); }
.log-type.RESEARCH   { background:var(--blue-bg);  color:var(--blue);  border:1px solid var(--blue-bd); }
.log-type.STATUS     { background:var(--bg3); color:var(--mute); border:1px solid var(--border); }
.log-type.RE_ARM     { background:var(--bg3); color:var(--mute); border:1px solid var(--border); }
.log-type.NOTE       { background:var(--bg3); color:var(--mute); border:1px solid var(--border); }
.log-spot { color:var(--orange); font-variant-numeric:tabular-nums; flex-shrink:0; }
.log-msg  { color:var(--fg); line-height:1.45; font-family:var(--sans); font-size:12px; }

/* ── shared ── */
.yes { color:var(--green); font-weight:700; }
.no  { color:var(--red);   font-weight:700; }
.pos { color:var(--green); }
.neg { color:var(--red); }
.dim { color:var(--mute); }
.num { font-variant-numeric:tabular-nums; }
.ticker { color:var(--blue); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.trunc  { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.empty  { padding:12px; color:var(--mute); font-size:12px; }
.score-pill { display:inline-flex; align-items:center; gap:6px; padding:3px 12px;
              border-radius:99px; font-size:12px; font-weight:700;
              border:1px solid var(--border); background:var(--bg3); }
.score-pill.pos { border-color:var(--green-bd); background:var(--green-bg); color:var(--green); }
.score-pill.neg { border-color:var(--red-bd);   background:var(--red-bg);   color:var(--red); }
</style>
</head>
<body>

<header>
  <span class="logo">kalshi</span>
  <span id="spot-btc" class="spot-btc">BTC $—</span>
  <span id="spot-eth" class="spot-eth">ETH $—</span>
  <a href="/whales" class="nav-link">→ whales</a>
  <a href="/crypto" class="nav-link">→ scanner</a>
  <span class="clock" id="clock">--:--:-- UTC</span>
</header>

<div class="thesis-bar" id="thesis-bar" style="display:none"></div>

<div id="flush-alert" style="display:none">
  <div class="flush-strip">
    <span class="flush-score" id="flush-score-val">⚡</span>
    <span id="flush-msg">FLUSH IN PROGRESS — buyers absorbing the drop</span>
  </div>
</div>

<!-- ── main signal banner ── -->
<div class="signal-banner" id="signal-banner">
  <div class="sig-dir-block">
    <div class="sig-direction waiting" id="sig-dir">—</div>
    <div class="sig-conf-pct" id="sig-conf-pct">—</div>
    <div id="sig-trade-type" style="display:none"></div>
  </div>
  <div class="sig-center">
    <div class="sig-range-row" id="sig-range-row">
      <span class="sig-range-buy" id="sig-range-buy">—</span>
      <span class="sig-range-arr">→</span>
      <span class="sig-range-sell" id="sig-range-sell">—</span>
      <span class="conf-bar-wrap"><div class="conf-bar" id="conf-bar" style="width:0%"></div></span>
    </div>
    <div id="dist-stat" style="display:none" class="dist-stat">
      <span class="dist-val" id="dist-val">—</span>
      <span class="dist-sub" id="dist-sub">btc vs strike</span>
    </div>
    <div class="sig-label" id="sig-label">waiting for market data…</div>
  </div>
  <div class="sig-right">
    <div class="sig-stats" id="sig-stats"></div>
    <div class="sig-components" id="sig-components"></div>
    <div style="display:flex;gap:8px;align-items:center;margin-top:3px">
      <span class="sig-ticker-label" id="sig-ticker"></span>
      <span id="sig-badge" style="display:none" class="sig-reset-badge">NEW MARKET</span>
    </div>
  </div>
</div>

<!-- ── position picker + guidance ── -->
<div class="pos-row">
  <span class="pos-label">I'm long</span>
  <button class="pos-btn" id="btn-yes"  onclick="setMyPos('YES')">YES</button>
  <button class="pos-btn" id="btn-no"   onclick="setMyPos('NO')">NO</button>
  <button class="pos-btn" id="btn-flat" onclick="setMyPos('FLAT')">FLAT</button>
</div>
<div class="guidance-wrap" id="guidance-wrap" style="display:none">
  <div class="guidance-bar neutral" id="guidance-bar">
    <span class="guidance-icon" id="guidance-icon">—</span>
    <span id="guidance-text">set your position above</span>
  </div>
</div>

<!-- ── limit order targets ── -->
<div class="limit-wrap">
  <div class="limit-card">
    <div class="limit-hdr">
      <span>LIMIT ORDER TARGETS</span>
      <span id="limit-spread-note" style="color:var(--mute)"></span>
    </div>
    <div class="limit-grid">
      <div class="limit-col" id="limit-yes-col">
        <div class="limit-col-title yes">BUY YES</div>
        <div id="limit-yes-rows"><div class="dim" style="font-size:11px">—</div></div>
        <div class="limit-note" id="limit-yes-note"></div>
      </div>
      <div class="limit-col" id="limit-no-col">
        <div class="limit-col-title no">BUY NO</div>
        <div id="limit-no-rows"><div class="dim" style="font-size:11px">—</div></div>
        <div class="limit-note" id="limit-no-note"></div>
      </div>
    </div>
  </div>
</div>

<!-- ── history strip ── -->
<div class="history-strip" id="history-strip">
  <div class="dim" style="font-size:11px;padding:4px">loading…</div>
</div>

<!-- ── BRS / account / limit targets, side by side ── -->
<div>
  <div class="panel-hdr">
    <span>Buy / Sell Range History</span>
    <span id="brs-meta" class="dim" style="color:var(--mute);font-weight:400">—</span>
  </div>
  <div class="panel-body" id="brs-body"><div class="empty">no settled markets yet</div></div>
</div>

<!-- ── bot open plays ── -->
<div>
  <div class="panel-hdr">
    <span>Bot Open Plays</span>
    <span id="bp-meta" class="dim" style="color:var(--mute);font-weight:400">—</span>
  </div>
  <div class="panel-body">
    <table class="bp-tbl" id="bp-table">
      <thead><tr><th>Ticker</th><th>Side</th><th>Pool</th><th>Qty</th><th>Entry</th><th>Notional</th></tr></thead>
      <tbody><tr><td colspan="6"><div class="empty">no open plays</div></td></tr></tbody>
    </table>
  </div>
</div>

<!-- ── round call history ── -->
<div class="hist-wrap">
  <div class="hist-label">
    Round History <span id="score-label"></span>
  </div>
  <div class="hist-grid" id="hist-grid"></div>
</div>

<!-- ── loop log ── -->
<div class="log-panel">
  <div class="log-panel-header">
    <span class="log-panel-title">Loop Analysis Log</span>
    <span class="log-panel-meta" id="log-meta">—</span>
    <button class="log-clear-btn" onclick="clearLog()">clear local</button>
  </div>
  <div class="log-entries" id="log-entries"><div class="empty">no log entries yet</div></div>
</div>

<script>
const $ = id => document.getElementById(id);

// ── state ────────────────────────────────────────────────────────────────────
let _lastSignal  = null;
let _btcSpot     = null;
let _dailyThesis = {bias:null, level:null, conviction:null, note:null};
let _lastTicker  = null;
let _bannerOffsets = {yes:{sell_low_offset_c:0,sell_high_offset_c:0,low_hit_rate:null,high_hit_rate:null,buy_touch_rate:null,n:0},
                      no: {sell_low_offset_c:0,sell_high_offset_c:0,low_hit_rate:null,high_hit_rate:null,buy_touch_rate:null,n:0}};
let _audioCtx    = null;
let _t1Timer     = null;
let _lastAlertTicker = null;
let myPos        = null;
let _lastDir     = null;
let _lastGoodSignalTs = 0;

// ── utils ────────────────────────────────────────────────────────────────────
function fmt$(n,d=0){ return n==null?'—':'$'+n.toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d}); }
function fmtC(n)    { return n==null?'—':(n*100).toFixed(1)+'¢'; }
function fmtN(n)    { if(n==null)return'—'; if(n>=1000)return(n/1000).toFixed(1)+'K'; return Math.round(n).toString(); }
function esc(s)     { return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
// Guarded fetch: timeout + fallback so one dead endpoint can't hang a poll.
const fj=(u,fb)=>fetch(u,{signal:AbortSignal.timeout(4000)}).then(r=>r.json()).catch(()=>fb);
// Pause polling in hidden tabs — but only after the first load, so a page
// opened in a background tab still populates.
let _firstLoadDone=false;
function sigComp(label,val){
  if(val==null)return'';
  const cls=val>8?'bull':val<-8?'bear':'neut';
  const arr=val>8?'▲':val<-8?'▼':'▶';
  return `<span class="sig-comp ${cls}">${arr} ${label}</span>`;
}
function tradeType(s){
  if(!s||s.status!=='ok'||s.mins_left==null||s.mins_left<2)return null;
  const isUp=s.direction==='YES', price=s.price;
  const bias=(_dailyThesis.bias||'').toUpperCase();
  const keyLevel=_dailyThesis.level?parseFloat(_dailyThesis.level):null;
  const nearKey=keyLevel&&s.spot?Math.abs(s.spot-keyLevel)<350:false;
  const counterThesis=(bias==='UP'&&!isUp)||(bias==='DOWN'&&isUp);
  const extreme=(isUp&&price>0.73)||(!isUp&&(1-price)>0.73);
  if(nearKey||(counterThesis&&extreme))return'SWING';
  const thesisAligned=!counterThesis&&bias&&bias!=='WAIT'&&bias!=='NONE';
  if(thesisAligned&&price>=0.28&&price<=0.72&&s.mins_left>=5)return'HOLD';
  return null;
}

// ── position picker ──────────────────────────────────────────────────────────
function setMyPos(p){
  myPos = myPos===p ? null : p;
  ['yes','no','flat'].forEach(x=>$('btn-'+x).className='pos-btn'+(myPos===x.toUpperCase()?' active-'+x:''));
  renderGuidance(_lastSignal);
}

// ── thesis bar ───────────────────────────────────────────────────────────────
function renderThesisBar(){
  const bar=$('thesis-bar'), t=_dailyThesis;
  if(!t||!t.bias||t.bias==='NONE'){bar.style.display='none';return;}
  const bias=t.bias.toUpperCase();
  const cls=bias==='UP'?'up':bias==='DOWN'?'down':'wait';
  const arrow=bias==='UP'?'▲':bias==='DOWN'?'▼':'◆';
  const lvl=t.level?parseFloat(t.level):null;
  const keyStr=lvl?` · key ${fmt$(lvl)}`:'';
  const convStr=t.conviction?`conv ${t.conviction}/5`:'';
  let spotHtml='';
  if(_btcSpot&&lvl){
    const diff=Math.round(_btcSpot-lvl);
    const sc=diff>=0?'above':'below';
    spotHtml=`<span class="thesis-bar-spot ${sc}">${fmt$(_btcSpot,0)} <span style="font-size:11px;opacity:.7">(${diff>=0?'+':''}${diff.toLocaleString()} vs key)</span></span>`;
  } else if(_btcSpot){
    spotHtml=`<span class="thesis-bar-spot">${fmt$(_btcSpot,0)}</span>`;
  }
  const rawNote=esc((t.note||'').replace(/;\s*key \$[\d,]+\.?/g,'').replace(/\.\s*$/,''));
  const noteHtml=rawNote?`<div class="thesis-bar-note" title="${rawNote}">${rawNote}</div>`:'';
  let regHtml='';
  const rg=t.regime;
  if(rg&&rg.regime){
    const rng=(rg.range_lo&&rg.range_hi)?` $${Math.round(rg.range_lo).toLocaleString()}–$${Math.round(rg.range_hi).toLocaleString()}`:'';
    regHtml=`<span class="thesis-bar-meta" style="color:var(--blue)" title="${esc(rg.note||'')}">⟳ ${esc(rg.regime)}${rng} · ${esc(rg.session||'')}</span>`;
  }
  bar.className=`thesis-bar ${cls}`;
  bar.style.display='';
  bar.innerHTML=`<span style="font-size:10px;color:var(--mute);text-transform:uppercase;letter-spacing:.8px">Thesis</span><span class="thesis-bar-bias ${cls}">${arrow} ${bias}</span><span class="thesis-bar-meta">${convStr}${keyStr}</span>${regHtml}${spotHtml}${noteHtml}`;
}

// ── flush alert ──────────────────────────────────────────────────────────────
function renderFlush(s){
  const el=$('flush-alert');
  if(s&&s.is_flush&&s.flush_score>=20){
    el.style.display='block';
    $('flush-score-val').textContent='⚡ FLUSH '+s.flush_score+'/100';
    const bp=s.buy_pressure?` | buy pressure +${Math.round(s.buy_pressure/1000)}K`:'';
    $('flush-msg').textContent='BUYERS ABSORBING THE DROP — bounce risk for YES'+bp;
  } else {
    el.style.display='none';
  }
}

// ── guidance logic ───────────────────────────────────────────────────────────
function renderGuidance(s){
  const wrap=$('guidance-wrap');
  if(!s||s.status!=='ok'||!myPos||myPos==='FLAT'){wrap.style.display='none';return;}
  wrap.style.display='block';
  const dist=s.distance||0, minsLeft=s.mins_left||0, momentum=s.momentum||0;
  const isFlush=s.is_flush, vol=s.btc_vol_per_min||50;
  const expected=vol*Math.sqrt(Math.max(minsLeft,.5));
  const yesWinning=dist>0;
  const myWinning=(myPos==='YES')===yesWinning;
  const gap=Math.abs(dist);
  const gapInVols=gap/Math.max(expected,1);
  const timeShort=minsLeft<3;
  const moMeaning=myPos==='YES'?momentum>5:momentum<-5;
  let type,icon,text;
  if(myWinning){
    if(timeShort&&gapInVols>0.6){type='hold';icon='✅';text=`HOLD — winning by $${gap.toFixed(0)} with ${minsLeft.toFixed(1)}m left. Let it ride.`;}
    else if(isFlush&&myPos==='NO'){type='caution';icon='⚡';text=`CAUTION — flush active. You're winning (NO) but buyers buying. Watch for bounce.`;}
    else{type='hold';icon='✅';text=`ON TRACK — ${myPos} winning by $${gap.toFixed(0)}. ${minsLeft.toFixed(1)}m left, expected ±$${Math.round(expected)}.`;}
  } else {
    if(timeShort&&gapInVols>1.0){type='danger';icon='🚨';text=`LIKELY DEAD — $${gap.toFixed(0)} against you with ${minsLeft.toFixed(1)}m left. Expected move only ±$${Math.round(expected)}. Consider cutting.`;}
    else if(isFlush&&myPos==='YES'){type='caution';icon='⚡';text=`FLUSH DETECTED — buyers absorbing. Hold ${myPos} if you have time (${minsLeft.toFixed(1)}m left, $${gap.toFixed(0)} to recover).`;}
    else if(moMeaning&&!timeShort){type='caution';icon='↩️';text=`MOMENTUM TURNING your way — $${gap.toFixed(0)} to recover, ${minsLeft.toFixed(1)}m left. Watch for follow-through.`;}
    else if(timeShort){type='danger';icon='🚨';text=`LOSING — $${gap.toFixed(0)} against ${myPos} with ${minsLeft.toFixed(1)}m left. Cut or accept the loss.`;}
    else{type='danger';icon='⚠️';text=`AT RISK — $${gap.toFixed(0)} against ${myPos}, ${minsLeft.toFixed(1)}m left.`;}
  }
  const bar=$('guidance-bar');
  bar.className='guidance-bar '+type;
  $('guidance-icon').textContent=icon;
  $('guidance-text').textContent=text;
}

// ── limit order panel ────────────────────────────────────────────────────────
function renderLimits(s){
  if(!s||s.status!=='ok')return;
  const yesAsk=s.yes_ask||0, noAsk=s.no_ask||0, spread=s.spread||0;
  if(!yesAsk||!noAsk)return;
  const sig=s.direction;
  const minsLeft=s.mins_left||0;
  function rows(ask,dir){
    const isSig=dir===sig;
    const urgency=minsLeft<5?'Under 5m — aggressive or market only.':
                  minsLeft<9?'Aggressive recommended (fills most of the time).':
                             'Patient order saves 3¢ — plenty of time.';
    return{html:`
      <div class="limit-row market"><span class="limit-tier">Market${isSig?' ← signal':''}</span><span class="limit-price">${fmtC(ask)}</span><span class="limit-save"></span></div>
      <div class="limit-row aggr"><span class="limit-tier">Aggressive</span><span class="limit-price">${fmtC(ask-.01)}</span><span class="limit-save">−1¢</span></div>
      <div class="limit-row patient"><span class="limit-tier">Patient</span><span class="limit-price">${fmtC(ask-.03)}</span><span class="limit-save">−3¢</span></div>
      <div class="limit-row best"><span class="limit-tier">Best price</span><span class="limit-price">${fmtC(ask-.06)}</span><span class="limit-save">−6¢</span></div>`,
    note:isSig?urgency:''};
  }
  const yr=rows(yesAsk,'YES'), nr=rows(noAsk,'NO');
  $('limit-yes-rows').innerHTML=yr.html; $('limit-yes-note').textContent=yr.note;
  $('limit-no-rows').innerHTML=nr.html;  $('limit-no-note').textContent=nr.note;
  $('limit-yes-col').className='limit-col'+(sig==='YES'?' signal-yes':'');
  $('limit-no-col').className='limit-col'+(sig==='NO'?' signal-no':'');
  $('limit-spread-note').textContent=`spread ${(spread*100).toFixed(1)}¢ · ${minsLeft.toFixed(1)}m left`;
}

// ── main signal banner ───────────────────────────────────────────────────────
const _EMPTY_SIDE={sell_low_offset_c:0,sell_high_offset_c:0,low_hit_rate:null,high_hit_rate:null,buy_touch_rate:null,n:0};
function renderSignalBanner(s, isT1=false){
  if(!s||s.status!=='ok')return;
  const banner=$('signal-banner');
  const isUp=s.direction==='YES', dirCls=isUp?'up':'down';
  $('sig-dir').textContent=isUp?'▲':'▼';
  $('sig-dir').className='sig-direction '+dirCls;
  // Flash only when the market or direction actually changed — replaying the
  // opacity-dip keyframe on every 4s poll makes the banner blink constantly.
  const flashKey=s.ticker+'|'+s.direction;
  const changed=flashKey!==_lastDir;
  _lastDir=flashKey;
  banner.className='signal-banner '+dirCls+(changed?' flash':'');
  if(changed)setTimeout(()=>banner.classList.remove('flash'),600);

  // big distance display
  if(s.spot!=null&&s.floor_strike!=null){
    const d=s.distance, pc=d>=0?'pos':'neg';
    const sign=d>=0?'+$':'−$';
    $('dist-val').textContent=sign+Math.abs(Math.round(d)).toLocaleString();
    $('dist-val').className='dist-val '+pc;
    $('dist-sub').textContent=`${fmt$(s.spot,0)} vs ${fmt$(s.floor_strike,0)} strike`;
    $('dist-stat').style.display='';
  } else {
    $('dist-stat').style.display='none';
  }

  const tradeable=s.price>=.05&&s.price<=.95&&s.mins_left!=null&&s.mins_left>=2;
  if(!tradeable){
    banner.classList.add('thesis-mute');
    $('sig-range-buy').textContent='—'; $('sig-range-sell').textContent='—';
    $('sig-label').innerHTML='<span style="background:var(--mute);color:#000;font-weight:700;padding:1px 6px;font-size:10px">NOT TRADEABLE</span> market decided or &lt;2 min left';
    $('sig-conf-pct').textContent=s.confidence+'%'; $('conf-bar').style.width=s.confidence+'%';
    $('sig-components').innerHTML=''; $('sig-stats').innerHTML=''; $('sig-trade-type').style.display='none';
    return;
  }

  const buySide=isUp?'YES':'NO', sideKey=isUp?'yes':'no';
  const sideOff=(_bannerOffsets&&_bannerOffsets[sideKey])||_EMPTY_SIDE;
  const buyPriceC=isUp?(s.price*100):((1-s.price)*100);
  const flowFairC=isUp?s.yes_pct:(100-s.yes_pct);
  const buyLowC=Math.max(1,buyPriceC-3), buyHighC=Math.min(95,buyPriceC+2);
  const sellLowC=Math.max(buyHighC+2,Math.min(95,buyHighC+10-(sideOff.sell_low_offset_c||0)));
  const sellHighC=Math.max(sellLowC+2,Math.min(95,flowFairC-(sideOff.sell_high_offset_c||0)));

  $('sig-range-buy').textContent=`BUY ${buySide} ${buyLowC.toFixed(1)}¢–${buyHighC.toFixed(1)}¢`;
  $('sig-range-sell').textContent=`SELL ${sellLowC.toFixed(1)}¢–${sellHighC.toFixed(1)}¢`;
  $('sig-conf-pct').textContent=s.confidence+'%';
  const bar=$('conf-bar'); bar.style.width=s.confidence+'%'; bar.className='conf-bar'+(isUp?'':' down');

  // thesis gate
  const bias=(_dailyThesis.bias||'').toUpperCase();
  let thesisFlag='', thesisCls='';
  if(bias==='WAIT'){thesisCls='thesis-mute';thesisFlag='<span style="background:var(--yellow);color:#000;font-weight:700;padding:1px 5px;font-size:10px">WAIT</span> ';}
  else if((bias==='UP'&&!isUp)||(bias==='DOWN'&&isUp)){thesisCls='thesis-counter';thesisFlag='<span style="background:var(--red);color:#fff;font-weight:700;padding:1px 5px;font-size:10px">COUNTER</span> ';}
  if(thesisCls) banner.classList.add(thesisCls);

  const minsStr=s.mins_left!=null?s.mins_left.toFixed(1)+'m left':'';
  $('sig-label').innerHTML=thesisFlag+
    `<span class="dim">flow ${flowFairC.toFixed(0)}% ${buySide} · edge +${Math.round(sellLowC-buyHighC)}¢</span>`+
    (minsStr?` · <span class="dim">${minsStr}</span>`:'');

  const tt=tradeType(s), ttEl=$('sig-trade-type');
  if(tt){ttEl.innerHTML=`<span class="trade-badge ${tt.toLowerCase()}">${tt}</span>`;ttEl.style.display='';}
  else ttEl.style.display='none';

  const trendStr=s.whale_trend!=null&&Math.abs(s.whale_trend)>2
    ?` <span class="${s.whale_trend>0?'pos':'neg'}">${s.whale_trend>0?'↑':'↓'}${Math.abs(s.whale_trend).toFixed(0)}</span>`:'';
  const spreadStr=s.spread!=null?`<span class="${s.spread>0.05?'neg':s.spread<0?'pos':'dim'}">${(s.spread*100).toFixed(1)}¢</span>`:'—';
  const keyLevel=_dailyThesis.level?parseFloat(_dailyThesis.level):null;
  const nearKey=keyLevel&&s.spot?Math.abs(s.spot-keyLevel)<350:false;
  const keyLvlStat=keyLevel
    ?`<div class="sig-stat"><span class="k">KEY LVL</span><span class="v ${nearKey?'':'dim'}" style="${nearKey?'color:var(--orange)':''}">${fmt$(keyLevel)}${nearKey?' ⚡':''}</span></div>`:'';
  $('sig-stats').innerHTML=`
    <div class="sig-stat"><span class="k">${s.has_whale_data?'Whales':'Flow'}</span><span class="v" style="color:${isUp?'var(--green)':'var(--red)'}">${s.yes_pct}%${trendStr}</span></div>
    <div class="sig-stat"><span class="k">YES/NO</span><span class="v"><span class="pos">${(s.yes_contracts/1000).toFixed(1)}K</span>/<span class="neg">${(s.no_contracts/1000).toFixed(1)}K</span></span></div>
    ${s.momentum!=null?`<div class="sig-stat"><span class="k">Momo</span><span class="v ${s.momentum>=0?'pos':'neg'}">${s.momentum>=0?'+':''}${s.momentum.toFixed(0)}/m</span></div>`:''}
    <div class="sig-stat"><span class="k">Spread</span><span class="v">${spreadStr}</span></div>
    ${keyLvlStat}`;
  $('sig-components').innerHTML=sigComp('Whale',s.sig_whale)+sigComp('Spot',s.sig_spot)+sigComp('Momo',s.sig_momentum)+
    (s.sig_combined!=null?`<span class="sig-comp ${s.sig_combined>8?'bull':s.sig_combined<-8?'bear':'neut'}" style="font-size:12px;padding:3px 9px">NET ${s.sig_combined>0?'+':''}${s.sig_combined}</span>`:'');
  $('sig-ticker').textContent=s.ticker.split('-').slice(1).join('-')||s.ticker;

  const badge=$('sig-badge');
  if(isT1){badge.textContent='T+1 UPDATE';badge.className='sig-reset-badge t1';badge.style.display='';setTimeout(()=>{badge.style.display='none';},8000);}

  renderFlush(s); renderGuidance(s); renderLimits(s);
}

// ── BRS history ──────────────────────────────────────────────────────────────
function renderBRS(rows, off, cur, stats){
  rows=rows||[];
  // prefer backend-computed stats over full history; fall back to subset
  const st=stats&&stats.total?stats:null;
  const totN   = st?st.total:rows.length;
  const entered= st?st.entered:rows.filter(r=>r.buy_touched).length;
  const wins   = st?st.wins:rows.filter(r=>r.buy_touched&&r.low_hit).length;
  const stretches=st?st.stretches:rows.filter(r=>r.buy_touched&&r.high_hit).length;
  const winPct = entered?(st?st.win_pct:(wins/entered*100)).toFixed(0):'—';
  const strPct = entered?(st?st.str_pct:(stretches/entered*100)).toFixed(0):'—';
  const wCls=winPct==='—'?'dim':winPct>=90?'pos':winPct>=70?'':' neg';
  const sCls=strPct==='—'?'dim':strPct>=60?'pos':strPct>=40?'':'neg';
  $('brs-meta').textContent=st?`all-time · ${totN} settled · ${entered} entered`:`n=${totN} · ${entered} entered`;
  if(!rows.length){$('brs-body').innerHTML='<div class="empty">no settled markets yet</div>';return;}
  let headline=`<div class="brs-stat-row">
    <div class="brs-stat"><span class="brs-stat-k">Win Rate</span><span class="brs-stat-v ${wCls}">${winPct==='—'?'—':winPct+'%'}</span><span class="brs-stat-sub">${wins}/${entered} · goal 90%</span></div>
    <div class="brs-stat"><span class="brs-stat-k">Stretch</span><span class="brs-stat-v ${sCls}">${strPct==='—'?'—':strPct+'%'}</span><span class="brs-stat-sub">${stretches}/${entered} · goal 60%</span></div>
    <div class="brs-stat"><span class="brs-stat-k">All-time</span><span class="brs-stat-v dim" style="font-size:32px">${entered}<span style="font-size:18px;opacity:.5">/${totN}</span></span><span class="brs-stat-sub">entries / settled</span></div>
  </div>`;
  const curList=Array.isArray(cur)?cur:(cur&&cur.ticker?[cur]:[]);
  let pending='';
  if(curList.length){
    const sorted=curList.slice().sort((a,b)=>(b.snap_idx||0)-(a.snap_idx||0));
    pending=sorted.map(sn=>{
      const tail=sn.ticker.split('-').slice(-2).join('-');
      const side=sn.side==='YES'?'<span class="yes">YES</span>':'<span class="no">NO</span>';
      const mins=sn.mins_left!=null?sn.mins_left.toFixed(1)+'m':'—';
      const lbl=!sn.buy_touched?'<span class="dim">no entry</span>':sn.low_hit_so_far?'<span class="pos font-weight:700">WIN ✓</span>':'<span class="neg">pending</span>';
      return `<div class="brs-row" style="background:#211e00;border-left-color:var(--yellow)">
        <span class="ticker trunc" style="color:var(--yellow)" title="${sn.ticker}">LIVE ${tail} ${mins}</span>
        ${side}<span class="num dim">${(sn.max_buy_c||0).toFixed(1)}¢</span>
        <span class="num dim">≥${(sn.sell_low||0).toFixed(1)}?</span>${lbl}
        <span class="num dim">≥${(sn.sell_high||0).toFixed(1)}?</span><span class="dim">·</span></div>`;
    }).join('');
  }
  const tableRows=rows.slice(0,60).map(r=>{
    const tail=r.ticker?r.ticker.split('-').slice(-2).join('-'):'';
    const side=r.side==='YES'?'<span class="yes">YES</span>':'<span class="no">NO</span>';
    let lbl,cls;
    if(!r.buy_touched){lbl='<span class="dim">no entry</span>';cls='no-entry';}
    else if(r.low_hit){lbl='<span class="pos" style="font-weight:700">WIN</span>';cls='win';}
    else{lbl='<span class="neg" style="font-weight:700">loss</span>';cls='loss';}
    return `<div class="brs-row ${cls}">
      <span class="ticker trunc" title="${r.ticker}">${tail}</span>${side}
      <span class="num dim">${(r.max_buy_c||0).toFixed(1)}¢</span>
      <span class="num dim">≥${(r.sell_low||0).toFixed(1)}?</span>${lbl}
      <span class="num dim">≥${(r.sell_high||0).toFixed(1)}?</span>
      <span class="${r.high_hit?'pos dim':'dim'}">${r.high_hit?'✓':'·'}</span></div>`;
  }).join('');
  $('brs-body').innerHTML=headline+pending+tableRows;
}

// ── call history ─────────────────────────────────────────────────────────────
function renderHistory(rows){
  const strip=$('history-strip');
  const grid=$('hist-grid');
  if(!rows||!rows.length){
    strip.innerHTML='<div class="dim" style="font-size:11px;padding:4px">no history yet</div>';
    grid.innerHTML='';
    return;
  }
  const wins=rows.filter(r=>r.correct===true).length;
  const settled=rows.filter(r=>r.outcome!=null).length;
  const pct=settled>0?Math.round(wins/settled*100):0;
  const pCls=pct>=55?'pos':pct<=45?'neg':'';
  $('score-label').innerHTML=settled>0
    ?`<span class="score-pill ${pCls}">${wins}/${settled} &nbsp;<span style="font-size:14px;font-weight:900">${pct}%</span></span>`:'';

  const reversed=rows.slice().reverse();

  // ── horizontal strip (most recent first, newest on left) ──
  strip.innerHTML=reversed.map(r=>{
    const isUp=r.direction==='YES', dCls=isUp?'yes':'no', dLabel=isUp?'▲ YES':'▼ NO';
    const ts=r.ts?new Date(r.ts*1000).toISOString().slice(11,16):'?';
    const label=r.ticker?r.ticker.split('-').slice(-2).join('-'):'';
    let outHtml,outCls,cardCls;
    if(r.outcome){
      outHtml=(r.correct?'✓ ':'✗ ')+r.outcome;
      outCls=r.correct?'ok':'bad';
      cardCls=r.correct?'correct':'wrong';
    } else {
      outHtml='pending…'; outCls=''; cardCls='pending';
    }
    return `<div class="hist-card ${cardCls}" title="${r.ticker||''}">
      <span class="hc-dir ${dCls}">${dLabel}</span>
      <span class="hc-conf">${r.conf!=null?r.conf+'% conf':''}</span>
      <span class="hc-out ${outCls}">${outHtml}</span>
      <span class="hc-time">${label} · ${ts}</span>
    </div>`;
  }).join('');

  // ── vertical detail list at bottom ──
  grid.innerHTML=reversed.map(r=>{
    const isUp=r.direction==='YES', dCls=isUp?'yes':'no', dLabel=isUp?'▲ YES':'▼ NO';
    const ts=r.ts?new Date(r.ts*1000).toISOString().slice(11,16):'?';
    const label=r.ticker?r.ticker.split('-').slice(-2).join('-'):'';
    let outHtml,outCls;
    if(r.outcome){outHtml=(r.correct?'✓ ':'✗ ')+r.outcome;outCls=r.correct?'ok':'bad';}
    else{outHtml='pending…';outCls='pending';}
    const cardCls=r.outcome?(r.correct?'correct':'wrong'):'pending';
    return `<div class="hcard ${cardCls}">
      <span class="hcard-dir ${dCls}" style="font-size:14px">${dLabel}</span>
      <span class="hcard-conf dim">${r.conf!=null?r.conf+'%':''}</span>
      <span class="hcard-out ${outCls}" style="font-size:13px">${outHtml}</span>
      <span class="hcard-time">${label} · ${ts} UTC</span>
    </div>`;
  }).join('');
}

// ── fills toggle ─────────────────────────────────────────────────────────────
// ── bot open plays ───────────────────────────────────────────────────────────
function renderBotPlays(d){
  const s=d.state||{};
  const open=Object.entries(s.open_plays||{});
  $('bp-meta').textContent=open.length?`${open.length} open`:'—';
  $('bp-table').tBodies[0].innerHTML=open.length?open.map(([t,p])=>{
    const pool=p.pool||'—';
    return `<tr><td>${esc(t)}</td>
      <td><span class="bp-side ${p.side.toLowerCase()}">${p.side}</span></td>
      <td class="bp-pool">${esc(pool)}</td>
      <td>${p.qty}</td>
      <td>${(p.entry.price*100).toFixed(1)}¢</td>
      <td>$${(p.entry.price*p.qty).toFixed(2)}</td></tr>`;
  }).join(''):'<tr><td colspan="6"><div class="empty">no open plays</div></td></tr>';
}

// ── loop log ─────────────────────────────────────────────────────────────────
let _logKeys=new Set();
function renderLog(entries){
  if(!entries||!entries.length){if(!_logKeys.size)$('log-entries').innerHTML='<div class="empty">no log entries yet</div>';return;}
  $('log-meta').textContent=entries.length+' entries';
  const el=$('log-entries');
  let added=0;
  entries.slice().reverse().forEach(e=>{
    const key=e.id||(e.ts+'|'+e.msg);
    if(_logKeys.has(key))return;
    _logKeys.add(key);
    const ts=e.ts?new Date(e.ts*1000).toISOString().slice(11,19):'?';
    const typeCls=(e.type||'').replace(/[^A-Z_]/g,'');
    const spotHtml=e.spot!=null?`<span class="log-spot">${fmt$(e.spot,0)}</span>`:'';
    const div=document.createElement('div');
    div.className='log-entry';
    div.innerHTML=`<span class="log-ts">${ts}</span><span class="log-type ${typeCls}">${e.type||'LOG'}</span>${spotHtml}<span class="log-msg">${esc(e.msg)}</span>`;
    if(el.children[0]&&el.children[0].classList.contains('empty'))el.innerHTML='';
    el.prepend(div); added++;
  });
  // Cap the panel: a STATUS entry lands every loop iteration, so an open tab
  // otherwise accumulates thousands of nodes (and _logKeys grows forever).
  while(el.children.length>300)el.removeChild(el.lastChild);
  if(_logKeys.size>1000)_logKeys=new Set(Array.from(_logKeys).slice(-600));
}
function clearLog(){_logKeys.clear();$('log-entries').innerHTML='<div class="empty">cleared</div>';$('log-meta').textContent='—';}

// ── sound ────────────────────────────────────────────────────────────────────
function playAlert(isUp){
  try{
    if(!_audioCtx)_audioCtx=new(window.AudioContext||window.webkitAudioContext)();
    const osc=_audioCtx.createOscillator(), gain=_audioCtx.createGain();
    osc.connect(gain); gain.connect(_audioCtx.destination);
    osc.frequency.value=isUp?880:440; osc.type='sine';
    gain.gain.setValueAtTime(.25,_audioCtx.currentTime);
    gain.gain.exponentialRampToValueAtTime(.001,_audioCtx.currentTime+.6);
    osc.start(_audioCtx.currentTime); osc.stop(_audioCtx.currentTime+.6);
  }catch(e){}
}

// ── signal poll (high frequency) ─────────────────────────────────────────────
let _sigBusy=false;
async function pollSignal(){
  if((document.hidden&&_firstLoadDone)||_sigBusy)return;
  _sigBusy=true;
  try{
    const [s, off, th]=await Promise.all([
      fj('/api/crypto/signal',null),
      fj('/api/crypto/banner_offsets',null),
      fj('/api/crypto/daily_thesis',null),
    ]);
    if(off&&off.yes&&off.no)_bannerOffsets=off;
    if(th)_dailyThesis=th;
    renderThesisBar();
    if(!s)return;               // fetch failed — tick() will flag staleness
    _lastSignal=s;
    _lastGoodSignalTs=Date.now();

    if(s.status==='between_markets'||s.status==='no_active_market'||s.status==='no_data'){
      const banner=$('signal-banner');
      banner.className='signal-banner';
      $('sig-dir').textContent='—'; $('sig-dir').className='sig-direction waiting';
      $('sig-range-buy').textContent='—'; $('sig-range-sell').textContent='—';
      $('dist-stat').style.display='none';
      const mins=s.mins_to_open;
      let waitLabel;
      if(s.status==='no_data') waitLabel='NO DATA — scanner has no market snapshot yet';
      else if(mins!=null&&mins>90){const hrs=Math.floor(mins/60),rm=Math.round(mins%60);waitLabel=`OFF HOURS — next session in ${hrs}h ${rm}m`;}
      else if(mins!=null&&mins>2) waitLabel=`Between markets — opens in ${mins.toFixed(1)}m`;
      else waitLabel='Market opening…';
      $('sig-label').textContent=waitLabel;
      const tickerShort=s.next_ticker?s.next_ticker.split('-').slice(-2).join('-'):'';
      $('sig-stats').innerHTML=tickerShort?`<div class="sig-stat"><span class="k">Next</span><span class="v dim">${tickerShort}</span></div>`:'';
      $('sig-ticker').textContent=''; $('sig-badge').style.display='none'; $('sig-trade-type').style.display='none';
      $('sig-conf-pct').textContent='—'; $('conf-bar').style.width='0%';
      renderFlush(null); renderGuidance(null);
      return;
    }
    if(s.status!=='ok'){return;}

    const isNew=_lastTicker!==null&&s.ticker!==_lastTicker;
    if(isNew){
      const badge=$('sig-badge');
      badge.textContent='NEW MARKET'; badge.className='sig-reset-badge';
      badge.style.display=''; setTimeout(()=>{badge.style.display='none';},8000);
      if(s.ticker!==_lastAlertTicker){playAlert(s.direction==='YES');_lastAlertTicker=s.ticker;}
      // T+1 re-render: whale flow is thin in a market's first minute, so pull
      // a fresh signal 60s after open (same feature as /crypto's banner).
      if(_t1Timer)clearTimeout(_t1Timer);
      _t1Timer=setTimeout(async()=>{
        try{
          const s2=await fj('/api/crypto/signal',null);
          if(s2&&s2.status==='ok'&&s2.ticker===_lastTicker)renderSignalBanner(s2,true);
        }catch(e){}
      },60000);
    }
    _lastTicker=s.ticker;
    renderSignalBanner(s,false);
  }catch(e){console.error('pollSignal',e);}
  finally{_sigBusy=false;}
}

// ── slow refresh (whales, history, BRS, bot plays, log) ──────────────────────
let _slowBusy=false;
async function pollSlow(){
  if((document.hidden&&_firstLoadDone)||_slowBusy)return;
  _slowBusy=true;
  try{
    const [spotR, histR, brsR, brsOff, brsCur, logR, botR]=await Promise.all([
      fj('/api/crypto/spot',{}),
      fj('/api/crypto/history',{rows:[]}),
      fj('/api/crypto/banner_history?limit=60',{rows:[],stats:{}}),
      fj('/api/crypto/banner_offsets',null),
      fj('/api/crypto/banner_current',null),
      fj('/api/loop_log',{entries:[]}),
      fj('/api/bot/status',null),
    ]);
    // Prefer fresh spot; fall back to the signal payload's spot (the reliable
    // source) and mark the header when the collector has gone stale.
    if(spotR.btc&&!spotR.stale){_btcSpot=spotR.btc;$('spot-btc').textContent='BTC '+fmt$(_btcSpot,0);}
    else{
      const fb=_lastSignal&&_lastSignal.spot;
      if(fb){_btcSpot=fb;$('spot-btc').textContent='BTC '+fmt$(fb,0)+'*';}
      else if(spotR.btc){$('spot-btc').textContent='BTC '+fmt$(spotR.btc,0)+' (stale)';}
    }
    if(spotR.eth)$('spot-eth').textContent='ETH '+fmt$(spotR.eth,0);
    if(brsOff&&brsOff.yes&&brsOff.no)_bannerOffsets=brsOff;
    renderThesisBar();
    renderHistory(histR.rows);
    renderBRS(brsR.rows, brsOff, brsCur, brsR.stats);
    if(logR.entries)renderLog(logR.entries);
    if(botR)renderBotPlays(botR);
    _firstLoadDone=true;
  }catch(e){console.error('pollSlow',e);}
  finally{_slowBusy=false;}
}
document.addEventListener('visibilitychange',()=>{if(!document.hidden){pollSignal();pollSlow();}});

// ── clock + data-staleness watchdog ──────────────────────────────────────────
function tick(){
  $('clock').textContent=new Date().toISOString().slice(11,19)+' UTC';
  // The clock updating every second makes the page look live even when the
  // signal feed died — grey the banner and say so instead.
  const age=_lastGoodSignalTs?(Date.now()-_lastGoodSignalTs)/1000:0;
  const banner=$('signal-banner');
  if(_lastGoodSignalTs&&age>15){
    banner.classList.add('stale-data');
    $('sig-ticker').textContent='DATA STALE — '+Math.round(age)+'s without a signal update';
  }else banner.classList.remove('stale-data');
}
tick(); setInterval(tick,1000);

pollSignal();  setInterval(pollSignal,  4000);
pollSlow();    setInterval(pollSlow,   12000);
</script>
</body>
</html>"""

_CRYPTO_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>kalshi-scanner · crypto dashboard</title>
<style>
:root { --bg:#0b0f17; --bg2:#121826; --bg3:#1b2334; --fg:#e8edf4; --mute:#8b96a8;
        --border:#263044; --hair:#1a2232;
        --green:#3fd68c; --red:#ff5c64; --yellow:#ffc53d;
        --blue:#5ca8ff; --orange:#ff9f45; --purple:#c29bff;
        --green-bg:rgba(63,214,140,.08);  --green-bd:rgba(63,214,140,.38);
        --red-bg:rgba(255,92,100,.08);    --red-bd:rgba(255,92,100,.38);
        --yellow-bg:rgba(255,197,61,.08); --yellow-bd:rgba(255,197,61,.38);
        --blue-bg:rgba(92,168,255,.09);   --blue-bd:rgba(92,168,255,.35);
        --sans:ui-sans-serif,system-ui,"Segoe UI",sans-serif; }
* { box-sizing:border-box; margin:0; padding:0; }
::-webkit-scrollbar { width:9px; height:9px; }
::-webkit-scrollbar-thumb { background:var(--bg3); border-radius:5px; border:2px solid var(--bg); }
::-webkit-scrollbar-thumb:hover { background:var(--border); }
body { font-family:ui-monospace,"SF Mono","Fira Code",monospace;
       background:radial-gradient(1100px 460px at 75% -12%, rgba(92,168,255,.055), transparent 65%), var(--bg);
       background-attachment:fixed;
       color:var(--fg); font-size:13px; min-height:100vh; }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation:none !important; transition:none !important; }
}

header { padding:10px 20px; border-bottom:1px solid var(--border); background:var(--bg2);
         display:flex; align-items:center; gap:20px; }
.logo { font-size:14px; font-weight:700; color:var(--blue); margin-right:8px; }
.spot-btc { color:var(--orange); font-weight:700; font-size:14px; }
.spot-eth { color:var(--purple); font-weight:700; font-size:14px; }
.clock { color:var(--mute); font-size:12px; margin-left:auto; }
.nav-link { color:var(--mute); font-size:11px; text-decoration:none; }
.nav-link:hover { color:var(--blue); }

/* ── daily thesis bar ── */
.thesis-bar {
  display:flex; flex-direction:column; justify-content:center; gap:2px;
  padding:6px 18px; border-bottom:2px solid var(--border);
  font-family:inherit; user-select:none; min-height:52px;
}
.thesis-bar-row1 { display:flex; align-items:center; gap:18px; }
.thesis-bar-note { font-size:10px; color:var(--mute); opacity:0.7; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:100%; }
.thesis-bar.up   { background:linear-gradient(90deg, var(--green-bg), transparent 70%); border-bottom-color:var(--green-bd); }
.thesis-bar.down { background:linear-gradient(90deg, var(--red-bg), transparent 70%); border-bottom-color:var(--red-bd); }
.thesis-bar.wait { background:linear-gradient(90deg, var(--yellow-bg), transparent 70%); border-bottom-color:var(--yellow-bd); }
.thesis-bar-label { font-size:10px; font-weight:700; letter-spacing:1.5px; color:var(--mute); text-transform:uppercase; }
.thesis-bar-bias  { font-size:22px; font-weight:900; line-height:1; letter-spacing:-1px; }
.thesis-bar-bias.up   { color:var(--green); }
.thesis-bar-bias.down { color:var(--red); }
.thesis-bar-bias.wait { color:var(--yellow); }
.thesis-bar-meta  { font-size:13px; color:var(--mute); }
.thesis-bar-spot  { margin-left:auto; font-size:13px; font-weight:700; font-variant-numeric:tabular-nums; }
.thesis-bar-spot.above { color:var(--green); }
.thesis-bar-spot.below { color:var(--red); }

/* ── signal banner ── */
.signal-banner {
  display:grid; grid-template-columns:100px 1fr auto;
  gap:0; border-bottom:3px solid var(--border);
  background:var(--bg2); transition:background 0.3s;
  min-height:96px;
}
.signal-banner.up   { border-bottom-color:var(--green);
  background:linear-gradient(125deg, rgba(63,214,140,.13), rgba(63,214,140,.02) 55%), var(--bg2); }
.signal-banner.down { border-bottom-color:var(--red);
  background:linear-gradient(125deg, rgba(255,92,100,.13), rgba(255,92,100,.02) 55%), var(--bg2); }
.signal-banner.flash { animation: flashpulse 0.5s ease-out; }
.signal-banner.thesis-mute    { opacity:0.5; filter:grayscale(50%); }
.signal-banner.thesis-counter { outline:2px dashed var(--red); outline-offset:-2px; }
.signal-banner.stale-data     { opacity:0.45; filter:grayscale(80%); }
@keyframes flashpulse { 0%{opacity:0.1} 40%{opacity:1} 100%{opacity:1} }

/* direction block — left panel */
.sig-dir-block {
  display:flex; flex-direction:column; align-items:center; justify-content:center;
  padding:10px 0; border-right:1px solid var(--border);
}
.sig-direction { font-size:52px; font-weight:900; line-height:1; letter-spacing:-2px; }
.sig-direction.up   { color:var(--green); text-shadow:0 0 30px rgba(63,214,140,.45); }
.sig-direction.down { color:var(--red);   text-shadow:0 0 30px rgba(255,92,100,.45); }
.sig-direction.waiting { color:var(--mute); font-size:32px; text-shadow:none; }
.sig-conf-pct { font-size:11px; font-weight:700; color:var(--mute); margin-top:2px; }

/* center panel — range + label */
.sig-center { display:flex; flex-direction:column; justify-content:center; gap:6px; padding:10px 16px; }
.sig-range-row {
  display:flex; align-items:baseline; gap:10px; flex-wrap:wrap;
}
.sig-range-buy  { font-size:22px; font-weight:900; color:var(--blue); font-variant-numeric:tabular-nums; }
.sig-range-arr  { font-size:16px; color:var(--mute); }
.sig-range-sell { font-size:22px; font-weight:900; color:var(--yellow); font-variant-numeric:tabular-nums; }
.sig-label { font-size:12px; color:var(--fg); line-height:1.4; }
.conf-bar-wrap { width:120px; height:5px; background:var(--hair); border-radius:2px; display:inline-block; vertical-align:middle; margin-left:6px; }
.conf-bar      { height:5px; border-radius:2px; background:var(--green); transition:width 0.3s; }
.conf-bar.down { background:var(--red); }

/* right panel — stats + components */
.sig-right { display:flex; flex-direction:column; justify-content:center; gap:6px; padding:10px 16px;
             border-left:1px solid var(--border); min-width:200px; }
.sig-stats { display:flex; gap:14px; flex-wrap:wrap; font-size:12px; }
.sig-stat  { display:flex; flex-direction:column; gap:1px; }
.sig-stat .k { font-size:10px; color:var(--mute); text-transform:uppercase; letter-spacing:0.5px; }
.sig-stat .v { font-weight:700; font-variant-numeric:tabular-nums; }
.sig-components { display:flex; gap:5px; align-items:center; flex-wrap:wrap; }
.sig-comp { padding:2px 7px; border-radius:3px; font-size:11px; font-weight:700;
            border:1px solid var(--border); white-space:nowrap; }
.sig-comp.bull { background:#0d1f10; color:var(--green); border-color:#2d5a3d; }
.sig-comp.bear { background:#1f0d0d; color:var(--red);   border-color:#5a2a2a; }
.sig-comp.neut { background:var(--bg3); color:var(--mute); }
.sig-ticker-label { font-size:10px; color:var(--mute); }
.sig-reset-badge  { font-size:10px; padding:2px 7px; border-radius:3px; background:#1a3a2a; color:var(--green);
                    border:1px solid #2d5a3d; white-space:nowrap; display:inline-block; }
.sig-reset-badge.t1 { background:#3a2e0a; color:var(--yellow); border-color:#5a4a10; }
.trade-badge { font-size:11px; font-weight:900; padding:2px 9px; border-radius:3px; letter-spacing:1px; margin-top:5px; display:inline-block; }
.trade-badge.swing { background:#2a1500; color:var(--orange); border:1px solid #5a3200; }
.trade-badge.hold  { background:#0d1a2a; color:var(--blue);   border:1px solid #1a4a8a; }

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

.layout { display:grid; grid-template-columns:3fr 2fr; gap:12px; padding:12px; height:calc(100vh - 57px - 99px); }
/* ── BRS panel ── */
.brs-card { border-color:var(--yellow) !important; }
.brs-hdr  { border-bottom:2px solid var(--yellow) !important; background:#1a1600 !important; }
.brs-title { color:var(--yellow) !important; font-size:13px !important; letter-spacing:1px !important; }
.brs-stat  { display:flex; flex-direction:column; gap:3px; padding-right:20px; border-right:1px solid var(--border); }
.brs-stat:last-child { border-right:none; padding-right:0; }
.brs-stat-k { font-size:10px; color:var(--mute); text-transform:uppercase; letter-spacing:1px; }
.brs-stat-v { font-size:36px; font-weight:900; font-variant-numeric:tabular-nums; line-height:1; letter-spacing:-1px; }
.brs-stat-sub { font-size:11px; color:var(--mute); }
/* BRS settled rows */
.brs-row { display:grid; grid-template-columns:1fr 44px 64px 64px 80px 58px 26px;
           padding:5px 10px; border-bottom:1px solid var(--hair); align-items:center; font-size:12px;
           border-left:3px solid transparent; }
.brs-row:last-child { border-bottom:none; }
.brs-row:hover { background:var(--hair); }
.brs-row.win  { border-left-color:var(--green); }
.brs-row.loss { border-left-color:var(--red); }
.brs-row.no-entry { border-left-color:var(--border); }
.col { display:flex; flex-direction:column; gap:12px; min-height:0; }

.card { background:var(--bg2); border:1px solid var(--border); border-radius:10px; overflow:hidden; display:flex; flex-direction:column; min-height:0;
        box-shadow:inset 0 1px 0 rgba(255,255,255,.035), 0 6px 20px rgba(0,0,0,.30); }
.card.grow { flex:1; min-height:0; }
.card-header { padding:7px 12px; border-bottom:1px solid var(--border); background:var(--bg3);
               display:flex; align-items:center; justify-content:space-between; flex-shrink:0; }
.card-title { font-size:11px; text-transform:uppercase; letter-spacing:1px; color:var(--fg); opacity:0.6; }
.card-meta { font-size:11px; color:var(--mute); }
.card-body { padding:0; overflow-y:auto; flex:1; min-height:0; }

/* ── strike ladder ── */
.strike-asset-row { padding:5px 10px; background:var(--bg3); color:var(--fg); font-weight:700; font-size:12px;
                    display:flex; align-items:center; gap:8px; border-bottom:1px solid var(--border); position:sticky; top:0; z-index:1; }
.strike-row { display:grid; grid-template-columns:36px 76px 72px 40px 48px 56px 36px 72px;
              gap:4px; padding:4px 10px; border-bottom:1px solid var(--hair); align-items:center; font-size:12px; }
.strike-row:last-child { border-bottom:none; }
.strike-row:hover { background:var(--hair); }
.itm  { color:var(--green); font-weight:700; font-size:11px; }
.otm  { color:var(--red);   font-weight:700; font-size:11px; }
.stype-t { color:var(--blue); }
.stype-b { color:var(--purple); }
.diff-pos { color:var(--green); }
.diff-neg { color:var(--red); }

/* ── up/down table ── */
.ud-row { display:grid; grid-template-columns:42px 1fr 48px 64px 36px 72px;
          gap:4px; padding:4px 10px; border-bottom:1px solid var(--hair); align-items:center; font-size:12px; }
.ud-row:last-child { border-bottom:none; }
.ud-row:hover { background:var(--hair); }

/* ── alpha signals ── */
.sig-row { display:grid; grid-template-columns:64px 1fr 52px 44px 50px 40px;
           gap:6px; padding:5px 10px; border-bottom:1px solid var(--hair); align-items:center; font-size:12px; }
.sig-row:last-child { border-bottom:none; }
.sig-row:hover { background:var(--hair); }
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
          gap:4px; padding:4px 10px; border-bottom:1px solid var(--hair); align-items:center; font-size:12px; }
.wh-row:last-child { border-bottom:none; }
.wh-row:hover { background:var(--hair); }
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

.flow-bar-wrap { width:60px; height:6px; background:var(--hair); border-radius:3px; display:inline-block; vertical-align:middle; }
.flow-bar { height:6px; border-radius:3px; }
.flow-yes { background:var(--green); }
.flow-no  { background:var(--red); }

footer { text-align:center; padding:8px; color:var(--mute); font-size:11px; border-top:1px solid var(--border); }

/* ── loop log panel ── */
.log-panel { margin:12px; border:1px solid var(--border); border-radius:8px; background:var(--bg2); overflow:hidden; }
.log-panel-header { display:flex; align-items:center; gap:10px; padding:8px 12px;
                    border-bottom:1px solid var(--border); background:var(--bg3); }
.log-panel-title { font-weight:700; font-size:12px; color:var(--blue); letter-spacing:1px; }
.log-panel-meta  { font-size:11px; color:var(--mute); margin-left:auto; }
.log-clear-btn   { font-size:10px; padding:2px 8px; border-radius:4px; border:1px solid var(--border);
                   background:transparent; color:var(--mute); cursor:pointer; }
.log-clear-btn:hover { color:var(--fg); border-color:var(--fg); }
.log-entries { max-height:260px; overflow-y:auto; padding:6px 0; }
.log-entry   { display:flex; gap:8px; align-items:baseline; padding:4px 12px; font-size:12px;
               border-bottom:1px solid var(--hair); }
.log-entry:last-child { border-bottom:none; }
.log-ts   { color:var(--mute); font-size:11px; white-space:nowrap; flex-shrink:0; }
.log-type { font-size:10px; font-weight:700; padding:1px 6px; border-radius:3px; white-space:nowrap; flex-shrink:0; }
.log-type.BREAK_UP   { background:#0d2a0d; color:var(--green); border:1px solid #2d5a2d; }
.log-type.BREAK_DOWN { background:#0d2a0d; color:var(--green); border:1px solid #2d5a2d; }
.log-type.REJECT     { background:#2a0d0d; color:var(--red);   border:1px solid #5a2d2d; }
.log-type.CROSS_ABOVE{ background:#2a0d0d; color:var(--red);   border:1px solid #5a2d2d; }
.log-type.LEVEL_TEST { background:#1a1600; color:var(--yellow); border:1px solid #3a3000; }
.log-type.HOLD       { background:#0d1a2a; color:var(--blue);  border:1px solid #1a3a5a; }
.log-type.SIGNAL     { background:#1a0d2a; color:var(--purple);border:1px solid #3a1a5a; }
.log-type.RESEARCH   { background:#0a1a2a; color:var(--blue);  border:1px solid #1a4a6a; }
.log-type.STATUS     { background:var(--bg3); color:var(--mute); border:1px solid var(--border); }
.log-type.RE_ARM     { background:var(--bg3); color:var(--mute); border:1px solid var(--border); }
.log-type.NOTE       { background:var(--bg3); color:var(--mute); border:1px solid var(--border); }
.log-spot { color:var(--orange); font-variant-numeric:tabular-nums; flex-shrink:0; }
.log-msg  { color:var(--fg); line-height:1.4; }
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

<div class="thesis-bar" id="thesis-bar" style="display:none"></div>

<div class="signal-banner" id="signal-banner">
  <div class="sig-dir-block">
    <div class="sig-direction waiting" id="sig-dir">—</div>
    <div class="sig-conf-pct" id="sig-conf-pct">—</div>
    <div id="sig-trade-type" style="display:none"></div>
  </div>
  <div class="sig-center">
    <div class="sig-range-row" id="sig-range-row">
      <span class="sig-range-buy" id="sig-range-buy">—</span>
      <span class="sig-range-arr">→</span>
      <span class="sig-range-sell" id="sig-range-sell">—</span>
      <span class="conf-bar-wrap"><div class="conf-bar" id="conf-bar" style="width:0%"></div></span>
    </div>
    <div class="sig-label" id="sig-label">waiting for market data…</div>
  </div>
  <div class="sig-right">
    <div class="sig-stats" id="sig-stats"></div>
    <div class="sig-components" id="sig-components"></div>
    <div style="display:flex;gap:8px;align-items:center;margin-top:2px">
      <span class="sig-ticker-label" id="sig-ticker"></span>
      <span id="sig-badge" style="display:none" class="sig-reset-badge">NEW MARKET</span>
    </div>
  </div>
</div>

<div class="history-strip" id="history-strip"></div>

<div class="layout">
  <div class="col">
    <div class="card grow brs-card">
      <div class="card-header brs-hdr">
        <span class="card-title brs-title">BUY / SELL RANGE HISTORY</span>
        <span class="card-meta" id="brs-meta">—</span>
      </div>
      <div class="card-body" id="brs-body"><div class="empty">no settled markets yet — grader is watching</div></div>
    </div>
  </div>
  <div class="col">
    <div class="card grow">
      <div class="card-header">
        <span class="card-title">BTC 15m · Alpha</span>
        <span class="card-meta" id="sig-meta">—</span>
      </div>
      <div class="card-body" id="signals"><div class="empty">loading…</div></div>
    </div>
  </div>
</div>

<div class="layout" style="margin-top:0">
  <div class="col" style="max-width:100%">
    <div class="card">
      <div class="card-header">
        <span class="card-title">BTC 15m · Whale Flow</span>
        <span class="card-meta" id="whale-meta">—</span>
      </div>
      <div class="card-body" style="max-height:220px;overflow-y:auto;padding:0" id="cwhales"><div class="empty">loading…</div></div>
    </div>
  </div>
</div>

<div class="log-panel">
  <div class="log-panel-header">
    <span class="log-panel-title">LOOP ANALYSIS LOG</span>
    <span class="log-panel-meta" id="log-meta">—</span>
    <button class="log-clear-btn" onclick="clearLog()">clear local</button>
  </div>
  <div class="log-entries" id="log-entries"><div class="empty">no log entries yet</div></div>
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

// ── Up/Down Markets (backend kept, UI removed) ───────────────────────
function renderUpDown(rows) { /* panel removed */ }
function _renderUpDownOld(rows) {
  const maxBP = Math.max(...rows.map(r=>Math.abs(r.buy_pressure)),1);
  return rows.map(r => {
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
  if(!rows||!rows.length){$('signals').innerHTML='<div class="empty">no 15m BTC signals yet</div>';$('sig-meta').textContent='0';return;}
  const primary = rows.filter(r => !r.context);
  const ctx     = rows.filter(r =>  r.context);
  $('sig-meta').textContent = primary.length + (ctx.length ? ` · +${ctx.length} ctx` : '');
  const toRow = r => {
    const dirCls = r.direction==='yes'?'dir-yes':'dir-no';
    const dirLabel = r.direction==='yes'?'▲YES':'▼NO';
    const barW = Math.round((r.strength||0)*40);
    const edgeSign = r.fair_value > r.kalshi_price ? '+' : '';
    const edgeCents = Math.round((r.fair_value - r.kalshi_price)*100);
    const edgeCls = edgeCents > 0 ? 'pos' : 'neg';
    const ctxStyle = r.context ? ' style="opacity:0.55"' : '';
    const ctxLabel = r.context ? '<span class="dim" style="font-size:9px;letter-spacing:.5px">CTX</span>' : '';
    return `<div class="sig-row"${ctxStyle} title="${esc(r.detail||'')}">
      ${typeBadge(r.type)}${ctxLabel}
      <span class="trunc dim" title="${esc(r.title||r.ticker)}">${shortTicker(r.title||r.ticker,28)}</span>
      <span class="${dirCls}">${dirLabel}</span>
      <span class="num dim">${fmtP(r.kalshi_price)}</span>
      <span class="num ${edgeCls}">${edgeSign}${edgeCents}¢</span>
      <div style="display:flex;align-items:center"><div class="str-bar" style="width:${barW}px"></div></div>
    </div>`;
  };
  $('signals').innerHTML = primary.map(toRow).join('') +
    (ctx.length ? `<div class="dim" style="font-size:9px;padding:4px 6px;letter-spacing:.8px">── BTC CONTEXT ──</div>` + ctx.map(toRow).join('') : '');
}

// ── BTC 15m Whale Flow ───────────────────────────────────────────────
function renderCWhales(rows) {
  const el = $('cwhales'), meta = $('whale-meta');
  if(!rows||!rows.length){ el.innerHTML='<div class="empty">no whale activity</div>'; meta.textContent='—'; return; }
  const btc15m = rows.filter(r => r.ticker.includes('KXBTC15M'));
  const btcd   = rows.filter(r => r.ticker.includes('KXBTCD') && !r.ticker.includes('15M'));
  meta.textContent = btc15m.length + (btcd.length ? ` · +${btcd.length} daily ctx` : '');
  const toRow = (r, ctx) => {
    const ts = r.ts_ms ? new Date(r.ts_ms).toISOString().slice(11,19) : '?';
    const side = r.side==='yes'?'<span class="yes">YES</span>':'<span class="no">NO</span>';
    const label = r.ticker.split('-').slice(-2).join('-') || r.ticker;
    const big = r.notional >= 200;
    const vs = r.vs_spot != null
      ? `<span class="${r.vs_spot>=0?'pos':'neg'}">${r.vs_spot>=0?'+':'-'}$${Math.abs(r.vs_spot).toLocaleString()}</span>`
      : '<span class="dim">—</span>';
    const ctxStyle = ctx ? 'opacity:0.5' : '';
    return `<div class="wh-row${big?' big':''}" style="${ctxStyle}" title="${r.ticker}">
      <span class="dim">${ts}</span>
      <span class="ticker trunc">${label}</span>
      ${side}
      <span class="num dim">${fmtN(r.contracts)}</span>
      <span class="num dim">${fmtP(r.price)}</span>
      <span class="num" style="color:var(--yellow)">$${Math.round(r.notional)}</span>
      ${vs}
    </div>`;
  };
  el.innerHTML = btc15m.map(r=>toRow(r,false)).join('')
    + (btcd.length ? `<div class="dim" style="font-size:9px;padding:3px 10px;letter-spacing:.8px">── DAILY BTC CONTEXT ──</div>` + btcd.map(r=>toRow(r,true)).join('') : '');
}
// ── Buy/Sell Range History ───────────────────────────────────────────
function renderBannerSuccess(rows, off, cur) {
  rows = rows || [];
  off = (off && off.yes && off.no) ? off : {yes: _EMPTY_SIDE, no: _EMPTY_SIDE};
  const n = rows.length;
  if(n === 0) {
    $('brs-meta').textContent = 'n=0';
    $('brs-body').innerHTML = '<div class="empty">no settled markets yet — grader is watching</div>';
    return;
  }
  // Only markets where buy was actually touched count for win/loss math.
  const tradeable = rows.filter(r => r.buy_touched);
  const wins  = tradeable.filter(r => r.low_hit).length;
  const stretches = tradeable.filter(r => r.high_hit).length;
  const touchPct = (tradeable.length / n * 100).toFixed(0);
  const winPct = tradeable.length ? (wins / tradeable.length * 100).toFixed(0) : '—';
  const strPct = tradeable.length ? (stretches / tradeable.length * 100).toFixed(0) : '—';
  const winCls = winPct === '—' ? 'dim' : (winPct >= 90 ? 'pos' : (winPct >= 70 ? '' : 'neg'));
  const strCls = strPct === '—' ? 'dim' : (strPct >= 60 ? 'pos' : (strPct >= 40 ? '' : 'neg'));
  $('brs-meta').textContent = `n=${n} · entered ${tradeable.length}`;

  const sideRow = (label, side) => {
    const lo = (side.sell_low_offset_c || 0).toFixed(1);
    const hi = (side.sell_high_offset_c || 0).toFixed(1);
    const lr = side.low_hit_rate != null ? (side.low_hit_rate*100).toFixed(0)+'%' : '—';
    const hr = side.high_hit_rate != null ? (side.high_hit_rate*100).toFixed(0)+'%' : '—';
    const tr = side.buy_touch_rate != null ? (side.buy_touch_rate*100).toFixed(0)+'%' : '—';
    return `<div style="font-size:11px"><span class="dim">${label}</span> <span class="${label==='YES'?'yes':'no'}" style="font-weight:700">${label}</span> <span class="dim">n=${side.n||0} · low ${lr}/90% · high ${hr}/60% · touch ${tr} · off ${lo}¢/${hi}¢</span></div>`;
  };

  let thesisLine = '';
  if(_dailyThesis.bias) {
    const bcol = _dailyThesis.bias === 'UP' ? 'var(--green)' : (_dailyThesis.bias === 'DOWN' ? 'var(--red)' : 'var(--yellow)');
    const lvl = _dailyThesis.level ? ` · key $${Number(_dailyThesis.level).toLocaleString()}` : '';
    thesisLine = `<div style="font-size:11px;padding:0 4px 6px"><span class="dim">today's thesis</span> <span style="color:${bcol};font-weight:700">${_dailyThesis.bias}</span> <span class="dim">conv ${_dailyThesis.conviction||'?'}${lvl}</span></div>`;
  }

  const headline = `
    ${thesisLine}
    <div style="padding:10px 14px 8px;display:flex;gap:28px;flex-wrap:wrap;border-bottom:1px solid var(--border);margin-bottom:8px;align-items:flex-end">
      <div class="brs-stat">
        <span class="brs-stat-k">WIN RATE</span>
        <span class="brs-stat-v ${winCls}">${winPct}${winPct==='—'?'':'%'}</span>
        <span class="brs-stat-sub">${wins}/${tradeable.length} · goal 90%</span>
      </div>
      <div class="brs-stat">
        <span class="brs-stat-k">STRETCH</span>
        <span class="brs-stat-v ${strCls}">${strPct}${strPct==='—'?'':'%'}</span>
        <span class="brs-stat-sub">${stretches}/${tradeable.length} · goal 60%</span>
      </div>
      <div class="brs-stat">
        <span class="brs-stat-k">BUY TOUCHED</span>
        <span class="brs-stat-v dim">${touchPct}%</span>
        <span class="brs-stat-sub">${tradeable.length}/${n} markets</span>
      </div>
    </div>
    <div style="padding:4px 14px 6px">
      ${sideRow('YES', off.yes)}
      ${sideRow('NO', off.no)}
    </div>
    <div style="border-top:1px solid var(--border);margin:2px 0 4px"></div>`;

  // Pending rows for every in-flight snapshot the banner has flashed during the
  // current market — each tracked independently until settle.
  let pendingRows = '';
  const curList = Array.isArray(cur) ? cur : (cur && cur.ticker ? [cur] : []);
  if(curList.length) {
    // Newest snapshot first so the latest recommendation is at the top
    const sorted = curList.slice().sort((a,b) => (b.snap_idx||0) - (a.snap_idx||0));
    pendingRows = sorted.map(sn => {
      const tail = sn.ticker.split('-').slice(-2).join('-');
      const side = sn.side === 'YES' ? '<span class="yes">YES</span>' : '<span class="no">NO</span>';
      const max = (sn.max_buy_c || 0).toFixed(1);
      const lo  = (sn.sell_low || 0).toFixed(1);
      const hi  = (sn.sell_high || 0).toFixed(1);
      const mins = sn.mins_left != null ? sn.mins_left.toFixed(1)+'m' : '—';
      const idx = sn.snap_idx != null ? '#'+sn.snap_idx : '';
      let liveLbl;
      if(!sn.buy_touched) {
        liveLbl = '<span class="dim" style="font-weight:700">no entry yet</span>';
      } else if(sn.low_hit_so_far) {
        liveLbl = '<span class="pos" style="font-weight:700">WIN ✓</span>';
      } else {
        liveLbl = '<span class="neg" style="font-weight:700">no win</span>';
      }
      const strLbl = sn.high_hit_so_far ? '<span class="pos">✓</span>' : '<span class="dim">·</span>';
      return `<div class="wh-row" style="grid-template-columns:1fr 44px 64px 64px 80px 58px 26px;background:#211e00;border-left:4px solid var(--yellow);padding-left:8px;padding-top:7px;padding-bottom:7px;box-shadow:inset 2px 0 8px rgba(210,153,34,0.08)">
        <span style="font-size:11px"><b style="color:var(--yellow);font-size:12px">LIVE</b> ${idx} <span class="ticker trunc" title="${sn.ticker}">${tail}</span> <span class="dim">${mins} · buy [${(sn.buy_low||0).toFixed(1)}-${(sn.buy_high||0).toFixed(1)}¢]</span></span>
        <span style="font-size:13px;font-weight:800">${side}</span>
        <span class="num dim" style="font-size:12px">max ${max}¢</span>
        <span class="num dim" style="font-size:12px">≥${lo}¢?</span>
        <span style="font-size:13px;font-weight:800">${liveLbl}</span>
        <span class="num dim" style="font-size:12px">≥${hi}¢?</span>
        ${strLbl}
      </div>`;
    }).join('');
  }
  const tableRows = rows.slice(0, 40).map(r => {
    const tail = r.ticker ? r.ticker.split('-').slice(-2).join('-') : '';
    const side = r.side === 'YES' ? '<span class="yes">YES</span>' : '<span class="no">NO</span>';
    const max = (r.max_buy_c || 0).toFixed(1);
    const lo  = (r.sell_low || 0).toFixed(1);
    const hi  = (r.sell_high || 0).toFixed(1);
    let resultLbl;
    let rowCls;
    if(!r.buy_touched) {
      resultLbl = '<span class="dim" style="font-weight:700">no entry</span>';
      rowCls = 'no-entry';
    } else if(r.low_hit) {
      resultLbl = '<span class="pos" style="font-weight:700">WIN</span>';
      rowCls = 'win';
    } else {
      resultLbl = '<span class="neg" style="font-weight:700">loss</span>';
      rowCls = 'loss';
    }
    const strLbl = r.high_hit ? '<span class="pos" style="font-weight:700">✓</span>' : '<span class="dim">·</span>';
    return `<div class="brs-row ${rowCls}">
      <span class="ticker trunc" title="${r.ticker}" style="font-size:12px">${tail}</span>
      <span style="font-size:13px;font-weight:800">${side}</span>
      <span class="num dim" style="font-size:12px">max ${max}¢</span>
      <span class="num dim" style="font-size:12px">≥${lo}¢?</span>
      <span style="font-size:13px;font-weight:800">${resultLbl}</span>
      <span class="num dim" style="font-size:12px">≥${hi}¢?</span>
      ${strLbl}
    </div>`;
  }).join('');
  $('brs-body').innerHTML = headline + pendingRows + tableRows;
}

// ── Signal history strip ─────────────────────────────────────────────
function renderHistory(rows) {
  const strip = $('history-strip');
  if(!strip) return;
  // Render the empty state instead of returning early — an early return
  // leaves the initial "loading…" placeholder up forever on a fresh day.
  if(!rows||!rows.length){strip.innerHTML='<span class="dim" style="font-size:10px;padding:4px 8px">no history yet</span>';return;}
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
// Guarded fetch: timeout + fallback so one dead endpoint (the spot
// collector is known-flaky) can't reject the whole Promise.all and
// freeze every panel on the page.
const fj = (u, fb) => fetch(u, {signal: AbortSignal.timeout(4000)}).then(r=>r.json()).catch(()=>fb);
function esc(s) { return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
// Pause polling in hidden tabs — but only after the first load, so a page
// opened in a background tab still populates.
let _firstLoadDone = false;
let _refreshBusy = false;
async function refresh() {
  if ((document.hidden && _firstLoadDone) || _refreshBusy) return;
  _refreshBusy = true;
  try {
    const [spotR, sigsR, histR, brsR, brsOff, brsCur, whalesR] = await Promise.all([
      fj('/api/crypto/spot', {}),
      fj('/api/crypto/signals', {rows:[]}),
      fj('/api/crypto/history', {rows:[]}),
      fj('/api/crypto/banner_history?limit=50', {rows:[]}),
      fj('/api/crypto/banner_offsets', null),
      fj('/api/crypto/banner_current', null),
      fj('/api/crypto/whales', {rows:[]}),
    ]);

    if(spotR.btc) {
      _btcSpot = spotR.btc;
      $('spot-btc').textContent = 'BTC ' + fmt$(spotR.btc) + (spotR.stale ? ' (stale)' : '');
    }
    if(spotR.eth) $('spot-eth').textContent = 'ETH ' + fmt$(spotR.eth);

    renderThesisBar(_btcSpot);
    renderSignals(sigsR.rows);
    renderHistory(histR.rows);
    renderBannerSuccess(brsR.rows, brsOff, brsCur);
    renderCWhales(whalesR.rows);
    _firstLoadDone = true;
  } catch(e) { console.error('refresh error', e); }
  finally { _refreshBusy = false; }
}
document.addEventListener('visibilitychange', () => { if(!document.hidden) { refresh(); pollSignal(); } });

let _lastGoodSignalTs = 0;  // declared before tick()'s first synchronous call
function tick() {
  $('clock').textContent = new Date().toISOString().slice(11,19)+' UTC';
  // The clock updating every second makes the page look live even when the
  // signal feed died — grey the banner and say so instead.
  const age = _lastGoodSignalTs ? (Date.now() - _lastGoodSignalTs)/1000 : 0;
  const banner = $('signal-banner');
  if(_lastGoodSignalTs && age > 15) {
    banner.classList.add('stale-data');
    $('sig-ticker').textContent = 'DATA STALE — ' + Math.round(age) + 's without a signal update';
  } else banner.classList.remove('stale-data');
}
tick(); setInterval(tick,1000);
refresh(); setInterval(refresh, 3000);

// ── Signal banner ────────────────────────────────────────────────────
let _lastTicker = null;
const _EMPTY_SIDE = {sell_low_offset_c: 0, sell_high_offset_c: 0, low_hit_rate: null, high_hit_rate: null, buy_touch_rate: null, n: 0};
let _bannerOffsets = {yes: {..._EMPTY_SIDE}, no: {..._EMPTY_SIDE}};
let _dailyThesis = {bias: null, level: null, conviction: null};
let _btcSpot = null;
let _bannerSnap = null;
let _t1Timer = null;
let _lastAlertTicker = null;
let _lastConfAbove50 = false;
let _lastFlashKey = null;

function renderThesisBar(btcSpot) {
  const bar = $('thesis-bar');
  const t = _dailyThesis;
  if(!t || !t.bias || t.bias === 'NONE') { bar.style.display = 'none'; return; }
  const bias = t.bias.toUpperCase();
  const cls = bias === 'UP' ? 'up' : bias === 'DOWN' ? 'down' : 'wait';
  const arrow = bias === 'UP' ? '▲' : bias === 'DOWN' ? '▼' : '◆';
  const lvl = t.level ? parseFloat(t.level) : null;
  const keyStr = lvl ? ` · key $${lvl.toLocaleString()}` : '';
  const convStr = t.conviction ? `conv ${t.conviction}` : '';
  let spotHtml = '';
  if(btcSpot && lvl) {
    const diff = Math.round(btcSpot - lvl);
    const spotCls = diff >= 0 ? 'above' : 'below';
    const diffStr = (diff >= 0 ? '+' : '') + diff.toLocaleString();
    spotHtml = `<span class="thesis-bar-spot ${spotCls}">BTC $${Math.round(btcSpot).toLocaleString()} <span style="font-size:11px;opacity:0.7">(${diffStr} vs key)</span></span>`;
  } else if(btcSpot) {
    spotHtml = `<span class="thesis-bar-spot">BTC $${Math.round(btcSpot).toLocaleString()}</span>`;
  }
  // Strip the redundant "key $X" suffix from note since it's already shown in meta
  const rawNote = esc((t.note || '').replace(/;\s*key \$[\d,]+\.?/g, '').replace(/\.\s*$/, ''));
  const noteHtml = rawNote ? `<div class="thesis-bar-note" title="${rawNote}">${rawNote}</div>` : '';
  let regHtml = '';
  const rg = t.regime;
  if(rg && rg.regime) {
    const rng = (rg.range_lo && rg.range_hi) ? ` $${Math.round(rg.range_lo).toLocaleString()}–$${Math.round(rg.range_hi).toLocaleString()}` : '';
    regHtml = `<span class="thesis-bar-meta" style="color:var(--blue)" title="${esc(rg.note||'')}">⟳ ${esc(rg.regime)}${rng} · ${esc(rg.session||'')}</span>`;
  }
  bar.className = `thesis-bar ${cls}`;
  bar.style.display = '';
  bar.innerHTML = `<div class="thesis-bar-row1"><span class="thesis-bar-label">Thesis</span><span class="thesis-bar-bias ${cls}">${arrow} ${bias}</span><span class="thesis-bar-meta">${convStr}${keyStr}</span>${regHtml}${spotHtml}</div>${noteHtml}`;
}

function fmt$2(n) { return n==null?'—':'$'+Math.round(n).toLocaleString(); }
function sigComp(label, val, suffix='') {
  if(val==null) return '';
  const cls = val > 8 ? 'bull' : val < -8 ? 'bear' : 'neut';
  const arrow = val > 8 ? '▲' : val < -8 ? '▼' : '▶';
  return `<span class="sig-comp ${cls}">${arrow} ${label}${suffix}</span>`;
}

function tradeType(s) {
  if (!s || s.status !== 'ok' || s.mins_left == null || s.mins_left < 2) return null;
  const isUp = s.direction === 'YES';
  const price = s.price;
  const bias = (_dailyThesis.bias || '').toUpperCase();
  const keyLevel = _dailyThesis.level ? parseFloat(_dailyThesis.level) : null;
  const nearKey = keyLevel && s.spot ? Math.abs(s.spot - keyLevel) < 350 : false;
  const counterThesis = (bias === 'UP' && !isUp) || (bias === 'DOWN' && isUp);
  const extreme = (isUp && price > 0.73) || (!isUp && (1 - price) > 0.73);
  if (nearKey || (counterThesis && extreme)) return 'SWING';
  const thesisAligned = !counterThesis && bias && bias !== 'WAIT' && bias !== 'NONE';
  const midRange = price >= 0.28 && price <= 0.72;
  if (thesisAligned && midRange && s.mins_left >= 5) return 'HOLD';
  return null;
}

function renderSignalBanner(s, isT1=false) {
  if(!s || s.status !== 'ok') return;
  const banner = $('signal-banner');
  const isUp = s.direction === 'YES';
  const dirCls = isUp ? 'up' : 'down';

  $('sig-dir').textContent = isUp ? '▲' : '▼';
  $('sig-dir').className = 'sig-direction ' + dirCls;
  // Flash only when the market or direction actually changed — replaying the
  // opacity-dip keyframe on every poll makes the banner blink constantly.
  const flashKey = s.ticker + '|' + s.direction;
  const changed = flashKey !== _lastFlashKey;
  _lastFlashKey = flashKey;
  banner.className = 'signal-banner ' + dirCls + (changed ? ' flash' : '');
  if(changed) setTimeout(() => banner.classList.remove('flash'), 600);

  const tradeable = s.price >= 0.05 && s.price <= 0.95 && s.mins_left != null && s.mins_left >= 2;
  if(!tradeable) {
    banner.classList.add('thesis-mute');
    $('sig-range-buy').textContent = '—';
    $('sig-range-sell').textContent = '—';
    $('sig-label').innerHTML = '<span style="background:var(--mute);color:#000;font-weight:700;padding:1px 6px;font-size:10px">NOT TRADEABLE</span> market decided or &lt;2 min left';
    $('sig-conf-pct').textContent = s.confidence + '%';
    $('conf-bar').style.width = s.confidence + '%';
    $('sig-components').innerHTML = '';
    $('sig-stats').innerHTML = '';
    $('sig-trade-type').style.display = 'none';
    _bannerSnap = null;
    return;
  }

  const buySide   = isUp ? 'YES' : 'NO';
  const sideKey   = isUp ? 'yes' : 'no';
  const sideOff   = (_bannerOffsets && _bannerOffsets[sideKey]) || _EMPTY_SIDE;
  const buyPriceC = isUp ? (s.price*100) : ((1-s.price)*100);
  const flowFairC = isUp ? s.yes_pct : (100 - s.yes_pct);
  const buyLowC   = Math.max(1, buyPriceC - 3);
  const buyHighC  = Math.min(95, buyPriceC + 2);
  const sellLowC  = Math.max(buyHighC + 2, Math.min(95, buyHighC + 10 - (sideOff.sell_low_offset_c || 0)));
  const sellHighC = Math.max(sellLowC + 2, Math.min(95, flowFairC - (sideOff.sell_high_offset_c || 0)));
  _bannerSnap = null;

  // Range display — large numbers
  $('sig-range-buy').textContent  = `BUY ${buySide} ${buyLowC.toFixed(1)}¢–${buyHighC.toFixed(1)}¢`;
  $('sig-range-sell').textContent = `SELL ${sellLowC.toFixed(1)}¢–${sellHighC.toFixed(1)}¢`;

  // Confidence
  $('sig-conf-pct').textContent = s.confidence + '%';
  const bar = $('conf-bar');
  bar.style.width = s.confidence + '%';
  bar.className = 'conf-bar' + (isUp ? '' : ' down');

  // Thesis gate label
  const bias = (_dailyThesis.bias || '').toUpperCase();
  let thesisFlag = ''; let thesisClass = '';
  if(bias === 'WAIT') {
    thesisClass = 'thesis-mute';
    thesisFlag = `<span style="background:var(--yellow);color:#000;font-weight:700;padding:1px 5px;font-size:10px">WAIT</span> `;
  } else if((bias==='UP'&&!isUp)||(bias==='DOWN'&&isUp)) {
    thesisClass = 'thesis-counter';
    thesisFlag = `<span style="background:var(--red);color:#fff;font-weight:700;padding:1px 5px;font-size:10px">COUNTER</span> `;
  }
  if(thesisClass) banner.classList.add(thesisClass);

  let spotStr = '';
  if(s.spot != null && s.floor_strike != null) {
    const dist = s.distance; const sign = dist>=0?'+':'';
    const dc = dist>=0?'pos':'neg';
    spotStr = ` · spot ${fmt$2(s.spot)} vs strike ${fmt$2(s.floor_strike)} (<span class="${dc}">${sign}$${Math.round(Math.abs(dist)).toLocaleString()}</span>)`;
  }
  const minsStr = s.mins_left != null ? s.mins_left.toFixed(1)+'m left' : '';
  $('sig-label').innerHTML = thesisFlag
    + `<span class="dim">flow ${flowFairC.toFixed(0)}% ${buySide} · edge +${Math.round(sellLowC-buyHighC)}¢</span>`
    + spotStr
    + (minsStr ? ` · <span class="dim">${minsStr}</span>` : '');

  // Trade type badge
  const tt = tradeType(s);
  const ttEl = $('sig-trade-type');
  if (tt) {
    ttEl.innerHTML = `<span class="trade-badge ${tt.toLowerCase()}">${tt}</span>`;
    ttEl.style.display = '';
  } else {
    ttEl.style.display = 'none';
  }

  // Stats — include thesis key level
  const trendStr = s.whale_trend!=null && Math.abs(s.whale_trend)>2
    ? ` <span class="${s.whale_trend>0?'pos':'neg'}">${s.whale_trend>0?'↑':'↓'}${Math.abs(s.whale_trend).toFixed(0)}</span>` : '';
  const spreadStr = s.spread!=null ? `<span class="${s.spread>0.05?'neg':s.spread<0?'pos':'dim'}">${(s.spread*100).toFixed(1)}¢</span>` : '—';
  const flowLabel = s.has_whale_data ? 'Whales' : 'Flow';
  const keyLevel = _dailyThesis.level ? parseFloat(_dailyThesis.level) : null;
  const nearKey = keyLevel && s.spot ? Math.abs(s.spot - keyLevel) < 350 : false;
  const keyLvlStat = keyLevel
    ? `<div class="sig-stat"><span class="k">KEY LVL</span><span class="v ${nearKey?'':'dim'}" style="${nearKey?'color:var(--orange)':''}">$${Number(keyLevel).toLocaleString()}${nearKey?' ⚡':''}</span></div>`
    : '';
  $('sig-stats').innerHTML = `
    <div class="sig-stat"><span class="k">${flowLabel}</span><span class="v" style="color:${isUp?'var(--green)':'var(--red)'}">${s.yes_pct}%${trendStr}</span></div>
    <div class="sig-stat"><span class="k">YES/NO</span><span class="v"><span class="pos">${(s.yes_contracts/1000).toFixed(1)}K</span>/<span class="neg">${(s.no_contracts/1000).toFixed(1)}K</span></span></div>
    ${s.momentum!=null?`<div class="sig-stat"><span class="k">Momo</span><span class="v ${s.momentum>=0?'pos':'neg'}">${s.momentum>=0?'+':''}${s.momentum.toFixed(0)}/m</span></div>`:''}
    <div class="sig-stat"><span class="k">Spread</span><span class="v">${spreadStr}</span></div>
    ${keyLvlStat}
  `;
  $('sig-components').innerHTML =
    sigComp('Whale', s.sig_whale) + sigComp('Spot', s.sig_spot) + sigComp('Momo', s.sig_momentum) +
    (s.sig_combined!=null?`<span class="sig-comp ${s.sig_combined>8?'bull':s.sig_combined<-8?'bear':'neut'}" style="font-size:12px;padding:3px 9px">NET ${s.sig_combined>0?'+':''}${s.sig_combined}</span>`:'');

  const parts = s.ticker.split('-');
  $('sig-ticker').textContent = parts.slice(1).join('-') || s.ticker;

  const badge = $('sig-badge');
  if(isT1) {
    badge.textContent = 'T+1 UPDATE'; badge.className = 'sig-reset-badge t1';
    badge.style.display = ''; setTimeout(() => { badge.style.display='none'; }, 8000);
  }
}

let _sigBusy = false;
async function pollSignal() {
  if((document.hidden && _firstLoadDone) || _sigBusy) return;
  _sigBusy = true;
  try {
    const [s, off, th] = await Promise.all([
      fj('/api/crypto/signal', null),
      fj('/api/crypto/banner_offsets', null),
      fj('/api/crypto/daily_thesis', null),
    ]);
    if(off && off.yes && off.no) _bannerOffsets = off;
    if(th) _dailyThesis = th;
    renderThesisBar(_btcSpot);
    if(!s) return;              // fetch failed — tick() will flag staleness
    _lastGoodSignalTs = Date.now();

    if(s.status === 'between_markets' || s.status === 'no_active_market' || s.status === 'no_data') {
      const banner = $('signal-banner');
      banner.className = 'signal-banner';
      _bannerSnap = null;
      $('sig-dir').textContent = '—'; $('sig-dir').className = 'sig-direction waiting';
      $('sig-range-buy').textContent = '—'; $('sig-range-sell').textContent = '—';
      const mins = s.mins_to_open != null ? s.mins_to_open : null;
      let waitLabel;
      if (s.status === 'no_data') {
        waitLabel = 'NO DATA — scanner has no market snapshot yet';
      } else if (mins != null && mins > 90) {
        const hrs = Math.floor(mins / 60), rm = Math.round(mins % 60);
        waitLabel = `OFF HOURS — next session in ${hrs}h ${rm}m`;
      } else if (mins != null && mins > 2) {
        waitLabel = `Between markets — opens in ${mins.toFixed(1)}m`;
      } else {
        waitLabel = 'Market opening…';
      }
      $('sig-label').textContent = waitLabel;
      const tickerShort = s.next_ticker ? s.next_ticker.split('-').slice(-2).join('-') : '';
      $('sig-stats').innerHTML = s.next_ticker
        ? `<div class="sig-stat"><span class="k">Next</span><span class="v dim">${tickerShort}</span></div>`
        + (mins != null && mins > 90 ? `<div class="sig-stat"><span class="k">Session opens</span><span class="v dim">${new Date(Date.now()+(mins*60000)).toISOString().slice(11,16)} UTC</span></div>` : '')
        : '';
      $('sig-ticker').textContent = ''; $('sig-badge').style.display = 'none';
      $('sig-trade-type').style.display = 'none';
      return;
    }
    if(s.status !== 'ok') return;

    const isNew = _lastTicker !== null && s.ticker !== _lastTicker;
    if(isNew) {
      const badge = $('sig-badge');
      badge.textContent = 'NEW MARKET'; badge.className = 'sig-reset-badge';
      badge.style.display = ''; setTimeout(() => { badge.style.display='none'; }, 8000);
      if(_t1Timer) clearTimeout(_t1Timer);
      _t1Timer = setTimeout(async () => {
        try {
          const s2 = await fj('/api/crypto/signal', null);
          if(s2 && s2.status === 'ok') renderSignalBanner(s2, true);
        } catch(e) {}
      }, 60000);
    }
    renderSignalBanner(s);
    _lastTicker = s.ticker;

    const confAbove50 = s.confidence >= 50;
    if(confAbove50 && (s.ticker !== _lastAlertTicker || !_lastConfAbove50)) {
      playAlert(s.direction === 'YES');
      _lastAlertTicker = s.ticker;
    }
    _lastConfAbove50 = confAbove50;
  } catch(e) { console.error('signal poll error', e); }
  finally { _sigBusy = false; }
}

pollSignal();
setInterval(pollSignal, 5000);

// ── Loop analysis log ─────────────────────────────────────────────────
const LOG_LS_KEY = 'kalshi_loop_log_v1';
let _logEntries = [];

function loadLogFromLS() {
  try { _logEntries = JSON.parse(localStorage.getItem(LOG_LS_KEY) || '[]'); } catch(e) { _logEntries = []; }
}

function saveLogToLS() {
  try { localStorage.setItem(LOG_LS_KEY, JSON.stringify(_logEntries.slice(0, 300))); } catch(e) {}
}

function clearLog() {
  _logEntries = [];
  saveLogToLS();
  renderLog();
}

function mergeLogEntries(serverRows) {
  const seen = new Set(_logEntries.map(e => e.ts + '|' + e.msg));
  let added = 0;
  for (const r of serverRows) {
    const key = r.ts + '|' + r.msg;
    if (!seen.has(key)) { _logEntries.push(r); seen.add(key); added++; }
  }
  if (added) {
    _logEntries.sort((a, b) => b.ts - a.ts);
    _logEntries = _logEntries.slice(0, 300);
    saveLogToLS();
  }
}

function renderLog() {
  const el = $('log-entries');
  const meta = $('log-meta');
  if (!_logEntries.length) {
    el.innerHTML = '<div class="empty">no log entries yet</div>';
    meta.textContent = '—';
    return;
  }
  meta.textContent = `${_logEntries.length} entries · last: ${new Date(_logEntries[0].ts * 1000).toISOString().slice(11,19)} UTC`;
  el.innerHTML = _logEntries.map(e => {
    const t = new Date(e.ts * 1000).toISOString().slice(11,19);
    const typeCls = (e.type || 'NOTE').replace(/[^A-Z_]/g, '');
    const spotStr = e.spot != null ? `<span class="log-spot">$${Math.round(e.spot).toLocaleString()}</span>` : '';
    return `<div class="log-entry">
      <span class="log-ts">${t}</span>
      <span class="log-type ${typeCls}">${e.type || 'NOTE'}</span>
      ${spotStr}
      <span class="log-msg">${esc(e.msg)}</span>
    </div>`;
  }).join('');
}

async function refreshLog() {
  if(document.hidden && _firstLoadDone) return;
  try {
    // Endpoint returns {"entries": [...]} (plus "rows" as an alias) — this
    // page destructured a key the server never sent and stayed empty forever.
    const j = await fj('/api/loop_log?limit=300', null);
    if(!j) return;
    mergeLogEntries(j.entries || j.rows || []);
    renderLog();
  } catch(e) {}
}

loadLogFromLS();
renderLog();
refreshLog();
setInterval(refreshLog, 10000);
</script>
</body>
</html>"""

_WHALES_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>kalshi-scanner · BTC signals</title>
<style>
:root { --bg:#0b0f17; --bg2:#121826; --bg3:#1b2334; --fg:#e8edf4; --mute:#8b96a8;
        --border:#263044; --hair:#1a2232;
        --green:#3fd68c; --red:#ff5c64; --yellow:#ffc53d; --blue:#5ca8ff;
        --green-bg:rgba(63,214,140,.08);  --green-bd:rgba(63,214,140,.38);
        --red-bg:rgba(255,92,100,.08);    --red-bd:rgba(255,92,100,.38); }
* { box-sizing:border-box; margin:0; padding:0; }
::-webkit-scrollbar { width:9px; height:9px; }
::-webkit-scrollbar-thumb { background:var(--bg3); border-radius:5px; border:2px solid var(--bg); }
::-webkit-scrollbar-thumb:hover { background:var(--border); }
body { font-family:ui-monospace,"SF Mono","Fira Code",monospace;
       background:radial-gradient(1100px 460px at 75% -12%, rgba(92,168,255,.055), transparent 65%), var(--bg);
       background-attachment:fixed;
       color:var(--fg); font-size:13px; }

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
           gap:6px; align-items:center; padding:7px 0; border-bottom:1px solid var(--hair); }
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
            gap:6px; padding:4px 0; border-bottom:1px solid var(--hair); align-items:center; }
.feed-row:last-child { border-bottom:none; }
.feed-row:hover { background:var(--bg3); }
.feed-row.hi { background:var(--green-bg); border-left:2px solid var(--green); padding-left:4px; }
.feed-row.hi.neg-hi { background:var(--red-bg); border-left-color:var(--red); }
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
  const ok = () => {
    btn.textContent = 'copied!';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = 'copy'; btn.classList.remove('copied'); }, 1500);
  };
  const fail = () => { btn.textContent = 'failed'; setTimeout(() => { btn.textContent = 'copy'; }, 1500); };
  // navigator.clipboard is undefined outside secure contexts (e.g. viewing
  // this page over http://<lan-ip>:9050) — fall back to execCommand there.
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(ticker).then(ok).catch(fail);
  } else {
    try {
      const ta = document.createElement('textarea');
      ta.value = ticker; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      document.execCommand('copy') ? ok() : fail();
      ta.remove();
    } catch(e) { fail(); }
  }
}

function parseExpiry15m(ticker) {
  const m = ticker.match(/(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})-/);
  if (!m) return null;
  const months = {JAN:0,FEB:1,MAR:2,APR:3,MAY:4,JUN:5,JUL:6,AUG:7,SEP:8,OCT:9,NOV:10,DEC:11};
  const etStr = `20${m[1]}-${String(months[m[2]]+1).padStart(2,'0')}-${m[3].padStart(2,'0')}T${m[4]}:${m[5]}:00`;
  // Determine whether EDT (-4) or EST (-5) is in effect at this date via Intl
  const probe = new Date(etStr + '-04:00');
  const tzAbbr = new Intl.DateTimeFormat('en-US', {timeZone:'America/New_York',timeZoneName:'short'})
    .formatToParts(probe).find(p => p.type === 'timeZoneName')?.value ?? 'EDT';
  return new Date(etStr + (tzAbbr === 'EDT' ? '-04:00' : '-05:00'));
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
  // Drop already-expired 15m markets: a stale alpha snapshot otherwise shows
  // a dead market as the #1 signal.
  let markets = data.markets || [];
  if (is15m) markets = markets.filter(m => {
    const exp = parseExpiry15m(m.ticker || '');
    return !exp || (exp - Date.now()) > -30000;
  });
  if (!markets.length) { bodyEl.innerHTML = '<span class="dim">no live markets</span>'; return; }

  bodyEl.innerHTML = markets.map((m, i) => {
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

let _refreshBusy = false;
// Pause polling in hidden tabs — but only after the first load, so a page
// opened in a background tab still populates.
let _firstLoadDone = false;
async function refresh() {
  if ((document.hidden && _firstLoadDone) || _refreshBusy) return;
  _refreshBusy = true;
  try {
    const fj = (u, fb) => fetch(u, {signal: AbortSignal.timeout(4000)}).then(r=>r.json()).catch(()=>fb);
    const [whalesR, btc] = await Promise.all([
      fj('/api/whales?limit=200', {rows:[]}),
      fj('/api/btc', null),
    ]);
    if (btc) {
      renderSignals('body-15m','age-15m', btc.btc_15m, true);
      renderSignals('body-d',  'age-d',   btc.btc_d,   false);
    }
    renderFeed(whalesR.rows);
    _firstLoadDone = true;
  } catch(e) { console.error(e); }
  finally { _refreshBusy = false; }
}
document.addEventListener('visibilitychange', () => { if(!document.hidden) refresh(); });

function tick() { document.getElementById('clock').textContent = new Date().toISOString().slice(11,19)+' UTC'; }
tick(); setInterval(tick, 1000);
refresh(); setInterval(refresh, 2000);
</script>
</body>
</html>"""
