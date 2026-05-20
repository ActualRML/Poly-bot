"""
Heuristic probability model for win_rate estimation.

Replaces the hardcoded 0.33 placeholder with deterministic signal-based scoring.
Same input → same output across machines (portable for shared codebase).

Score is 0-6, mapped linearly to probability 0.20-0.80.

Signals:
1. Momentum 15m strong (>0.3% absolute, aligned with buy_outcome)
2. Momentum 5m strong (>0.1% absolute, aligned with buy_outcome)
3. MTF aligned (5m and 15m same sign)
4. Vol regime normal (0.20 ≤ vol_annual ≤ 0.60)
5. BTC correlation aligned (BTC m15m sign matches buy_outcome for non-BTC, or always pass for BTC)
6. Time sweet spot (15 ≤ t_min ≤ 40)
"""

from typing import Optional


SCORE_TO_PROB = {
    0: 0.20,
    1: 0.30,
    2: 0.40,
    3: 0.50,
    4: 0.60,
    5: 0.70,
    6: 0.80,
}

MOMENTUM_15M_STRONG = 0.003
MOMENTUM_5M_STRONG = 0.001
VOL_REGIME_MIN = 0.20
VOL_REGIME_MAX = 0.60
TIME_SWEET_SPOT_MIN = 15.0
TIME_SWEET_SPOT_MAX = 40.0


def calculate_winrate(
    symbol: str,
    buy_outcome: str,
    sym_mtf: Optional[dict],
    vol_annual: float,
    t_min: float,
    btc_m15m: Optional[float],
) -> tuple[float, dict]:
    """
    Returns (winrate, breakdown_dict).
    breakdown_dict shows which signals passed for logging/debugging.
    """
    if sym_mtf is None:
        return 0.20, {"reason": "no_momentum_data"}

    is_up = buy_outcome == "Up"
    score = 0
    breakdown: dict = {}

    m_15m = sym_mtf.get("m_15m", 0.0)
    m_5m = sym_mtf.get("m_5m", 0.0)

    sig_mom15 = abs(m_15m) > MOMENTUM_15M_STRONG and ((m_15m > 0) == is_up)
    breakdown["mom_15m_strong_aligned"] = sig_mom15
    if sig_mom15:
        score += 1

    sig_mom5 = abs(m_5m) > MOMENTUM_5M_STRONG and ((m_5m > 0) == is_up)
    breakdown["mom_5m_strong_aligned"] = sig_mom5
    if sig_mom5:
        score += 1

    sig_mtf = (m_5m > 0) == (m_15m > 0) and abs(m_5m) > 1e-6 and abs(m_15m) > 1e-6
    breakdown["mtf_aligned"] = sig_mtf
    if sig_mtf:
        score += 1

    sig_vol = VOL_REGIME_MIN <= vol_annual <= VOL_REGIME_MAX
    breakdown["vol_normal"] = sig_vol
    if sig_vol:
        score += 1

    if symbol.upper() == "BTC":
        sig_btc = True
    elif btc_m15m is None:
        sig_btc = False
    else:
        sig_btc = abs(btc_m15m) > 1e-6 and ((btc_m15m > 0) == is_up)
    breakdown["btc_corr_aligned"] = sig_btc
    if sig_btc:
        score += 1

    sig_time = TIME_SWEET_SPOT_MIN <= t_min <= TIME_SWEET_SPOT_MAX
    breakdown["time_sweet_spot"] = sig_time
    if sig_time:
        score += 1

    breakdown["score"] = score
    breakdown["max_score"] = 6

    winrate = SCORE_TO_PROB[score]
    breakdown["winrate"] = winrate

    return winrate, breakdown
