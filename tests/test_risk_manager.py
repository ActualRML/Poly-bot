"""
Tests for fixed-fractional per-symbol sizing (2026-05-23 refactor).

Old streak-heuristic tests removed — see git history (1c185c0) for the
loss-streak / win-streak behavior. New sizing:
  size = clamp(capital * 0.05 * SYMBOL_SIZE_MULT[sym], MIN, MAX)
"""
from src.risk.manager import (
    calculate_position_size,
    MIN_POSITION_USDC,
    MAX_POSITION_USDC,
    BASE_POSITION_USDC,
    BASE_SIZE_PCT,
    SYMBOL_SIZE_MULT,
    DEFAULT_SYMBOL_MULT,
)


def test_constants_sanity():
    assert BASE_SIZE_PCT == 0.08
    assert SYMBOL_SIZE_MULT["BTC"] == 1.0
    assert SYMBOL_SIZE_MULT["XRP"] == 0.3
    assert DEFAULT_SYMBOL_MULT == 0.5


def test_zero_capital_returns_base_fallback():
    assert calculate_position_size([], capital=0.0, symbol="BTC") == BASE_POSITION_USDC


def test_btc_larger_than_xrp_at_mid_capital():
    # Cap chosen so both symbols are above MIN floor and below MAX cap.
    # cap=500, BASE=0.08 → BTC=40 (8%×1.0), XRP=12 (8%×0.3); both within [3, 75].
    cap = 500.0
    btc = calculate_position_size([], capital=cap, symbol="BTC")
    xrp = calculate_position_size([], capital=cap, symbol="XRP")
    assert btc > xrp
    assert btc == cap * BASE_SIZE_PCT * 1.0  # = 40
    assert xrp == cap * BASE_SIZE_PCT * 0.3  # = 12


def test_min_floor_only_xrp_at_saldo_awal():
    """At SALDO_AWAL=$120 with BASE=0.08 + MIN=$3, only XRP clamps to floor."""
    expected = {
        "BTC":  9.60,   # 120 * 0.08 * 1.0
        "ETH":  5.76,   # 120 * 0.08 * 0.6
        "SOL":  5.76,
        "BNB":  4.80,   # 120 * 0.08 * 0.5
        "DOGE": 3.84,   # 120 * 0.08 * 0.4
        "XRP":  3.00,   # 120 * 0.08 * 0.3 = 2.88 → clamped to MIN=3.0
    }
    for sym, want in expected.items():
        got = calculate_position_size([], capital=120.0, symbol=sym)
        assert abs(got - want) < 0.01, f"{sym} got ${got:.2f}, expected ${want:.2f}"
    # XRP must be at the floor; BTC must NOT be at the floor
    assert calculate_position_size([], capital=120.0, symbol="XRP") == MIN_POSITION_USDC
    assert calculate_position_size([], capital=120.0, symbol="BTC") > MIN_POSITION_USDC


def test_max_cap_at_huge_capital():
    huge = 100_000.0
    btc = calculate_position_size([], capital=huge, symbol="BTC")
    assert btc == MAX_POSITION_USDC


def test_unknown_symbol_uses_default_mult():
    # cap=500, BASE=0.08, DEFAULT=0.5 → 20.0 (within [3, 75])
    cap = 500.0
    size = calculate_position_size([], capital=cap, symbol="FOO")
    assert size == cap * BASE_SIZE_PCT * DEFAULT_SYMBOL_MULT


def test_none_symbol_uses_default_mult():
    cap = 500.0
    size = calculate_position_size([], capital=cap, symbol=None)
    assert size == cap * BASE_SIZE_PCT * DEFAULT_SYMBOL_MULT


def test_legacy_args_ignored():
    """last_5_trades and winrate are accepted but ignored — sizing is deterministic."""
    cap = 2000.0
    sym = "ETH"
    base = calculate_position_size([], capital=cap, symbol=sym)
    with_loss_streak = calculate_position_size(
        [{"pnl": -1.0}, {"pnl": -1.0}], capital=cap, symbol=sym
    )
    with_win_streak = calculate_position_size(
        [{"pnl": 1.0}] * 5, capital=cap, symbol=sym, winrate=0.9
    )
    assert base == with_loss_streak == with_win_streak


def test_symbol_case_insensitive():
    cap = 2000.0
    upper = calculate_position_size([], capital=cap, symbol="BTC")
    lower = calculate_position_size([], capital=cap, symbol="btc")
    mixed = calculate_position_size([], capital=cap, symbol="Btc")
    assert upper == lower == mixed
