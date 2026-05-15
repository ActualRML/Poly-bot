import logging
from typing import Optional

logger = logging.getLogger(__name__)

MIN_STOP_FRACTION: float    = 0.05
MAX_STOP_FRACTION: float    = 0.45

MIN_POSITION_USDC: float    = 10.0
MAX_POSITION_USDC: float    = 75.0
BASE_POSITION_USDC: float   = 30.0

def vol_size_multiplier(vol_state: str) -> float:
    """Size multiplier based on market volatility regime."""
    return {
        "EXTREME_HIGH": 0.50,
        "HIGH":         0.75,
        "NORMAL":       1.00,
        "LOW":          1.10,
        "EXTREME_LOW":  1.20,
    }.get(vol_state, 1.00)


def get_dynamic_stop_loss(
    current_P: float,
    vol_annual: Optional[float] = None,
) -> float:
    base_stop = current_P * (1.0 - current_P) * 2.0

    if vol_annual is not None:
        vol_scale = max(0.80, min(1.20, vol_annual / 0.40))
        base_stop *= vol_scale

    result = max(MIN_STOP_FRACTION, min(MAX_STOP_FRACTION, base_stop))

    logger.debug(
        f"[DYNAMIC STOP] P={current_P:.3f} "
        f"→ base={current_P*(1-current_P)*2:.3f} "
        f"→ final={result:.3f} ({result:.0%})"
    )
    return result

def calculate_position_size(
    last_5_trades: list[dict],
    capital: float = 0.0,
    base_size: float = BASE_POSITION_USDC,
) -> float:
    from src.utils.config import config
    _base_pct = getattr(config, "RISK_BASE_SIZE_PCT", 0.15)
    _min_pct  = getattr(config, "RISK_MIN_SIZE_PCT",  0.08)
    _max_pct  = getattr(config, "RISK_MAX_SIZE_PCT",  0.20)

    if capital > 0:
        base_size = max(MIN_POSITION_USDC, capital * _base_pct)

    if not last_5_trades:
        return base_size

    consecutive_wins   = 0
    consecutive_losses = 0

    for trade in reversed(last_5_trades):
        pnl = float(trade.get("pnl", 0))
        if pnl > 0:
            if consecutive_losses > 0:
                break
            consecutive_wins += 1
        else:
            if consecutive_wins > 0:
                break
            consecutive_losses += 1

    if consecutive_losses >= 2:
        size = max(MIN_POSITION_USDC, capital * _min_pct) if capital > 0 else MIN_POSITION_USDC
        logger.info(
            f"[RISK] {consecutive_losses} consecutive losses "
            f"→ posisi turun ke ${size:.1f}"
        )
        return size

    if consecutive_wins >= 3:
        if capital > 0:
            new_size = max(MIN_POSITION_USDC, capital * _max_pct)
        else:
            bonus    = min((consecutive_wins - 2) * 5.0, MAX_POSITION_USDC - base_size)
            new_size = min(base_size + bonus, MAX_POSITION_USDC)
        logger.info(
            f"[RISK] {consecutive_wins} consecutive wins "
            f"→ posisi naik ke ${new_size:.1f}"
        )
        return new_size

    return base_size
