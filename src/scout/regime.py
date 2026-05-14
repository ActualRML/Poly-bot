"""
Market regime detection for hourly contrarian strategy.

Combines three signals to classify whether the broader market is trending or ranging:
1. Cross-asset correlation — majority of crypto basket moving same way
2. Higher timeframe (1h/4h) trend alignment on BTC
3. US session bias — US_OPEN window has stronger trending bias

When the composite score indicates a clearly trending regime, contrarian
mean-reversion entries should be skipped.
"""
from __future__ import annotations

import asyncio
import logging
import time as _time
from datetime import datetime, timezone
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

CRYPTO_BASKET = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"]

_regime_cache: dict = {}
_REGIME_CACHE_TTL_S: float = 60.0  # cache regime for 60s to avoid hammering Binance


# ── Pure-function classifiers (no I/O, fully testable) ────────────────────────

def classify_correlation(
    moves: list[tuple[str, float]],
    move_threshold_pct: float = 0.003,
    align_threshold: float = 0.7,
    min_assets: int = 3,
) -> dict:
    """
    Pure function: classify cross-asset alignment from a list of (symbol, %move) tuples.

    moves              : [(symbol, signed_pct_move), ...]
    move_threshold_pct : ignore moves smaller than this (count as "flat")
    align_threshold    : fraction of assets needed for "trending" (e.g. 0.7 = 70%)
    min_assets         : need at least this many data points to classify

    Returns {trending, direction, aligned_count, total_count, avg_move_pct, moves, reason}
    """
    if len(moves) < min_assets:
        return {
            "trending": False, "direction": None,
            "aligned_count": 0, "total_count": len(moves),
            "avg_move_pct": 0.0, "moves": moves,
            "reason": "insufficient_data",
        }

    up_count   = sum(1 for _, m in moves if m >  move_threshold_pct)
    down_count = sum(1 for _, m in moves if m < -move_threshold_pct)
    total      = len(moves)
    avg_move   = sum(m for _, m in moves) / total

    if up_count / total >= align_threshold:
        direction, aligned, reason = "up", up_count, "majority_up"
        trending = True
    elif down_count / total >= align_threshold:
        direction, aligned, reason = "down", down_count, "majority_down"
        trending = True
    else:
        direction, aligned, reason = None, max(up_count, down_count), "mixed"
        trending = False

    return {
        "trending": trending, "direction": direction,
        "aligned_count": aligned, "total_count": total,
        "avg_move_pct": round(avg_move, 5),
        "moves": [(s, round(m, 5)) for s, m in moves],
        "reason": reason,
    }


def classify_htf_trend(
    closes_1h: list[float],
    closes_4h: list[float],
    flat_threshold_pct: float = 0.005,
) -> dict:
    """
    Pure function: classify 1h and 4h trend from close price series.
    Trend = (latest close vs avg of prior closes) percent change.

    Returns {tf_1h, tf_4h, aligned, label}
    """
    def _trend(closes: list[float]) -> Optional[str]:
        if len(closes) < 3:
            return None
        prior = closes[:-1]
        prior_avg = sum(prior) / len(prior)
        if prior_avg <= 0:
            return None
        diff_pct = (closes[-1] - prior_avg) / prior_avg
        if diff_pct >  flat_threshold_pct: return "up"
        if diff_pct < -flat_threshold_pct: return "down"
        return "flat"

    tf_1h = _trend(closes_1h)
    tf_4h = _trend(closes_4h)
    aligned = (
        tf_1h is not None and tf_4h is not None
        and tf_1h == tf_4h and tf_1h != "flat"
    )
    if aligned:
        label = f"trending_{tf_1h}"
    elif tf_1h == "flat" and tf_4h == "flat":
        label = "ranging"
    elif tf_1h is None or tf_4h is None:
        label = "no_data"
    else:
        label = "mixed"

    return {"tf_1h": tf_1h, "tf_4h": tf_4h, "aligned": aligned, "label": label}


def session_bias(now_utc: Optional[datetime] = None) -> dict:
    """
    Pure function: classify current US session phase.
    Returns {session, bias_strength, trending_likely}.

    Session windows (UTC, EDT-aware approximation):
    - 13:30–15:30  → US_OPEN  (high trending bias, US equity open momentum)
    - 15:30–21:00  → US_MAIN  (medium bias)
    - 21:00–08:00  → ASIA     (low bias, typically ranging)
    - 08:00–13:30  → EU       (medium bias)
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    h = now_utc.hour + now_utc.minute / 60.0

    if 13.5 <= h < 15.5:
        return {"session": "US_OPEN",  "bias_strength": "high",   "trending_likely": True}
    if 15.5 <= h < 21.0:
        return {"session": "US_MAIN",  "bias_strength": "medium", "trending_likely": False}
    if h >= 21.0 or h < 8.0:
        return {"session": "ASIA",     "bias_strength": "low",    "trending_likely": False}
    return     {"session": "EU",       "bias_strength": "medium", "trending_likely": False}


def composite_regime(
    cross: dict,
    htf: dict,
    sess: dict,
    skip_threshold: int = 4,
) -> dict:
    """
    Pure function: combine the three classifiers into a regime label.

    Scoring:
      cross-asset trending      : +2 (strongest signal)
      HTF aligned with cross    : +1
      HTF aligned vs cross diff : -1 (conflict)
      US_OPEN session           : +1

    Returns {regime, trend_score, skip_contrarian, direction}
    """
    score = 0
    direction = None

    if cross.get("trending"):
        score += 2
        direction = cross.get("direction")

    if htf.get("aligned"):
        htf_dir = htf.get("tf_1h")
        if direction is None:
            direction = htf_dir
            score += 1
        elif htf_dir == direction:
            score += 1
        else:
            score -= 1

    if sess.get("trending_likely"):
        score += 1

    if score >= skip_threshold and direction:
        regime = f"TRENDING_{direction.upper()}"
        skip   = True
    elif score >= skip_threshold:
        regime = "TRENDING"
        skip   = True
    elif score >= 1:
        regime = "MIXED"
        skip   = False
    else:
        regime = "RANGING"
        skip   = False

    return {
        "regime": regime,
        "trend_score": score,
        "skip_contrarian": skip,
        "direction": direction,
    }


# ── Async I/O wrappers (fetch then classify) ──────────────────────────────────

async def cross_asset_correlation(
    session: aiohttp.ClientSession,
    lookback_min: int = 30,
    move_threshold_pct: float = 0.003,
    align_threshold: float = 0.7,
) -> dict:
    """Fetch recent moves for the crypto basket, then classify alignment."""
    from src.execute.updown import calculate_recent_momentum

    tasks = [
        calculate_recent_momentum(s, session, minutes=lookback_min)
        for s in CRYPTO_BASKET
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    moves: list[tuple[str, float]] = []
    for sym, r in zip(CRYPTO_BASKET, results):
        if isinstance(r, Exception) or r is None:
            continue
        moves.append((sym, float(r)))

    return classify_correlation(moves, move_threshold_pct, align_threshold)


async def higher_timeframe_trend(
    session: aiohttp.ClientSession,
    symbol: str = "BTC",
) -> dict:
    """Fetch 1h+4h klines for `symbol`, then classify trend."""
    from src.api.binance_client import fetch_klines
    try:
        klines_1h, klines_4h = await asyncio.gather(
            fetch_klines(symbol, session, interval="1h", limit=8),
            fetch_klines(symbol, session, interval="4h", limit=4),
        )
    except Exception as e:
        logger.debug(f"[HTF] Fetch error: {e}")
        return {"tf_1h": None, "tf_4h": None, "aligned": False, "label": "no_data"}

    closes_1h = [k[4] for k in klines_1h] if klines_1h else []
    closes_4h = [k[4] for k in klines_4h] if klines_4h else []
    return classify_htf_trend(closes_1h, closes_4h)


async def detect_market_regime(
    session: aiohttp.ClientSession,
    use_cache: bool = True,
) -> dict:
    """
    Composite market regime detector. Result cached for 60s.

    Returns:
      {
        regime          : "TRENDING_UP" | "TRENDING_DOWN" | "TRENDING" | "MIXED" | "RANGING",
        trend_score     : int (-1..4),
        skip_contrarian : bool,
        direction       : "up" | "down" | None,
        cross_asset     : { ... cross_asset_correlation result ... },
        higher_tf       : { ... higher_timeframe_trend result ... },
        session         : { ... session_bias result ... },
      }
    """
    if use_cache:
        cached_ts = _regime_cache.get("ts", 0.0)
        if _time.monotonic() - cached_ts < _REGIME_CACHE_TTL_S and "result" in _regime_cache:
            return _regime_cache["result"]

    cross = await cross_asset_correlation(session)
    htf   = await higher_timeframe_trend(session)
    sess  = session_bias()

    composite = composite_regime(cross, htf, sess)

    result = {
        **composite,
        "cross_asset": cross,
        "higher_tf":   htf,
        "session":     sess,
    }

    if use_cache:
        _regime_cache["result"] = result
        _regime_cache["ts"]     = _time.monotonic()

    return result
