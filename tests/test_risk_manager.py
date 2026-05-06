import pytest
from hypothesis import given, strategies as st

from src.logic.risk_manager import (
    get_dynamic_stop_loss,
    calculate_position_size,
    MIN_STOP_FRACTION,
    MAX_STOP_FRACTION,
    MIN_POSITION_USDC,
    MAX_POSITION_USDC,
    BASE_POSITION_USDC,
)

@given(
    current_P=st.floats(0.0, 1.0, allow_nan=False, allow_infinity=False),
    vol_annual=st.one_of(
        st.none(),
        st.floats(0.01, 5.0, allow_nan=False, allow_infinity=False),
    ),
)
def test_stop_loss_always_in_bounds(current_P, vol_annual):
    result = get_dynamic_stop_loss(current_P, vol_annual)
    assert MIN_STOP_FRACTION <= result <= MAX_STOP_FRACTION

@given(
    trades=st.lists(
        st.fixed_dictionaries({
            "pnl": st.floats(-1000.0, 1000.0, allow_nan=False, allow_infinity=False)
        }),
        min_size=0,
        max_size=5,
    )
)
def test_position_size_always_in_bounds(trades):
    result = calculate_position_size(trades)
    assert MIN_POSITION_USDC <= result <= MAX_POSITION_USDC

def test_stop_loss_at_prob_zero():
    assert get_dynamic_stop_loss(0.0) == MIN_STOP_FRACTION

def test_stop_loss_at_prob_one():
    assert get_dynamic_stop_loss(1.0) == MIN_STOP_FRACTION

def test_stop_loss_capped_at_max_when_prob_half():
    result = get_dynamic_stop_loss(0.5, vol_annual=None)
    assert result == MAX_STOP_FRACTION

def test_consecutive_losses_reduce_size():
    trades = [{"pnl": -1.0}, {"pnl": -1.0}]
    assert calculate_position_size(trades) == MIN_POSITION_USDC

def test_three_consecutive_wins_increase_size():
    trades = [{"pnl": 1.0}, {"pnl": 1.0}, {"pnl": 1.0}]
    assert calculate_position_size(trades) > BASE_POSITION_USDC

def test_empty_trades_returns_base():
    assert calculate_position_size([]) == BASE_POSITION_USDC
