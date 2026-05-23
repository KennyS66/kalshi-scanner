"""Outcome-based calibration of BTC pick weights.

Reads picks_log.jsonl (written by sink.write_btc_picks), looks up the
realised outcome for each pick on Kalshi, then fits logistic regression
on (base_prob, flow_prob) → realised(YES) and writes the resulting
weights to calibration.json. sink.py picks the file up on the next
write.

The model is intentionally tiny: two features plus a bias. With a few
hundred settled picks we have plenty of data to identify two weights;
anything richer would overfit.
"""
from __future__ import annotations

import contextlib
import json
import math
import time
import urllib.request as ur
from pathlib import Path

_DATA_DIR = Path("data/whales")
_PICKS_LOG = _DATA_DIR / "picks_log.jsonl"
_OUTCOMES_FILE = _DATA_DIR / "picks_outcomes.json"
_CALIBRATION_FILE = _DATA_DIR / "calibration.json"

# Need at least this many settled picks before we trust fitted weights
_MIN_SETTLED = 30


def _read_picks() -> list[dict]:
    if not _PICKS_LOG.exists():
        return []
    rows = []
    with _PICKS_LOG.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            with contextlib.suppress(Exception):
                rows.append(json.loads(line))
    return rows


def _read_outcomes() -> dict:
    if not _OUTCOMES_FILE.exists():
        return {}
    with contextlib.suppress(Exception):
        return json.loads(_OUTCOMES_FILE.read_text())
    return {}


def _write_outcomes(outcomes: dict) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _OUTCOMES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(outcomes))
    tmp.replace(_OUTCOMES_FILE)


def _fetch_outcome(ticker: str) -> str | None:
    """Return 'YES'/'NO' if ticker has finalised on Kalshi, else None."""
    url = f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}"
    req = ur.Request(url, headers={"User-Agent": "kalshi-scanner/1.0"})
    try:
        with ur.urlopen(req, timeout=4) as r:
            mkt = json.loads(r.read()).get("market", {})
    except Exception:
        return None
    if mkt.get("status") != "finalized":
        return None
    result = (mkt.get("result") or "").upper()
    return result if result in ("YES", "NO") else None


def refresh_outcomes(max_lookups: int = 60) -> dict:
    """Look up Kalshi outcomes for any pick we haven't resolved yet.

    Caps work per call so we don't hammer Kalshi after a long downtime.
    Skips tickers whose mins_left at pick time was so high they probably
    aren't settled yet.
    """
    picks = _read_picks()
    outcomes = _read_outcomes()
    now = time.time()
    pending: list[str] = []
    seen: set[str] = set()
    for p in picks:
        t = p.get("ticker")
        if not t or t in outcomes or t in seen:
            continue
        seen.add(t)
        ts = p.get("ts", 0)
        mins_left = p.get("mins_left") or 0
        # Only bother once we're past the pick's likely expiry
        if (now - ts) / 60 < mins_left + 1:
            continue
        pending.append(t)
    for t in pending[:max_lookups]:
        result = _fetch_outcome(t)
        if result is not None:
            outcomes[t] = result
    _write_outcomes(outcomes)
    return outcomes


def _fit_logreg(features: list[list[float]], labels: list[int],
                lr: float = 0.05, epochs: int = 400) -> tuple[list[float], float]:
    """Tiny logistic regression. Returns (weights, bias)."""
    if not features:
        return [0.0] * (len(features[0]) if features else 0), 0.0
    n_feats = len(features[0])
    w = [0.0] * n_feats
    b = 0.0
    n = len(features)
    for _ in range(epochs):
        grad_w = [0.0] * n_feats
        grad_b = 0.0
        for x, y in zip(features, labels):
            z = b + sum(wi * xi for wi, xi in zip(w, x))
            p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
            err = p - y
            for i, xi in enumerate(x):
                grad_w[i] += err * xi
            grad_b += err
        for i in range(n_feats):
            w[i] -= lr * grad_w[i] / n
        b -= lr * grad_b / n
    return w, b


def fit() -> dict | None:
    """Fit calibration weights from accumulated outcomes. Writes calibration.json.

    Returns the fitted dict (also written to disk), or None if not enough data.
    """
    outcomes = refresh_outcomes()
    if not outcomes:
        return None

    picks = _read_picks()
    # Keep only the latest pick per ticker so we don't train on duplicates
    by_ticker: dict[str, dict] = {}
    for p in picks:
        t = p.get("ticker")
        if t and t in outcomes:
            by_ticker[t] = p

    rows = []
    for t, pick in by_ticker.items():
        bp = pick.get("base_prob")
        fp = pick.get("flow_prob")
        if bp is None and fp is None:
            continue
        y = 1 if outcomes[t] == "YES" else 0
        rows.append((bp if bp is not None else 0.5,
                     fp if fp is not None else 0.5,
                     y))

    if len(rows) < _MIN_SETTLED:
        return None

    features = [[r[0], r[1]] for r in rows]
    labels = [r[2] for r in rows]
    w, b = _fit_logreg(features, labels)

    # Translate logistic weights → blend weights in [0,1] for sink.py
    # Just normalise the absolute weights; bias rolls into the implicit prior.
    abs_sum = abs(w[0]) + abs(w[1]) or 1.0
    base_weight = abs(w[0]) / abs_sum
    flow_weight = abs(w[1]) / abs_sum

    # Hit rate over the training set (sanity check; written for the user)
    hits = 0
    for (bp, fp, y) in rows:
        z = b + w[0] * bp + w[1] * fp
        p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
        pred = 1 if p >= 0.5 else 0
        hits += int(pred == y)

    out = {
        "base_weight": round(base_weight, 4),
        "flow_weight": round(flow_weight, 4),
        "logreg_w": [round(wi, 4) for wi in w],
        "logreg_b": round(b, 4),
        "settled_picks": len(rows),
        "in_sample_accuracy": round(hits / len(rows), 4),
        "fitted_at": int(time.time()),
    }
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _CALIBRATION_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=2))
    tmp.replace(_CALIBRATION_FILE)
    return out


if __name__ == "__main__":
    result = fit()
    if result is None:
        print("Not enough settled picks yet — collect more data and retry.")
    else:
        print(json.dumps(result, indent=2))
