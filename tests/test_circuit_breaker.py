import tempfile
import pytest
from hypothesis import given, settings, strategies as st
from hypothesis import HealthCheck

from src.logic.circuit_breaker import CircuitBreaker


@pytest.fixture
def cb(tmp_path, monkeypatch):
    state_file = tmp_path / "circuit_breaker.json"
    monkeypatch.setattr("src.logic.circuit_breaker.STATE_FILE", state_file)
    return CircuitBreaker(starting_capital=100.0)


def test_initial_state_allows_trade(cb):
    assert cb.check().can_trade

def test_saklar_1_daily_loss_limit(cb):
    cb.record_trade(-6.0)
    cb.record_trade(-6.0)
    status = cb.check()
    assert not status.can_trade
    assert status.saklar_1_triggered

def test_saklar_2_consecutive_losses(cb):
    cb.record_trade(-1.0)
    cb.record_trade(-1.0)
    cb.record_trade(-1.0)
    status = cb.check()
    assert not status.can_trade
    assert status.saklar_2_triggered

def test_saklar_3_drawdown(cb):
    cb.state.current_capital = 69.0
    status = cb.check()
    assert not status.can_trade
    assert status.saklar_3_triggered

def test_win_resets_consecutive_loss_streak(cb):
    cb.record_trade(-1.0)
    cb.record_trade(-1.0)
    cb.record_trade(5.0)
    assert cb.state.consecutive_losses == 0

def test_reset_consecutive_allows_trade(cb):
    cb.record_trade(-1.0)
    cb.record_trade(-1.0)
    cb.record_trade(-1.0)
    cb.reset_consecutive("test")
    assert cb.check().can_trade

def test_capital_never_below_zero(cb):
    cb.record_trade(-9999.0)
    assert cb.state.current_capital >= 0.0

def test_safety_vol_extreme_halts(cb):
    status = cb.check_safety_thresholds(current_vol=1.10, daily_drawdown=0.0)
    assert status.halt_new_entries
    assert status.trigger == "vol_extreme"

def test_safety_drawdown_halts(cb):
    status = cb.check_safety_thresholds(current_vol=0.40, daily_drawdown=-0.20)
    assert status.halt_new_entries
    assert status.trigger == "drawdown_daily"

def test_safety_normal_does_not_halt(cb):
    status = cb.check_safety_thresholds(current_vol=0.50, daily_drawdown=-0.05)
    assert not status.halt_new_entries

@given(pnl=st.floats(-10_000.0, 10_000.0, allow_nan=False, allow_infinity=False))
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_record_trade_never_corrupts_capital(pnl, monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        from pathlib import Path
        state_file = Path(tmpdir) / "cb.json"
        monkeypatch.setattr("src.logic.circuit_breaker.STATE_FILE", state_file)
        breaker = CircuitBreaker(starting_capital=100.0)
        breaker.record_trade(pnl)
        assert breaker.state.current_capital >= 0.0
        assert isinstance(breaker.state.consecutive_losses, int)
        assert breaker.state.consecutive_losses >= 0

@given(
    current_vol=st.floats(0.0, 5.0, allow_nan=False, allow_infinity=False),
    daily_drawdown=st.floats(-1.0, 0.0, allow_nan=False, allow_infinity=False),
)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_safety_thresholds_never_crash(current_vol, daily_drawdown, tmp_path, monkeypatch):
    state_file = tmp_path / "cb_safety.json"
    monkeypatch.setattr("src.logic.circuit_breaker.STATE_FILE", state_file)
    breaker = CircuitBreaker(starting_capital=100.0)
    status = breaker.check_safety_thresholds(current_vol, daily_drawdown)
    assert isinstance(status.halt_new_entries, bool)
