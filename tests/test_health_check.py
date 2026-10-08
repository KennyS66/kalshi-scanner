from health_check import check_process, check_age, check_latency, overall


def test_process_check_fails_when_none_running():
    assert check_process("exit_watcher", 1)["ok"] is True
    r = check_process("exit_watcher", 0)
    assert r["ok"] is False and "exit_watcher" in r["detail"]


def test_process_check_flags_duplicates():
    """Two of the same daemon means two writers on one journal."""
    r = check_process("swing_bot", 2)
    assert r["ok"] is False and "2" in r["detail"]


def test_age_check_is_inclusive_at_the_limit():
    assert check_age("heartbeat", 60.0, limit=60.0)["ok"] is True
    assert check_age("heartbeat", 60.1, limit=60.0)["ok"] is False


def test_age_check_handles_never_seen():
    r = check_age("spot", None, limit=300.0)
    assert r["ok"] is False and "never" in r["detail"].lower()


def test_latency_check_fails_when_the_tail_exceeds_the_consumers_timeout():
    """The 2026-08-08 feed_down cause: p90 4.73s against a 4s timeout.
    A healthy median hides it, so the check must look at the TAIL."""
    fast = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2]
    assert check_latency(fast, timeout=4.0)["ok"] is True
    slow = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 5.0, 5.5, 6.1]
    r = check_latency(slow, timeout=4.0)
    assert r["ok"] is False and "3/10" in r["detail"]


def test_latency_check_needs_samples():
    assert check_latency([], timeout=4.0)["ok"] is False


def test_overall_is_false_if_any_check_fails():
    ok = [{"ok": True, "name": "a", "detail": ""},
          {"ok": True, "name": "b", "detail": ""}]
    assert overall(ok) is True
    assert overall(ok + [{"ok": False, "name": "c", "detail": "x"}]) is False


def test_footprint_passes_at_normal_size():
    from health_check import check_footprint
    r = check_footprint(rss_mb=259, cpu_pct=33)
    assert r["ok"] is True and "259" in r["detail"]


def test_footprint_fails_at_the_pathological_size_we_actually_saw():
    """2026-08-09: a 5-day-old scanner sat at 2462MB / 102% of a core with an
    unbounded market_snapshots, starving uvicorn's event loop via the GIL."""
    from health_check import check_footprint
    r = check_footprint(rss_mb=2462, cpu_pct=102)
    assert r["ok"] is False
    assert "2462" in r["detail"] and "102" in r["detail"]


def test_footprint_flags_either_dimension_alone():
    from health_check import check_footprint
    assert check_footprint(rss_mb=2000, cpu_pct=20)["ok"] is False
    assert check_footprint(rss_mb=200, cpu_pct=95)["ok"] is False


def test_footprint_reports_unknown_without_failing_the_stack():
    """If the process is gone the process check already fails; the footprint
    check must not double-report it as a second unrelated failure."""
    from health_check import check_footprint
    r = check_footprint(rss_mb=None, cpu_pct=None)
    assert r["ok"] is True and "unknown" in r["detail"].lower()


def test_tape_age_fails_when_the_rig_stops_writing():
    """A dead rig mid-capture yields a partial tape nobody notices for days."""
    from health_check import check_tape_age
    assert check_tape_age(4000.0, limit=600.0)["ok"] is False


def test_tape_age_passes_when_fresh():
    from health_check import check_tape_age
    assert check_tape_age(30.0, limit=600.0)["ok"] is True


def test_tape_age_absent_is_a_failure_not_a_pass():
    from health_check import check_tape_age
    assert check_tape_age(None, limit=600.0)["ok"] is False


def test_check_scan_fails_when_scanner_holds_no_markets_or_whales():
    from health_check import check_scan
    bad = check_scan({"market_snapshots": 0, "whale_alerts": 0, "scan_errors": 41,
                      "last_scan_error": "HTTPError: 429 Too Many Requests"})
    assert bad["ok"] is False and "429" in bad["detail"]
    assert check_scan({"market_snapshots": 280, "whale_alerts": 0})["ok"] is False
    assert check_scan({"market_snapshots": 280, "whale_alerts": 1902,
                       "scan_errors": 0})["ok"] is True
    assert check_scan(None)["ok"] is True          # unknown -> latency check owns it
