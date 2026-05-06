from decimal import Decimal
from datetime import datetime, timezone, timedelta
import pytest
from hypothesis import given, strategies as st

from src.logic.exit_strategy import ExitEvaluator, ExitSignal, Position

evaluator = ExitEvaluator()


def make_position(entry: float, current: float, highest: float, days_resolve: int, days_held: int) -> Position:
    return Position(
        condition_id="0xTEST",
        outcome="Yes",
        entry_price=Decimal(str(entry)),
        current_price=Decimal(str(current)),
        highest_price=Decimal(str(max(highest, current))),
        shares=Decimal("10"),
        capital_at_risk=Decimal(str(round(entry * 10, 4))),
        resolve_date=datetime.now(timezone.utc) + timedelta(days=days_resolve),
        entry_time=datetime.now(timezone.utc) - timedelta(days=days_held),
    )


@given(
    entry=st.floats(0.0001, 0.9990, allow_nan=False, allow_infinity=False),
    current=st.floats(0.0001, 0.9999, allow_nan=False, allow_infinity=False),
    highest=st.floats(0.0001, 0.9999, allow_nan=False, allow_infinity=False),
    days_resolve=st.integers(0, 365),
    days_held=st.integers(0, 365),
)
def test_evaluate_never_crashes(entry, current, highest, days_resolve, days_held):
    pos = make_position(entry, current, highest, days_resolve, days_held)
    decision = evaluator.evaluate(pos)
    assert decision.signal in ExitSignal
    assert isinstance(decision.should_exit, bool)

@given(
    entry=st.floats(0.0001, 0.9990, allow_nan=False, allow_infinity=False),
    current=st.floats(0.0001, 0.9999, allow_nan=False, allow_infinity=False),
    highest=st.floats(0.0001, 0.9999, allow_nan=False, allow_infinity=False),
    days_resolve=st.integers(0, 365),
    days_held=st.integers(0, 365),
)
def test_should_exit_is_bool(entry, current, highest, days_resolve, days_held):
    pos = make_position(entry, current, highest, days_resolve, days_held)
    assert evaluator.evaluate(pos).should_exit in (True, False)

def test_trailing_stop_triggers_exit():
    pos = make_position(entry=0.45, current=0.50, highest=0.80, days_resolve=10, days_held=5)
    assert evaluator.evaluate(pos).should_exit

def test_hold_to_resolve_when_near_expiry():
    pos = make_position(entry=0.45, current=0.92, highest=0.92, days_resolve=2, days_held=5)
    d = evaluator.evaluate(pos)
    assert d.signal == ExitSignal.HOLD_TO_RESOLVE
    assert not d.should_exit

def test_tight_stop_locks_profit():
    pos = make_position(entry=0.45, current=0.86, highest=0.95, days_resolve=10, days_held=5)
    d = evaluator.evaluate(pos)
    assert d.should_exit
    assert d.signal == ExitSignal.EXIT_LOCK_PROFIT

def test_hold_when_no_trigger():
    pos = make_position(entry=0.45, current=0.62, highest=0.65, days_resolve=15, days_held=5)
    d = evaluator.evaluate(pos)
    assert not d.should_exit
    assert d.signal == ExitSignal.HOLD


# ── updown_hourly trailing stop skip ─────────────────────────────────────────

def make_hourly_position(strategy_mode: str, current: float, highest: float) -> Position:
    return Position(
        condition_id="0xHOURLY",
        outcome="Up",
        entry_price=Decimal("0.45"),
        current_price=Decimal(str(current)),
        highest_price=Decimal(str(max(highest, current))),
        shares=Decimal("22.22"),
        capital_at_risk=Decimal("10.00"),
        resolve_date=datetime.now(timezone.utc) + timedelta(minutes=30),
        entry_time=datetime.now(timezone.utc) - timedelta(minutes=25),
        strategy_mode=strategy_mode,
    )


@pytest.mark.parametrize("strategy_mode", ["updown_hourly", "updown_hourly_dry_run"])
def test_updown_hourly_never_exits_trailing_stop(strategy_mode):
    # Harga crash jauh di bawah trailing stop threshold — seharusnya tetap HOLD
    pos = make_hourly_position(strategy_mode, current=0.10, highest=0.45)
    d = evaluator.evaluate(pos)
    assert d.should_exit is False
    assert d.signal == ExitSignal.HOLD


@pytest.mark.parametrize("strategy_mode", ["updown_hourly", "updown_hourly_dry_run"])
def test_updown_hourly_never_exits_even_near_zero(strategy_mode):
    pos = make_hourly_position(strategy_mode, current=0.01, highest=0.45)
    d = evaluator.evaluate(pos)
    assert d.should_exit is False


@pytest.mark.parametrize("strategy_mode", ["updown_hourly", "updown_hourly_dry_run"])
def test_updown_hourly_never_exits_tight_stop_zone(strategy_mode):
    # Masuk profit zone (current > 0.85) tapi tetap HOLD untuk hourly
    pos = make_hourly_position(strategy_mode, current=0.88, highest=0.95)
    d = evaluator.evaluate(pos)
    assert d.should_exit is False
    assert d.signal == ExitSignal.HOLD


def test_non_hourly_strategy_still_applies_trailing_stop():
    # Pastikan strategy lain tidak ikut kena disable
    pos = make_hourly_position("updown_daily_dry_run", current=0.10, highest=0.45)
    d = evaluator.evaluate(pos)
    assert d.should_exit is True
    assert d.signal == ExitSignal.EXIT_TRAILING


def test_empty_strategy_mode_still_applies_trailing_stop():
    pos = make_hourly_position("", current=0.10, highest=0.45)
    d = evaluator.evaluate(pos)
    assert d.should_exit is True
