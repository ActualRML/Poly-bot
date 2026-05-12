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

def make_hourly_position(strategy_mode: str, current: float, highest: float, minutes_left: int = 15, entry_age_minutes: float = 25.0) -> Position:
    return Position(
        condition_id="0xHOURLY",
        outcome="Up",
        entry_price=Decimal("0.45"),
        current_price=Decimal(str(current)),
        highest_price=Decimal(str(max(highest, current))),
        shares=Decimal("22.22"),
        capital_at_risk=Decimal("10.00"),
        resolve_date=datetime.now(timezone.utc) + timedelta(minutes=minutes_left),
        entry_time=datetime.now(timezone.utc) - timedelta(minutes=entry_age_minutes),
        strategy_mode=strategy_mode,
    )

@pytest.mark.parametrize("strategy_mode", ["updown_hourly", "updown_hourly_dry_run"])
def test_updown_hourly_no_early_sl_outside_t4_window(strategy_mode):
    # 42m left = outside T4 window (>40m) → no SL regardless of loss
    pos = make_hourly_position(strategy_mode, current=0.10, highest=0.45, minutes_left=42)
    d = evaluator.evaluate(pos)
    assert d.should_exit is False
    assert d.signal == ExitSignal.HOLD

@pytest.mark.parametrize("strategy_mode", ["updown_hourly", "updown_hourly_dry_run"])
def test_updown_hourly_late_sl_early_fires_on_deep_loss(strategy_mode):
    # entry=0.45 current=0.22 → PnL=-51% ≤ -45%, 30m left, age=25m → T4 fires
    pos = make_hourly_position(strategy_mode, current=0.22, highest=0.45, minutes_left=30)
    d = evaluator.evaluate(pos)
    assert d.should_exit is True
    assert d.signal == ExitSignal.EXIT_CATASTROPHIC

@pytest.mark.parametrize("strategy_mode", ["updown_hourly", "updown_hourly_dry_run"])
def test_updown_hourly_late_sl_early_holds_on_moderate_loss(strategy_mode):
    # entry=0.45 current=0.30 → PnL=-33% > -45% → T4 does NOT fire, HOLD
    pos = make_hourly_position(strategy_mode, current=0.30, highest=0.45, minutes_left=30)
    d = evaluator.evaluate(pos)
    assert d.should_exit is False

@pytest.mark.parametrize("strategy_mode", ["updown_hourly", "updown_hourly_dry_run"])
def test_updown_hourly_late_sl_early_blocked_by_age_guard(strategy_mode):
    # entry=0.45 current=0.10 → PnL=-78%, 30m left, but position only 3m old → T4 blocked
    pos = make_hourly_position(strategy_mode, current=0.10, highest=0.45, minutes_left=30, entry_age_minutes=3.0)
    d = evaluator.evaluate(pos)
    assert d.should_exit is False

@pytest.mark.parametrize("strategy_mode", ["updown_hourly", "updown_hourly_dry_run"])
def test_updown_hourly_late_sl_early_boundary_at_40m(strategy_mode):
    # entry=0.45 current=0.22 → PnL=-51%, exactly 40m left (upper bound of T4) → fires
    pos = make_hourly_position(strategy_mode, current=0.22, highest=0.45, minutes_left=40)
    d = evaluator.evaluate(pos)
    assert d.should_exit is True
    assert d.signal == ExitSignal.EXIT_CATASTROPHIC

@pytest.mark.parametrize("strategy_mode", ["updown_hourly", "updown_hourly_dry_run"])
def test_updown_hourly_late_sl_outer_fires_on_small_loss(strategy_mode):
    pos = make_hourly_position(strategy_mode, current=0.30, highest=0.45, minutes_left=18)
    d = evaluator.evaluate(pos)
    assert d.should_exit is True
    assert d.signal == ExitSignal.EXIT_CATASTROPHIC

@pytest.mark.parametrize("strategy_mode", ["updown_hourly", "updown_hourly_dry_run"])
def test_updown_hourly_late_sl_middle_fires_only_on_moderate_loss(strategy_mode):
    pos = make_hourly_position(strategy_mode, current=0.20, highest=0.45, minutes_left=8)
    d = evaluator.evaluate(pos)
    assert d.should_exit is True
    assert d.signal == ExitSignal.EXIT_CATASTROPHIC

@pytest.mark.parametrize("strategy_mode", ["updown_hourly", "updown_hourly_dry_run"])
def test_updown_hourly_late_sl_middle_holds_on_small_loss(strategy_mode):
    pos = make_hourly_position(strategy_mode, current=0.30, highest=0.45, minutes_left=7)
    d = evaluator.evaluate(pos)
    assert d.should_exit is False

@pytest.mark.parametrize("strategy_mode", ["updown_hourly", "updown_hourly_dry_run"])
def test_updown_hourly_late_sl_inner_fires_only_on_extreme(strategy_mode):
    pos = make_hourly_position(strategy_mode, current=0.10, highest=0.45, minutes_left=4)
    d = evaluator.evaluate(pos)
    assert d.should_exit is True
    assert d.signal == ExitSignal.EXIT_CATASTROPHIC

@pytest.mark.parametrize("strategy_mode", ["updown_hourly", "updown_hourly_dry_run"])
def test_updown_hourly_late_sl_inner_holds_on_moderate_loss(strategy_mode):
    pos = make_hourly_position(strategy_mode, current=0.20, highest=0.45, minutes_left=3)
    d = evaluator.evaluate(pos)
    assert d.should_exit is False

@pytest.mark.parametrize("strategy_mode", ["updown_hourly", "updown_hourly_dry_run"])
def test_updown_hourly_tp_t1_fires_at_near_expiry(strategy_mode):
    # entry=0.45 current=0.88 → PnL=95.6% > T1(80%) with 15m > 5m gate → T1 fires
    pos = make_hourly_position(strategy_mode, current=0.88, highest=0.95, minutes_left=15)
    d = evaluator.evaluate(pos)
    assert d.should_exit is True
    assert d.signal == ExitSignal.EXIT_LOCK_PROFIT

@pytest.mark.parametrize("strategy_mode", ["updown_hourly", "updown_hourly_dry_run"])
def test_updown_hourly_no_fixed_profit_lock(strategy_mode):
    pos = make_hourly_position(strategy_mode, current=0.60, highest=0.60, minutes_left=35)
    d = evaluator.evaluate(pos)
    assert d.should_exit is False
    assert d.signal == ExitSignal.HOLD

@pytest.mark.parametrize("strategy_mode", ["updown_hourly", "updown_hourly_dry_run"])
def test_updown_hourly_holds_below_tp_t2_threshold(strategy_mode):
    # entry=0.45 current=0.61 → PnL=35.6% < T2(50%) → HOLD
    pos = make_hourly_position(strategy_mode, current=0.61, highest=0.61, minutes_left=25)
    d = evaluator.evaluate(pos)
    assert d.should_exit is False
    assert d.signal == ExitSignal.HOLD

@pytest.mark.parametrize("strategy_mode", ["updown_hourly", "updown_hourly_dry_run"])
def test_updown_hourly_tp_t2_fires_above_threshold(strategy_mode):
    # entry=0.45 current=0.70 → PnL=55.6% > T2(50%) with 20m > 15m gate → T2 fires
    pos = make_hourly_position(strategy_mode, current=0.70, highest=0.70, minutes_left=20)
    d = evaluator.evaluate(pos)
    assert d.should_exit is True
    assert d.signal == ExitSignal.EXIT_LOCK_PROFIT

@pytest.mark.parametrize("strategy_mode", ["updown_hourly", "updown_hourly_dry_run"])
def test_updown_hourly_holds_under_trailing_threshold(strategy_mode):
    pos = make_hourly_position(strategy_mode, current=0.55, highest=0.55, minutes_left=35)
    d = evaluator.evaluate(pos)
    assert d.should_exit is False
    assert d.signal == ExitSignal.HOLD

def test_non_hourly_strategy_still_applies_trailing_stop():
    pos = make_hourly_position("updown_daily_dry_run", current=0.10, highest=0.45)
    d = evaluator.evaluate(pos)
    assert d.should_exit is True
    assert d.signal == ExitSignal.EXIT_TRAILING

def test_empty_strategy_mode_still_applies_trailing_stop():
    pos = make_hourly_position("", current=0.10, highest=0.45)
    d = evaluator.evaluate(pos)
    assert d.should_exit is True

@pytest.mark.parametrize("strategy_mode", ["updown_hourly", "updown_hourly_dry_run"])
def test_hourly_always_holds_regardless_of_pnl(strategy_mode):
    pos = make_hourly_position(strategy_mode, current=0.50625, highest=0.5625, minutes_left=35)
    d = evaluator.evaluate(pos)
    assert d.should_exit is False
    assert d.signal == ExitSignal.HOLD

@pytest.mark.parametrize("strategy_mode", ["updown_hourly", "updown_hourly_dry_run"])
def test_hourly_holds_below_tp_threshold(strategy_mode):
    # entry=0.45 current=0.61 → PnL=35.6% < T2(50%) → HOLD
    pos = make_hourly_position(strategy_mode, current=0.61, highest=0.61, minutes_left=20)
    d = evaluator.evaluate(pos)
    assert d.should_exit is False
    assert d.signal == ExitSignal.HOLD

@pytest.mark.parametrize("strategy_mode", ["updown_hourly", "updown_hourly_dry_run"])
def test_hourly_holds_when_losing_below_t4_threshold(strategy_mode):
    # entry=0.45 current=0.28 → PnL=-38%, 35m left → below T4(-45%) threshold → HOLD
    pos = make_hourly_position(strategy_mode, current=0.28, highest=0.45, minutes_left=35)
    d = evaluator.evaluate(pos)
    assert d.should_exit is False
    assert d.signal == ExitSignal.HOLD


# --- SL T3 min-age guard ---

@pytest.mark.parametrize("strategy_mode", ["updown_hourly", "updown_hourly_dry_run"])
def test_late_sl_t3_blocked_for_new_position(strategy_mode):
    # Late entry: position only 3m old, -35% PnL, 18m left → T3 should NOT fire (age < 10m)
    pos = make_hourly_position(strategy_mode, current=0.29, highest=0.45, minutes_left=18, entry_age_minutes=3.0)
    d = evaluator.evaluate(pos)
    assert d.should_exit is False
    assert d.signal == ExitSignal.HOLD

@pytest.mark.parametrize("strategy_mode", ["updown_hourly", "updown_hourly_dry_run"])
def test_late_sl_t3_fires_when_position_old_enough(strategy_mode):
    # Position 10m old, -35% PnL, 14m left → T3 fires (age >= 10m)
    pos = make_hourly_position(strategy_mode, current=0.29, highest=0.45, minutes_left=14, entry_age_minutes=10.0)
    d = evaluator.evaluate(pos)
    assert d.should_exit is True
    assert d.signal == ExitSignal.EXIT_CATASTROPHIC

@pytest.mark.parametrize("strategy_mode", ["updown_hourly", "updown_hourly_dry_run"])
def test_late_sl_t3_fires_normally_for_early_entry(strategy_mode):
    # Early entry: position 30m old, -35% PnL, 18m left → T3 fires as before (not affected by fix)
    pos = make_hourly_position(strategy_mode, current=0.29, highest=0.45, minutes_left=18, entry_age_minutes=30.0)
    d = evaluator.evaluate(pos)
    assert d.should_exit is True
    assert d.signal == ExitSignal.EXIT_CATASTROPHIC

