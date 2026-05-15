from src.risk.manager import calculate_position_size, MIN_POSITION_USDC

W = {"pnl": 1.0}
L = {"pnl": -1.0}


def test_no_trades_no_capital():
    assert calculate_position_size([]) == 30.0


def test_no_trades_with_capital():
    size = calculate_position_size([], capital=70.0)
    assert abs(size - 17.5) < 0.01


def test_loss_streak_with_capital():
    size = calculate_position_size([L, L], capital=70.0)
    assert size == 10.0


def test_win_streak_with_capital():
    size = calculate_position_size([W, W, W], capital=70.0)
    assert abs(size - 28.0) < 0.01


def test_min_floor_tiny_capital():
    size = calculate_position_size([L, L], capital=10.0)
    assert size == MIN_POSITION_USDC


def test_mixed_streak_no_streak():
    size = calculate_position_size([W, L], capital=70.0)
    assert abs(size - 17.5) < 0.01


def test_capital_zero_fallback_absolute():
    size = calculate_position_size([L, L], capital=0.0)
    assert size == MIN_POSITION_USDC
