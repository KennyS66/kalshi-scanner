#!/usr/bin/env python3
"""Rebuild KXBTC15M signal-feature rows for a past window from public history.

The live feature log (data/whales/signal_feature_log.jsonl) only exists while
the scanner runs. When it was down, replay/tuner/settle studies have a hole.
This fills it from:
  - Kalshi public REST: settled markets (strike/result), 1-min candlesticks
    (yes_ask/yes_bid OHLC), and every trade (whale flow, buy pressure).
  - Coinbase public 1-min BTC-USD candles for spot. Kalshi serves no spot,
    and the live poller reads Coinbase too.

Rows mirror web.py's /api/crypto/signal (same whale threshold, decay,
aggression weight, component signals, blend weights, flush floor) but are
APPROXIMATIONS: spot is 1-min interpolated (live polls every few seconds),
asks step on trades + minute candles, whale_trend uses the same 180s slope
over rebuilt samples. Every row carries "source": "backfill".

NEVER writes the live log. Output goes to data/whales/backfill/.

Usage:
  python3 kalshi_backfill.py fetch --start 2026-09-12 --end 2026-10-07
  python3 kalshi_backfill.py build --start 2026-09-12 --end 2026-10-07 \
      [--out data/whales/backfill/features_<start>_<end>.jsonl]
Raw API responses are cached gzip-per-market under data/whales/backfill/raw,
so `build` is offline and re-runnable.
"""
import argparse
import bisect
import datetime as dt
import gzip
import json
import math
import time
import urllib.parse
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
COINBASE = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
SERIES = "KXBTC15M"
ROOT = Path(__file__).resolve().parent / "data" / "whales" / "backfill"
RAW = ROOT / "raw"

# Mirrors of the live scanner constants (scanner.py / web.py).
WHALE_THRESHOLD = 50          # main.py --threshold default; start.sh passes none
DECAY = math.log(2) / (8 * 60)  # 8-min half-life on whale notional
AGGR_ASK = 1.4                # ask-side taker weight
ROW_STEP_S = 5.0              # live median row gap Sep 1-11 was 5.08s

_last_req = 0.0
_rate_lock = threading.Lock()


def _get(url, min_gap=0.11, tries=6):
    """Rate-limited GET with backoff (global across threads; ~9 req/s,
    just under the ~10/s where Kalshi starts returning 429). Read-only public endpoints only."""
    global _last_req
    for i in range(tries):
        with _rate_lock:
            wait = min_gap - (time.time() - _last_req)
            if wait > 0:
                time.sleep(wait)
            _last_req = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "kalshi-scanner-backfill/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(2 ** i)
                continue
            raise
        except Exception:
            time.sleep(2 ** i)
    raise RuntimeError(f"GET failed after {tries} tries: {url}")


def _day_ts(s):
    return int(dt.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=dt.UTC).timestamp())


def _iso_ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _f(x, default=0.0):
    try:
        # expiration_value is occasionally comma-formatted ("77,362.10").
        return float(x.replace(",", "")) if isinstance(x, str) else float(x)
    except (TypeError, ValueError):
        return default


# ── fetch ───────────────────────────────────────────────────────────────────

def fetch_markets(t0, t1):
    out, cursor = [], ""
    while True:
        q = {"series_ticker": SERIES, "status": "settled", "min_close_ts": t0,
             "max_close_ts": t1, "limit": 1000}
        if cursor:
            q["cursor"] = cursor
        j = _get(f"{KALSHI}/markets?{urllib.parse.urlencode(q)}")
        out += j.get("markets", [])
        cursor = j.get("cursor") or ""
        if not cursor:
            break
    return [m for m in out if m.get("status") == "finalized"]


_TRADE_KEYS = ("created_time", "count_fp", "yes_price_dollars", "no_price_dollars",
               "taker_outcome_side", "taker_side", "taker_book_side")


def _candles(m):
    close = int(_iso_ts(m["close_time"]))
    open_ = int(_iso_ts(m["open_time"])) if m.get("open_time") else close - 900
    return _get(f"{KALSHI}/series/{SERIES}/markets/{m['ticker']}/candlesticks?"
                f"start_ts={open_ - 60}&end_ts={close}&period_interval=1")


def fetch_market_candles(m):
    """Tier 1: one request per market. No trade tape, so no whale flow."""
    path = RAW / f"{m['ticker']}.candles.json.gz"
    if path.exists() or (RAW / f"{m['ticker']}.json.gz").exists():
        return False
    c = _candles(m)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt") as f:
        json.dump({"market": m, "candles": c.get("candlesticks", []), "trades": None}, f)
    tmp.rename(path)
    return True


def fetch_market(m):
    """Tier 2: candles + the full trade tape (~40 pages per market)."""
    path = RAW / f"{m['ticker']}.json.gz"
    if path.exists():
        return False
    c = _candles(m)
    trades, cursor = [], ""
    while True:
        q = {"ticker": m["ticker"], "limit": 1000}
        if cursor:
            q["cursor"] = cursor
        j = _get(f"{KALSHI}/markets/trades?{urllib.parse.urlencode(q)}")
        # Keep only the fields build_market reads (~5x smaller on disk).
        trades += [{k: x.get(k) for k in _TRADE_KEYS} for x in j.get("trades", [])]
        cursor = j.get("cursor") or ""
        if not cursor:
            break
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt") as f:
        json.dump({"market": m, "candles": c.get("candlesticks", []), "trades": trades}, f)
    tmp.rename(path)
    return True


def fetch_spot(t0, t1):
    path = RAW / f"spot_{t0}_{t1}.json.gz"
    if path.exists():
        return
    rows = {}
    s = t0 - 3600  # first market opens before t0; momentum/vol need lead-in
    while s < t1:
        e = min(s + 300 * 60, t1)
        q = {"granularity": 60,
             "start": dt.datetime.fromtimestamp(s, dt.UTC).isoformat(),
             "end": dt.datetime.fromtimestamp(e, dt.UTC).isoformat()}
        for k in _get(f"{COINBASE}?{urllib.parse.urlencode(q)}", min_gap=0.35):
            rows[int(k[0])] = float(k[4])  # [time, low, high, open, close, vol]
        s = e
    with gzip.open(path, "wt") as f:
        json.dump(sorted(rows.items()), f)


def cmd_fetch(a):
    RAW.mkdir(parents=True, exist_ok=True)
    t0, t1 = _day_ts(a.start), _day_ts(a.end)
    fetch_spot(t0, t1)
    print(f"spot cached", flush=True)
    ms = fetch_markets(t0, t1)
    (RAW / f"markets_{t0}_{t1}.json").write_text(json.dumps(ms))
    print(f"{len(ms)} finalized markets {a.start}..{a.end}", flush=True)
    done = [0, 0, 0]  # finished, new, failed

    def one(m):
        try:
            new = (fetch_market_candles if a.candles_only else fetch_market)(m)
        except Exception as e:
            new = None
            print(f"FAIL {m['ticker']}: {e}", flush=True)
        with _rate_lock:
            done[0] += 1
            done[1] += bool(new)
            done[2] += new is None
            if done[0] % 50 == 0:
                print(f"  {done[0]}/{len(ms)} ({done[1]} new, {done[2]} failed)", flush=True)

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(one, sorted(ms, key=lambda m: m["close_time"])))
    print(f"FETCH_DONE markets={len(ms)} new={done[1]} failed={done[2]}", flush=True)


# ── build ───────────────────────────────────────────────────────────────────

class Spot:
    """1-min Coinbase closes, linearly interpolated (minute close at t+60)."""

    def __init__(self, pairs):
        # Candle k[0] is the minute OPEN; its close price is at k[0]+60.
        self.t = [t + 60 for t, _ in pairs]
        self.p = [p for _, p in pairs]

    def at(self, t):
        i = bisect.bisect_right(self.t, t)
        if i == 0 or i >= len(self.t):
            return None
        t0, t1 = self.t[i - 1], self.t[i]
        if t1 - t0 > 300:
            return None
        return self.p[i - 1] + (self.p[i] - self.p[i - 1]) * (t - t0) / (t1 - t0)


def _signal(t, mins_left, yes_pct, whale_trend, spot, floor, momentum, vol, bp):
    """web.py signal math, verbatim weights."""
    whale_signal = (yes_pct - 0.5) * 2 * 0.70 + max(-1.0, min(1.0, whale_trend * 4)) * 0.30
    mins_remaining = max(mins_left or 15.0, 0.5)
    distance = round(spot - floor, 2) if spot is not None and floor is not None else None
    spot_signal = 0.0
    if distance is not None:
        spot_signal = max(-1.0, min(1.0, distance / max(vol * math.sqrt(mins_remaining), 1.0)))
    mom_signal = 0.0 if momentum is None else max(-1.0, min(1.0, momentum / max(vol, 1.0)))
    if mins_remaining < 2:
        w = (0.15, 0.70, 0.15)
    elif mins_remaining < 5:
        w = (0.25, 0.50, 0.25)
    elif mins_remaining < 9:
        w = (0.35, 0.35, 0.30)
    else:
        w = (0.45, 0.20, 0.35)
    if distance is None:
        combined = (whale_signal * w[0] + mom_signal * w[2]) / ((w[0] + w[2]) or 1.0)
    else:
        combined = whale_signal * w[0] + spot_signal * w[1] + mom_signal * w[2]
    is_flush, flush_score = False, 0
    if distance is not None and distance < -5 and bp > 5000 and mins_remaining > 3:
        flush_score = min(100, int(bp / 300))
        if flush_score >= 20:
            is_flush = True
            combined = max(combined, -0.15)
    return distance, whale_signal, spot_signal, mom_signal, combined, is_flush, flush_score


def build_market(blob, spot):
    m = blob["market"]
    close = _iso_ts(m["close_time"])
    open_ = _iso_ts(m["open_time"]) if m.get("open_time") else close - 900
    floor = _f(m.get("floor_strike"), None)

    has_tape = blob["trades"] is not None
    trades = sorted(
        ({"t": _iso_ts(x["created_time"]), "c": _f(x.get("count_fp")),
          "yp": _f(x.get("yes_price_dollars")), "np": _f(x.get("no_price_dollars")),
          "side": x.get("taker_outcome_side", x.get("taker_side", "?")),
          "book": x.get("taker_book_side", "?")} for x in (blob["trades"] or [])),
        key=lambda x: x["t"])
    # Minute candles keyed by period END: asks during (end-60, end] are known
    # only at end, so use the previous candle's close as the opening quote.
    candles = sorted(blob["candles"], key=lambda c: c["end_period_ts"])
    c_end = [c["end_period_ts"] for c in candles]

    rows, wh_hist = [], []
    ti = 0
    yes_vol = no_vol = vol_all = 0.0
    whales = []
    last_yes_ask = last_no_ask = None
    last_price = None
    last_yes_t = last_no_t = -1.0
    t = open_ + ROW_STEP_S
    while t < close:
        while ti < len(trades) and trades[ti]["t"] <= t:
            x = trades[ti]
            ti += 1
            vol_all += x["c"]
            if x["side"] == "yes":
                yes_vol += x["c"]
                last_yes_ask, last_yes_t = x["yp"], x["t"]
            else:
                no_vol += x["c"]
                last_no_ask, last_no_t = x["np"], x["t"]
            last_price = x["yp"]
            if x["c"] >= WHALE_THRESHOLD:
                whales.append(x)
        ci = bisect.bisect_right(c_end, t) - 1
        if ci >= 0:
            ya = _f(candles[ci]["yes_ask"].get("close_dollars"), None)
            yb = _f(candles[ci]["yes_bid"].get("close_dollars"), None)
            # A taker trade after the candle closed is fresher than the candle.
            cend = c_end[ci]
            yes_ask = last_yes_ask if last_yes_t > cend else ya
            no_ask = last_no_ask if last_no_t > cend else (round(1 - yb, 4) if yb is not None else None)
        else:
            yes_ask, no_ask = last_yes_ask, last_no_ask
        if not yes_ask or not no_ask:
            t += ROW_STEP_S
            continue

        yes_c = no_c = 0.0
        yes_not = no_not = yes_w = no_w = 0.0
        for x in whales:
            notional = x["c"] * x["yp"]  # scanner.WhaleAlert: contracts * yes price
            w = notional * math.exp(-DECAY * max(0.0, t - x["t"])) * (AGGR_ASK if x["book"] == "ask" else 1.0)
            if x["side"] == "yes":
                yes_c += x["c"]; yes_not += notional; yes_w += w
            else:
                no_c += x["c"]; no_not += notional; no_w += w
        bp = yes_vol - no_vol
        if yes_w + no_w > 0:
            yes_pct = yes_w / (yes_w + no_w)
        else:
            yes_pct = (bp / vol_all + 1) / 2 if vol_all > 0 else 0.5
        wh_hist.append((t, yes_pct))
        rec = [(a, b) for a, b in wh_hist[-40:] if t - a < 180]
        whale_trend = 0.0
        if len(rec) >= 2 and rec[-1][0] - rec[0][0] > 1:
            whale_trend = (rec[-1][1] - rec[0][1]) / (rec[-1][0] - rec[0][0]) * 60

        sp = spot.at(t)
        sp90 = spot.at(t - 90)
        momentum = round((sp - sp90) / 90 * 60, 2) if sp is not None and sp90 is not None else None
        pts = [spot.at(t - k * 60) for k in range(5, -1, -1)]
        pts = [p for p in pts if p is not None]
        vol = (sum(abs(b - a) for a, b in zip(pts, pts[1:])) / (len(pts) - 1)) if len(pts) >= 3 else 50.0
        mins_left = round((close - t) / 60, 1)
        distance, s_wh, s_sp, s_mo, comb, is_flush, flush_score = _signal(
            t, mins_left, yes_pct, whale_trend, sp, floor, momentum, vol, bp)
        rows.append({
            "status": "ok", "ticker": m["ticker"],
            "direction": "YES" if comb >= 0 else "NO",
            "confidence": round(min(abs(comb), 1.0) * 100),
            "price": round(last_price if last_price is not None else yes_ask, 4),
            "yes_pct": round(yes_pct * 100, 1), "has_whale_data": (yes_w + no_w) > 0,
            "yes_contracts": round(yes_c), "no_contracts": round(no_c),
            "yes_notional": round(yes_not), "no_notional": round(no_not),
            "whale_count": len(whales), "buy_pressure": round(bp),
            "whale_trend": round(whale_trend * 100, 1),
            "spot": round(sp, 2) if sp is not None else None,
            "floor_strike": floor, "distance": distance,
            "btc_vol_per_min": round(vol, 1),
            "spread": round(yes_ask + no_ask - 1.0, 4),
            "yes_ask": round(yes_ask, 4), "no_ask": round(no_ask, 4),
            "momentum": momentum, "is_flush": is_flush, "flush_score": flush_score,
            "sig_whale": round(s_wh * 100), "sig_spot": round(s_sp * 100),
            "sig_momentum": round(s_mo * 100), "sig_combined": round(comb * 100),
            "mins_left": mins_left, "ts": round(t, 3), "spot_age_s": 0.0,
            "source": "backfill", "tier": "full" if has_tape else "candles",
            "result": m.get("result"),
            "expiration_value": _f(m.get("expiration_value"), None),
        })
        t += ROW_STEP_S
    return rows


def cmd_build(a):
    t0, t1 = _day_ts(a.start), _day_ts(a.end)
    sp = RAW / f"spot_{t0}_{t1}.json.gz"
    with gzip.open(sp, "rt") as f:
        spot = Spot(json.load(f))
    ms = json.loads((RAW / f"markets_{t0}_{t1}.json").read_text())
    out = Path(a.out or ROOT / f"features_{a.start}_{a.end}.jsonl")
    tmp = out.with_suffix(".tmp")
    n_rows = n_mk = skipped = n_tier1 = 0
    with open(tmp, "w") as f:
        for m in sorted(ms, key=lambda m: m["close_time"]):
            p = RAW / f"{m['ticker']}.json.gz"
            if not p.exists():
                p = RAW / f"{m['ticker']}.candles.json.gz"
                n_tier1 += p.exists()
            if not p.exists():
                skipped += 1
                continue
            with gzip.open(p, "rt") as g:
                rows = build_market(json.load(g), spot)
            for r in rows:
                f.write(json.dumps(r) + "\n")
            n_rows += len(rows)
            n_mk += 1
    tmp.rename(out)
    print(f"BUILD_DONE markets={n_mk} (candles-only={n_tier1}) skipped={skipped} "
          f"rows={n_rows} -> {out}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("fetch", "build"):
        p = sub.add_parser(name)
        p.add_argument("--start", required=True)
        p.add_argument("--end", required=True)
        if name == "fetch":
            p.add_argument("--workers", type=int, default=6)
            p.add_argument("--candles-only", action="store_true",
                           help="tier 1: skip the trade tape (1 request/market)")
        if name == "build":
            p.add_argument("--out")
    a = ap.parse_args()
    (cmd_fetch if a.cmd == "fetch" else cmd_build)(a)


if __name__ == "__main__":
    main()
