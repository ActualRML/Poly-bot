from decimal import Decimal
import pytest
from hypothesis import given, strategies as st

from src.logic.pricing import (
    ke_decimal,
    validasi_harga,
    hitung_midpoint,
    tambah_tick,
    hitung_spread,
    hitung_pnl_realisasi,
    HARGA_MINIMUM,
    HARGA_MAKSIMUM,
    TICK_SIZE,
)

@given(val=st.floats(allow_nan=False, allow_infinity=False, min_value=-1e10, max_value=1e10))
def test_ke_decimal_from_float_is_decimal(val):
    assert isinstance(ke_decimal(val), Decimal)

@given(val=st.integers(-1_000_000, 1_000_000))
def test_ke_decimal_from_int_is_decimal(val):
    assert isinstance(ke_decimal(val), Decimal)

@given(
    price=st.decimals(
        min_value=Decimal("0.0001"),
        max_value=Decimal("0.9999"),
        places=4,
        allow_nan=False,
        allow_infinity=False,
    ),
    ticks=st.integers(1, 20),
)
def test_tambah_tick_up_stays_in_bounds(price, ticks):
    result = tambah_tick(price, ticks)
    assert HARGA_MINIMUM <= result <= HARGA_MAKSIMUM

@given(
    price=st.decimals(
        min_value=Decimal("0.0001"),
        max_value=Decimal("0.9999"),
        places=4,
        allow_nan=False,
        allow_infinity=False,
    ),
    ticks=st.integers(-20, -1),
)
def test_tambah_tick_down_stays_in_bounds(price, ticks):
    result = tambah_tick(price, ticks)
    assert HARGA_MINIMUM <= result <= HARGA_MAKSIMUM

@given(
    bid=st.decimals(
        min_value=Decimal("0.0001"),
        max_value=Decimal("0.9998"),
        places=4,
        allow_nan=False,
        allow_infinity=False,
    ),
    gap=st.decimals(
        min_value=Decimal("0.0001"),
        max_value=Decimal("0.0010"),
        places=4,
        allow_nan=False,
        allow_infinity=False,
    ),
)
def test_midpoint_between_bid_and_ask(bid, gap):
    ask = min(bid + gap, HARGA_MAKSIMUM)
    if bid < ask:
        mid = hitung_midpoint(bid, ask)
        assert bid <= mid <= ask

@given(
    buy=st.decimals(min_value=Decimal("0.0001"), max_value=Decimal("0.9999"), places=4, allow_nan=False, allow_infinity=False),
    sell=st.decimals(min_value=Decimal("0.0001"), max_value=Decimal("0.9999"), places=4, allow_nan=False, allow_infinity=False),
    qty=st.decimals(min_value=Decimal("0.0001"), max_value=Decimal("10000"), places=4, allow_nan=False, allow_infinity=False),
)
def test_pnl_sign_matches_direction(buy, sell, qty):
    pnl = hitung_pnl_realisasi(buy, sell, qty)
    if sell > buy:
        assert pnl > 0
    elif sell < buy:
        assert pnl < 0
    else:
        assert pnl == 0

def test_spread_raises_on_inverted():
    with pytest.raises(ValueError):
        hitung_spread(Decimal("0.60"), Decimal("0.50"))
