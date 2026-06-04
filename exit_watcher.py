#!/usr/bin/env python3
"""
Signal watcher — polls /api/crypto/signal every 5s.
Writes alerts to data/ when key conditions fire:

  ENTRY alerts (data/entry_alert.txt):
    - Flush bounce: flush_score>=90, sig_whale>0, buy_pressure>30000, price $0.25-$0.40
    - Strong NO setup: sig_combined<=-20 AND whale_trend<0 AND distance<$20

  EXIT alert (data/exit_alert.txt):
    - Was in flush bounce AND whale_trend flips positive->negative

Run in background: python3 -u exit_watcher.py &
"""
import time
import json
import urllib.request
from pathlib import Path

API_URL      = "http://localhost:9050/api/crypto/signal"
EXIT_FILE    = Path(__file__).parent / "data" / "exit_alert.txt"
ENTRY_FILE   = Path(__file__).parent / "data" / "entry_alert.txt"
PREWARN_FILE = Path(__file__).parent / "data" / "prewarn_alert.txt"
POLL_INTERVAL = 5  # seconds

def fetch_signal():
    try:
        with urllib.request.urlopen(API_URL, timeout=4) as r:
            return json.loads(r.read())
    except Exception:
        return None

def write_alert(path: Path, msg: str):
    path.write_text(msg)
    print(f"  🚨 ALERT [{path.name}]: {msg}", flush=True)

def main():
    print("Signal watcher started — polling every 5s", flush=True)
    EXIT_FILE.parent.mkdir(parents=True, exist_ok=True)

    prev_whale_trend    = None
    prev_sig_combined   = None
    prev_ticker         = None
    in_flush_bounce     = False
    flush_entry_fired   = False
    no_entry_fired      = False
    no_prewarn_fired    = False
    flush_prewarn_fired = False

    while True:
        sig = fetch_signal()
        if sig is None:
            time.sleep(POLL_INTERVAL)
            continue

        ticker        = sig.get("ticker")
        whale_trend   = sig.get("whale_trend", 0)
        flush_score   = sig.get("flush_score", 0)
        sig_whale     = sig.get("sig_whale", 0)
        sig_combined  = sig.get("sig_combined", 0)
        buy_pressure  = sig.get("buy_pressure", 0)
        price         = sig.get("price", 0)
        mins_left     = sig.get("mins_left", 99)
        distance      = sig.get("distance", 0)
        whale_count   = sig.get("whale_count", 0)

        # ── Reset state on new cycle ──────────────────────────────────────────
        if ticker != prev_ticker:
            prev_whale_trend  = None
            prev_sig_combined = None
            in_flush_bounce     = False
            flush_entry_fired   = False
            no_entry_fired      = False
            no_prewarn_fired    = False
            flush_prewarn_fired = False
            prev_ticker         = ticker
            print(f"New cycle: {ticker} | strike_dist={distance or 0:.1f} | price={price or 0}", flush=True)

        # ── PRE-WARN: NO setup building ──────────────────────────────────────
        # Fires ~30-60s before full NO_ENTRY — get ready to click
        if (sig_combined <= -10
                and whale_trend < 0
                and distance <= 30
                and whale_count >= 30
                and not no_prewarn_fired
                and not no_entry_fired
                and mins_left > 4):
            no_prewarn_fired = True
            write_alert(PREWARN_FILE,
                f"NO_PREWARN|{ticker}|price={price}|sig_combined={sig_combined}|"
                f"whale_trend={whale_trend}|distance={distance}|"
                f"mins_left={mins_left}|ts={time.time():.0f}")

        # ── PRE-WARN: Flush bounce building ──────────────────────────────────
        # Fires when flush is building toward 90 threshold
        if (flush_score >= 50
                and sig_whale > 0
                and buy_pressure >= 15000
                and price <= 0.55
                and not flush_prewarn_fired
                and not flush_entry_fired):
            flush_prewarn_fired = True
            write_alert(PREWARN_FILE,
                f"FLUSH_PREWARN|{ticker}|price={price}|flush={flush_score}|"
                f"sig_whale={sig_whale}|buy_pressure={buy_pressure}|"
                f"mins_left={mins_left}|ts={time.time():.0f}")

        # ── ENTRY: Flush bounce ───────────────────────────────────────────────
        # Conditions: flush>=90, whale positive, big buy pressure, price in range
        if (flush_score >= 90
                and sig_whale > 0
                and buy_pressure >= 30000
                and 0.20 <= price <= 0.45
                and not flush_entry_fired):
            in_flush_bounce   = True
            flush_entry_fired = True
            write_alert(ENTRY_FILE,
                f"FLUSH_ENTRY|{ticker}|price={price}|flush={flush_score}|"
                f"sig_whale={sig_whale}|buy_pressure={buy_pressure}|"
                f"whale_trend={whale_trend}|distance={distance}|"
                f"mins_left={mins_left}|ts={time.time():.0f}")

        # ── ENTRY: Strong NO setup ────────────────────────────────────────────
        # Time-aware distance guard:
        #   Before 18:00 UTC: BTC must be $30+ BELOW floor (avoids floor-touch bounces)
        #   After  18:00 UTC: BTC must be $60+ BELOW floor (late-session bounce defense)
        import datetime as _dt
        _hour = _dt.datetime.utcnow().hour
        _dist_limit = -60 if _hour >= 18 else -30
        if (sig_combined <= -20
                and whale_trend < 0
                and distance <= _dist_limit
                and whale_count >= 50
                and not no_entry_fired
                and mins_left > 3):
            no_entry_fired = True
            write_alert(ENTRY_FILE,
                f"NO_ENTRY|{ticker}|price={price}|sig_combined={sig_combined}|"
                f"whale_trend={whale_trend}|distance={distance}|"
                f"mins_left={mins_left}|ts={time.time():.0f}")

        # ── EXIT: Flush bounce whale_trend flip ───────────────────────────────
        if (in_flush_bounce
                and prev_whale_trend is not None
                and prev_whale_trend >= 0
                and whale_trend < 0):
            write_alert(EXIT_FILE,
                f"EXIT|{ticker}|price={price}|whale_trend={whale_trend}|"
                f"prev={prev_whale_trend}|distance={distance}|"
                f"mins_left={mins_left}|ts={time.time():.0f}")

        # ── EXIT: sig_combined flips NO during any active trade ───────────────
        # Catches non-flush reversals: was firmly YES, now firmly NO
        if (prev_sig_combined is not None
                and prev_sig_combined >= 20
                and sig_combined <= -10
                and mins_left < 8):
            write_alert(EXIT_FILE,
                f"EXIT_FLIP|{ticker}|price={price}|sig_combined={sig_combined}|"
                f"prev_combined={prev_sig_combined}|whale_trend={whale_trend}|"
                f"mins_left={mins_left}|ts={time.time():.0f}")

        prev_whale_trend  = whale_trend
        prev_sig_combined = sig_combined

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
