import math
import pytest
from datetime import datetime, timezone, timedelta
from hypothesis import given, strategies as st

from src.logic.strategy import get_dynamic_threshold, should_force_exit, MIN_THRESHOLD, MAX_THRESHOLD


# ==============================================================================
# get_dynamic_threshold
# ==============================================================================

def test_threshold_uses_asset_vol():
    result = get_dynamic_threshold("BTC", {"BTC": 0.40})
    expected = max(MIN_THRESHOLD, min(MAX_THRESHOLD, 0.40 / math.sqrt(24) * 1.5))
    assert abs(result - expected) < 1e-9

def test_threshold_falls_back_to_default():
    result = get_dynamic_threshold("SOL", {"DEFAULT": 0.40})
    result2 = get_dynamic_threshold("BTC", {"BTC": 0.40})
    assert abs(result - result2) < 1e-9

def test_threshold_falls_back_to_hardcoded_when_no_default():
    result = get_dynamic_threshold("BTC", {})
    assert result == pytest.approx(max(MIN_THRESHOLD, min(MAX_THRESHOLD, 0.40 / math.sqrt(24) * 1.5)))

def test_threshold_clamped_at_min_for_low_vol():
    result = get_dynamic_threshold("BTC", {"BTC": 0.001})
    assert result == MIN_THRESHOLD

def test_threshold_clamped_at_max_for_high_vol():
    result = get_dynamic_threshold("BTC", {"BTC": 99.0})
    assert result == MAX_THRESHOLD

def test_threshold_asset_key_case_insensitive():
    r1 = get_dynamic_threshold("btc", {"BTC": 0.50})
    r2 = get_dynamic_threshold("BTC", {"BTC": 0.50})
    assert r1 == r2

@given(vol=st.floats(0.001, 10.0, allow_nan=False, allow_infinity=False))
def test_threshold_always_in_bounds(vol):
    result = get_dynamic_threshold("BTC", {"BTC": vol})
    assert MIN_THRESHOLD <= result <= MAX_THRESHOLD


# ==============================================================================
# should_force_exit
# ==============================================================================

def test_force_exit_true_when_under_buffer():
    expiry = datetime.now(timezone.utc) + timedelta(minutes=5)
    assert should_force_exit(expiry) is True

def test_force_exit_false_when_over_buffer():
    expiry = datetime.now(timezone.utc) + timedelta(minutes=30)
    assert should_force_exit(expiry) is False

def test_force_exit_true_when_already_expired():
    expiry = datetime.now(timezone.utc) - timedelta(minutes=1)
    assert should_force_exit(expiry) is True

def test_force_exit_handles_naive_datetime():
    naive = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5)
    assert naive.tzinfo is None
    assert should_force_exit(naive) is True

def test_force_exit_custom_buffer():
    expiry = datetime.now(timezone.utc) + timedelta(minutes=20)
    assert should_force_exit(expiry, buffer_minutes=30) is True
    assert should_force_exit(expiry, buffer_minutes=10) is False
