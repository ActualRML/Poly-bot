"""
GBM-based directional entry for Up/Down hourly markets.

Replaces the contrarian (mean-reversion) heuristic with a probabilistic
fair-value comparison:

    P(Up) = gbm_prob_above(current, strike, vol_annual, T_remaining)
    edge_up   = P(Up)       - market_price_up   - fee
    edge_down = (1 - P(Up)) - market_price_down - fee

If edge_up >= min_edge → buy Up (model says under-priced)
If edge_down >= min_edge → buy Down (model says over-priced)
Otherwise → skip.

Strike for these markets is the open of the 1h Binance candle that the
Polymarket "Up or Down 1AM ET" event resolves on. Strike is static per
market — cached per (symbol, start_date_iso).
"""
from __future__ import annotations

import asyncio
import logging
import time as _time
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from src.scout.oracle_arb import gbm_prob_above

logger = logging.getLogger(__name__)


# ── Strike cache (static per market) ──────────────────────────────────────────

_strike_cache: dict[tuple[str, str], tuple[float, float]] = {}
_STRIKE_CACHE_TTL_S: float = 3600.0  # 1h — strike of a closed candle never changes


async def get_hourly_strike(
    symbol: str,
    start_date: datetime,
    session: aiohttp.ClientSession,
) -> Optional[float]:
    """
    Fetch the 1h candle open at start_date as the strike. Cached per
    (symbol, start_date_iso) for STRIKE_CACHE_TTL_S.
    """
    from src.execute.updown import fetch_reference_price_hourly

    key = (symbol.upper(), start_date.isoformat())
    cached = _strike_cache.get(key)
    if cached:
        price, ts = cached
        if _time.monotonic() - ts < _STRIKE_CACHE_TTL_S:
            return price

    price = await fetch_reference_price_hourly(symbol, session, start_date)
    if price and price > 0:
        _strike_cache[key] = (price, _time.monotonic())
        _cleanup_strike_cache()
        return price
    return None


def _cleanup_strike_cache(max_entries: int = 256) -> None:
    """Drop oldest entries when cache grows too big."""
    if len(_strike_cache) <= max_entries:
        return
    # Sort by ts asc, drop oldest half
    items = sorted(_strike_cache.items(), key=lambda kv: kv[1][1])
    for k, _ in items[: len(items) // 2]:
        _strike_cache.pop(k, None)


# ── Opposite re-entry gate (pure function) ────────────────────────────────────

def passes_opposite_reentry_gate(
    locked_outcome: Optional[str],
    proposed_outcome: str,
    time_remaining_s: float,
    min_minutes: int = 10,
) -> tuple[bool, str]:
    """
    Decide whether a GBM-driven entry into a profit-locked market is allowed.

    - locked_outcome=None → not a re-entry case, always allow.
    - time_remaining < min_minutes → block (no room for safety).
    - proposed_outcome == locked_outcome → block (same-direction chase).
    - else → allow (opposite re-entry).

    Returns (allowed: bool, reason: str).
    """
    if locked_outcome is None:
        return True, "NOT_REENTRY"
    if time_remaining_s / 60.0 < min_minutes:
        return False, f"TIME_FLOOR_{min_minutes}M"
    if proposed_outcome == locked_outcome:
        return False, "SAME_DIRECTION_CHASE_BLOCKED"
    return True, "OPPOSITE_REENTRY_OK"


# ── Direction decision (pure function, no I/O) ────────────────────────────────

def pick_gbm_direction(
    prob_up: float,
    market_price_up: float,
    fee: float = 0.018,
    min_edge: float = 0.05,
    max_edge: float = 0.25,
    trend_bias: float = 0.0,
) -> dict:
    """
    Pure function: pick direction (Up/Down/skip) from GBM probability vs market.

    `trend_bias` shifts prob_up before edge calculation (range [-0.10, +0.10]).
    Positive bias = bullish (EMA uptrend), negative = bearish.

    Returns dict with keys:
      - action      : "BUY" | "SKIP"
      - outcome     : "Up" | "Down" | None
      - buy_price   : market price of the chosen outcome (None if skip)
      - edge        : signed edge of the chosen side (or best abs edge if skip)
      - edge_up     : P(Up)_biased - market_price_up - fee
      - edge_down   : (1 - P(Up)_biased) - (1 - market_price_up) - fee
      - reason      : short label for logging
      - prob_up_raw : original GBM probability before bias
      - trend_bias  : bias value applied
    """
    if not (0.0 <= prob_up <= 1.0):
        return {
            "action": "SKIP", "outcome": None, "buy_price": None,
            "edge": 0.0, "edge_up": 0.0, "edge_down": 0.0,
            "reason": "INVALID_PROB",
            "prob_up_raw": prob_up, "trend_bias": trend_bias,
        }
    if not (0.0 < market_price_up < 1.0):
        return {
            "action": "SKIP", "outcome": None, "buy_price": None,
            "edge": 0.0, "edge_up": 0.0, "edge_down": 0.0,
            "reason": "INVALID_MARKET_PRICE",
            "prob_up_raw": prob_up, "trend_bias": trend_bias,
        }

    prob_up_raw    = prob_up
    prob_up_biased = round(min(max(prob_up + trend_bias, 0.01), 0.99), 4)

    market_price_down = 1.0 - market_price_up

    edge_up   = prob_up_biased           - market_price_up   - fee
    edge_down = (1.0 - prob_up_biased)   - market_price_down - fee

    _base = {
        "edge_up": round(edge_up, 4),
        "edge_down": round(edge_down, 4),
        "prob_up_raw": round(prob_up_raw, 4),
        "trend_bias": round(trend_bias, 3),
    }

    # Pick the side with the larger edge; both can't be ≥ min_edge
    # simultaneously since edge_up + edge_down = 1 - market_up - market_down - 2·fee
    # = -2·fee (when market sums to 1). So at most one passes the gate.
    if edge_up >= min_edge and edge_up >= edge_down:
        # Edge cap: extreme edge = GBM overconfident (T too short + large S/K deviation).
        # When T < 1h, even 1% price move above strike pushes prob_up to 90%+.
        # Market pricing at 46% is more reliable than GBM at that point.
        if edge_up > max_edge:
            return {"action": "SKIP", "outcome": None, "buy_price": None,
                    "edge": round(edge_up, 4), "reason": "EDGE_TOO_HIGH_UNRELIABLE", **_base}
        return {"action": "BUY", "outcome": "Up",
                "buy_price": round(market_price_up, 4),
                "edge": round(edge_up, 4), "reason": "MODEL_UNDERPRICES_UP", **_base}
    if edge_down >= min_edge:
        if edge_down > max_edge:
            return {"action": "SKIP", "outcome": None, "buy_price": None,
                    "edge": round(edge_down, 4), "reason": "EDGE_TOO_HIGH_UNRELIABLE", **_base}
        return {"action": "BUY", "outcome": "Down",
                "buy_price": round(market_price_down, 4),
                "edge": round(edge_down, 4), "reason": "MODEL_OVERPRICES_UP", **_base}

    best_abs = max(abs(edge_up), abs(edge_down))
    return {"action": "SKIP", "outcome": None, "buy_price": None,
            "edge": round(best_abs, 4), "reason": "EDGE_BELOW_MIN", **_base}


# ── Convenience wrapper that combines I/O + decision ──────────────────────────

async def evaluate_hourly_entry(
    symbol: str,
    start_date: datetime,
    end_date: datetime,
    market_price_up: float,
    vol_annual: float,
    session: aiohttp.ClientSession,
    fee: float = 0.018,
    min_edge: float = 0.05,
    max_edge: float = 0.25,
    current_price: Optional[float] = None,
    use_trend_bias: bool = True,
) -> Optional[dict]:
    """
    End-to-end GBM entry evaluator. Returns a decision dict (see
    pick_gbm_direction) extended with current/strike/prob_up/T_seconds, or
    None when strike or current price is unavailable.

    `current_price` may be passed in to avoid a redundant Binance fetch.
    `use_trend_bias` enables EMA-based probability adjustment (6h+24h EMA).
    """
    from src.api.binance_client import fetch_price, fetch_trend_bias

    now = datetime.now(timezone.utc)
    delta_sec = (end_date - now).total_seconds()
    if delta_sec <= 0:
        return None

    if current_price is None:
        current_price = await fetch_price(symbol, session)
    if not current_price or current_price <= 0:
        return None

    strike = await get_hourly_strike(symbol, start_date, session)
    if not strike or strike <= 0:
        return None

    trend_bias = 0.0
    if use_trend_bias:
        try:
            trend_bias = await fetch_trend_bias(symbol, session)
        except Exception as _e:
            logger.debug(f"[GBM] {symbol} trend_bias fetch failed: {_e}")

    prob_up_raw = gbm_prob_above(current_price, strike, vol_annual, delta_sec)
    decision = pick_gbm_direction(
        prob_up=prob_up_raw,
        market_price_up=market_price_up,
        fee=fee,
        min_edge=min_edge,
        max_edge=max_edge,
        trend_bias=trend_bias,
    )
    prob_up_biased = round(min(max(prob_up_raw + trend_bias, 0.01), 0.99), 4)
    decision.update({
        "current":   current_price,
        "strike":    strike,
        "prob_up":   prob_up_biased,   # biased — what drove the direction decision
        "T_seconds": delta_sec,
        "vol":       vol_annual,
    })
    return decision
