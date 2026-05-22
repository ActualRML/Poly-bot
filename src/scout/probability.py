"""
INTERIM STATE (2026-05-23 refactor):
Probability model flat 0.50. SCORE_TO_PROB lookup dropped karena
anti-calibrated di high-conviction bins (predicted 0.75 -> realized 0.21
dari 151-trade sample). Re-evaluate setelah >=500 trade di mode ini.

Signature kept backward compatible: callers may pass any args, output is
always (0.50, breakdown_dict).
"""

from typing import Optional


# SCORE_TO_PROB = {
#     0: 0.20,
#     1: 0.30,
#     2: 0.40,
#     3: 0.50,
#     4: 0.60,
#     5: 0.70,
#     6: 0.80,
# }
#
# MOMENTUM_15M_STRONG = 0.003
# MOMENTUM_5M_STRONG = 0.001
# VOL_REGIME_MIN = 0.20
# VOL_REGIME_MAX = 0.60
# TIME_SWEET_SPOT_MIN = 15.0
# TIME_SWEET_SPOT_MAX = 40.0


FLAT_WINRATE = 0.50


def calculate_winrate(
    symbol: str,
    buy_outcome: str,
    sym_mtf: Optional[dict],
    vol_annual: float,
    t_min: float,
    btc_m15m: Optional[float],
) -> tuple[float, dict]:
    """
    Flat winrate model (interim). Returns (0.50, breakdown).

    All arguments are accepted for backward compatibility but ignored.
    breakdown still reports score=0/6 so downstream logging stays consistent.
    """
    breakdown: dict = {
        "mode":      "flat_interim_2026_05_23",
        "score":     0,
        "max_score": 6,
        "winrate":   FLAT_WINRATE,
        "reason":    "flat_probability_drop_anticalibrated",
    }
    return FLAT_WINRATE, breakdown
