from decimal import Decimal
import pytest
from hypothesis import given, settings, strategies as st

from src.logic.kelly import KellySizer

sizer = KellySizer()

@given(
    winrate=st.floats(0.0, 1.0, allow_nan=False, allow_infinity=False),
    market_price=st.floats(0.0001, 0.9999, allow_nan=False, allow_infinity=False),
    capital=st.floats(0.01, 10_000.0, allow_nan=False, allow_infinity=False),
)
def test_bet_never_exceeds_capital_cap(winrate, market_price, capital):
    r = sizer.calculate(winrate, market_price, capital)
    max_allowed = capital * float(sizer.max_fraction)
    assert float(r.bet_usdc) <= max_allowed + 0.01

@given(
    winrate=st.floats(0.0, 1.0, allow_nan=False, allow_infinity=False),
    market_price=st.floats(0.0001, 0.9999, allow_nan=False, allow_infinity=False),
    capital=st.floats(0.0, 10_000.0, allow_nan=False, allow_infinity=False),
)
def test_bet_never_negative(winrate, market_price, capital):
    r = sizer.calculate(winrate, market_price, capital)
    assert r.bet_usdc >= Decimal("0")

@given(
    market_price=st.floats(0.0001, 0.9999, allow_nan=False, allow_infinity=False),
    capital=st.floats(0.01, 10_000.0, allow_nan=False, allow_infinity=False),
)
def test_bet_zero_when_winrate_below_min(market_price, capital):
    r = sizer.calculate(0.40, market_price, capital)
    assert r.bet_usdc == Decimal("0")

def test_bet_zero_when_capital_zero():
    r = sizer.calculate(0.70, 0.45, 0.0)
    assert r.bet_usdc == Decimal("0")

def test_bet_zero_when_negative_ev():
    r = sizer.calculate(0.40, 0.80, 100.0)
    assert r.bet_usdc == Decimal("0")
    assert not r.is_positive_ev

def test_strong_edge_produces_bet():
    r = sizer.calculate(0.70, 0.40, 100.0)
    assert r.bet_usdc > Decimal("0")
    assert r.is_positive_ev

def test_bet_capped_at_max_fraction():
    r = sizer.calculate(0.99, 0.01, 1000.0)
    assert float(r.bet_usdc) <= 1000.0 * float(sizer.max_fraction) + 0.01
