import dashboard


def test_log_loop_error_writes_stderr_and_throttles(capsys, monkeypatch):
    dashboard._loop_err_last.clear()
    t = [1000.0]
    monkeypatch.setattr(dashboard.time, "time", lambda: t[0])
    try:
        raise RuntimeError("429 Too Many Requests")
    except RuntimeError as e:
        dashboard._log_loop_error("scan_trades", e)
        dashboard._log_loop_error("scan_trades", e)       # same minute: dropped
        t[0] += 61
        dashboard._log_loop_error("scan_trades", e)       # next minute: logged
    err = capsys.readouterr().err
    assert err.count("scanner ERROR") == 2
    assert "scan_trades: RuntimeError: 429 Too Many Requests" in err
    assert "Traceback" in err
