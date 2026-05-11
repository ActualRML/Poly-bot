import math
import pytest
from hypothesis import given, strategies as st

from src.logic.updown_strategy import (
    _norm_cdf,
    _wma,
    _hma_series,
    _rsi,
    _ema_series,
    _macd_calc,
    _heikin_ashi,
    _ha_trend,
    _atr,
    _renko_analysis,
    _assess_master_trend,
    _cvd_series,
    _cvd_divergence,
    _absorption_check,
    _poc_position_approx,
    _trapped_traders,
    detect_updown_market,
)

@given(x=st.floats(allow_nan=False, allow_infinity=False, min_value=-1e10, max_value=1e10))
def test_norm_cdf_always_in_01(x):
    assert 0.0 <= _norm_cdf(x) <= 1.0

def test_norm_cdf_at_zero_is_half():
    assert abs(_norm_cdf(0.0) - 0.5) < 1e-10

def test_norm_cdf_monotone():
    assert _norm_cdf(-3.0) < _norm_cdf(-1.0) < _norm_cdf(0.0) < _norm_cdf(1.0) < _norm_cdf(3.0)

def test_norm_cdf_symmetry():
    for x in [0.5, 1.0, 2.0, 3.0]:
        assert abs(_norm_cdf(x) + _norm_cdf(-x) - 1.0) < 1e-10

@pytest.mark.parametrize("question,expected_symbol", [
    ("BTC Up or Down Daily",                           "BTC"),
    ("Bitcoin Up or Down Daily",                       "BTC"),
    ("bitcoin up or down - may 6, 2026, 1am et",       "BTC"),
    ("ETH Up or Down Daily",                           "ETH"),
    ("Ethereum Up or Down Daily",                      "ETH"),
    ("Solana Up or Down Daily",                        "SOL"),
    ("SOL Up or Down Daily",                           "SOL"),
    ("XRP Up or Down Daily",                           "XRP"),
    ("Ripple Up or Down Daily",                        "XRP"),
    ("Dogecoin Up or Down Daily",                      "DOGE"),
    ("DOGE Up or Down Daily",                          "DOGE"),
    ("BNB Up or Down Daily",                           "BNB"),
    ("Binance Coin Up or Down Daily",                  "BNB"),
])
def test_detect_updown_market_known_symbols(question, expected_symbol):
    result = detect_updown_market(question)
    assert result is not None
    assert result == (expected_symbol, "Up")

@pytest.mark.parametrize("question", [
    "Will BTC be above $80,000?",
    "Who will win the 2024 election?",
    "Is ETH going to moon?",
    "",
])
def test_detect_updown_market_no_updown_phrase_returns_none(question):
    assert detect_updown_market(question) is None

def test_detect_updown_market_unknown_symbol_returns_none():
    assert detect_updown_market("PEPE Up or Down Daily") is None

def test_detect_updown_market_with_valid_outcomes():
    result = detect_updown_market("BTC Up or Down Daily", outcomes=["Up", "Down"])
    assert result == ("BTC", "Up")

def test_detect_updown_market_with_invalid_outcomes_returns_none():
    assert detect_updown_market("BTC Up or Down Daily", outcomes=["Yes", "No"]) is None

def test_wma_single_element():
    assert _wma([5.0], 1) == 5.0

def test_wma_equal_prices_returns_price():
    assert abs(_wma([3.0, 3.0, 3.0], 3) - 3.0) < 1e-10

def test_wma_linearly_increasing_weights_recent():
    assert abs(_wma([1.0, 2.0, 3.0], 3) - 14 / 6) < 1e-10

def test_hma_series_insufficient_data_returns_empty():
    assert _hma_series([100.0] * 5, 9) == []

def test_hma_series_constant_prices_flat():
    prices = [100.0] * 30
    hma = _hma_series(prices, 9)
    assert len(hma) > 0
    for v in hma:
        assert abs(v - 100.0) < 1e-6

def test_hma_series_uptrend_direction():
    prices = [float(i) for i in range(1, 35)]
    hma = _hma_series(prices, 9)
    assert len(hma) >= 2
    assert hma[-1] > hma[-2]

def test_hma_series_downtrend_direction():
    prices = [float(i) for i in range(34, 0, -1)]
    hma = _hma_series(prices, 9)
    assert len(hma) >= 2
    assert hma[-1] < hma[-2]

def test_rsi_insufficient_data_returns_none():
    assert _rsi([1.0, 2.0, 3.0], period=7) is None

def test_rsi_always_in_range():
    import random
    random.seed(42)
    prices = [100 + random.gauss(0, 1) for _ in range(30)]
    val = _rsi(prices, 7)
    assert val is not None
    assert 0.0 <= val <= 100.0

def test_rsi_all_gains_returns_100():
    prices = [float(i) for i in range(1, 15)]
    assert _rsi(prices, 7) == 100.0

def test_rsi_all_losses_returns_near_zero():
    prices = [float(i) for i in range(14, 0, -1)]
    val = _rsi(prices, 7)
    assert val is not None
    assert val < 1.0

@given(
    prices=st.lists(st.floats(0.01, 1000.0, allow_nan=False, allow_infinity=False),
                    min_size=9, max_size=50),
)
def test_rsi_hypothesis_range(prices):
    val = _rsi(prices, 7)
    if val is not None:
        assert 0.0 <= val <= 100.0

def test_ema_series_insufficient_returns_empty():
    assert _ema_series([1.0, 2.0], 5) == []

def test_ema_series_constant_prices():
    result = _ema_series([5.0] * 20, 5)
    for v in result:
        assert abs(v - 5.0) < 1e-10

def test_ema_series_length():
    result = _ema_series(list(range(1, 31)), 5)
    assert len(result) == 26

def test_macd_insufficient_returns_none():
    assert _macd_calc([1.0] * 10) is None

def test_macd_returns_dict_keys():
    prices = [float(i) + (0.1 * (i % 3)) for i in range(40)]
    result = _macd_calc(prices)
    assert result is not None
    assert "histogram" in result
    assert "cross" in result
    assert "pending_cross" in result

def test_macd_constant_prices_zero_histogram():
    prices = [100.0] * 40
    result = _macd_calc(prices)
    assert result is not None
    assert abs(result["histogram"]) < 1e-8

def _mk_klines(ohlc_list):
    return [(0, o, h, l, c) for o, h, l, c in ohlc_list]

def test_heikin_ashi_empty_returns_empty():
    assert _heikin_ashi([]) == []

def test_heikin_ashi_single_bar():
    result = _heikin_ashi(_mk_klines([(100, 110, 90, 105)]))
    assert len(result) == 1
    ha_o, ha_h, ha_l, ha_c = result[0]
    assert abs(ha_c - (100 + 110 + 90 + 105) / 4) < 1e-10
    assert abs(ha_o - (100 + 105) / 2) < 1e-10
    assert ha_h >= ha_o and ha_h >= ha_c
    assert ha_l <= ha_o and ha_l <= ha_c

def test_heikin_ashi_length_preserved():
    klines = _mk_klines([(100 + i, 110 + i, 90 + i, 105 + i) for i in range(10)])
    assert len(_heikin_ashi(klines)) == 10

def test_ha_trend_empty_returns_neutral():
    assert _ha_trend([]) == ("neutral", 0)

def test_ha_trend_bullish_streak():
    bars = [(95.0, 110.0, 90.0, 105.0)] * 4
    direction, streak = _ha_trend(bars)
    assert direction == "up"
    assert streak == 4

def test_ha_trend_bearish_streak():
    bars = [(105.0, 110.0, 90.0, 95.0)] * 3
    direction, streak = _ha_trend(bars)
    assert direction == "down"
    assert streak == 3

def test_ha_trend_mixed_streak_resets():
    bars = [
        (95.0, 110.0, 90.0, 105.0),
        (96.0, 111.0, 91.0, 106.0),
        (106.0, 111.0, 91.0, 96.0),
    ]
    direction, streak = _ha_trend(bars)
    assert direction == "down"
    assert streak == 1

def test_atr_insufficient_returns_none():
    klines = _mk_klines([(100, 105, 95, 102)] * 5)
    assert _atr(klines, period=14) is None

def test_atr_constant_candles():
    klines = _mk_klines([(100, 102, 98, 100)] * 20)
    val = _atr(klines, period=14)
    assert val is not None
    assert abs(val - 4.0) < 1e-10

def test_atr_positive():
    import random
    random.seed(1)
    klines = _mk_klines([
        (100 + random.gauss(0, 1), 105 + random.gauss(0, 1),
         95 + random.gauss(0, 1), 100 + random.gauss(0, 1))
        for _ in range(20)
    ])
    val = _atr(klines, period=14)
    assert val is not None and val > 0

def test_renko_empty_returns_sideways():
    assert _renko_analysis([], 1.0) == {"direction": "sideways", "consecutive": 0}

def test_renko_zero_brick_returns_sideways():
    assert _renko_analysis([100.0, 101.0], 0.0) == {"direction": "sideways", "consecutive": 0}

def test_renko_sideways_no_bricks():
    closes = [100.0, 100.1, 100.2, 100.1, 100.0]
    result = _renko_analysis(closes, 1.0)
    assert result["direction"] == "sideways"
    assert result["consecutive"] == 0

def test_renko_uptrend_bricks():
    closes = [100.0, 101.0, 102.0, 103.0, 104.0]
    result = _renko_analysis(closes, 1.0)
    assert result["direction"] == "up"
    assert result["consecutive"] == 4

def test_renko_downtrend_bricks():
    closes = [104.0, 103.0, 102.0, 101.0, 100.0]
    result = _renko_analysis(closes, 1.0)
    assert result["direction"] == "down"
    assert result["consecutive"] == 4

def test_assess_master_trend_empty_returns_neutral():
    assert _assess_master_trend([], []) == "neutral"

def test_assess_master_trend_strong_bullish():
    ha_bars = [(95.0, 110.0, 90.0, 105.0)] * 5
    closes_5m = [float(i) for i in range(1, 35)]
    result = _assess_master_trend(ha_bars, closes_5m)
    assert result == "strong_bullish"

def test_assess_master_trend_strong_bearish():
    ha_bars = [(105.0, 110.0, 90.0, 95.0)] * 5
    closes_5m = [float(i) for i in range(34, 0, -1)]
    result = _assess_master_trend(ha_bars, closes_5m)
    assert result == "strong_bearish"

def test_assess_master_trend_conflicting_returns_neutral():
    ha_bars = [(95.0, 110.0, 90.0, 105.0)] * 5
    closes_5m = [float(i) for i in range(34, 0, -1)]
    result = _assess_master_trend(ha_bars, closes_5m)
    assert result == "neutral"

def _mk_kext(rows):
    return [(0, o, h, l, c, v, bv) for o, h, l, c, v, bv in rows]

def test_cvd_series_all_buys_increasing():
    kext = _mk_kext([(100, 101, 99, 100, 10.0, 10.0)] * 5)
    cvd = _cvd_series(kext)
    assert len(cvd) == 5
    for i in range(1, 5):
        assert cvd[i] > cvd[i - 1]

def test_cvd_series_all_sells_decreasing():
    kext = _mk_kext([(100, 101, 99, 100, 10.0, 0.0)] * 5)
    cvd = _cvd_series(kext)
    for i in range(1, 5):
        assert cvd[i] < cvd[i - 1]

def test_cvd_series_equal_volume_flat():
    kext = _mk_kext([(100, 101, 99, 100, 10.0, 5.0)] * 5)
    cvd = _cvd_series(kext)
    assert all(v == 0.0 for v in cvd)

def test_cvd_divergence_insufficient_data():
    kext = _mk_kext([(100, 105, 95, 100, 10.0, 5.0)] * 2)
    assert _cvd_divergence(kext) is None

def test_cvd_divergence_bearish():
    kext = _mk_kext([
        (100, 100, 98, 99, 10.0, 8.0),
        (100, 101, 98, 100, 10.0, 6.0),
        (100, 102, 98, 101, 10.0, 4.0),
    ])
    assert _cvd_divergence(kext) == "bearish_div"

def test_cvd_divergence_bullish():
    kext = _mk_kext([
        (100, 102, 100, 101, 10.0, 2.0),
        (100, 102, 99, 100,  10.0, 3.0),
        (100, 102, 98, 100,  10.0, 6.0),
    ])
    assert _cvd_divergence(kext) == "bullish_div"

def test_cvd_divergence_no_divergence():
    kext = _mk_kext([
        (100, 100, 98, 99, 10.0, 8.0),
        (100, 101, 98, 100, 10.0, 9.0),
        (100, 102, 98, 101, 10.0, 10.0),
    ])
    assert _cvd_divergence(kext) is None

def test_absorption_insufficient_returns_none():
    kext = _mk_kext([(100, 102, 98, 100, 10.0, 5.0)] * 2)
    assert _absorption_check(kext) is None

def test_absorption_high_vol_narrow_spread_detected():
    ref_bars = [(100, 102, 98, 100, 10.0, 5.0)] * 4
    last_bar  = [(100, 100.5, 99.8, 100, 40.0, 5.0)]
    kext = _mk_kext(ref_bars + last_bar)
    result = _absorption_check(kext)
    assert result is not None
    assert result["vol_ratio"] > 2.0
    assert result["location"] in ("support", "resistance")

def test_poc_position_close_at_top():
    bar = (0, 100, 110, 90, 108, 10, 5)
    assert _poc_position_approx(bar) == "top"

def test_poc_position_close_at_bottom():
    bar = (0, 100, 110, 90, 92, 10, 5)
    assert _poc_position_approx(bar) == "bottom"

def test_poc_position_zero_range_returns_middle():
    bar = (0, 100, 100, 100, 100, 10, 5)
    assert _poc_position_approx(bar) == "middle"

def test_trapped_buyers_bullish_upper_wick():
    bar = (0, 90, 110, 90, 94, 10, 5)
    assert _trapped_traders(bar) == "buyers"

def test_trapped_sellers_bearish_lower_wick():
    bar = (0, 110, 110, 90, 106, 10, 5)
    assert _trapped_traders(bar) == "sellers"

def test_no_trapped_normal_candle():
    bar = (0, 100, 103, 99, 102, 10, 6)
    assert _trapped_traders(bar) is None
