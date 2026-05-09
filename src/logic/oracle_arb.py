"""
Oracle Arbitrage & Micro-Momentum Engine.

Pure math — no I/O, fully testable.
Latency arbitrage: Binance WebSocket as leading indicator vs Polymarket price oracle.
GBM probability simulation for remaining time (<60s window).
Cross-exchange sell wall detection (fakeout filter).
"""
from __future__ import annotations

import contextlib
import logging
import math
import random
import time
from typing import Optional


# ── Time urgency ──────────────────────────────────────────────────────────────

def time_urgency(seconds_remaining: float) -> str:
    """Classify urgency tier based on time left to resolution."""
    if seconds_remaining <= 10.0:
        return "FINAL_SECOND_SNIPE"
    if seconds_remaining <= 30.0:
        return "HIGH_ALERT"
    return "NORMAL"


# ── Latency arbitrage detector ────────────────────────────────────────────────

def detect_latency_arb(
    binance_move_pct: float,
    elapsed_s: float,
    polymarket_move_pct: float = 0.0,
    threshold_pct: float = 0.1,
    max_lag_s: float = 5.0,
) -> dict:
    """
    Detect if Binance moved materially while Polymarket has not yet caught up.

    binance_move_pct    : % change in Binance spot over the snipe window (signed)
    elapsed_s           : seconds since Binance move started
    polymarket_move_pct : % change Polymarket has already priced in (signed)
    threshold_pct       : minimum absolute Binance move to consider (default 0.1%)
    max_lag_s           : beyond this lag the arb window is likely closed

    Returns {detected, gap_pct, direction, label}
    """
    gap_pct = abs(binance_move_pct) - abs(polymarket_move_pct)
    direction = "up" if binance_move_pct > 0 else "down"
    detected = (
        abs(binance_move_pct) >= threshold_pct
        and gap_pct > 0
        and elapsed_s <= max_lag_s
    )
    label = "ARB_DETECTED" if detected else "NO_ARB"
    return {
        "detected": detected,
        "gap_pct": round(gap_pct, 4),
        "direction": direction,
        "label": label,
    }


# ── Geometric Brownian Motion probability ─────────────────────────────────────

def gbm_prob_above(
    current: float,
    strike: float,
    vol_annual: float,
    time_remaining_s: float,
) -> float:
    """
    Closed-form GBM probability that price finishes above strike.
    Uses the zero-drift risk-neutral formula: P = Φ(d2).
    d2 = (ln(S/K) − 0.5·σ²·T) / (σ·√T)
    """
    if current <= 0 or strike <= 0 or vol_annual <= 0 or time_remaining_s <= 0:
        return 0.5
    T = time_remaining_s / (365.25 * 86400)
    sigma_sqrt_T = vol_annual * math.sqrt(T)
    if sigma_sqrt_T < 1e-12:
        return 1.0 if current >= strike else 0.0
    d2 = (math.log(current / strike) - 0.5 * vol_annual ** 2 * T) / sigma_sqrt_T
    return _norm_cdf(d2)


def gbm_prob_below(
    current: float,
    strike: float,
    vol_annual: float,
    time_remaining_s: float,
) -> float:
    """GBM probability that price finishes below strike (complement)."""
    return 1.0 - gbm_prob_above(current, strike, vol_annual, time_remaining_s)


def _mc_seed() -> int:
    """Microsecond-timestamp seed for MC reproducibility / audit trail."""
    return int(time.time() * 1_000_000) & 0xFFFF_FFFF


def gbm_mc_prob_above(
    current: float,
    strike: float,
    vol_annual: float,
    time_remaining_s: float,
    n_paths: int = 500,
    seed: Optional[int] = None,
) -> float:
    """
    Monte Carlo GBM probability that price finishes above strike.
    S_T = S · exp(−0.5σ²T + σ√T · Z), Z ~ N(0,1)

    When seed is None, a microsecond-timestamp seed is auto-generated
    (unique per call, prevents pattern repetition).
    """
    if current <= 0 or strike <= 0 or vol_annual <= 0 or time_remaining_s <= 0:
        return 0.5
    actual_seed = seed if seed is not None else _mc_seed()
    rng = random.Random(actual_seed)
    T = time_remaining_s / (365.25 * 86400)
    sigma_sqrt_T = vol_annual * math.sqrt(T)
    drift = -0.5 * vol_annual ** 2 * T
    hits = 0
    for _ in range(n_paths):
        z = rng.gauss(0.0, 1.0)
        s_t = current * math.exp(drift + sigma_sqrt_T * z)
        if s_t > strike:
            hits += 1
    return hits / n_paths


def gbm_mc_with_audit(
    current: float,
    strike: float,
    vol_annual: float,
    time_remaining_s: float,
    n_paths: int = 500,
    seed: Optional[int] = None,
) -> dict:
    """
    Same as gbm_mc_prob_above but returns a metadata dict for audit trails.
    Returns {prob, seed_used, n_paths, current, strike, time_remaining_s}
    """
    actual_seed = seed if seed is not None else _mc_seed()
    prob = gbm_mc_prob_above(current, strike, vol_annual, time_remaining_s, n_paths, seed=actual_seed)
    return {
        "prob": prob,
        "seed_used": actual_seed,
        "n_paths": n_paths,
        "current": current,
        "strike": strike,
        "time_remaining_s": time_remaining_s,
    }


# ── Strike price synchronization ─────────────────────────────────────────────

def sync_strike_price(
    polymarket_strike: float,
    binance_ref: float,
    tolerance: float = 0.005,
) -> dict:
    """
    Validate that Polymarket strike price and Binance reference price agree
    within `tolerance` (default 0.5%).

    If the gap exceeds tolerance → label "PRICE_MISMATCH", ok=False.
    Otherwise → use the more conservative (higher) strike so GBM probability
    is never overstated, label "SYNCED".

    Returns {ok, strike, label, diff_pct}
    """
    if binance_ref <= 0 or polymarket_strike <= 0:
        return {
            "ok": False,
            "strike": polymarket_strike,
            "label": "PRICE_MISMATCH",
            "diff_pct": 0.0,
        }
    diff_pct = abs(polymarket_strike - binance_ref) / binance_ref
    if diff_pct > tolerance:
        return {
            "ok": False,
            "strike": polymarket_strike,
            "label": "PRICE_MISMATCH",
            "diff_pct": round(diff_pct, 6),
        }
    # Conservative: higher strike = harder to clear = lower win probability = safer
    conservative_strike = max(polymarket_strike, binance_ref)
    return {
        "ok": True,
        "strike": round(conservative_strike, 6),
        "label": "SYNCED",
        "diff_pct": round(diff_pct, 6),
    }


# ── Order book imbalance ──────────────────────────────────────────────────────

def order_book_imbalance(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    current_price: float,
    range_pct: float = 0.005,
) -> dict:
    """
    Compute bid/ask volume imbalance within ±range_pct of current_price.
    Returns {bid_vol, ask_vol, ratio, signal}.
    signal: "buy_pressure" | "sell_pressure" | "neutral"
    """
    lo = current_price * (1.0 - range_pct)
    hi = current_price * (1.0 + range_pct)
    bid_vol = sum(sz for px, sz in bids if lo <= px <= hi)
    ask_vol = sum(sz for px, sz in asks if lo <= px <= hi)
    total = bid_vol + ask_vol
    if total <= 0:
        return {"bid_vol": 0.0, "ask_vol": 0.0, "ratio": 1.0, "signal": "neutral"}
    ratio = round(bid_vol / ask_vol, 4) if ask_vol > 0 else float("inf")
    if ratio >= 2.0:
        signal = "buy_pressure"
    elif ratio <= 0.5:
        signal = "sell_pressure"
    else:
        signal = "neutral"
    return {
        "bid_vol": round(bid_vol, 4),
        "ask_vol": round(ask_vol, 4),
        "ratio": ratio,
        "signal": signal,
    }


# ── Sell wall detector ────────────────────────────────────────────────────────

def sell_wall_check(
    asks: list[tuple[float, float]],
    current_price: float,
    strike_price: float,
    scan_range_pct: float = 0.003,
    wall_ratio: float = 5.0,
) -> dict:
    """
    Detect if a large sell wall sits between current_price and strike_price.
    A wall blocks upward movement → contrarian_signal "down" if wall detected.

    scan_range_pct : width of zone above current to scan (default 0.3%)
    wall_ratio     : a level must be wall_ratio × baseline avg size to qualify
                     (baseline = avg of asks OUTSIDE the zone to avoid self-inflation)

    Returns {wall_detected, wall_price, wall_size, baseline_avg, contrarian_signal}
    """
    zone_lo = current_price
    zone_hi = min(strike_price, current_price * (1.0 + scan_range_pct))
    zone_asks = [(px, sz) for px, sz in asks if zone_lo < px <= zone_hi]
    if not zone_asks:
        return {
            "wall_detected": False,
            "wall_price": None,
            "wall_size": 0.0,
            "baseline_avg": 0.0,
            "contrarian_signal": None,
        }
    outside_sizes = [sz for px, sz in asks if not (zone_lo < px <= zone_hi)]
    if not outside_sizes:
        outside_sizes = [sz for _, sz in asks if sz != max(sz2 for _, sz2 in zone_asks)]
    baseline_avg = sum(outside_sizes) / len(outside_sizes) if outside_sizes else 0.0
    biggest_px, biggest_sz = max(zone_asks, key=lambda x: x[1])
    wall_detected = baseline_avg > 0 and biggest_sz >= baseline_avg * wall_ratio
    return {
        "wall_detected": wall_detected,
        "wall_price": round(biggest_px, 6) if wall_detected else None,
        "wall_size": round(biggest_sz, 4),
        "baseline_avg": round(baseline_avg, 4),
        "contrarian_signal": "down" if wall_detected else None,
    }


# ── Arbitrage edge ────────────────────────────────────────────────────────────

def arb_edge(
    simulated_prob: float,
    polymarket_price: float,
    taker_fee: float = 0.018,
) -> float:
    """
    Edge = simulated_prob − polymarket_price − taker_fee.
    Positive → bet is +EV at this price.
    """
    return round(simulated_prob - polymarket_price - taker_fee, 4)


# ── Composite alpha signal ────────────────────────────────────────────────────

def alpha_signal(
    latency_arb: dict,
    edge: float,
    wall: dict,
    urgency: str,
    min_edge: float = 0.02,
    audit: Optional[dict] = None,
) -> dict:
    """
    Combine latency arb + GBM edge + sell-wall filter into a single trade signal.

    audit : optional dict injected into FINAL_SECOND_SNIPE result for post-trade
            reconstruction — typically {"mc_seed": int, "strike_synced": float}.

    Returns {action, direction, confidence, label[, audit_trail]}
    action: "ENTER" | "WAIT" | "SKIP"
    """
    if edge < min_edge:
        return {"action": "WAIT", "direction": None, "confidence": 0.0, "label": "EDGE_TOO_SMALL"}

    arb_detected = latency_arb.get("detected", False)
    arb_direction = latency_arb.get("direction", None)

    # Sell-wall blocks upward entry
    if wall.get("wall_detected") and arb_direction == "up":
        return {"action": "SKIP", "direction": "up", "confidence": 0.0, "label": "SELL_WALL_BLOCK"}

    base_conf = min(0.5 + edge * 2.0, 0.95)
    if arb_detected:
        base_conf = min(base_conf + 0.1, 0.99)

    label = urgency if urgency in ("FINAL_SECOND_SNIPE", "HIGH_ALERT") else "ARB_ENTRY"
    action = "ENTER" if arb_detected or edge >= min_edge * 2 else "WAIT"

    result: dict = {
        "action": action,
        "direction": arb_direction,
        "confidence": round(base_conf, 4),
        "label": label,
    }
    # Audit trail for FINAL_SECOND_SNIPE — records MC seed + strike for replay
    if urgency == "FINAL_SECOND_SNIPE" and audit:
        result["audit_trail"] = audit
    return result


# ── Silent execution context manager ─────────────────────────────────────────

@contextlib.contextmanager
def silent_execution():
    """
    Suppress all logging output during the final-second execution window.
    Restores original log level on exit.
    """
    root = logging.getLogger()
    original_level = root.level
    root.setLevel(logging.CRITICAL + 1)
    try:
        yield
    finally:
        root.setLevel(original_level)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    """Cumulative standard normal distribution via math.erfc."""
    return 0.5 * math.erfc(-x / math.sqrt(2))
