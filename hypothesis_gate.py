#!/usr/bin/env python3
"""Falsification harness — automates the KILLING of trading ideas, not the guessing.

You hand it an entry-rule hypothesis; it runs a fixed protocol and returns
SURVIVED or DIED with the evidence. Ideas still come from a human.

Why this exists, from both halves of the record:

  bot_tuner.py already tried automated SEARCH -- it swept knobs with
  itertools and suggested whichever combo placed top-3 on train and stayed
  profitable on validate. That is best-of-N selection: it manufactures
  winners. Its one real suggestion lost on 2/3 independent windows and it
  was disabled 2026-08-02.

  Against that, the 2026-08-04/10 sessions killed roughly two dozen ideas
  and passed none that survived execution -- but every validation was
  hand-written. The bottleneck was never generating ideas. It was the cost
  of a trustworthy kill.

The protocol is fixed and NOT adjustable per idea, because an adjustable
protocol is where fudging lives.

Deviation from the 2026-08-07 spec, on evidence gathered after it was written:
the spec priced entries at the resting bid. Everything measured since shows
maker fills are adversely selected -- settle_bot's filled markets won 46.9%
against 82.1% for the ones that never filled (t=-3.02). So this computes BOTH
bases honestly (maker: bid, zero fee; taker: ask, taker fee) and VERDICTS ON
THE TAKER number, which always fills. A maker-only pass is reported as
`fill_dependent` and DIES, because that is precisely the shape that has
already lost money here.

Usage:  python3 hypothesis_gate.py --list
        python3 hypothesis_gate.py --run "<name>" [--retest]
"""
import argparse
import datetime as dt
import hashlib
import inspect
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).parent
FEATURE_LOG = BASE / "data" / "whales" / "signal_feature_log.jsonl"
HYP_DIR = BASE / "data" / "hypotheses"
REGISTRY_PATH = HYP_DIR / "registry.jsonl"

# Quotes only exist in the feature log from this date; earlier rows replay to
# nothing and silently shrink the sample.
QUOTE_EPOCH = dt.datetime(2026, 6, 30, tzinfo=dt.timezone.utc).timestamp()
MIN_SAMPLES = 60
# Realistic delay between a signal appearing and an order reaching the book:
# the scanner polls every 5s (so ~2.5s just to see the tick), plus HTTP and
# placement. The momentum rule measured +0.0572 at the signal tick and
# +0.0231 by 10s -- an edge you cannot reach is not an edge.
ACTION_DELAY_S = 10.0
TARGET_ALPHA = 0.05

_REGISTERED: dict[str, "Hypothesis"] = {}


def taker_fee(price: float) -> float:
    """Kalshi taker fee. Maker is zero, confirmed from real fills 2026-08-03."""
    return 0.07 * price * (1.0 - price)


# ── declaring a hypothesis ────────────────────────────────────────────────

class Hypothesis:
    def __init__(self, fn, name, band, window):
        self.fn, self.name, self.band, self.window = fn, name, band, window
        self.hypothesis_name = name

    def __call__(self, row):
        return self.fn(row)


def hypothesis(name, band=None, window=(5.0, 11.0)):
    """Declare a hypothesis. `band` is REQUIRED and pre-registers what result
    would count as success -- you cannot see the number first and decide
    afterwards. `window` is the mins_left range entries may fire in."""
    if band is None or len(band) != 2 or band[0] >= band[1]:
        raise ValueError(
            f"hypothesis {name!r} must declare an acceptance band (lo, hi) "
            "BEFORE it is run — pre-registration is the point")

    def deco(fn):
        h = Hypothesis(fn, name, tuple(band), tuple(window))
        _REGISTERED[name] = h
        return h
    return deco


def predicate_hash(fn) -> str:
    """Content address of the RULE, so a disproof can be recognised again.

    Hashes the body only — decorators, comments, blank lines and the `def`
    signature are stripped. Renaming a function must not launder a rule past
    the already-DIED check, which is the whole reason this exists.
    """
    src = inspect.getsource(getattr(fn, "fn", fn))
    lines, seen_def = [], False
    for raw in src.splitlines():
        line = raw.strip()
        if not line or line.startswith(("@", "#")):
            continue
        if not seen_def:
            if line.startswith("def "):
                seen_def = True      # drop the signature itself
            continue
        lines.append(line)
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]


# ── the registry ──────────────────────────────────────────────────────────

class Registry:
    """Append-only record of every test ever run against this dataset."""

    def __init__(self, path=REGISTRY_PATH):
        self.path = Path(path)

    def _rows(self):
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue          # one bad line must not blind the registry
        return out

    def record(self, row: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = dict(row)
        row.setdefault("ts", dt.datetime.now(dt.timezone.utc).isoformat())
        with self.path.open("a") as f:
            f.write(json.dumps(row) + "\n")

    def comparisons(self) -> int:
        """How many tests this dataset has already seen. The significance bar
        rises with it -- after 25 tests a t of 2 is expected by chance."""
        return len(self._rows())

    def is_dead(self, phash: str) -> bool:
        return any(r.get("hash") == phash and r.get("verdict") == "DIED"
                   for r in self._rows())


def adjusted_bar(comparisons: int, alpha: float = TARGET_ALPHA) -> float:
    """Two-tailed Bonferroni |t| bar for `comparisons` tests at `alpha`."""
    n = max(int(comparisons), 1)
    return statistics.NormalDist().inv_cdf(1.0 - (alpha / 2.0) / n)


# ── the protocol ──────────────────────────────────────────────────────────

def _stats(values):
    n = len(values)
    if n < 2:
        return n, (values[0] if values else 0.0), 0.0
    m = sum(values) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in values) / n)
    return n, m, sd / math.sqrt(n)


def assess(samples, band, comparisons=1):
    """Run the fixed protocol over one-observation-per-market samples.

    Each sample: {ts, day, edge_taker, edge_maker}. Verdict is on the TAKER
    basis; a maker-only pass is flagged fill_dependent and still DIES.
    """
    failed = []
    samples = sorted(samples, key=lambda s: s["ts"])
    n = len(samples)
    if n < MIN_SAMPLES:
        return {"verdict": "DIED", "failed": ["samples"], "n": n,
                "edge": None, "t": None, "bar": adjusted_bar(comparisons),
                "windows_positive": 0, "fill_dependent": False,
                "latency_dependent": False,
                "detail": f"only {n} samples (need {MIN_SAMPLES})"}

    taker = [s["edge_taker"] for s in samples]
    instant = [s.get("edge_taker_instant", s["edge_taker"]) for s in samples]
    maker = [s["edge_maker"] for s in samples]
    _, edge, se = _stats(taker)
    _, edge_maker, _ = _stats(maker)
    # Degenerate input has no dispersion to estimate significance from, and
    # float error leaves se at ~1e-20 rather than exactly 0 -- truthy, so a
    # naive `if se` guard misses it and t explodes to ~1e16, reporting
    # SURVIVED with overwhelming confidence on constant data. Found by the
    # harness's own test suite; it is exactly the failure that would make
    # this tool dangerous rather than merely useless.
    degenerate = se < 1e-12
    t = 0.0 if degenerate else edge / se
    bar = adjusted_bar(comparisons)

    third = n // 3
    wins = []
    for a, b in ((0, third), (third, 2 * third), (2 * third, n)):
        chunk = taker[a:b]
        wins.append(sum(chunk) / len(chunk) if chunk else 0.0)
    windows_positive = sum(1 for w in wins if w > 0)

    by_day = defaultdict(list)
    for s in samples:
        by_day[s["day"]].append(s["edge_taker"])
    day_means = {d: sum(v) / len(v) for d, v in by_day.items()}
    best2 = sorted(by_day, key=lambda d: -sum(by_day[d]))[:2]
    rest = [x for d in by_day if d not in best2 for x in by_day[d]]
    drop2 = (sum(rest) / len(rest)) if rest else 0.0

    if degenerate:
        failed.append("degenerate")
    if windows_positive < 3:
        failed.append("windows")
    if t < bar:
        failed.append("t")
    if not (band[0] <= edge <= band[1]):
        failed.append("band")
    if drop2 <= 0:
        failed.append("drop2")

    n_i, edge_instant, se_i = _stats(instant)
    n_m, _em, se_m = _stats(maker)
    # A flag means "this clears ONLY under the optimistic assumption", not
    # merely "the number went negative". The momentum rule scored +0.0573
    # instant vs +0.0233 delayed -- both positive, but only the first clears,
    # and only the second is reachable. A sign test misses exactly that case.
    def _clears(mean, se):
        return se > 1e-12 and (mean / se) >= bar and mean > 0
    passes_taker = _clears(edge, se)
    latency_dependent = _clears(edge_instant, se_i) and not passes_taker
    fill_dependent = _clears(edge_maker, se_m) and not passes_taker
    return {"verdict": "SURVIVED" if not failed else "DIED",
            "failed": failed, "n": n,
            "edge": round(edge, 4), "edge_maker": round(edge_maker, 4),
            "se": round(se, 4), "t": round(t, 2), "bar": round(bar, 2),
            "windows": [round(w, 4) for w in wins],
            "windows_positive": windows_positive,
            "days_positive": sum(1 for m in day_means.values() if m > 0),
            "days_total": len(day_means), "drop2": round(drop2, 4),
            "edge_instant": round(edge_instant, 4),
            "fill_dependent": bool(fill_dependent),
            "latency_dependent": bool(latency_dependent)}


# ── turning the feature log into samples ──────────────────────────────────

def load_markets(log_path=FEATURE_LOG):
    """{ticker: [rows sorted by ts]} for BTC15M rows with usable quotes."""
    by = defaultdict(list)
    with open(log_path) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue          # the log genuinely contains torn lines
            if "BTC15M" not in (r.get("ticker") or ""):
                continue
            if (r.get("ts") or 0) < QUOTE_EPOCH or r.get("yes_ask") is None:
                continue
            by[r["ticker"]].append(r)
    for rows in by.values():
        rows.sort(key=lambda r: r["ts"])
    return by


def sample(h: Hypothesis, markets):
    """One observation per market: the FIRST tick where the rule fires inside
    the window. Multiple ticks from one market are not independent."""
    lo, hi = h.window
    out = []
    for ticker, rows in markets.items():
        settled = "YES" if (rows[-1].get("distance") or 0) > 0 else "NO"
        for i, r in enumerate(rows):
            m = r.get("mins_left")
            if m is None or not (lo <= m <= hi):
                continue
            side = h(r)
            if side not in ("YES", "NO"):
                continue
            key = "yes_ask" if side == "YES" else "no_ask"
            ask_now = r.get(key)
            if ask_now is None:
                break
            # The SIDE is decided here, but the order reaches the book
            # ACTION_DELAY_S later and pays whatever the price is by then.
            ent = next((x for x in rows[i:]
                        if x["ts"] >= r["ts"] + ACTION_DELAY_S), None)
            if ent is None or ent.get(key) is None:
                break            # market ended before we could have acted
            ask = ent[key]
            spread = max(0.0, r.get("spread") or 0.0)
            bid = max(0.01, round(ask_now - spread, 4))
            win = 1.0 if side == settled else 0.0
            out.append({
                "ts": r["ts"],
                "day": dt.datetime.utcfromtimestamp(r["ts"]).strftime("%Y-%m-%d"),
                "edge_taker": win - ask - taker_fee(ask),            # reachable
                "edge_taker_instant": win - ask_now - taker_fee(ask_now),
                "edge_maker": win - bid,                             # assumes a fill
            })
            break
    return out


# ── CLI ───────────────────────────────────────────────────────────────────

def run(h: Hypothesis, markets, registry: Registry, retest=False):
    phash = predicate_hash(h)
    if registry.is_dead(phash) and not retest:
        raise SystemExit(
            f"REFUSED: {h.name!r} (hash {phash}) is already recorded DIED. "
            "Re-deriving disproofs is how time gets wasted here — pass "
            "--retest if you genuinely have new data.")
    comparisons = registry.comparisons() + 1
    result = assess(sample(h, markets), h.band, comparisons)
    result.update({"name": h.name, "hash": phash, "band": list(h.band),
                   "window": list(h.window), "comparisons": comparisons})
    registry.record(result)
    return result


def report(result) -> str:
    r = result
    lines = [f"# {r['name']}", "",
             f"**{r['verdict']}**" + (f" — failed: {', '.join(r['failed'])}"
                                      if r["failed"] else ""), ""]
    if r.get("edge") is None:
        lines.append(r.get("detail", ""))
        return "\n".join(lines)
    lines += [
        f"- n = {r['n']} markets (one observation each)",
        f"- taker edge **{r['edge']:+.4f}** ± {r['se']:.4f}  "
        f"(t = {r['t']:+.2f}, bar = {r['bar']:.2f} after {r['comparisons']} comparisons)",
        f"- maker edge {r['edge_maker']:+.4f}"
        + ("  ⚠️ **fill-dependent** — clears only if resting orders fill, which"
           " is exactly what killed settle_bot" if r["fill_dependent"] else ""),
        f"- windows {r['windows']} → {r['windows_positive']}/3 positive",
        f"- days {r['days_positive']}/{r['days_total']} positive; "
        f"dropping the two best leaves {r['drop2']:+.4f}",
        f"- declared band {r['band']}",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--run", metavar="NAME")
    ap.add_argument("--retest", action="store_true")
    args = ap.parse_args()

    # Consult the MODULE object, not this file's globals: run as a script
    # these are two different namespaces (see the test of the same name), and
    # reading globals() here makes --list silently print nothing.
    import hypothesis_gate as hg
    import hypotheses  # noqa: F401 — importing registers the declared rules
    registered = hg._REGISTERED

    if args.list or not args.run:
        reg = Registry()
        print(f"{reg.comparisons()} comparisons recorded; "
              f"bar for the next is |t| >= {adjusted_bar(reg.comparisons()+1):.2f}\n")
        for name, h in registered.items():
            print(f"  {name}\n      band={h.band} window={h.window}")
        return

    h = registered.get(args.run)
    if h is None:
        raise SystemExit(f"no hypothesis named {args.run!r} — try --list")
    markets = load_markets()
    result = run(h, markets, Registry(), retest=args.retest)
    text = report(result)
    print(text)
    HYP_DIR.mkdir(parents=True, exist_ok=True)
    out = HYP_DIR / f"report-{dt.date.today()}.md"
    with out.open("a") as f:
        f.write(text + "\n\n")


if __name__ == "__main__":
    main()
