from web import _vol_1m


def test_vol_1m_ignores_ticks_between_minute_samples():
    # 5s samples alternating +-10 around a steady climb of $30/min. The
    # minute marks share a phase, so this checks that only those samples are
    # read -- the per-tick estimator would see ~$240/min of |move| here.
    now = 1000.0
    hist = [(now - 5 * i, 100 + (200 - 5 * i) / 2 + (10 if i % 2 else -10))
            for i in range(40, -1, -1)]
    v = _vol_1m(hist, now)
    assert v is not None and abs(v - 30.0) < 1e-6


def test_vol_1m_needs_two_minutes_of_history():
    assert _vol_1m([], 1000.0) is None
    assert _vol_1m([(940.0, 1.0), (1000.0, 2.0)], 1000.0) is None


def test_vol_1m_refuses_stale_gaps():
    # nothing within 15s of now-120 -> stop, too few points
    hist = [(860.0, 1.0), (940.0, 2.0), (1000.0, 3.0)]
    assert _vol_1m(hist, 1000.0) is None
