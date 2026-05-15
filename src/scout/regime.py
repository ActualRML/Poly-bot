"""
Market regime detection for hourly contrarian strategy.

Combines signals to classify the market into 8 states:

Global (Binance-based, cached 60s):
  TRENDING_HIGH_VOL   — trending kuat + vol tinggi  → skip contrarian, size down
  TRENDING_LOW_VOL    — trending tapi tenang         → skip contrarian, size normal
  SIDEWAYS_HIGH_VOL   — choppy + volatile            → candle ok, contrarian hati-hati
  SIDEWAYS_LOW_VOL    — flat + tenang                → ideal contrarian
  EXTREME_HIGH_VOL    — vol > 100% annualized        → skip semua, size ×0.5
  EXTREME_LOW_VOL     — vol < 20% annualized         → edge kecil, pasar tidur

Per-market (Polymarket-based, pure functions):
  ILLIQUID            — volume tipis / spread lebar  → SKIP entry
  ONE_SIDED           — harga di ekstrem / collapsing → SKIP entry
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

def classify_volatility(vol_annual: float) -> dict:
    """
    Classify realized annualized volatility into 5 buckets.

    Returns {vol_state, vol_ratio} where vol_ratio = vol / BTC_BASELINE (0.44).

    States:
      EXTREME_LOW  : vol < 20%
      LOW          : 20% ≤ vol < 35%
      NORMAL       : 35% ≤ vol < 70%
      HIGH         : 70% ≤ vol < 100%
      EXTREME_HIGH : vol ≥ 100%
    """
    _BASELINE = 0.44
    vol_ratio = vol_annual / _BASELINE if _BASELINE > 0 else 1.0

    if vol_annual >= 1.00:
        state = "EXTREME_HIGH"
    elif vol_annual >= 0.70:
        state = "HIGH"
    elif vol_annual >= 0.35:
        state = "NORMAL"
    elif vol_annual >= 0.20:
        state = "LOW"
    else:
        state = "EXTREME_LOW"

    return {"vol_state": state, "vol_ratio": round(vol_ratio, 3)}


def classify_market_state(
    market_price_up: float,
    volume_24h: float,
    price_velocity: float | None = None,
    intended_outcome: str | None = None,
    vol_min_usd: float = 1000.0,
    one_sided_high: float = 0.82,
    one_sided_low: float = 0.18,
    velocity_threshold: float = 0.05,
) -> dict:
    """
    Per-market state check (pure function, no I/O).

    Detects two special conditions that should block entry:
      ILLIQUID  — volume_24h terlalu kecil
      ONE_SIDED — harga outcome sudah di ekstrem, atau sedang collapse

    Parameters
    ----------
    market_price_up  : Polymarket mid-price for "Up" outcome (0-1)
    volume_24h       : USD volume in last 24h
    price_velocity   : % change in market_price_up over last 5 min (from stagnation tracker)
    intended_outcome : "Up" or "Down" — used for directional velocity check
    vol_min_usd      : minimum 24h volume to be considered liquid
    one_sided_high   : if market_price_up > this, Down is a bad bet
    one_sided_low    : if market_price_up < this, Up is a bad bet
    velocity_threshold : abs(velocity) > this = price collapsing

    Returns {state: "NORMAL"|"ILLIQUID"|"ONE_SIDED", reasons: [str]}
    """
    # ILLIQUID check
    if 0 < volume_24h < vol_min_usd:
        return {
            "state": "ILLIQUID",
            "reasons": [f"volume ${volume_24h:.0f} < ${vol_min_usd:.0f}"],
        }

    # ONE_SIDED: price at extreme
    if market_price_up > one_sided_high:
        return {
            "state": "ONE_SIDED",
            "reasons": [f"up={market_price_up:.3f} > {one_sided_high} (Up sudah pasti)"],
        }
    if market_price_up < one_sided_low:
        return {
            "state": "ONE_SIDED",
            "reasons": [f"up={market_price_up:.3f} < {one_sided_low} (Down sudah pasti)"],
        }

    # ONE_SIDED: price collapsing in the direction we're betting against
    if price_velocity is not None and abs(price_velocity) >= velocity_threshold:
        if intended_outcome == "Down" and price_velocity > 0:
            return {
                "state": "ONE_SIDED",
                "reasons": [
                    f"Up price naik {price_velocity:+.1%} in 5m (Down collapsing)"
                ],
            }
        if intended_outcome == "Up" and price_velocity < 0:
            return {
                "state": "ONE_SIDED",
                "reasons": [
                    f"Up price turun {price_velocity:+.1%} in 5m (Up collapsing)"
                ],
            }

    return {"state": "NORMAL", "reasons": []}


def classify_event_horizon(
    t_min: float,
    strategy: str = "gbm",
    floor_min: float = 20.0,
    contrarian_min: float = 25.0,
    tight_max: float = 35.0,
    critical_max: float = 25.0,
    edge_mult_tight: float = 1.5,
    edge_mult_critical: float = 2.0,
) -> dict:
    """
    Classify time-to-resolve into 4 tiers and derive entry policy.

    Tiers (priority top-down):
      BELOW_FLOOR (t < floor_min)            → block all entries
      CRITICAL    (floor_min ≤ t < critical_max) → GBM: edge × critical mult, contrarian: SKIP
      TIGHT       (critical_max ≤ t < tight_max) → edge × tight mult
      WIDE        (t ≥ tight_max)            → no change

    Returns {tier, allowed, edge_mult, reason}.
    """
    if t_min < floor_min:
        return {
            "tier": "BELOW_FLOOR", "allowed": False, "edge_mult": 1.0,
            "reason": f"T={t_min:.0f}m < {floor_min:.0f}m floor",
        }

    if t_min < critical_max:
        if strategy == "contrarian" and t_min < contrarian_min:
            return {
                "tier": "CRITICAL", "allowed": False, "edge_mult": 1.0,
                "reason": f"T={t_min:.0f}m < {contrarian_min:.0f}m contrarian floor",
            }
        return {
            "tier": "CRITICAL", "allowed": True, "edge_mult": edge_mult_critical,
            "reason": f"T={t_min:.0f}m critical, edge ×{edge_mult_critical}",
        }

    if t_min < tight_max:
        return {
            "tier": "TIGHT", "allowed": True, "edge_mult": edge_mult_tight,
            "reason": f"T={t_min:.0f}m tight, edge ×{edge_mult_tight}",
        }

    return {
        "tier": "WIDE", "allowed": True, "edge_mult": 1.0,
        "reason": f"T={t_min:.0f}m wide window",
    }


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
    vol_state: str = "NORMAL",
) -> dict:
    """
    Pure function: combine all classifiers into an 8-state regime label.

    Trend scoring:
      cross-asset trending   : +2 (strongest signal)
      HTF aligned with cross : +1
      HTF conflicts cross    : -1
      US_OPEN session        : +1

    8-state label logic (priority top-down):
      EXTREME_HIGH_VOL / EXTREME_LOW_VOL  — vol dominates, overrides trend
      TRENDING_HIGH_VOL / TRENDING_LOW_VOL — score ≥ 3 + vol
      SIDEWAYS_HIGH_VOL / SIDEWAYS_LOW_VOL — score ≤ 1 + vol
      MIXED_HIGH_VOL / MIXED_LOW_VOL       — ambiguous trend + vol

    Returns {regime, trend_state, trend_score, skip_contrarian, direction}
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

    # Trend state from score
    if score >= 3:
        trend_state = "TRENDING"
    elif score <= 1:
        trend_state = "SIDEWAYS"
    else:
        trend_state = "MIXED"

    # skip_contrarian: strong trend OR extreme volatility
    skip = (score >= skip_threshold and bool(direction)) or vol_state == "EXTREME_HIGH"

    # Build 8-state regime label
    _is_high_vol = vol_state in ("HIGH", "EXTREME_HIGH")
    _vol_suffix  = "HIGH_VOL" if _is_high_vol else "LOW_VOL"

    if vol_state == "EXTREME_HIGH":
        regime = "EXTREME_HIGH_VOL"
    elif vol_state == "EXTREME_LOW":
        regime = "EXTREME_LOW_VOL"
    elif trend_state == "TRENDING":
        regime = f"TRENDING_{_vol_suffix}"
    elif trend_state == "SIDEWAYS":
        regime = f"SIDEWAYS_{_vol_suffix}"
    else:
        regime = f"MIXED_{_vol_suffix}"

    return {
        "regime":          regime,
        "trend_state":     trend_state,
        "trend_score":     score,
        "skip_contrarian": skip,
        "direction":       direction,
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
        regime          : 8-state label (e.g. "TRENDING_HIGH_VOL", "SIDEWAYS_LOW_VOL"),
        trend_state     : "TRENDING" | "SIDEWAYS" | "MIXED",
        trend_score     : int (-1..4),
        skip_contrarian : bool,
        direction       : "up" | "down" | None,
        vol_state       : "EXTREME_LOW" | "LOW" | "NORMAL" | "HIGH" | "EXTREME_HIGH",
        vol_annual      : float | None  (BTC annualized realized vol),
        vol_ratio       : float         (vol / 0.44 BTC baseline),
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

    # Fetch BTC realized vol as market-wide volatility proxy
    vol_annual: float | None = None
    vol_data: dict = {"vol_state": "NORMAL", "vol_ratio": 1.0}
    try:
        from src.api.binance_client import fetch_realized_vol
        vol_annual = await fetch_realized_vol("BTC", session)
        if vol_annual is not None:
            vol_data = classify_volatility(vol_annual)
    except Exception as _ve:
        logger.debug(f"[REGIME] Vol fetch error: {_ve}")

    composite = composite_regime(cross, htf, sess, vol_state=vol_data["vol_state"])

    result = {
        **composite,
        "vol_state":  vol_data["vol_state"],
        "vol_annual": vol_annual,
        "vol_ratio":  vol_data["vol_ratio"],
        "cross_asset": cross,
        "higher_tf":   htf,
        "session":     sess,
    }

    if use_cache:
        _regime_cache["result"] = result
        _regime_cache["ts"]     = _time.monotonic()

    return result
