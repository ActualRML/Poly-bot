"""Pure technical indicator functions for GBM entry filtering. No I/O."""
from __future__ import annotations
import math


def compute_rsi(closes: list[float], period: int = 14) -> float | None:
    """Simplified SMA-based RSI over the last `period` price changes."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(len(closes) - period, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100.0 - 100.0 / (1.0 + rs), 2)


def compute_zscore(closes: list[float], window: int = 20) -> float | None:
    """Z-score of the last close vs its rolling mean over `window` bars."""
    if len(closes) < window:
        return None
    recent = closes[-window:]
    mean = sum(recent) / window
    variance = sum((x - mean) ** 2 for x in recent) / window
    std = math.sqrt(variance)
    if std == 0:
        return 0.0
    return round((closes[-1] - mean) / std, 3)


def detect_volume_spike(volumes: list[float], multiplier: float = 3.0) -> bool:
    """True if the last bar's volume exceeds `multiplier` × prior-bar average."""
    if len(volumes) < 5:
        return False
    avg = sum(volumes[:-1]) / len(volumes[:-1])
    return avg > 0 and volumes[-1] > avg * multiplier


def compute_trend(closes: list[float], lookback: int = 4) -> float | None:
    """Percentage price change from `lookback` bars ago to now. Positive = uptrend."""
    if len(closes) < lookback + 1:
        return None
    prev = closes[-(lookback + 1)]
    curr = closes[-1]
    if prev <= 0:
        return None
    return round((curr - prev) / prev, 5)


def compute_ema(closes: list[float], period: int) -> float | None:
    """Standard EMA with SMA seed. Returns None if insufficient data."""
    if len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 6)


def compute_trend_bias(closes: list[float], max_bias: float = 0.10) -> float:
    """
    EMA-based directional bias in [-max_bias, +max_bias].

    Two components (0.05 each):
    - current vs 24h EMA: above = bullish, below = bearish
    - 6h EMA slope: rising = bullish, falling = bearish
    """
    bias = 0.0

    ema_24 = compute_ema(closes, 24)
    if ema_24 is not None:
        if closes[-1] > ema_24:
            bias += 0.05
        elif closes[-1] < ema_24:
            bias -= 0.05

    ema_6_now  = compute_ema(closes, 6)
    ema_6_prev = compute_ema(closes[:-3], 6) if len(closes) > 9 else None
    if ema_6_now is not None and ema_6_prev is not None:
        if ema_6_now > ema_6_prev:
            bias += 0.05
        elif ema_6_now < ema_6_prev:
            bias -= 0.05

    return round(max(-max_bias, min(max_bias, bias)), 3)
