"""Web dashboard for kalshi-scanner — /whales BTC signals + /crypto full dashboard."""
from __future__ import annotations

import asyncio
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


def _pick_writer_loop(interval: int = 30) -> None:
    while True:
        time.sleep(interval)
        if _scanner is not None:
            with contextlib.suppress(Exception):
                from sink import write_btc_picks
                write_btc_picks(_scanner)


def start_background(port: int = 9050) -> threading.Thread:
    threading.Thread(target=_pick_writer_loop, daemon=True).start()
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
    """Analyze active BTC 15m market and return UP/DOWN recommendation."""
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
            # Times in tickers are US Eastern Time (ET), not UTC
            return dt.datetime(2000+int(m.group(1)), _months[m.group(2)], int(m.group(3)),
                               int(m.group(4)), int(m.group(5)), tzinfo=_ET)
        except Exception:
            return None

    # Find active (unsettled) BTC 15m market — prefer most whale activity
    active = None
    for ticker, snap in list(_scanner.market_snapshots.items()):
        if "KXBTC15M" not in ticker.upper():
            continue
        price = snap.last_price or snap.yes_price or 0
        if not (0.01 < price < 0.99):
            continue
        exp = _ticker_expiry(ticker)
        if exp and (exp.timestamp() - time.time()) < -120:
            continue  # expired more than 2 minutes ago
        if active is None or snap.recent_whale_count > active[1].recent_whale_count:
            active = (ticker, snap)

    # If no active market found, also check Kalshi directly for the current ticker
    if not active:
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

    ticker, snap = active
    price = snap.last_price or snap.yes_price or 0.5

    # Tally whale YES vs NO on this ticker
    yes_c = no_c = yes_not = no_not = 0
    for a in list(_scanner.whale_alerts):
        if a.ticker != ticker:
            continue
        if a.side == "yes":
            yes_c += a.contracts
            yes_not += a.notional
        else:
            no_c += a.contracts
            no_not += a.notional

    total_c = yes_c + no_c or 1
    yes_pct = yes_c / total_c

    # Direction from buy_pressure (fastest signal) + whale ratio
    bp_dir = "YES" if snap.buy_pressure >= 0 else "NO"
    flow_dir = "YES" if yes_pct >= 0.5 else "NO"
    direction = "YES" if (yes_pct >= 0.5 and snap.buy_pressure >= 0) else \
                "NO"  if (yes_pct < 0.5 and snap.buy_pressure < 0) else \
                bp_dir  # tiebreak on buy_pressure

    # Confidence 0-100
    ratio_conf = abs(yes_pct - 0.5) * 2          # 0-1
    bp_mag = min(abs(snap.buy_pressure) / 10000, 1.0)
    whale_conf = min(snap.recent_whale_count / 300, 1.0)
    confidence = round((ratio_conf * 0.5 + bp_mag * 0.3 + whale_conf * 0.2) * 100)

    # Parse expiry minutes remaining from ticker (KXBTC15M-26MAY201645-45)
    mins_left = None
    m = re.search(r'(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})', ticker)
    if m:
        months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                  "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
        import datetime as dt
        from zoneinfo import ZoneInfo
        # Ticker times are US Eastern (ET) — YYMMMDDHHMIN e.g. 26MAY201745 = 2026-05-20 17:45 ET
        exp = dt.datetime(2000+int(m.group(1)), months[m.group(2)], int(m.group(3)),
                          int(m.group(4)), int(m.group(5)), tzinfo=ZoneInfo("America/New_York"))
        mins_left = round((exp.timestamp() - time.time()) / 60, 1)

    return JSONResponse({
        "status": "ok",
        "ticker": ticker,
        "direction": direction,
        "confidence": confidence,
        "price": round(price, 4),
        "yes_pct": round(yes_pct * 100, 1),
        "yes_contracts": round(yes_c),
        "no_contracts": round(no_c),
        "yes_notional": round(yes_not),
        "no_notional": round(no_not),
        "whale_count": snap.recent_whale_count,
        "buy_pressure": round(snap.buy_pressure),
        "mins_left": mins_left,
        "ts": time.time(),
    })


@app.get("/api/crypto/updown")
async def api_crypto_updown() -> JSONResponse:
    if _scanner is None:
        return JSONResponse({"rows": []})
    rows = []
    for ticker, snap in list(_scanner.market_snapshots.items()):
        if not _is_crypto(ticker) or not _is_15m_or_1h(ticker):
            continue
        price = snap.last_price or snap.yes_price or 0
        rows.append({
            "ticker": ticker,
            "title": snap.title or ticker,
            "price": round(price, 4),
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

.sig-ticker-label { font-size:11px; color:var(--mute); margin-left:auto; }
.sig-reset-badge  { font-size:10px; padding:2px 7px; border-radius:10px; background:#1a3a2a; color:var(--green);
                    border:1px solid #2d5a3d; white-space:nowrap; }
.sig-reset-badge.t1 { background:#3a2e0a; color:var(--yellow); border-color:#5a4a10; }

.layout { display:grid; grid-template-columns:1fr 1fr; gap:12px; padding:12px; height:calc(100vh - 45px - 64px); }
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
.ud-row { display:grid; grid-template-columns:1fr 48px 64px 36px 72px;
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
  <div>
    <div style="font-size:13px;font-weight:700;margin-bottom:3px" id="sig-label">waiting for market data…</div>
    <div class="sig-conf">
      Confidence <span id="sig-conf-val">—</span>
      <span class="conf-bar-wrap"><div class="conf-bar" id="conf-bar" style="width:0%"></div></span>
    </div>
  </div>
  <div class="sig-stats" id="sig-stats"></div>
  <span class="sig-ticker-label" id="sig-ticker"></span>
  <span id="sig-badge" style="display:none" class="sig-reset-badge">NEW MARKET</span>
</div>

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
  if(!rows||!rows.length){$('updown').innerHTML='<div class="empty">no 15m/1h markets yet</div>';return;}
  $('ud-meta').textContent = rows.length + ' markets';
  const maxBP = Math.max(...rows.map(r=>Math.abs(r.buy_pressure)),1);
  $('updown').innerHTML = rows.map(r => {
    const bpDir = r.buy_pressure >= 0 ? '<span class="yes">YES</span>' : '<span class="no">NO</span>';
    return `<div class="ud-row">
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

// ── Main refresh ─────────────────────────────────────────────────────
async function refresh() {
  try {
    const [spotR, strikesR, udR, sigsR, whR] = await Promise.all([
      fetch('/api/crypto/spot').then(r=>r.json()),
      fetch('/api/crypto/strikes').then(r=>r.json()),
      fetch('/api/crypto/updown').then(r=>r.json()),
      fetch('/api/crypto/signals').then(r=>r.json()),
      fetch('/api/crypto/whales').then(r=>r.json()),
    ]);

    if(spotR.btc) $('spot-btc').textContent = 'BTC ' + fmt$(spotR.btc);
    if(spotR.eth) $('spot-eth').textContent = 'ETH ' + fmt$(spotR.eth);

    renderStrikes(strikesR.rows, strikesR.btc_spot, strikesR.eth_spot);
    renderUpDown(udR.rows);
    renderSignals(sigsR.rows);
    renderCWhales(whR.rows);
  } catch(e) { console.error('refresh error', e); }
}

function tick() { $('clock').textContent = new Date().toISOString().slice(11,19)+' UTC'; }
tick(); setInterval(tick,1000);
refresh(); setInterval(refresh, 3000);

// ── Signal banner ────────────────────────────────────────────────────
let _lastTicker = null;
let _t1Timer = null;

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

  const edgeCents = Math.round(Math.abs((isUp ? s.yes_pct/100 : (100-s.yes_pct)/100) - s.price) * 100);
  $('sig-label').textContent = isUp
    ? `BUY YES — whales ${s.yes_pct}% YES at ${(s.price*100).toFixed(1)}¢ (edge ~${edgeCents}¢)`
    : `BUY NO  — whales ${(100-s.yes_pct).toFixed(1)}% NO at ${((1-s.price)*100).toFixed(1)}¢ (edge ~${edgeCents}¢)`;

  $('sig-conf-val').textContent = s.confidence + '%';
  const bar = $('conf-bar');
  bar.style.width = s.confidence + '%';
  bar.className = 'conf-bar' + (isUp ? '' : ' down');

  const minsStr = s.mins_left != null
    ? (s.mins_left < 0 ? 'expired' : s.mins_left.toFixed(1) + 'm left')
    : '';

  $('sig-stats').innerHTML = `
    <div class="sig-stat"><span class="k">Whale flow</span><span class="v" style="color:${isUp?'var(--green)':'var(--red)'}">${s.yes_pct}% YES</span></div>
    <div class="sig-stat"><span class="k">YES contracts</span><span class="v pos">${s.yes_contracts.toLocaleString()}</span></div>
    <div class="sig-stat"><span class="k">NO contracts</span><span class="v neg">${s.no_contracts.toLocaleString()}</span></div>
    <div class="sig-stat"><span class="k">Net pressure</span><span class="v ${s.buy_pressure>=0?'pos':'neg'}">${s.buy_pressure>=0?'+':''}${s.buy_pressure.toLocaleString()}</span></div>
    <div class="sig-stat"><span class="k">Whales</span><span class="v">${s.whale_count}</span></div>
    ${minsStr ? `<div class="sig-stat"><span class="k">Expires</span><span class="v dim">${minsStr}</span></div>` : ''}
  `;

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
