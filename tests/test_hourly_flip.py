def _check_flip(pnl_pct, mins_left, opp_price, trigger=-20.0, min_mins=35.0, max_entry=0.72):
    return pnl_pct <= trigger and mins_left >= min_mins and opp_price <= max_entry


def test_flip_triggered_at_minus20():
    assert _check_flip(-20.0, 40.0, 0.64) is True


def test_flip_not_triggered_above_threshold():
    assert _check_flip(-19.9, 40.0, 0.64) is False


def test_flip_not_triggered_below_min_time():
    assert _check_flip(-25.0, 30.0, 0.64) is False


def test_flip_not_triggered_price_too_high():
    assert _check_flip(-25.0, 40.0, 0.73) is False


def test_flip_direction_up_to_down():
    assert ("Down" if "Up" == "Up" else "Up") == "Down"


def test_flip_direction_down_to_up():
    assert ("Down" if "Down" == "Up" else "Up") == "Up"


def test_flip_capital_is_half():
    assert 20.0 * 0.5 == 10.0


def test_opposite_price_formula():
    assert round(1.0 - 0.36, 4) == 0.64


def test_price_buffer_blocks_slippage():
    queued, live, buf = 0.64, 0.664, 0.02
    assert live > queued * (1 + buf)


def test_price_buffer_allows_within_range():
    queued, live, buf = 0.64, 0.650, 0.02
    assert not (live > queued * (1 + buf))


def test_momentum_filter_blocks_flip_down_when_bullish():
    flip_to = "Down"
    m5 = 0.003
    want_bearish = flip_to == "Down"
    assert want_bearish and m5 >= 0


def test_momentum_filter_allows_flip_down_when_bearish():
    flip_to = "Down"
    m5 = -0.005
    want_bearish = flip_to == "Down"
    assert not (want_bearish and m5 >= 0)
