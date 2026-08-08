from exit_watcher import _num


def test_num_returns_the_value_when_present():
    assert _num({"distance": -25.9}, "distance", 0) == -25.9
    assert _num({"mins_left": 7.5}, "mins_left", 99) == 7.5


def test_num_substitutes_the_default_when_the_key_is_absent():
    assert _num({}, "distance", 0) == 0
    assert _num({}, "mins_left", 99) == 99


def test_num_substitutes_the_default_when_the_value_is_None():
    """The 2026-08-08 crash: DNS failures make the spot poller emit
    distance=None -- the key is PRESENT, so dict.get's default never fires
    and `distance <= 30` raised TypeError, killing the daemon."""
    assert _num({"distance": None}, "distance", 0) == 0
    assert _num({"whale_count": None}, "whale_count", 0) == 0


def test_num_preserves_a_legitimate_zero():
    """mins_left defaults to 99, so `or`-style fallback would turn a real
    0.0 (at expiry) into 99 and invert every time comparison."""
    assert _num({"mins_left": 0.0}, "mins_left", 99) == 0.0
    assert _num({"distance": 0}, "distance", 5) == 0
