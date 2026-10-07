"""Which side a KXBTC15M market settled on.

Prefer Kalshi's official `result`. The old basis -- sign of `distance` on the
last logged tick -- agreed with the official result on only 96.9% of Sep 1-11
2026 markets (32/1040 wrong, some by $30+), not the 99.5% previously quoted.
Two causes, measured against `expiration_value`:
  - Kalshi settles on an index averaged over the final minute; one Coinbase
    spot sample ~2.5s before close misses it by up to $26 (p90).
  - Coinbase spot reads ~$4.4 below the settlement index on average.
A 60s mean of logged spot recovers 99.0% (held-out half), so it is the
fallback when no official result is available (offline, or not yet final).

Official results are cached in data/whales/kalshi_results.json and fetched in
batches from the public REST API. Markets settled before Kalshi's historical
cutoff (2026-08-08 at time of writing) live under /historical/markets, so both
endpoints are queried.
"""
import json
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
CACHE = Path(__file__).resolve().parent / "data" / "whales" / "kalshi_results.json"
SETTLE_AVG_S = 60
BATCH = 100


def _load():
    try:
        return json.loads(CACHE.read_text())
    except Exception:
        return {}


def _save(cache):
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, sort_keys=True))
    tmp.replace(CACHE)


def _get_batch(ep, batch, tries=5):
    q = urllib.parse.urlencode({"tickers": ",".join(batch), "limit": BATCH})
    req = urllib.request.Request(f"{KALSHI}{ep}?{q}",
                                 headers={"User-Agent": "kalshi-scanner/1.0"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read()).get("markets", [])
        except urllib.error.HTTPError as e:
            if e.code != 429 or i == tries - 1:
                raise
            time.sleep(1.5 * 2 ** i)
    return []


def _fetch(tickers):
    """Partial results survive a failed batch: one 429 must not discard
    every batch already fetched."""
    out = {}
    for ep in ("/markets", "/historical/markets"):
        todo = [t for t in tickers if t not in out]
        for i in range(0, len(todo), BATCH):
            try:
                ms = _get_batch(ep, todo[i:i + BATCH])
            except Exception:
                continue
            for m in ms:
                if m.get("status") == "finalized" and m.get("result") in ("yes", "no"):
                    out[m["ticker"]] = m["result"].upper()
            time.sleep(0.15)
    return out


def official_results(tickers, fetch=True):
    """{ticker: "YES"|"NO"} for every ticker Kalshi has finalized.

    Cache-first; missing tickers are fetched when `fetch` is true. Network
    failure degrades to whatever is cached -- callers fall back per ticker."""
    cache = _load()
    missing = sorted({t for t in tickers if t not in cache})
    if missing and fetch:
        try:
            got = _fetch(missing)
        except Exception:
            got = {}
        if got:
            cache.update(got)
            _save(cache)
    return {t: cache[t] for t in tickers if t in cache}


def estimate_side(rows, expiry):
    """Spot-basis estimate: mean logged spot over the final 60s vs strike.

    None when no row with spot+strike falls in [expiry-60, expiry] -- a guess
    from an earlier tick is how the old basis went wrong."""
    w = [r for r in rows
         if r.get("spot") is not None and r.get("floor_strike") is not None
         and expiry - SETTLE_AVG_S <= (r.get("ts") or 0) <= expiry]
    if not w:
        return None
    s = statistics.mean(r["spot"] for r in w)
    return "YES" if s >= w[-1]["floor_strike"] else "NO"


def outcome(ticker, rows, results=None):
    """Best available side for analysis tools: official, else spot60 (expiry
    derived from the last row's mins_left), else the legacy last-row distance
    sign so coverage never shrinks below what callers had before."""
    if results and ticker in results:
        return results[ticker]
    if not rows:
        return None
    last = rows[-1]
    if last.get("mins_left") is not None and last.get("ts") is not None:
        est = estimate_side(rows, last["ts"] + last["mins_left"] * 60)
        if est:
            return est
    return "YES" if (last.get("distance") or 0) > 0 else "NO"
