import pytest
from hypothesis import given, strategies as st

from src.logic.probability import CryptoProbabilityCalculator, _get_calibration_correction

calc = CryptoProbabilityCalculator()

@given(x=st.floats(allow_nan=False, allow_infinity=False, min_value=-1e10, max_value=1e10))
def test_norm_cdf_always_in_01(x):
    assert 0.0 <= calc._norm_cdf(x) <= 1.0

def test_norm_cdf_at_zero_is_half():
    assert abs(calc._norm_cdf(0.0) - 0.5) < 1e-10

def test_norm_cdf_monotone():
    assert calc._norm_cdf(-2.0) < calc._norm_cdf(0.0) < calc._norm_cdf(2.0)

@given(
    S=st.floats(0.01, 1_000_000.0, allow_nan=False, allow_infinity=False),
    K=st.floats(0.01, 1_000_000.0, allow_nan=False, allow_infinity=False),
    T=st.floats(0.001, 10.0, allow_nan=False, allow_infinity=False),
    vol=st.floats(0.01, 5.0, allow_nan=False, allow_infinity=False),
    direction=st.sampled_from(["above", "below"]),
)
def test_barrier_prob_in_01(S, K, T, vol, direction):
    mu_adj = -0.5 * vol ** 2
    result = calc._barrier_prob(S, K, T, vol, mu_adj, direction)
    assert 0.0 <= result <= 1.0

@given(
    S=st.floats(0.01, 1_000_000.0, allow_nan=False, allow_infinity=False),
    K=st.floats(0.01, 1_000_000.0, allow_nan=False, allow_infinity=False),
    T=st.floats(0.001, 10.0, allow_nan=False, allow_infinity=False),
    vol=st.floats(0.01, 5.0, allow_nan=False, allow_infinity=False),
    direction=st.sampled_from(["above", "below"]),
)
def test_expiry_prob_in_01(S, K, T, vol, direction):
    mu_adj = -0.5 * vol ** 2
    result = calc._expiry_prob(S, K, T, vol, mu_adj, direction)
    assert 0.0 <= result <= 1.0

@given(
    asset=st.sampled_from(["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE"]),
    current_price=st.floats(0.01, 1_000_000.0, allow_nan=False, allow_infinity=False),
    target_price=st.floats(0.01, 1_000_000.0, allow_nan=False, allow_infinity=False),
    days_remaining=st.integers(1, 365),
    vol=st.floats(0.10, 3.0, allow_nan=False, allow_infinity=False),
    direction=st.sampled_from(["above", "below", "auto"]),
    use_barrier=st.booleans(),
)
def test_calculate_prob_always_valid(asset, current_price, target_price, days_remaining, vol, direction, use_barrier):
    r = calc.calculate(
        asset, current_price, target_price, days_remaining,
        volatility=vol, direction=direction, use_barrier=use_barrier,
    )
    assert 0.0 <= r.probability <= 1.0

@given(
    asset=st.sampled_from(["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE"]),
    target_pct=st.floats(-0.5, 0.5, allow_nan=False, allow_infinity=False),
    model=st.sampled_from(["at_expiry", "barrier"]),
)
def test_calibration_correction_non_negative(asset, target_pct, model):
    result = _get_calibration_correction(asset, target_pct, model)
    assert result >= 0.0

def test_invalid_inputs_return_zero_prob():
    r = calc.calculate("BTC", 0.0, 80000.0, 5, volatility=0.40)
    assert r.probability == 0.0
    r2 = calc.calculate("BTC", 80000.0, 80000.0, 0, volatility=0.40)
    assert r2.probability == 0.0
