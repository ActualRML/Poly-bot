"""Tests for src/logic/technical.py"""
import pytest
from src.logic.technical import compute_rsi, compute_zscore, detect_volume_spike, compute_trend, compute_ema, compute_trend_bias


# ── compute_rsi ───────────────────────────────────────────────────────────────

def test_rsi_insufficient_data():
    assert compute_rsi([100.0] * 14, period=14) is None  # need period+1
    assert compute_rsi([], period=14) is None


def test_rsi_all_up_returns_100():
    closes = [100 + i for i in range(20)]
    assert compute_rsi(closes, period=14) == 100.0


def test_rsi_all_down_returns_zero():
    closes = [200 - i for i in range(20)]
    assert compute_rsi(closes, period=14) == pytest.approx(0.0, abs=1e-3)


def test_rsi_flat_returns_50():
    closes = [100.0] * 20
    assert compute_rsi(closes, period=14) == 50.0


def test_rsi_overbought_range():
    # 11 up days, 3 down days → RSI well above 70
    closes = [100.0]
    for _ in range(11):
        closes.append(closes[-1] + 1.0)
    for _ in range(3):
        closes.append(closes[-1] - 0.1)
    rsi = compute_rsi(closes, period=14)
    assert rsi is not None and rsi > 70


def test_rsi_oversold_range():
    # 11 down days, 3 up days → RSI well below 30
    closes = [100.0]
    for _ in range(11):
        closes.append(closes[-1] - 1.0)
    for _ in range(3):
        closes.append(closes[-1] + 0.1)
    rsi = compute_rsi(closes, period=14)
    assert rsi is not None and rsi < 30


def test_rsi_neutral_range():
    # Alternating up/down of equal magnitude → ~50
    closes = [100.0]
    for i in range(20):
        closes.append(closes[-1] + (1.0 if i % 2 == 0 else -1.0))
    rsi = compute_rsi(closes, period=14)
    assert rsi is not None and 40 <= rsi <= 60


def test_rsi_uses_last_period_bars():
    # Append a big up move at the end — RSI should reflect only last 14 bars
    closes = [100.0] * 10 + [100.0 + i for i in range(15)]
    rsi = compute_rsi(closes, period=14)
    assert rsi is not None and rsi > 60


# ── compute_zscore ────────────────────────────────────────────────────────────

def test_zscore_insufficient_data():
    assert compute_zscore([100.0] * 19, window=20) is None
    assert compute_zscore([], window=20) is None


def test_zscore_at_mean_is_zero():
    closes = [100.0] * 20
    assert compute_zscore(closes, window=20) == pytest.approx(0.0)


def test_zscore_above_mean_is_positive():
    closes = [100.0] * 19 + [110.0]
    z = compute_zscore(closes, window=20)
    assert z is not None and z > 0


def test_zscore_below_mean_is_negative():
    closes = [100.0] * 19 + [90.0]
    z = compute_zscore(closes, window=20)
    assert z is not None and z < 0


def test_zscore_at_extreme_above():
    # Last price = mean + 3*std
    import math
    closes = [100.0] * 19 + [130.0]
    mean = sum(closes) / 20
    std = math.sqrt(sum((x - mean) ** 2 for x in closes) / 20)
    z = compute_zscore(closes, window=20)
    assert z is not None
    assert abs(z - (closes[-1] - mean) / std) < 1e-3


def test_zscore_typical_threshold_trigger():
    # Set up data where last bar is clearly > 2.5 std from mean
    closes = [100.0] * 19 + [150.0]
    z = compute_zscore(closes, window=20)
    assert z is not None and z > 2.5


# ── detect_volume_spike ───────────────────────────────────────────────────────

def test_volume_spike_insufficient_data():
    assert detect_volume_spike([1.0, 10.0], multiplier=3.0) is False
    assert detect_volume_spike([], multiplier=3.0) is False


def test_volume_spike_detected():
    # last bar = 10x average of prior bars
    vols = [100.0, 100.0, 100.0, 100.0, 1000.0]
    assert detect_volume_spike(vols, multiplier=3.0) is True


def test_volume_spike_not_detected_normal():
    vols = [100.0, 110.0, 90.0, 105.0, 115.0]
    assert detect_volume_spike(vols, multiplier=3.0) is False


def test_volume_spike_exact_threshold():
    # last bar = exactly 3x avg → True (> not >=)
    vols = [100.0, 100.0, 100.0, 100.0, 300.1]
    assert detect_volume_spike(vols, multiplier=3.0) is True
    vols2 = [100.0, 100.0, 100.0, 100.0, 300.0]
    assert detect_volume_spike(vols2, multiplier=3.0) is False


def test_volume_spike_zero_average():
    vols = [0.0, 0.0, 0.0, 0.0, 100.0]
    assert detect_volume_spike(vols, multiplier=3.0) is False


# ── compute_trend ─────────────────────────────────────────────────────────────

def test_trend_insufficient_data():
    assert compute_trend([100.0, 101.0], lookback=4) is None  # need 5 bars
    assert compute_trend([], lookback=4) is None
    assert compute_trend([100.0] * 4, lookback=4) is None     # exactly lookback, need +1


def test_trend_uptrend():
    # 4% up over 4 bars
    closes = [100.0, 101.0, 102.0, 103.0, 104.0]
    t = compute_trend(closes, lookback=4)
    assert t is not None
    assert t == pytest.approx(0.04, rel=1e-4)


def test_trend_downtrend():
    closes = [104.0, 103.0, 102.0, 101.0, 100.0]
    t = compute_trend(closes, lookback=4)
    assert t is not None and t < 0
    assert t == pytest.approx((100 - 104) / 104, rel=1e-4)


def test_trend_flat():
    closes = [100.0] * 10
    assert compute_trend(closes, lookback=4) == pytest.approx(0.0)


def test_trend_above_threshold():
    # 3% in 4h should exceed 2% threshold
    closes = [100.0, 100.0, 100.0, 100.0, 103.0]
    t = compute_trend(closes, lookback=4)
    assert t is not None and t > 0.02


def test_trend_uses_last_bars_only():
    # Old prices moved down, last 4 bars moved up — should report uptrend
    closes = [110.0, 105.0, 100.0, 101.0, 102.0, 103.0, 104.0]
    t = compute_trend(closes, lookback=4)
    assert t is not None and t > 0


def test_trend_lookback_1():
    closes = [100.0, 105.0]
    t = compute_trend(closes, lookback=1)
    assert t == pytest.approx(0.05)


def test_trend_zero_prev_price():
    closes = [0.0, 0.0, 0.0, 0.0, 100.0]
    assert compute_trend(closes, lookback=4) is None


# ── compute_ema ───────────────────────────────────────────────────────────────

def test_ema_insufficient_data():
    assert compute_ema([100.0] * 5, period=6) is None
    assert compute_ema([], period=5) is None


def test_ema_flat_series():
    assert compute_ema([100.0] * 10, period=5) == pytest.approx(100.0)


def test_ema_exactly_period_bars():
    # SMA seed only, no iteration → equals mean of first period bars
    closes = [10.0, 20.0, 30.0, 40.0, 50.0]
    result = compute_ema(closes, period=5)
    assert result == pytest.approx(30.0)


def test_ema_lags_in_uptrend():
    closes = [100.0 + i for i in range(30)]
    ema = compute_ema(closes, period=24)
    assert ema is not None and ema < closes[-1]


def test_ema_leads_above_price_in_downtrend():
    closes = [200.0 - i for i in range(30)]
    ema = compute_ema(closes, period=24)
    assert ema is not None and ema > closes[-1]


def test_ema_period_1_equals_last_price():
    closes = [10.0, 20.0, 55.0]
    # With period=1, SMA seed = closes[0], then each step: ema = price*1 + ema*0
    # → ema after all bars = closes[-1]
    result = compute_ema(closes, period=1)
    assert result == pytest.approx(55.0)


# ── compute_trend_bias ────────────────────────────────────────────────────────

def test_trend_bias_uptrend_is_positive():
    closes = [100.0 + i * 0.5 for i in range(30)]
    assert compute_trend_bias(closes) > 0


def test_trend_bias_downtrend_is_negative():
    closes = [200.0 - i * 0.5 for i in range(30)]
    assert compute_trend_bias(closes) < 0


def test_trend_bias_clamped_to_max():
    closes = [100.0 + i * 10 for i in range(30)]
    result = compute_trend_bias(closes, max_bias=0.10)
    assert result <= 0.10


def test_trend_bias_clamped_to_min():
    closes = [300.0 - i * 10 for i in range(30)]
    result = compute_trend_bias(closes, max_bias=0.10)
    assert result >= -0.10


def test_trend_bias_flat_series():
    closes = [100.0] * 30
    # Flat: current == EMA_24, EMA_6 unchanged → both components = 0
    assert compute_trend_bias(closes) == pytest.approx(0.0, abs=0.001)


def test_trend_bias_insufficient_data_returns_zero():
    # Fewer than 24 bars → EMA_24 returns None → bias from that component = 0
    closes = [100.0 + i for i in range(10)]
    result = compute_trend_bias(closes)
    assert result == pytest.approx(0.0, abs=0.10)  # only 6h component fires
