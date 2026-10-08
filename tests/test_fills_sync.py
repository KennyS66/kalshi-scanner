import json

from fills_sync import (normalize_fill, merge_fills, tag_fills,
                        grade_markets, session_report)


def _api_fill(**over):
    """Shape verified against the live API 2026-08-03: dollar-string
    prices, fractional count_fp, itemized fee_cost, epoch ts."""
    base = {"trade_id": "t1", "order_id": "o1", "ticker": "KXBTC15M-X-10",
            "side": "yes", "action": "buy", "count_fp": "2",
            "yes_price_dollars": "0.4300", "no_price_dollars": "0.5700",
            "fee_cost": "0.05", "is_taker": True, "ts": 1785726900,
            "created_time": "2026-08-03T03:15:00Z"}
    base.update(over)
    return base


def test_normalize_fill_uses_live_api_schema():
    row = normalize_fill(_api_fill())
    assert row["id"] == "t1" and row["side"] == "YES" and row["action"] == "buy"
    assert row["qty"] == 2.0 and row["price"] == 0.43      # yes side, dollars
    assert row["fee"] == 0.05
    assert row["ts"] == 1785726900.0
    no = normalize_fill(_api_fill(side="no", trade_id="t2", count_fp="8.05"))
    assert no["price"] == 0.57 and no["qty"] == 8.05       # fractional contracts


def test_normalize_fill_falls_back_to_cents_schema():
    row = normalize_fill({"trade_id": "t3", "ticker": "T", "side": "yes",
                          "action": "buy", "count": 3, "yes_price": 41,
                          "no_price": 59, "is_taker": False,
                          "created_time": "2026-08-03T03:15:00Z"})
    assert row["qty"] == 3.0 and row["price"] == 0.41 and row["fee"] == 0.0
    assert row["ts"] == 1785726900.0                       # parsed created_time


def test_merge_fills_dedupes_by_id_and_sorts_by_ts():
    old = [{"id": "a", "ts": 100.0}, {"id": "b", "ts": 200.0}]
    new = [{"id": "b", "ts": 200.0}, {"id": "c", "ts": 50.0}]
    merged = merge_fills(old, new)
    assert [r["id"] for r in merged] == ["c", "a", "b"]


def test_tag_fills_matches_bot_signals_by_ticker_side_and_time():
    fills = [
        {"id": "1", "ticker": "T1", "side": "YES", "ts": 1000.0},   # signal 20s before
        {"id": "2", "ticker": "T1", "side": "NO", "ts": 1000.0},    # wrong side
        {"id": "3", "ticker": "T2", "side": "YES", "ts": 1000.0},   # no signal
        {"id": "4", "ticker": "T1", "side": "YES", "ts": 5000.0},   # signal too old
    ]
    signals = [{"ts": 980.0, "ticker": "T1", "side": "YES"}]
    tagged = tag_fills(fills, signals, window_s=180)
    assert [r["source"] for r in tagged] == ["bot", "manual", "manual", "manual"]


def _f(id, ticker, side, action, qty, price, ts, fee=0.0):
    return {"id": id, "ticker": ticker, "side": side, "action": action,
            "qty": qty, "price": price, "ts": ts, "fee": fee,
            "source": "manual"}


def test_grade_markets_reports_lean_and_settlement_not_pnl():
    """P&L is deliberately NOT derived from fills: Kalshi reports each
    fill from both book sides and Kenny flips sides mid-market, so
    reconstruction double counts (an earlier version scored a ~$30
    account at +$408 lifetime). What IS authoritative: cash legs,
    itemized fees, Kalshi's settlement revenue, and the result."""
    fills = [
        _f("1", "M1", "YES", "buy", 2, 0.40, 100.0, fee=0.05),
        _f("3", "M1", "YES", "sell", 1, 0.55, 150.0),
        _f("2", "M2", "NO", "buy", 1, 0.70, 200.0),
    ]
    graded = grade_markets(fills, {"M1": {"result": "yes", "revenue": 1.0},
                                   "M2": {"result": "yes", "revenue": 0.0}})
    m1 = next(g for g in graded if g["ticker"] == "M1")
    assert m1["cost"] == 0.80 and m1["proceeds"] == 0.55 and m1["fees"] == 0.05
    assert m1["revenue"] == 1.0 and m1["result"] == "YES"
    assert m1["lean"] == "YES" and m1["won"] is True      # read was right
    assert "pnl_net" not in m1                            # never claimed
    m2 = next(g for g in graded if g["ticker"] == "M2")
    assert m2["lean"] == "NO" and m2["result"] == "YES" and m2["won"] is False


def test_lean_follows_buy_dollars_not_contract_count():
    """A flipped market's lean is where the money went, so a large
    late reversal outweighs a small early probe."""
    fills = [
        _f("1", "M3", "YES", "buy", 20, 0.10, 100.0),   # $2.00 on YES
        _f("2", "M3", "NO", "buy", 5, 0.80, 110.0),     # $4.00 on NO
    ]
    g = grade_markets(fills, {"M3": {"result": "no", "revenue": 5.0}})[0]
    assert g["lean"] == "NO" and g["won"] is True


def test_grade_markets_leaves_unsettled_markets_open():
    graded = grade_markets([_f("1", "M9", "YES", "buy", 1, 0.5, 100.0)], {})
    assert graded[0]["won"] is None and graded[0]["revenue"] is None
    assert graded[0]["lean"] == "YES"     # the read is known before settlement


def test_session_report_buckets_by_source_and_session():
    graded = [
        {"ticker": "A", "source": "manual", "entry_ts": 1784592000.0,   # wd_night
         "won": True, "fees": 0.25},
        {"ticker": "B", "source": "bot", "entry_ts": 1784592000.0,
         "won": False, "fees": 0.10},
    ]
    rep = session_report(graded)
    man = rep["manual"]["weekday_night"]
    assert man["n"] == 1 and man["wins"] == 1 and man["fees"] == 0.25
    assert rep["bot"]["weekday_night"]["n"] == 1


def test_load_local_settlements_are_dicts(tmp_path, monkeypatch):
    # --report used to hand grade_markets bare result strings -> AttributeError.
    import fills_sync as fs
    monkeypatch.setattr(fs, "FILLS_FILE", tmp_path / "f.jsonl")
    monkeypatch.setattr(fs, "SETTLE_FILE", tmp_path / "s.jsonl")
    (tmp_path / "f.jsonl").write_text(json.dumps(
        _f("1", "T1", "YES", "buy", 1, 0.40, 1000.0, fee=0.02)) + "\n")
    (tmp_path / "s.jsonl").write_text(json.dumps(
        {"ticker": "T1", "result": "yes", "revenue": 1.0}) + "\n"
        + json.dumps({"ticker": "T2", "result": None}) + "\n")
    fills, settle = fs.load_local()
    assert settle == {"T1": {"ticker": "T1", "result": "yes", "revenue": 1.0}}
    assert "read-right" in fs.report(fills, settle)       # no crash


def test_manual_summary_windows_and_price_adjusted_read():
    from fills_sync import manual_summary
    now = 100 * 86400.0
    fills = [
        # bought YES at 30c, settled YES: read right at a 30c price
        _f("1", "A", "YES", "buy", 2, 0.30, now - 1 * 86400, fee=0.03),
        # bought NO at 60c, settled YES: read wrong
        _f("2", "B", "NO", "buy", 1, 0.60, now - 2 * 86400, fee=0.02),
        # 20 days old: only in the 30d window; maker fill
        _f("3", "C", "YES", "buy", 1, 0.50, now - 20 * 86400),
        # unsettled: counted as open, never scored
        _f("4", "D", "YES", "buy", 1, 0.50, now - 3600, fee=0.02),
        # bot-tagged fill: excluded from the manual panel
        dict(_f("5", "E", "YES", "buy", 1, 0.50, now - 3600), source="bot"),
    ]
    fills[2]["taker"] = False
    for f in fills[:2] + fills[3:]:
        f["taker"] = True
    settle = {"A": {"result": "yes"}, "B": {"result": "yes"}, "C": {"result": "no"},
              "E": {"result": "yes"}}
    s = manual_summary(fills, settle, now=now)
    w7, w30 = s["windows"]["7d"], s["windows"]["30d"]
    assert (w7["markets"], w7["right"]) == (2, 1)
    assert w7["avg_price"] == 0.45                       # (0.30 + 0.60) / 2
    assert w7["read_minus_price_c"] == 5.0               # 50% right vs 45c paid
    assert w7["fees"] == 0.05 and w7["taker_share"] == 1.0
    assert (w30["markets"], w30["right"]) == (3, 1)
    assert w30["taker_share"] == 0.67                    # 2 of 3 settled-market fills
    assert s["open"] == ["D"]
    assert [b["band"] for b in s["bands"]] == ["30-50c", "50-70c"]
    b30 = s["bands"][0]                                  # A @30c right, C @50c wrong
    assert (b30["markets"], b30["right_pct"], b30["read_minus_price_c"]) == (1, 100.0, 70.0)


def test_manual_summary_empty_is_safe():
    from fills_sync import manual_summary
    s = manual_summary([], {}, now=1e9)
    assert s["windows"]["7d"]["markets"] == 0 and s["open"] == [] and s["bands"] == []
