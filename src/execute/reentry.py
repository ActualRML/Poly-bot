# DISABLED via REENTRY_AFTER_TP_ENABLED=false in .env. See STRATEGY_MISTAKES.md.
"""
Re-entry logic untuk hourly contrarian strategy.

Setelah take-profit lock, monitor posisi. Kalau current shares price drop drastis
karena panic selling tapi underlying asset di Binance masih support outcome kita,
itu mispricing — masuk lagi dengan ukuran setengah.

Pure-function module — no I/O.
"""
from __future__ import annotations

from typing import Optional


def estimate_fair_value(
    outcome: str,
    btc_scalp: Optional[dict],
    sym_mtf: Optional[dict],
) -> Optional[float]:
    """
    Estimate fair value for a contrarian outcome based on current Binance state.

    outcome   : "Up" or "Down" — outcome we're betting on
    btc_scalp : current scalping signal dict (from calculate_scalping_signals)
    sym_mtf   : multi-TF momentum dict for the symbol

    Returns fair value (probability) in [0, 1], or None if insufficient data.

    Logic:
      Bot is contrarian, so we bet OPPOSITE of momentum.
      If momentum still aligned with our contrarian thesis (i.e. asset still
      "overshooting" in opposite direction), fair value remains high.
      If momentum has reversed away from our thesis, fair value drops.
    """
    if btc_scalp is None or sym_mtf is None:
        return None

    confidence = float(btc_scalp.get("confidence", 0.55))
    momentum_score = float(btc_scalp.get("momentum_score", 0.0))
    sym_15m = float(sym_mtf.get("m_15m", 0.0))

    # Contrarian: we bet Down when momentum is up, Up when momentum is down
    expected_outcome = "Down" if momentum_score > 0 else "Up"

    if outcome != expected_outcome:
        # Momentum reversed — our thesis weakened
        return max(0.30, confidence - 0.20)

    # Confirm with symbol's own momentum
    if outcome == "Down" and sym_15m > 0:
        # Asset still going up → contrarian Down still has merit
        return max(0.50, min(0.85, confidence))
    if outcome == "Up" and sym_15m < 0:
        # Asset still going down → contrarian Up still has merit
        return max(0.50, min(0.85, confidence))

    # Mixed signals — neutral
    return 0.50


def check_reentry_signal(
    exit_price: float,
    current_market_price: float,
    fair_value: float,
    drop_threshold: float = 0.30,
    min_edge: float = 0.05,
    taker_fee: float = 0.018,
) -> dict:
    """
    Decide if conditions favor re-entry.

    exit_price          : last take-profit exit price
    current_market_price: current shares price on Polymarket
    fair_value          : estimated fair probability from estimate_fair_value
    drop_threshold      : price must drop ≥ this fraction from exit
    min_edge            : minimum (fair − price − fee) edge
    taker_fee           : Polymarket taker fee

    Returns {should_reenter, reason, drop_pct, edge}
    """
    if exit_price <= 0 or current_market_price <= 0 or fair_value <= 0:
        return {
            "should_reenter": False, "reason": "invalid_prices",
            "drop_pct": 0.0, "edge": 0.0,
        }

    drop_pct = (exit_price - current_market_price) / exit_price
    if drop_pct < drop_threshold:
        return {
            "should_reenter": False,
            "reason": f"drop {drop_pct:.1%} < {drop_threshold:.0%} threshold",
            "drop_pct": round(drop_pct, 4), "edge": 0.0,
        }

    edge = fair_value - current_market_price - taker_fee
    if edge < min_edge:
        return {
            "should_reenter": False,
            "reason": f"edge {edge:.3f} < {min_edge:.3f}",
            "drop_pct": round(drop_pct, 4), "edge": round(edge, 4),
        }

    return {
        "should_reenter": True,
        "reason": f"mispricing: fair {fair_value:.2f} vs cur {current_market_price:.2f} "
                  f"(drop {drop_pct:.0%}, edge {edge:.3f})",
        "drop_pct": round(drop_pct, 4),
        "edge":     round(edge, 4),
    }


def validate_reentry_orderbook(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    capital_required: float,
    spread_max: float = 0.05,
) -> dict:
    """
    Validate Polymarket orderbook for re-entry.

    Checks:
      - spread = (best_ask − best_bid) / best_ask not too wide
      - sufficient ask-side liquidity to fill capital_required without big slippage

    Returns {ok, reason, spread, ask_liquidity_usdc}
    """
    if not bids or not asks:
        return {
            "ok": False, "reason": "empty_book",
            "spread": 1.0, "ask_liquidity_usdc": 0.0,
        }

    best_bid = bids[0][0]
    best_ask = asks[0][0]
    if best_ask <= 0 or best_bid <= 0:
        return {
            "ok": False, "reason": "invalid_prices",
            "spread": 1.0, "ask_liquidity_usdc": 0.0,
        }

    spread = (best_ask - best_bid) / best_ask
    if spread > spread_max:
        return {
            "ok": False, "reason": f"spread {spread:.1%} > {spread_max:.0%}",
            "spread": round(spread, 4), "ask_liquidity_usdc": 0.0,
        }

    # Liquidity at top-of-book ask
    ask_liquidity_usdc = best_ask * asks[0][1]
    if ask_liquidity_usdc < capital_required * 0.8:
        # Walk down asks to see if combined depth is enough
        accumulated = 0.0
        for px, sz in asks[:5]:
            if px > best_ask * (1.0 + spread_max):
                break
            accumulated += px * sz
        if accumulated < capital_required:
            return {
                "ok": False,
                "reason": f"insufficient ask liquidity: ${accumulated:.2f} < ${capital_required:.2f}",
                "spread": round(spread, 4),
                "ask_liquidity_usdc": round(accumulated, 2),
            }
        ask_liquidity_usdc = accumulated

    return {
        "ok": True, "reason": "ok",
        "spread": round(spread, 4),
        "ask_liquidity_usdc": round(ask_liquidity_usdc, 2),
    }


def passes_time_gate(
    minutes_to_resolve: float,
    min_minutes: float = 15.0,
) -> bool:
    """Re-entry blocked when too close to resolve (volatile irrational pricing)."""
    return minutes_to_resolve >= min_minutes


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
