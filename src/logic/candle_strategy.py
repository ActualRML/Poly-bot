"""
ATR-normalized candle prediction strategy for Polymarket 1h Up/Down markets.

Edge source: Polymarket prices these using recency bias and slow updates.
When a 1h candle is already 1-2+ ATR away from its open with little time left,
the mathematical probability of staying above/below is very high, but market
often still prices it at only 0.65-0.80.

Key formula:
    gap_sigma = (current - strike) / (strike × ATR_1h_pct × √T_remaining_h)
    P(Up) = N(gap_sigma)

ATR_1h_pct = mean(|high - low| / close) over last 6 completed 1h candles.
Uses current volatility regime, NOT long-term annualized vol.
"""
from __future__ import annotations

import math
from statistics import NormalDist
from typing import Optional

_norm = NormalDist()


def norm_cdf(x: float) -> float:
    return _norm.cdf(x)


def compute_1h_atr_pct(klines: list) -> float:
    """
    ATR as fraction of close price, averaged over provided klines.
    klines: list of (ts, open, high, low, close) tuples.
    Returns 0.005 (0.5%) as fallback if data is insufficient.
    """
    if not klines:
        return 0.005
    atrs = []
    for k in klines:
        try:
            high, low, close = float(k[2]), float(k[3]), float(k[4])
            if close > 0:
                atrs.append((high - low) / close)
        except (IndexError, ValueError, TypeError):
            continue
    if len(atrs) < 3:
        return 0.005
    return sum(atrs) / len(atrs)


def candle_gap_sigma(
    current: float,
    strike: float,
    atr_1h_pct: float,
    t_remaining_h: float,
) -> float:
    """
    How many standard deviations is current price from strike,
    given remaining time and current hourly ATR.

    Positive = above strike (favors Up).
    Negative = below strike (favors Down).
    Returns 0.0 on invalid inputs.
    """
    if strike <= 0 or atr_1h_pct <= 0 or t_remaining_h <= 0:
        return 0.0
    gap_pct = (current - strike) / strike
    sigma_remaining = atr_1h_pct * math.sqrt(t_remaining_h)
    return gap_pct / sigma_remaining


def candle_fair_prob_up(
    gap_sigma: float,
    trend_12h: float = 0.0,
    rsi: Optional[float] = None,
    direction: str = "Up",
) -> float:
    """
    P(candle close >= candle open) based on ATR-normalized gap + context.

    trend_12h : (close_now - close_12h_ago) / close_12h_ago
    rsi       : 0-100, penalizes overbought Up bets and oversold Down bets
    direction : side we're considering ("Up" or "Down")

    Returns probability in [0.03, 0.97].
    """
    base = norm_cdf(gap_sigma)

    # Trend nudge: max ±2%
    trend_adj = max(-0.02, min(0.02, trend_12h * 1.5))

    rsi_adj = 0.0
    if rsi is not None:
        if direction == "Up" and rsi > 72:
            rsi_adj = -0.04
        elif direction == "Down" and rsi < 28:
            rsi_adj = -0.04

    return min(0.97, max(0.03, base + trend_adj + rsi_adj))


def evaluate_candle_direction(
    current: float,
    strike: float,
    atr_1h_pct: float,
    t_remaining_h: float,
    market_price_up: float,
    fee: float = 0.018,
    min_edge: float = 0.05,
    min_gap_sigma: float = 0.8,
    trend_12h: float = 0.0,
    rsi: Optional[float] = None,
) -> dict:
    """
    Decide BUY Up, BUY Down, or SKIP.

    Returns dict with:
      action      : "BUY" | "SKIP"
      outcome     : "Up" | "Down" | None
      buy_price   : market price of the chosen outcome
      edge        : expected edge of the chosen side
      gap_sigma   : ATR-normalized distance from strike
      p_up        : fair P(Up) after adjustments
      strike      : candle open (price to beat)
      current     : current asset price
      atr_1h_pct  : hourly ATR used in calculation
      reason      : short label
    """
    gap_sigma = candle_gap_sigma(current, strike, atr_1h_pct, t_remaining_h)

    base = {
        "gap_sigma":   round(gap_sigma, 3),
        "strike":      strike,
        "current":     current,
        "atr_1h_pct":  round(atr_1h_pct, 5),
    }

    # Gap too small → model unreliable, skip
    if abs(gap_sigma) < min_gap_sigma:
        return {
            **base,
            "action": "SKIP", "outcome": None, "buy_price": None, "edge": 0.0,
            "p_up": norm_cdf(gap_sigma),
            "reason": f"GAP_SIGMA_{gap_sigma:.2f}_BELOW_{min_gap_sigma}",
        }

    direction = "Up" if gap_sigma > 0 else "Down"
    p_up = candle_fair_prob_up(gap_sigma, trend_12h, rsi, direction)
    p_down = 1.0 - p_up

    market_price_down = round(1.0 - market_price_up, 4)
    edge_up   = p_up   - market_price_up   - fee
    edge_down = p_down - market_price_down - fee

    best = {**base, "p_up": round(p_up, 4)}

    if edge_up >= min_edge and edge_up >= edge_down:
        return {**best,
                "action": "BUY", "outcome": "Up",
                "buy_price": market_price_up,
                "edge": round(edge_up, 4),
                "reason": "UP_UNDERPRICED"}

    if edge_down >= min_edge:
        return {**best,
                "action": "BUY", "outcome": "Down",
                "buy_price": market_price_down,
                "edge": round(edge_down, 4),
                "reason": "DOWN_UNDERPRICED"}

    return {**best,
            "action": "SKIP", "outcome": None, "buy_price": None,
            "edge": round(max(abs(edge_up), abs(edge_down)), 4),
            "reason": "EDGE_BELOW_MIN"}
