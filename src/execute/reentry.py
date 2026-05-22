"""
Candle strategy reverse re-entry helper.

After a stop-loss fires on a candle position, decide whether a reverse
re-entry (opposite outcome) is warranted based on 15m momentum.

Pure-function module — no I/O.
"""
from __future__ import annotations


def check_candle_reverse_reentry(
    sl_outcome: str,
    momentum_15m: float,
    minutes_to_resolve: float,
    market_price_opposite: float,
    momentum_threshold: float = 0.0015,
    min_minutes: float = 20.0,
    max_buy_price: float = 0.60,
) -> dict:
    """
    After SL fires on a candle position, decide if reverse re-entry makes sense.

    sl_outcome            : outcome that just got stopped out ("Up" or "Down")
    momentum_15m          : (close[-1] - close[-15]) / close[-15] from 1m klines
    minutes_to_resolve    : time left until market resolves
    market_price_opposite : current Polymarket price of the opposite outcome
    momentum_threshold    : abs(momentum) must exceed this to confirm reversal
    min_minutes           : don't re-enter if less than this much time left
    max_buy_price         : skip if market already pricing reversal too high

    Returns dict: {should_reenter, outcome, reason, momentum_15m}
    """
    base = {"momentum_15m": round(momentum_15m, 5)}

    if minutes_to_resolve < min_minutes:
        return {**base, "should_reenter": False, "outcome": None,
                "reason": f"TIME_FLOOR_{min_minutes:.0f}M"}

    if not (0.0 < market_price_opposite < max_buy_price):
        return {**base, "should_reenter": False, "outcome": None,
                "reason": f"PRICE_OUT_OF_RANGE_{market_price_opposite:.3f}"}

    opposite = "Down" if sl_outcome == "Up" else "Up"

    # Momentum must confirm the opposite direction
    if sl_outcome == "Up" and momentum_15m < -momentum_threshold:
        return {**base, "should_reenter": True, "outcome": opposite,
                "reason": f"REVERSE_MOM_{momentum_15m:+.4f}"}
    if sl_outcome == "Down" and momentum_15m > momentum_threshold:
        return {**base, "should_reenter": True, "outcome": opposite,
                "reason": f"REVERSE_MOM_{momentum_15m:+.4f}"}

    return {**base, "should_reenter": False, "outcome": None,
            "reason": f"MOM_FLAT_{momentum_15m:+.4f}"}
