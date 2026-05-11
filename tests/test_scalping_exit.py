import pytest
from unittest.mock import patch
from datetime import datetime, timezone

from src.logic.scalping_exit import (
    market_price_atr,
    binary_trailing_stop,
    atr_trailing_active,
    net_profit_usdc,
    min_exit_price_for_profit,
    expected_fill_and_slippage,
    liquidity_check,
    volatility_kelly_mult,
    SessionRiskTracker,
    risk_dashboard,
)

def test_market_price_atr_insufficient_data():
    assert market_price_atr([0.5, 0.51], period=14) is None

def test_market_price_atr_constant_prices():
    prices = [0.5] * 20
    val = market_price_atr(prices, period=14)
    assert val is not None
    assert abs(val) < 1e-10

def test_market_price_atr_known_value():
    prices = [0.5 + i * 0.01 for i in range(16)]
    val = market_price_atr(prices, period=14)
    assert val is not None
    assert abs(val - 0.01) < 1e-10

def test_market_price_atr_uses_last_period():
    prices = [0.5 + i * 0.1 for i in range(6)] + [prices_[-1] + i * 0.01
              for i, prices_ in enumerate([[0.5 + 5 * 0.1]] * 15)]
    p = [0.50, 0.60] * 5 + [0.50, 0.51] * 8
    val = market_price_atr(p, period=14)
    assert val is not None and val > 0

def test_binary_trailing_stop_normal():
    stop = binary_trailing_stop(0.70, 0.02, multiplier=2.5)
    assert abs(stop - 0.65) < 1e-4

def test_binary_trailing_stop_near_ceiling_tightens():
    stop_normal = binary_trailing_stop(0.96, 0.02, multiplier=2.5, ceiling=0.95)
    stop_expected = 0.96 - 1.5 * 0.02
    assert abs(stop_normal - stop_expected) < 1e-4

def test_binary_trailing_stop_below_ceiling_uses_full_mult():
    stop = binary_trailing_stop(0.80, 0.02, multiplier=2.5, ceiling=0.95)
    assert abs(stop - (0.80 - 2.5 * 0.02)) < 1e-4

def test_binary_trailing_stop_never_below_floor():
    stop = binary_trailing_stop(0.10, 0.50, multiplier=2.5)
    assert stop >= 0.001

def test_binary_trailing_stop_rounds_to_4dp():
    stop = binary_trailing_stop(0.700001, 0.020001)
    assert len(str(stop).split(".")[-1]) <= 4

def test_atr_trailing_active_below_threshold():
    assert not atr_trailing_active(49.9)

def test_atr_trailing_active_at_threshold():
    assert atr_trailing_active(50.0)

def test_atr_trailing_active_above_threshold():
    assert atr_trailing_active(75.0)

def test_net_profit_zero_entry():
    assert net_profit_usdc(0.0, 0.5, 20.0) == 0.0

def test_net_profit_breakeven_minus_fee():
    net = net_profit_usdc(0.40, 0.40, 20.0, taker_fee_pct=0.018)
    assert net < 0.0

def test_net_profit_profitable_trade():
    net = net_profit_usdc(0.40, 0.60, 20.0, taker_fee_pct=0.018, slippage_usdc=0.0)
    expected = 50 * (0.60 - 0.40) - 50 * 0.60 * 0.018
    assert abs(net - expected) < 1e-4

def test_net_profit_negative_when_losing():
    net = net_profit_usdc(0.40, 0.30, 20.0, taker_fee_pct=0.018)
    assert net < 0

def test_net_profit_slippage_reduces_net():
    net_no_slip = net_profit_usdc(0.40, 0.60, 20.0, taker_fee_pct=0.018, slippage_usdc=0.0)
    net_with_slip = net_profit_usdc(0.40, 0.60, 20.0, taker_fee_pct=0.018, slippage_usdc=1.0)
    assert net_with_slip == net_no_slip - 1.0

def test_min_exit_price_zero_entry():
    assert min_exit_price_for_profit(0.0, 20.0) == 1.0

def test_min_exit_price_breakeven():
    floor = min_exit_price_for_profit(0.40, 20.0, taker_fee_pct=0.018, min_net_profit=0.0)
    net = net_profit_usdc(0.40, floor, 20.0, taker_fee_pct=0.018)
    assert net >= -0.01  # within rounding

def test_min_exit_price_with_target():
    floor = min_exit_price_for_profit(0.40, 20.0, taker_fee_pct=0.018, min_net_profit=1.0)
    net = net_profit_usdc(0.40, floor, 20.0, taker_fee_pct=0.018)
    assert net >= 1.0 - 0.01  # within rounding

def test_min_exit_price_capped_at_0_9999():
    floor = min_exit_price_for_profit(0.01, 0.01, taker_fee_pct=0.018, min_net_profit=1000.0)
    assert floor <= 0.9999

def test_expected_fill_empty_bids():
    fill, slip = expected_fill_and_slippage([], 10.0)
    assert fill == 0.0
    assert slip == 1.0

def test_expected_fill_sufficient_liquidity():
    bids = [(0.45, 100.0)]
    fill, slip = expected_fill_and_slippage(bids, 50.0)
    assert abs(fill - 0.45) < 1e-10
    assert abs(slip - 0.0) < 1e-10

def test_expected_fill_walks_down_book():
    bids = [(0.50, 10.0), (0.48, 10.0), (0.46, 10.0)]
    fill, slip = expected_fill_and_slippage(bids, 30.0)
    expected_fill = (10*0.50 + 10*0.48 + 10*0.46) / 30
    assert abs(fill - round(expected_fill, 4)) < 1e-4
    assert slip > 0  # best bid was 0.50, avg < 0.50

def test_expected_fill_partial_fill_on_thin_book():
    bids = [(0.45, 5.0)]
    fill, slip = expected_fill_and_slippage(bids, 100.0)
    assert abs(fill - 0.45) < 1e-4  # only partial fill at 0.45

def test_expected_fill_slippage_non_negative():
    bids = [(0.50, 50.0), (0.49, 50.0)]
    _, slip = expected_fill_and_slippage(bids, 50.0)
    assert slip >= 0.0

def test_liquidity_check_empty_bids_not_ok():
    result = liquidity_check([], 50.0, entry_price=0.40, capital_usdc=20.0)
    assert not result["ok"]

def test_liquidity_check_deep_book_ok():
    bids = [(0.60, 1000.0)]
    result = liquidity_check(bids, 50.0, entry_price=0.40, capital_usdc=20.0)
    assert result["ok"]
    assert result["net_profit_usdc"] > 0

def test_liquidity_check_thin_book_warns():
    bids = [(0.60, 2.0), (0.30, 2.0), (0.10, 2.0)]
    result = liquidity_check(bids, 50.0, entry_price=0.40, capital_usdc=20.0)
    assert result["warning"] is not None

def test_liquidity_check_slippage_threshold_warn():
    bids = [(0.60, 10.0), (0.40, 90.0)]
    result = liquidity_check(bids, 100.0, entry_price=0.40, capital_usdc=20.0,
                             slippage_warn_threshold=0.03)
    if result["slippage_pct"] > 0.03:
        assert result["warning"] is not None

def test_volatility_kelly_zero_avg():
    assert volatility_kelly_mult(0.01, 0.0) == 1.0

def test_volatility_kelly_normal():
    assert volatility_kelly_mult(0.01, 0.01) == 1.0

def test_volatility_kelly_slightly_elevated():
    assert volatility_kelly_mult(1.4 * 0.01, 0.01) == 1.0

def test_volatility_kelly_moderately_high():
    assert volatility_kelly_mult(1.6 * 0.01, 0.01) == 0.75

def test_volatility_kelly_high():
    assert volatility_kelly_mult(2.5 * 0.01, 0.01) == 0.5

def test_volatility_kelly_extreme_hard_stop():
    assert volatility_kelly_mult(3.1 * 0.01, 0.01) == 0.0

@pytest.mark.parametrize("ratio,expected", [
    (0.5, 1.0), (1.0, 1.0), (1.49, 1.0),
    (1.5, 1.0),    # exactly 1.5 → not > 1.5, so still 1.0
    (1.51, 0.75),
    (2.0, 0.75),   # exactly 2.0 → not > 2.0, so still 0.75
    (2.01, 0.5), (2.99, 0.5),
    (3.0, 0.5),    # exactly 3.0 → not > 3.0, so still 0.5
    (3.01, 0.0),
])
def test_volatility_kelly_boundaries(ratio, expected):
    assert volatility_kelly_mult(ratio * 0.01, 0.01) == expected

def test_session_risk_initial_state():
    t = SessionRiskTracker(starting_equity=120.0)
    assert t.can_trade()
    assert not t.in_cooldown()
    assert t.peak_equity == 120.0

def test_session_risk_no_cooldown_on_small_loss():
    t = SessionRiskTracker(starting_equity=120.0, max_drawdown_pct=0.05)
    triggered = t.update(116.0)  # 3.3% drawdown < 5%
    assert not triggered
    assert t.can_trade()

def test_session_risk_cooldown_triggered_at_threshold():
    t = SessionRiskTracker(starting_equity=120.0, max_drawdown_pct=0.05)
    triggered = t.update(113.9)  # 5.08% drawdown > 5%
    assert triggered
    assert not t.can_trade()

def test_session_risk_peak_updates_on_gain():
    t = SessionRiskTracker(starting_equity=100.0)
    t.update(110.0)
    assert t.peak_equity == 110.0

def test_session_risk_cooldown_not_double_triggered():
    t = SessionRiskTracker(starting_equity=100.0, max_drawdown_pct=0.05)
    t.update(90.0)  # triggers cooldown
    triggered_again = t.update(85.0)  # already in cooldown
    assert not triggered_again

def test_session_risk_reset_cooldown():
    t = SessionRiskTracker(starting_equity=100.0, max_drawdown_pct=0.05)
    t.update(90.0)
    assert not t.can_trade()
    t.reset_cooldown()
    assert t.can_trade()

def test_session_risk_dashboard_keys():
    t = SessionRiskTracker(starting_equity=100.0)
    t.update(95.0)
    d = t.dashboard()
    for key in ("starting_equity", "current_equity", "peak_equity",
                "session_drawdown_pct", "max_drawdown_pct", "cooldown_active", "can_trade"):
        assert key in d

def test_risk_dashboard_empty_on_zero_entry():
    assert risk_dashboard(0, 0.5, 0.5, 20.0, 0.01, 0.01) == {}

def test_risk_dashboard_normal_action():
    d = risk_dashboard(0.40, 0.45, 0.45, 20.0, market_atr=0.005, atr_avg=0.005)
    assert d["action_hint"] == "NORMAL"
    assert not d["trailing_stop_active"]

def test_risk_dashboard_trailing_active_above_50pct():
    d = risk_dashboard(0.40, 0.61, 0.61, 20.0, market_atr=0.005, atr_avg=0.005)
    assert d["trailing_stop_active"]
    assert d["stop_level"] is not None
    assert d["action_hint"] == "ATR_TRAILING_ACTIVE"

def test_risk_dashboard_hard_stop_on_extreme_volatility():
    d = risk_dashboard(0.40, 0.45, 0.45, 20.0, market_atr=0.04, atr_avg=0.01)
    assert d["action_hint"] == "HARD_STOP_VOLATILITY"
    assert d["kelly_multiplier"] == 0.0

def test_risk_dashboard_half_size_on_high_vol():
    d = risk_dashboard(0.40, 0.45, 0.45, 20.0, market_atr=0.022, atr_avg=0.01)
    assert d["action_hint"] == "HALF_SIZE"
    assert d["kelly_multiplier"] == 0.5

