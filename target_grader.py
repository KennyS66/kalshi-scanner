#!/usr/bin/env python3
"""Banner target grader.

Watches 15m BTC markets' lifecycles, applies tradeability filters, tracks
whether the buy range was actually touched (entry was possible), grades
sell-low and sell-high hits, and tunes per-side calibration offsets.

Filters: a market is only graded if it opens tradeable (price between
0.05-0.95, mins_left >= 2) AND price actually entered the buy range at
some point. Untouched markets are recorded for reference but excluded
from win-rate math (you couldn't have entered them).

Targets:
  sell-low hit rate goal:  90%  (the WIN line)
  sell-high hit rate goal: 60%  (stretch)

Adjustments capped at +/- 2.5 cents per cycle per offset.

Writes:
  data/whales/banner_targets.jsonl  - one record per settled market
  data/whales/banner_offsets.json   - per-side calibration state

Reads:
  http://localhost:9050/api/crypto/signal
"""
import json
import time
import urllib.request
from pathlib import Path

from bot_core import compute_side_ranges

DATA = Path(__file__).parent / "data" / "whales"
DATA.mkdir(parents=True, exist_ok=True)
JOURNAL = DATA / "banner_targets.jsonl"
OFFSETS = DATA / "banner_offsets.json"
CURRENT = DATA / "banner_current.json"

LOW_TARGET = 0.90
HIGH_TARGET = 0.60
LOW_LOOSEN = 0.95
HIGH_LOOSEN = 0.75
ADJUST_STEP_C = 2.5
MAX_OFFSET_C = 8.0   # sell_low floor is buy_high+2 min, so offset can't exceed ~8 meaningfully
WINDOW = 10
MIN_SAMPLES = 5
POLL_SEC = 30

PRICE_DECIDED_LOW = 0.05
PRICE_DECIDED_HIGH = 0.95
MIN_MINS_LEFT = 2.0

_DEFAULT_SIDE = {
    "sell_low_offset_c": 0.0,
    "sell_high_offset_c": 0.0,
    "low_hit_rate": None,
    "high_hit_rate": None,
    "buy_touch_rate": None,
    "n": 0,
}


def fetch():
    try:
        with urllib.request.urlopen(
            "http://localhost:9050/api/crypto/signal", timeout=25
        ) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"fetch error: {e}", flush=True)
        return None


def load_offsets():
    if OFFSETS.exists():
        try:
            d = json.loads(OFFSETS.read_text())
            if "yes" in d and "no" in d:
                return d
        except Exception:
            pass
    return {"yes": dict(_DEFAULT_SIDE), "no": dict(_DEFAULT_SIDE)}


def save_offsets(o):
    OFFSETS.write_text(json.dumps(o, indent=2))


def compute_ranges(s, side_offsets):
    # Single source of truth for the range math: bot_core.compute_side_ranges
    # (the swing bot gates its entries/exits on the same formula).
    is_up = s["direction"] == "YES"
    r = compute_side_ranges(s["price"], s["yes_pct"],
                            "YES" if is_up else "NO", side_offsets)
    return is_up, r["buy_low"], r["buy_high"], r["sell_low"], r["sell_high"]


def is_tradeable(s):
    if s.get("price") is None or s.get("mins_left") is None:
        return False
    if s["price"] < PRICE_DECIDED_LOW or s["price"] > PRICE_DECIDED_HIGH:
        return False
    if s["mins_left"] < MIN_MINS_LEFT:
        return False
    return True


def _adjust(offset, hit_rate, target_hit, loosen_hit):
    if hit_rate is None:
        return offset
    if hit_rate < target_hit:
        return min(offset + ADJUST_STEP_C, MAX_OFFSET_C)
    if hit_rate >= loosen_hit:
        return max(offset - ADJUST_STEP_C, 0.0)
    return offset


def update_calibration():
    if not JOURNAL.exists():
        return
    rows = [json.loads(l) for l in JOURNAL.read_text().splitlines() if l.strip()]
    o = load_offsets()
    for side in ("YES", "NO"):
        key = side.lower()
        side_all = [r for r in rows if r.get("side") == side][-WINDOW:]
        graded = [r for r in side_all if r.get("buy_touched")]
        cur = o.get(key, dict(_DEFAULT_SIDE))
        if side_all:
            touches = sum(1 for r in side_all if r.get("buy_touched"))
            cur["buy_touch_rate"] = round(touches / len(side_all), 3)
        if len(graded) >= MIN_SAMPLES:
            low_hits = sum(1 for r in graded if r.get("low_hit"))
            high_hits = sum(1 for r in graded if r.get("high_hit"))
            low_rate = low_hits / len(graded)
            high_rate = high_hits / len(graded)
            cur["low_hit_rate"] = round(low_rate, 3)
            cur["high_hit_rate"] = round(high_rate, 3)
            cur["n"] = len(graded)
            cur["sell_low_offset_c"] = _adjust(
                cur.get("sell_low_offset_c", 0.0), low_rate, LOW_TARGET, LOW_LOOSEN
            )
            cur["sell_high_offset_c"] = _adjust(
                cur.get("sell_high_offset_c", 0.0), high_rate, HIGH_TARGET, HIGH_LOOSEN
            )
        o[key] = cur
    save_offsets(o)


SNAP_MIN_DELTA_C = 2.0   # only create new snapshot if range moved >= 2c on any edge
MAX_SNAPS_PER_MARKET = 12  # cap to avoid clutter


def write_current(snaps, mins_left):
    """Write all active snapshots so the GUI can show them as live pending rows."""
    if not snaps:
        CURRENT.write_text("null")
        return
    out = []
    for sn in snaps:
        out.append({
            **sn,
            "low_hit_so_far": sn["max_buy_c"] >= sn["sell_low"],
            "high_hit_so_far": sn["max_buy_c"] >= sn["sell_high"],
            "mins_left": mins_left,
            "settled": False,
        })
    CURRENT.write_text(json.dumps(out))


def make_snapshot(s, side_offsets, snap_idx):
    """Build a fresh snapshot from the current signal + offsets."""
    is_up, bl, bh, sl, sh = compute_ranges(s, side_offsets)
    buy_c = (s["price"] if is_up else 1 - s["price"]) * 100
    return {
        "ticker": s["ticker"],
        "snap_idx": snap_idx,
        "side": "YES" if is_up else "NO",
        "buy_low": bl,
        "buy_high": bh,
        "sell_low": sl,
        "sell_high": sh,
        "entry_price_c": buy_c,
        "max_buy_c": buy_c,
        "buy_touched": bl <= buy_c <= bh,
        "opened_ts": time.time(),
    }


def ranges_changed(prev, bl, bh, sl, sh, side):
    """True if the new range differs from the previous snapshot's by >= SNAP_MIN_DELTA_C
    on any edge, or if the side flipped."""
    if prev["side"] != side:
        return True
    return (
        abs(prev["buy_low"] - bl) >= SNAP_MIN_DELTA_C
        or abs(prev["buy_high"] - bh) >= SNAP_MIN_DELTA_C
        or abs(prev["sell_low"] - sl) >= SNAP_MIN_DELTA_C
        or abs(prev["sell_high"] - sh) >= SNAP_MIN_DELTA_C
    )


def settle_snaps(snaps):
    """Settle every snapshot for a finished market — append each to journal."""
    if not snaps:
        return
    with JOURNAL.open("a") as f:
        for sn in snaps:
            rec = {
                **sn,
                "low_hit": sn["max_buy_c"] >= sn["sell_low"],
                "high_hit": sn["max_buy_c"] >= sn["sell_high"],
                "settled_ts": time.time(),
            }
            f.write(json.dumps(rec) + "\n")
    update_calibration()
    wins = sum(1 for sn in snaps if sn["buy_touched"] and sn["max_buy_c"] >= sn["sell_low"])
    print(
        f"graded {snaps[0]['ticker']}: {len(snaps)} snapshot(s), {wins} win(s)",
        flush=True,
    )


def main():
    print(f"target_grader: writing {JOURNAL}, {OFFSETS}, {CURRENT}", flush=True)
    snaps = []         # active snapshots for the current ticker
    cur_ticker = None
    while True:
        s = fetch()
        if not (s and s.get("status") == "ok"):
            write_current([], None)
            time.sleep(POLL_SEC)
            continue
        t = s["ticker"]
        if cur_ticker != t:
            # Settle the prior market's snapshots, reset for the new one
            settle_snaps(snaps)
            snaps = []
            cur_ticker = t
            if not is_tradeable(s):
                print(
                    f"skip {t}: not tradeable "
                    f"(price={s.get('price')}, mins_left={s.get('mins_left')})",
                    flush=True,
                )
                write_current([], None)
                time.sleep(POLL_SEC)
                continue
            offsets = load_offsets()
            side_key = "yes" if s["direction"] == "YES" else "no"
            side_offsets = offsets.get(side_key, dict(_DEFAULT_SIDE))
            snap = make_snapshot(s, side_offsets, 0)
            snaps.append(snap)
            print(
                f"watching {t}: snap#0 {snap['side']} "
                f"buy [{snap['buy_low']:.1f}-{snap['buy_high']:.1f}] "
                f"sell [{snap['sell_low']:.1f}-{snap['sell_high']:.1f}] "
                f"entry={snap['entry_price_c']:.1f}c"
                f"{' (in buy zone)' if snap['buy_touched'] else ''}",
                flush=True,
            )
        else:
            # Same ticker — update every active snapshot's live state and maybe add a new one
            offsets = load_offsets()
            side_key = "yes" if s["direction"] == "YES" else "no"
            side_offsets = offsets.get(side_key, dict(_DEFAULT_SIDE))
            is_up, bl, bh, sl, sh = compute_ranges(s, side_offsets)
            buy_c = (s["price"] if is_up else 1 - s["price"]) * 100
            for sn in snaps:
                is_up_sn = sn["side"] == "YES"
                buy_c_sn = (s["price"] if is_up_sn else 1 - s["price"]) * 100
                if buy_c_sn > sn["max_buy_c"]:
                    sn["max_buy_c"] = buy_c_sn
                if not sn["buy_touched"] and sn["buy_low"] <= buy_c_sn <= sn["buy_high"]:
                    sn["buy_touched"] = True
            side = "YES" if is_up else "NO"
            if (
                len(snaps) < MAX_SNAPS_PER_MARKET
                and (not snaps or ranges_changed(snaps[-1], bl, bh, sl, sh, side))
            ):
                snap = make_snapshot(s, side_offsets, len(snaps))
                snaps.append(snap)
                print(
                    f"new snap#{snap['snap_idx']} on {t}: {snap['side']} "
                    f"buy [{snap['buy_low']:.1f}-{snap['buy_high']:.1f}] "
                    f"sell [{snap['sell_low']:.1f}-{snap['sell_high']:.1f}]",
                    flush=True,
                )
        write_current(snaps, s.get("mins_left"))
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
