#!/usr/bin/env python3
"""
daily_research.py — Auto-derive a daily BTC thesis from live web data.

Sources (all free, no API keys required):
  1. Local scanner or CoinGecko  : BTC spot price
  2. CoinGecko market_chart      : 200-day price history → 50d / 200d MA trend
  3. CoinGecko market_chart      : 7-day price history  → recent swing high/low (key level)
  4. alternative.me              : Fear & Greed Index → sentiment
  5. bitbo.io/treasuries/etf-flows: US spot BTC ETF net flows (best-effort, weekday data only)
  6. System clock                : weekday vs weekend, trading session

Bias derivation:
  - MA trend, F&G, ETF flow, session each contribute to a score.
  - Weekend → conviction capped at 2, bias softened to WAIT unless very strong.
  - Fetch failure on any single source degrades gracefully (WAIT on critical failure).
  - Always records spot + source date in the note so you can audit the call.

Writes one thesis per day via daily_thesis.record() (upsert — safe to re-run).

Usage:
  python3 daily_research.py            # run and record thesis
  python3 daily_research.py --dry-run  # print only, do not record
"""
import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from statistics import mean

from daily_thesis import record as thesis_record


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def _fetch_json(url, timeout=12):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (kalshi-scanner/1.0)"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _fetch_text(url, timeout=12):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (kalshi-scanner/1.0)"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="ignore")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------

def btc_spot_from_scanner():
    for url, key in (
        ("http://localhost:9050/api/crypto/spot", "btc"),
        ("http://localhost:9050/api/crypto/signal", "spot"),
    ):
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                v = json.loads(r.read()).get(key)
                if v:
                    return float(v)
        except Exception:
            pass
    return None


def btc_spot_coingecko():
    data = _fetch_json(
        "https://api.coingecko.com/api/v3/simple/price"
        "?ids=bitcoin&vs_currencies=usd&include_24hr_change=true"
    )
    if data and "bitcoin" in data:
        return float(data["bitcoin"]["usd"]), data["bitcoin"].get("usd_24h_change", 0.0)
    return None, None


def price_history(days=200):
    """Return list of daily closes (oldest first), or [] on failure."""
    url = (
        f"https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
        f"?vs_currency=usd&days={days}&interval=daily"
    )
    data = _fetch_json(url)
    if data and "prices" in data:
        return [p[1] for p in data["prices"]]
    return []


def fear_greed_index():
    """Return (value: int, label: str) or (None, None)."""
    data = _fetch_json("https://api.alternative.me/fng/?limit=1&format=json")
    if data and data.get("data"):
        d = data["data"][0]
        return int(d["value"]), d["value_classification"]
    return None, None


def etf_net_flow_usd():
    """
    Scrape bitbo.io for the most recent weekday BTC ETF net flow.
    Returns (date_str, net_millions: float) or (None, None) on failure.

    The page is server-rendered HTML. We look for:
      - A date line like "Jun 25, 2026"
      - A total net flow number on the same row

    This is best-effort: any parse ambiguity returns (None, None).
    """
    text = _fetch_text("https://bitbo.io/treasuries/etf-flows/")
    if not text:
        return None, None

    # Match "Month DD, YYYY" dates
    date_re = re.compile(
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),\s+(\d{4})"
    )
    # Match a total that looks like "$-1,234.5M" or "$1.2B" — total line often says "Total"
    # We look for a dollar sign followed by optional minus and digits with commas/dots,
    # then optional M or B suffix
    flow_re = re.compile(r"([-+]?\$[\d,]+\.?\d*)\s*([MB])?", re.IGNORECASE)

    # Find first date in the page (most recent entry)
    dm = date_re.search(text)
    if not dm:
        return None, None
    date_str = dm.group(0)

    # Look for a net total after the date — scan ~2000 chars after the date
    snippet = text[dm.start(): dm.start() + 2000]
    # Find all dollar amounts and pick the largest absolute value as the total
    amounts = []
    for m in flow_re.finditer(snippet):
        raw = m.group(1).replace("$", "").replace(",", "")
        try:
            val = float(raw)
        except ValueError:
            continue
        suffix = (m.group(2) or "M").upper()
        if suffix == "B":
            val *= 1000
        amounts.append(val)

    if not amounts:
        return date_str, None

    # The total net flow is the largest absolute value in the snippet
    net = max(amounts, key=abs)
    return date_str, round(net, 1)


# ---------------------------------------------------------------------------
# Derived inputs
# ---------------------------------------------------------------------------

def compute_ma(prices, n):
    if len(prices) < n:
        return None
    return mean(prices[-n:])


def session_context():
    """
    Returns (is_weekend, day_name, session_name, liquidity_note).
    """
    now = datetime.now(timezone.utc)
    wd = now.weekday()   # 0=Mon … 6=Sun
    h = now.hour
    is_weekend = wd >= 5
    day_name = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][wd]

    if is_weekend:
        session = "weekend"
        liq = "thin liquidity; no ETF creations/redemptions; wider spreads; wick risk elevated"
    elif 13 <= h < 21:    # 9am–5pm ET (UTC-4 summer)
        session = "US session"
        liq = "primary session; ETF flow active; institutional participation"
    elif 7 <= h < 13:
        session = "London session"
        liq = "EU/UK hours; moderate volume; ETF pre-market"
    else:
        session = "Asia session"
        liq = "off-hours; thinner liquidity; futures-led"
    return is_weekend, day_name, session, liq


def nearest_key_level(prices_7d, spot):
    """
    Derive the most relevant key level for the day.
    Priority: significant round number within $3k, then 7-day swing high or low
    that is ≥$500 away (to avoid hugging current price).
    Returns an int snapped to the nearest $500.
    """
    candidates = []

    # Round $1k levels within ±$3k of spot
    base = round(spot / 1000) * 1000
    for delta in range(-3, 4):
        c = base + delta * 1000
        candidates.append((abs(c - spot), c))

    # 7-day swing high and low
    if prices_7d and len(prices_7d) >= 3:
        hi = max(prices_7d)
        lo = min(prices_7d)
        for lvl in (hi, lo):
            if abs(lvl - spot) >= 500:
                candidates.append((abs(lvl - spot), lvl))

    if not candidates:
        return int(round(spot / 1000) * 1000)

    # Prefer round thousands closest to spot
    round_K = [(d, c) for d, c in candidates if int(c) % 1000 == 0]
    pool = round_K if round_K else candidates
    _, best = min(pool, key=lambda x: x[0])
    # Snap to nearest $500
    return int(round(best / 500) * 500)


# ---------------------------------------------------------------------------
# Scoring → bias
# ---------------------------------------------------------------------------

def derive_thesis(spot, prices_200, prices_7, fg_val, fg_cls,
                  etf_date, etf_net_m, is_weekend, day_name, session, liq):
    """
    Score each signal (+UP / -DOWN) and combine into a bias + conviction.
    Returns (bias, conviction, key_level, note).
    """
    score = 0.0
    note_parts = []

    ma50  = compute_ma(prices_200, 50)
    ma200 = compute_ma(prices_200, 200)

    # 1. Moving average structure (strongest signal)
    if ma50 and ma200:
        above50  = spot > ma50
        above200 = spot > ma200
        if above50 and above200:
            score += 2.0
            note_parts.append(
                f"above 50d (${ma50:,.0f}) + 200d (${ma200:,.0f}) MA — bullish structure"
            )
        elif not above50 and not above200:
            score -= 2.0
            note_parts.append(
                f"below 50d (${ma50:,.0f}) + 200d (${ma200:,.0f}) MA — bearish structure"
            )
        elif above50 and not above200:
            score += 0.5
            note_parts.append(f"above 50d (${ma50:,.0f}) but below 200d — mixed")
        else:
            score -= 0.5
            note_parts.append(f"below 50d (${ma50:,.0f}) but above 200d — mixed")
    elif ma50:
        if spot > ma50:
            score += 1.0
            note_parts.append(f"above 50d MA (${ma50:,.0f})")
        else:
            score -= 1.0
            note_parts.append(f"below 50d MA (${ma50:,.0f})")

    # 2. Fear & Greed
    if fg_val is not None:
        if fg_val <= 20:
            # Extreme Fear — bearish momentum, but note contrarian potential
            score -= 0.5
            note_parts.append(f"F&G {fg_val} ({fg_cls}) — extreme panic, high bounce risk")
        elif fg_val <= 35:
            score -= 0.5
            note_parts.append(f"F&G {fg_val} ({fg_cls})")
        elif fg_val >= 80:
            score += 0.5
            note_parts.append(f"F&G {fg_val} ({fg_cls}) — euphoric, watch for exhaustion")
        elif fg_val >= 65:
            score += 0.5
            note_parts.append(f"F&G {fg_val} ({fg_cls})")
        else:
            note_parts.append(f"F&G {fg_val} ({fg_cls}) — neutral")

    # 3. ETF flow (weekday data only, may be 1-2 days stale on weekends)
    if etf_net_m is not None:
        if etf_net_m > 300:
            score += 1.5
            note_parts.append(f"ETF flow +${etf_net_m:.0f}M ({etf_date}) — strong inflow")
        elif etf_net_m > 100:
            score += 0.75
            note_parts.append(f"ETF flow +${etf_net_m:.0f}M ({etf_date}) — moderate inflow")
        elif etf_net_m < -300:
            score -= 1.5
            note_parts.append(f"ETF flow -${abs(etf_net_m):.0f}M ({etf_date}) — heavy outflow")
        elif etf_net_m < -100:
            score -= 0.75
            note_parts.append(f"ETF flow -${abs(etf_net_m):.0f}M ({etf_date}) — outflow")
        else:
            note_parts.append(f"ETF flow ~${etf_net_m:.0f}M ({etf_date}) — roughly neutral")
    else:
        note_parts.append("ETF flow: unavailable (check bitbo.io/treasuries/etf-flows)")

    # 4. Weekend / session modifier
    if is_weekend:
        note_parts.append(f"{day_name} — {liq}")
    else:
        note_parts.append(f"{session} — {liq}")

    # --- Derive raw bias ---
    if abs(score) < 0.75:
        bias = "WAIT"
        raw_conv = 1
    elif score > 0:
        bias = "UP"
        raw_conv = min(int(score + 0.5), 5)
    else:
        bias = "DOWN"
        raw_conv = min(int(abs(score) + 0.5), 5)

    # Weekend caps: never go above conviction 2, force WAIT if borderline
    if is_weekend:
        raw_conv = min(raw_conv, 2)
        if abs(score) < 1.5:   # weak signal + weekend → WAIT
            bias = "WAIT"
            raw_conv = 1

    conviction = max(1, min(raw_conv, 5))

    # Key level
    key = nearest_key_level(prices_7, spot)
    note_parts.append(f"key ${key:,}")

    note = "; ".join(note_parts) + "."
    return bias, conviction, key, note


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--dry-run", action="store_true",
                    help="Print thesis but do not write to daily_thesis.jsonl")
    args = ap.parse_args()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    is_weekend, day_name, session, liq = session_context()
    print(f"\n=== DAILY BTC RESEARCH  {today} ({day_name} · {session}) ===\n")

    # Spot price
    spot = btc_spot_from_scanner()
    change_24h = None
    if spot is None:
        spot, change_24h = btc_spot_coingecko()
    if spot is None:
        print("CRITICAL: BTC spot unavailable from scanner and CoinGecko. Aborting.")
        sys.exit(1)
    print(f"  BTC spot       : ${spot:,.0f}"
          + (f"  ({change_24h:+.2f}% 24h)" if change_24h is not None else ""))

    # Price history
    print("  Fetching 200-day price history...")
    prices_200 = price_history(200)
    prices_7   = price_history(7)
    ma50  = compute_ma(prices_200, 50)
    ma200 = compute_ma(prices_200, 200)
    if ma50:
        trend50 = "ABOVE" if spot > ma50 else "BELOW"
        print(f"  50d MA         : ${ma50:,.0f}  ({trend50})")
    else:
        print("  50d MA         : unavailable")
    if ma200:
        trend200 = "ABOVE" if spot > ma200 else "BELOW"
        print(f"  200d MA        : ${ma200:,.0f}  ({trend200})")
    else:
        print("  200d MA        : unavailable")

    # Fear & Greed
    fg_val, fg_cls = fear_greed_index()
    if fg_val is not None:
        print(f"  Fear & Greed   : {fg_val} / 100  ({fg_cls})")
    else:
        print("  Fear & Greed   : unavailable")

    # ETF flows
    print("  Fetching ETF flow data (bitbo.io)...")
    etf_date, etf_net_m = etf_net_flow_usd()
    if etf_date and etf_net_m is not None:
        sign = "+" if etf_net_m >= 0 else ""
        print(f"  ETF net flow   : {sign}${etf_net_m:.0f}M  (most recent: {etf_date})")
    elif is_weekend:
        print("  ETF flow       : weekend — no new creations/redemptions")
    else:
        print("  ETF flow       : unavailable")

    print()

    # Derive thesis
    bias, conviction, key, note = derive_thesis(
        spot, prices_200, prices_7, fg_val, fg_cls,
        etf_date, etf_net_m, is_weekend, day_name, session, liq,
    )

    print(f"  Bias           : {bias}")
    print(f"  Conviction     : {conviction}/5")
    print(f"  Key level      : ${key:,}")
    print(f"  Note           : {note}")
    print()

    if not args.dry_run:
        thesis_record(
            bias=bias,
            spot=spot,
            conviction=conviction,
            level=str(key),
            note=note,
            date=today,
        )
        print(f"Thesis recorded for {today}.")
    else:
        print("[dry-run] Thesis NOT recorded.")


if __name__ == "__main__":
    main()
