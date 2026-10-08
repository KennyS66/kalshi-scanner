import json
import math

import scorecard as sc


def row(ticker, ts, mins_left, direction, yes_ask, no_ask):
    return {"ticker": ticker, "ts": ts, "mins_left": mins_left,
            "direction": direction, "yes_ask": yes_ask, "no_ask": no_ask,
            "status": "ok"}


def test_taker_fee_rounds_up_to_the_cent():
    assert sc.taker_fee(0.50) == 0.02          # 0.07*.25 = 0.0175 -> 2c
    assert sc.taker_fee(0.10) == 0.01          # 0.0063 -> 1c
    assert sc.taker_fee(0.0) == 0.0


def test_one_sample_per_market_at_decision_point():
    by = {
        # first row at/below 10 min left is the decision: YES @ 0.40, wins
        "A": [row("A", 100, 12.0, "NO", .6, .41), row("A", 200, 9.8, "YES", .40, .61),
              row("A", 300, 9.0, "NO", .7, .31)],
        # NO @ 0.30, loses
        "B": [row("B", 1000, 9.5, "NO", .71, .30)],
        # never reached the window -> skipped
        "C": [row("C", 50, 13.0, "YES", .5, .51)],
        # decided price -> skipped
        "D": [row("D", 60, 9.0, "YES", .99, .02)],
    }
    res = {"A": "YES", "B": "YES", "C": "NO", "D": "YES"}
    s = sc.samples(by, res)
    assert [x["ticker"] for x in s] == ["A", "B"]
    a, b = s
    assert a["pnl"] == round(1 - 0.40 - sc.taker_fee(0.40), 4)
    assert b["pnl"] == round(0 - 0.30 - sc.taker_fee(0.30), 4)


def test_summarise_windows():
    now = 30 * 86400.0
    s = [{"ts": now - 86400, "pnl": 0.10, "won": True, "px": 0.5},
         {"ts": now - 2 * 86400, "pnl": -0.20, "won": False, "px": 0.5},
         {"ts": now - 20 * 86400, "pnl": 0.40, "won": True, "px": 0.5}]
    out = sc.summarise(s, now)
    w7, w30 = out["7d"], out["30d"]
    assert w7["n"] == 2 and w7["cents"] == -5.0 and w7["right_pct"] == 50.0
    assert w30["n"] == 3 and w30["cents"] == round(100 * 0.3 / 3, 2)
    assert w7["t"] is not None and math.isfinite(w7["t"])
    assert sc.summarise([], now)["7d"] == {"n": 0, "cents": None, "t": None,
                                           "right_pct": None, "avg_price_c": None}


def test_recent_rows_binary_searches_chronological_log(tmp_path):
    p = tmp_path / "log.jsonl"
    with open(p, "w") as f:
        for i in range(2000):
            f.write(json.dumps({"ticker": f"KXBTC15M-{i // 100}", "ts": float(i)}) + "\n")
        f.write("{torn line\n")
    rows = sc.recent_rows(p, since=1500.0)
    assert min(r["ts"] for r in rows) >= 1500.0
    assert len(rows) == 500
