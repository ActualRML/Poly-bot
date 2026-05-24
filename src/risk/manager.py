import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)

MIN_STOP_FRACTION: float    = 0.05
MAX_STOP_FRACTION: float    = 0.45

MIN_POSITION_USDC: float    = 3.0
MAX_POSITION_USDC: float    = 75.0
# Dead-code fallback: only returned by calculate_position_size when capital<=0.
# Live path always has capital>0, so this value never reaches a real order.
BASE_POSITION_USDC: float   = 30.0


@dataclass
class SizingResult:
    """Minimal sizing result for the live entry path (replaces KellyResult).
    Field names match the legacy KellyResult shape so downstream code that
    reads ctx.kelly.bet_usdc / .shares / .bet_fraction / .expected_value
    keeps working without changes."""
    bet_usdc: Decimal
    shares: Decimal
    bet_fraction: Decimal
    expected_value: Decimal

# Interim sizing (2026-05-23 refactor): fixed-fractional per-symbol.
# Replaces 5-trade streak heuristic + Kelly half-bet. Deterministic.
BASE_SIZE_PCT: float = 0.08  # 8% of capital per trade (pre-multiplier)

SYMBOL_SIZE_MULT: dict[str, float] = {
    "BTC":  1.0,
    "ETH":  0.6,
    "SOL":  0.6,
    "DOGE": 0.4,
    "XRP":  0.3,
    "BNB":  0.5,
}
DEFAULT_SYMBOL_MULT: float = 0.5  # fallback for unknown symbols


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
    last_5_trades: Optional[list] = None,
    capital: float = 0.0,
    base_size: float = BASE_POSITION_USDC,
    symbol: Optional[str] = None,
    winrate: Optional[float] = None,
) -> float:
    """
    Fixed-fractional per-symbol sizing (2026-05-23 refactor).

    size = clamp(capital * BASE_SIZE_PCT * SYMBOL_SIZE_MULT[symbol],
                 MIN_POSITION_USDC, MAX_POSITION_USDC)

    `last_5_trades` and `winrate` are accepted for backward compatibility
    but ignored — sizing is deterministic per (capital, symbol).
    """
    if capital <= 0:
        return base_size

    sym = (symbol or "").upper()
    mult = SYMBOL_SIZE_MULT.get(sym, DEFAULT_SYMBOL_MULT)
    raw = capital * BASE_SIZE_PCT * mult
    sized = max(MIN_POSITION_USDC, min(MAX_POSITION_USDC, raw))
    logger.debug(
        f"[SIZE] {sym or '?'} cap=${capital:.2f} mult={mult:.2f} "
        f"raw=${raw:.2f} sized=${sized:.2f}"
    )
    return sized
