
from __future__ import annotations

import asyncio
import re
import math
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

_STRIKE_TS_THRESHOLD_S: float = 120.0  # reject kline if open_time drifts > 2m from start_date

SYMBOL_KEYWORDS = {
    "BTC":  ["bitcoin", "btc"],
    "ETH":  ["ethereum", "eth"],
    "SOL":  ["solana", "sol"],
    "BNB":  ["bnb", "binance coin"],
    "XRP":  ["xrp", "ripple"],
    "DOGE": ["dogecoin", "doge"],
}

_ref_cache: dict[str, tuple[float, float]] = {}
_REF_CACHE_TTL = 300

def detect_updown_market(question: str, outcomes: list = None) -> Optional[tuple[str, str]]:

    if outcomes:
        outcomes_lower = [str(o).lower() for o in outcomes]
        if "up" in outcomes_lower and "down" in outcomes_lower:
            pass
        else:
            return None
    elif "up or down" not in question.lower():
        return None

    q = question.lower()
    symbol = None
    for sym, keywords in SYMBOL_KEYWORDS.items():
        if any(kw in q for kw in keywords):
            symbol = sym
            break

    if not symbol:
        logger.debug(f"[UPDOWN] Tidak bisa deteksi symbol dari: {question[:50]}")
        return None

    return (symbol, "Up")

async def fetch_reference_price(
    symbol: str,
    session: aiohttp.ClientSession,
) -> Optional[float]:

    from src.api.binance_client import fetch_klines

    symbol = symbol.upper()
    now = datetime.now(timezone.utc).timestamp()

    if symbol in _ref_cache:
        price, ts = _ref_cache[symbol]
        if now - ts < _REF_CACHE_TTL:
            return price

    yesterday_noon = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
        hour=16, minute=0, second=0, microsecond=0
    )
    start_ms = int(yesterday_noon.timestamp() * 1000)

    klines = await fetch_klines(symbol, session, interval="1m", limit=1,
                                 start_ms=start_ms)
    if not klines:
        logger.warning(f"[UPDOWN] Gagal fetch noon ET reference untuk {symbol}")
        return None

    ref_price = klines[-1][4]
    if ref_price <= 0:
        return None

    _ref_cache[symbol] = (ref_price, now)
    logger.debug(
        f"[UPDOWN] {symbol} reference (noon ET prev day, 16:00 UTC) = ${ref_price:,.4f}"
    )
    return ref_price

async def calculate_updown_probability(
    symbol: str,
    session: aiohttp.ClientSession,
    vol_data: dict,
    market_end_date: datetime,
) -> Optional[float]:

    from src.api.binance_client import fetch_price

    now = datetime.now(timezone.utc)

    delta_sec = (market_end_date - now).total_seconds()
    if delta_sec <= 0:
        logger.debug(f"[UPDOWN] {symbol} market sudah expired")
        return None
    T = delta_sec / 86400.0

    current_price = await fetch_price(symbol, session)
    if not current_price:
        logger.warning(f"[UPDOWN] Gagal fetch current price {symbol}")
        return None

    reference_price = await fetch_reference_price(symbol, session)
    if not reference_price:
        logger.warning(f"[UPDOWN] Gagal fetch reference price {symbol}")
        return None

    vol = vol_data.get(symbol) or vol_data.get("DEFAULT") or 0.40

    mu_adj = -0.5 * vol ** 2
    try:
        denominator = vol * math.sqrt(T)
        if denominator == 0:
            return None
        d2 = (math.log(current_price / reference_price) + mu_adj * T) / denominator
        prob_up = _norm_cdf(d2)
    except (ValueError, ZeroDivisionError) as e:
        logger.warning(f"[UPDOWN] Kalkulasi error {symbol}: {e}")
        return None

    pct_from_ref = (current_price - reference_price) / reference_price * 100
    logger.debug(
        f"[UPDOWN] {symbol} current=${current_price:,.2f} ref=${reference_price:,.2f} "
        f"({pct_from_ref:+.2f}%) T={T*24:.1f}h vol={vol:.0%} → P(Up)={prob_up:.3f}"
    )
    return prob_up

async def fetch_reference_price_hourly(
    symbol: str,
    session: aiohttp.ClientSession,
    start_date: datetime,
) -> Optional[float]:
    from src.api.binance_client import fetch_klines

    symbol     = symbol.upper()
    # Round down to clean hour — Polymarket start_date has minute offset (e.g. 01:07 UTC)
    # so Binance with start_ms=01:07 would skip the 01:00 candle and return 02:00 instead.
    start_hour = start_date.replace(minute=0, second=0, microsecond=0)
    start_ms   = int(start_hour.timestamp() * 1000)
    end_ms     = start_ms + 3_600_000

    klines = await fetch_klines(symbol, session, interval="1h", limit=1,
                                 start_ms=start_ms, end_ms=end_ms)
    if not klines:
        logger.warning(f"[UPDOWN HOURLY] Gagal fetch reference price {symbol}")
        return None

    ref_price = klines[0][1]
    if ref_price <= 0:
        return None

    kline_open_time: datetime = klines[0][0]
    drift_s = abs((kline_open_time - start_hour).total_seconds())
    if drift_s > _STRIKE_TS_THRESHOLD_S:
        logger.warning(
            f"[UPDOWN HOURLY] {symbol} strike timestamp mismatch: "
            f"got {kline_open_time.strftime('%H:%M UTC')} "
            f"expected {start_hour.strftime('%H:%M UTC')} "
            f"(drift={drift_s:.0f}s) — skip"
        )
        return None

    logger.debug(f"[UPDOWN HOURLY] {symbol} reference (1h open @ {start_hour.strftime('%H:%M UTC')}) = ${ref_price:,.4f}")
    return ref_price

async def calculate_updown_probability_hourly(
    symbol: str,
    session: aiohttp.ClientSession,
    vol_data: dict,
    market_end_date: datetime,
    market_start_date: datetime,
) -> Optional[float]:
    from src.api.binance_client import fetch_price

    now = datetime.now(timezone.utc)

    delta_sec = (market_end_date - now).total_seconds()
    if delta_sec <= 0:
        logger.debug(f"[UPDOWN HOURLY] {symbol} market sudah expired")
        return None
    T = delta_sec / 86400.0

    current_price = await fetch_price(symbol, session)
    if not current_price:
        logger.warning(f"[UPDOWN HOURLY] Gagal fetch current price {symbol}")
        return None

    reference_price = await fetch_reference_price_hourly(symbol, session, market_start_date)
    if not reference_price:
        return None

    vol = vol_data.get(symbol) or vol_data.get("DEFAULT") or 0.40

    mu_adj = -0.5 * vol ** 2
    try:
        denominator = vol * math.sqrt(T)
        if denominator == 0:
            return None
        d2 = (math.log(current_price / reference_price) + mu_adj * T) / denominator
        prob_up = _norm_cdf(d2)
    except (ValueError, ZeroDivisionError) as e:
        logger.warning(f"[UPDOWN HOURLY] Kalkulasi error {symbol}: {e}")
        return None

    pct_from_ref = (current_price - reference_price) / reference_price * 100
    logger.debug(
        f"[UPDOWN HOURLY] {symbol} current=${current_price:,.2f} ref=${reference_price:,.2f} "
        f"({pct_from_ref:+.2f}%) T={T*24*60:.0f}m vol={vol:.0%} → P(Up)={prob_up:.3f}"
    )
    return prob_up

async def calculate_multi_tf_momentum(
    symbol: str,
    session: aiohttp.ClientSession,
) -> Optional[dict]:
    """
    Multi-timeframe momentum + volume confirmation for a single asset.

    Fetches 30 bars of 1m extended klines (single API call) and computes:
      - m_5m, m_15m, m_30m  : signed % move over each lookback
      - vol_ratio           : recent 5-bar avg volume / 30-bar avg volume
      - direction           : "up" / "down" / None (consensus across timeframes)
      - in_sweet_spot       : True if 15m momentum in [thr, max] and other TFs agree
      - all_tf_aligned      : True if 5m, 15m, 30m all same sign
    Returns None if data insufficient.
    """
    from src.api.binance_client import fetch_klines_extended
    klines = await fetch_klines_extended(symbol, session, interval="1m", limit=30)
    if len(klines) < 30:
        return None

    closes  = [k[4] for k in klines]
    volumes = [k[5] for k in klines]

    def _pct(idx_lookback: int) -> float:
        if idx_lookback >= len(closes) or closes[-1 - idx_lookback] <= 0:
            return 0.0
        return (closes[-1] - closes[-1 - idx_lookback]) / closes[-1 - idx_lookback]

    m_5m  = _pct(5)
    m_15m = _pct(15)
    m_30m = _pct(29)

    recent_vol_avg   = sum(volumes[-5:]) / 5
    baseline_vol_avg = sum(volumes) / len(volumes)
    vol_ratio = recent_vol_avg / baseline_vol_avg if baseline_vol_avg > 0 else 1.0

    signs = [1 if m > 0 else (-1 if m < 0 else 0) for m in (m_5m, m_15m, m_30m)]
    pos_count = sum(1 for s in signs if s > 0)
    neg_count = sum(1 for s in signs if s < 0)
    if pos_count >= 2:
        direction = "up"
    elif neg_count >= 2:
        direction = "down"
    else:
        direction = None
    all_tf_aligned = abs(pos_count - neg_count) == 3

    return {
        "m_5m":           round(m_5m,  5),
        "m_15m":          round(m_15m, 5),
        "m_30m":          round(m_30m, 5),
        "vol_ratio":      round(vol_ratio, 3),
        "direction":      direction,
        "all_tf_aligned": all_tf_aligned,
    }


async def calculate_recent_momentum(
    symbol: str,
    session: aiohttp.ClientSession,
    minutes: int = 15,
) -> Optional[float]:
    from src.api.binance_client import fetch_klines, fetch_price

    now_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - minutes * 60 * 1000

    klines = await fetch_klines(symbol, session, interval="1m", limit=1, start_ms=start_ms)
    if not klines:
        return None

    past_price    = klines[0][1]
    current_price = await fetch_price(symbol, session)
    if not current_price or past_price <= 0:
        return None

    momentum = (current_price - past_price) / past_price
    logger.debug(
        f"[MOMENTUM] {symbol} {minutes}m: ${past_price:,.2f} → ${current_price:,.2f} "
        f"= {momentum:+.3%}"
    )
    return momentum


async def calculate_scalping_signals(
    symbol: str,
    session: aiohttp.ClientSession,
    hma_period: int = 9,
    rsi_period: int = 7,
    macd_fast: int = 8,
    macd_slow: int = 21,
    macd_signal: int = 5,
) -> Optional[dict]:
    """
    Full-stack scalping analyzer (3 layers):
    Layer 1 — 1m micro: HMA(9) + RSI(7) + MACD(8,21,5) + Heikin Ashi + Renko(ATR)
    Layer 2 — Order flow: CVD divergence + Absorption + POC/Trapped traders
    Layer 3 — 5m master trend: HMA + HA gate, trend_factor x1.5/x0.5

    momentum_score = indicator_score + order_flow_adjustment (clipped ±1)
    effective_score = momentum_score * trend_factor (clipped ±1)
    confidence = 0.95 when technical + trend + trapped traders all align
    """
    from src.api.binance_client import fetch_klines, fetch_klines_extended

    limit_1m = max(macd_slow + macd_signal + 5, hma_period * 3, rsi_period + 15, 20)
    klines_ext_1m, klines_5m = await asyncio.gather(
        fetch_klines_extended(symbol, session, interval="1m", limit=limit_1m),
        fetch_klines(symbol, session, interval="5m", limit=30),
    )
    if len(klines_ext_1m) < rsi_period + 1:
        return None

    # klines_ext_1m[i] = (ts, O, H, L, C, vol, taker_buy_vol)
    # Positions 1-4 match standard klines so existing helpers work unchanged
    closes_1m = [k[4] for k in klines_ext_1m]

    # ── Layer 1a: HMA / RSI / MACD ────────────────────────────────────────
    hma = _hma_series(closes_1m, hma_period)
    hma_direction = "neutral"
    if len(hma) >= 2:
        hma_direction = "up" if hma[-1] > hma[-2] else "down"

    rsi_val = _rsi(closes_1m, rsi_period)
    rsi_signal = "neutral"
    if rsi_val is not None:
        if rsi_val >= 75:
            rsi_signal = "overbought"
        elif rsi_val <= 25:
            rsi_signal = "oversold"

    macd_result = _macd_calc(closes_1m, macd_fast, macd_slow, macd_signal)

    score = 0.0
    n_sig = 0
    if hma_direction != "neutral":
        score += 1.0 if hma_direction == "up" else -1.0
        n_sig += 1
    if rsi_signal == "overbought":
        score -= 0.5; n_sig += 1
    elif rsi_signal == "oversold":
        score += 0.5; n_sig += 1
    if macd_result:
        cross, pending = macd_result["cross"], macd_result["pending_cross"]
        if cross == "golden" or pending == "approaching_golden":
            score += 1.0; n_sig += 1
        elif cross == "death" or pending == "approaching_death":
            score -= 1.0; n_sig += 1
        elif macd_result["histogram"] != 0:
            score += 0.3 if macd_result["histogram"] > 0 else -0.3

    # ── Layer 1b: Heikin Ashi + Renko ─────────────────────────────────────
    ha_bars_1m = _heikin_ashi(klines_ext_1m)
    ha_direction, ha_streak = _ha_trend(ha_bars_1m)

    atr_val  = _atr(klines_ext_1m, period=14)
    atr_avg  = _atr(klines_ext_1m, period=min(len(klines_ext_1m) - 1, 50)) or atr_val or 0.0
    brick_size = atr_val if atr_val else (closes_1m[-1] * 0.0005)
    renko = _renko_analysis(closes_1m, brick_size)
    renko_dir, renko_consec = renko["direction"], renko["consecutive"]
    noise_level = "low" if renko_consec >= 2 else ("medium" if renko_consec == 1 else "high")

    from src.logic.scalping_exit import volatility_kelly_mult as _vkm
    kelly_multiplier = _vkm(atr_val or 0.0, atr_avg)

    # ── Layer 2: Order flow ───────────────────────────────────────────────
    of_adjustment = 0.0
    of_context: dict = {}

    cvd_div = _cvd_divergence(klines_ext_1m)
    if cvd_div == "bearish_div":
        of_adjustment -= 0.3
        of_context["cvd"] = "CVD divergensi negatif — kenaikan adalah 'empty move'"
    elif cvd_div == "bullish_div":
        of_adjustment += 0.3
        of_context["cvd"] = "Tekanan Jual Melemah/Habis — CVD Higher Low"

    absorption = _absorption_check(klines_ext_1m)
    if absorption:
        of_context["absorption"] = (
            f"Whale: absorpsi di {absorption['location']} "
            f"({absorption['vol_ratio']:.1f}x avg vol)"
        )

    last_ext = klines_ext_1m[-1]
    poc_pos = _poc_position_approx(last_ext)
    of_context["poc"] = f"POC ~{poc_pos}"

    trapped = _trapped_traders(last_ext)
    if trapped == "buyers":
        of_adjustment -= 0.5
        of_context["trapped"] = "Potential Trapped Buyers"
    elif trapped == "sellers":
        of_adjustment += 0.5
        of_context["trapped"] = "Potential Trapped Sellers"

    # ── Final momentum score (indicator base + OF adjustment) ─────────────
    momentum_score = round(max(-1.0, min(1.0, score / max(n_sig, 1) + of_adjustment)), 3)

    # ── Layer 3: 5m master trend ──────────────────────────────────────────
    master_trend = "neutral"
    if klines_5m:
        ha_5m = _heikin_ashi(klines_5m)
        closes_5m = [k[4] for k in klines_5m]
        master_trend = _assess_master_trend(ha_5m, closes_5m)

    trend_bullish = master_trend in ("bullish", "strong_bullish")
    trend_bearish = master_trend in ("bearish", "strong_bearish")
    if (trend_bullish and momentum_score > 0) or (trend_bearish and momentum_score < 0):
        trend_factor, aligned = 1.5, True
    elif (trend_bullish and momentum_score < 0) or (trend_bearish and momentum_score > 0):
        trend_factor, aligned = 0.5, False
    else:
        trend_factor, aligned = 1.0, True

    effective_score = round(max(-1.0, min(1.0, momentum_score * trend_factor)), 3)

    # ── Action ────────────────────────────────────────────────────────────
    strong_counter = not aligned and master_trend.startswith("strong_")
    if noise_level == "high":
        action = "WAIT_NOISE"
        validation_msg = "High Noise: Renko tidak membentuk bata baru — Wait and See"
    elif strong_counter:
        action = "WAIT_TREND"
        validation_msg = f"Sinyal 30s diabaikan karena melawan tren utama ({master_trend})"
    else:
        action = "TRADE"
        validation_msg = (
            "Sinyal 30s aktif (tren 5m netral)" if master_trend == "neutral"
            else f"Sinyal 30s tervalidasi oleh tren 5m ({master_trend})"
        )

    # Upgrade: absorption + RSI extreme = STRONG_TRADE
    if action == "TRADE" and absorption:
        if absorption["location"] == "resistance" and rsi_signal == "overbought":
            action = "STRONG_TRADE"
        elif absorption["location"] == "support" and rsi_signal == "oversold":
            action = "STRONG_TRADE"

    # ── Confidence ────────────────────────────────────────────────────────
    technical_ok = abs(momentum_score) > 0.3
    if technical_ok and aligned and trapped is not None:
        confidence = 0.95
    else:
        confidence = round(0.5 + abs(effective_score) * 0.45, 2)

    rsi_str = f"{rsi_val:.1f}" if rsi_val is not None else "N/A"
    hist_str = f"{macd_result['histogram']:.6f}" if macd_result else "N/A"
    logger.debug(
        f"[SCALPING] {symbol} master={master_trend} HA={ha_direction}x{ha_streak} "
        f"Renko={renko_dir}x{renko_consec} HMA={hma_direction} RSI={rsi_str} "
        f"MACD={hist_str} CVD={cvd_div} trapped={trapped} "
        f"score={momentum_score:+.3f} eff={effective_score:+.3f} conf={confidence:.0%} → {action}"
    )
    return {
        "hma_direction":     hma_direction,
        "rsi":               round(rsi_val, 2) if rsi_val is not None else None,
        "rsi_signal":        rsi_signal,
        "macd":              macd_result,
        "momentum_score":    momentum_score,
        "ha_direction":      ha_direction,
        "ha_streak":         ha_streak,
        "renko_direction":   renko_dir,
        "renko_consecutive": renko_consec,
        "noise_level":       noise_level,
        "cvd_divergence":    cvd_div,
        "absorption":        absorption,
        "poc_position":      poc_pos,
        "trapped":           trapped,
        "order_flow":        of_context,
        "master_trend":      master_trend,
        "trend_factor":      trend_factor,
        "effective_score":   effective_score,
        "confidence":        confidence,
        "action":            action,
        "kelly_multiplier":  kelly_multiplier,
        "validation_msg":    validation_msg,
    }


def _wma(prices: list[float], period: int) -> float:
    n = min(period, len(prices))
    if n == 0:
        return 0.0
    subset = prices[-n:]
    denom = n * (n + 1) // 2
    return sum(p * (i + 1) for i, p in enumerate(subset)) / denom


def _hma_series(prices: list[float], period: int) -> list[float]:
    """Hull Moving Average: WMA(2*WMA(n/2) - WMA(n), sqrt(n))."""
    half = max(2, period // 2)
    sqrt_p = max(2, round(math.sqrt(period)))

    raw: list[float] = []
    for i in range(period - 1, len(prices)):
        w = prices[: i + 1]
        raw.append(2 * _wma(w, half) - _wma(w, period))

    result: list[float] = []
    for i in range(sqrt_p - 1, len(raw)):
        result.append(_wma(raw[: i + 1], sqrt_p))
    return result


def _rsi(closes: list[float], period: int = 7) -> Optional[float]:
    """RSI using Wilder's smoothing."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(0.0, d) for d in deltas]
    losses = [max(0.0, -d) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss < 1e-10:
        return 100.0
    return 100.0 - 100.0 / (1 + avg_gain / avg_loss)


def _ema_series(prices: list[float], period: int) -> list[float]:
    if len(prices) < period:
        return []
    k = 2.0 / (period + 1)
    ema = sum(prices[:period]) / period
    result = [ema]
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
        result.append(ema)
    return result


def _macd_calc(
    closes: list[float],
    fast: int = 8,
    slow: int = 21,
    signal: int = 5,
) -> Optional[dict]:
    """MACD(fast, slow, signal). Returns histogram, cross, and pending_cross."""
    if len(closes) < slow + signal:
        return None
    ema_f = _ema_series(closes, fast)
    ema_s = _ema_series(closes, slow)
    offset = len(ema_f) - len(ema_s)
    macd_line = [ema_f[i + offset] - ema_s[i] for i in range(len(ema_s))]
    sig_line = _ema_series(macd_line, signal)
    if not sig_line:
        return None
    off2 = len(macd_line) - len(sig_line)
    histogram = [macd_line[i + off2] - sig_line[i] for i in range(len(sig_line))]
    if len(histogram) < 2:
        return None
    hist_cur, hist_prev = histogram[-1], histogram[-2]
    cross: Optional[str] = None
    if hist_prev < 0 and hist_cur >= 0:
        cross = "golden"
    elif hist_prev > 0 and hist_cur <= 0:
        cross = "death"
    pending: Optional[str] = None
    if cross is None:
        if hist_cur < 0 and hist_cur > hist_prev:
            pending = "approaching_golden"
        elif hist_cur > 0 and hist_cur < hist_prev:
            pending = "approaching_death"
    return {"histogram": hist_cur, "cross": cross, "pending_cross": pending}


def _heikin_ashi(klines: list) -> list[tuple]:
    """Convert klines (ts, O, H, L, C) → Heikin Ashi (ha_o, ha_h, ha_l, ha_c)."""
    if not klines:
        return []
    _, o, h, l, c = klines[0][0], klines[0][1], klines[0][2], klines[0][3], klines[0][4]
    ha_o = (o + c) / 2
    ha_c = (o + h + l + c) / 4
    ha_h = max(h, ha_o, ha_c)
    ha_l = min(l, ha_o, ha_c)
    result = [(ha_o, ha_h, ha_l, ha_c)]
    for k in klines[1:]:
        o, h, l, c = k[1], k[2], k[3], k[4]
        prev_ha_o, _, _, prev_ha_c = result[-1]
        ha_o = (prev_ha_o + prev_ha_c) / 2
        ha_c = (o + h + l + c) / 4
        ha_h = max(h, ha_o, ha_c)
        ha_l = min(l, ha_o, ha_c)
        result.append((ha_o, ha_h, ha_l, ha_c))
    return result


def _ha_trend(ha_bars: list[tuple]) -> tuple[str, int]:
    """Returns (direction, consecutive_same_color_bars). Color: ha_close >= ha_open = bullish."""
    if not ha_bars:
        return "neutral", 0
    last_bull = ha_bars[-1][3] >= ha_bars[-1][0]
    streak = 0
    for bar in reversed(ha_bars):
        if (bar[3] >= bar[0]) == last_bull:
            streak += 1
        else:
            break
    return ("up" if last_bull else "down"), streak


def _atr(klines: list, period: int = 14) -> Optional[float]:
    """Average True Range over last `period` bars."""
    if len(klines) < period + 1:
        return None
    trs = []
    for i in range(1, len(klines)):
        h, l, prev_c = klines[i][2], klines[i][3], klines[i - 1][4]
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    return sum(trs[-period:]) / period


def _renko_analysis(closes: list[float], brick_size: float) -> dict:
    """Build Renko bricks and return direction + consecutive brick count."""
    if not closes or brick_size <= 0:
        return {"direction": "sideways", "consecutive": 0}
    bricks: list[str] = []
    level = closes[0]
    for price in closes[1:]:
        diff = price - level
        if diff >= brick_size:
            n = int(diff / brick_size)
            bricks.extend(["up"] * n)
            level += brick_size * n
        elif diff <= -brick_size:
            n = int(-diff / brick_size)
            bricks.extend(["down"] * n)
            level -= brick_size * n
    if not bricks:
        return {"direction": "sideways", "consecutive": 0}
    last_dir = bricks[-1]
    consecutive = 0
    for b in reversed(bricks):
        if b == last_dir:
            consecutive += 1
        else:
            break
    return {"direction": last_dir, "consecutive": consecutive}


def _assess_master_trend(ha_bars: list[tuple], closes_5m: list[float]) -> str:
    """Combine 5m HA color streak + HMA direction → trend label."""
    if not ha_bars or not closes_5m:
        return "neutral"
    ha_dir, streak = _ha_trend(ha_bars)
    hma = _hma_series(closes_5m, 9)
    hma_dir = "neutral"
    if len(hma) >= 2:
        hma_dir = "up" if hma[-1] > hma[-2] else "down"
    if ha_dir == "up" and hma_dir == "up":
        return "strong_bullish" if streak >= 3 else "bullish"
    if ha_dir == "down" and hma_dir == "down":
        return "strong_bearish" if streak >= 3 else "bearish"
    return "neutral"


def _cvd_series(klines_ext: list[tuple]) -> list[float]:
    """Cumulative Volume Delta: sum of (taker_buy_vol - taker_sell_vol) per bar."""
    cvd, cumulative = [], 0.0
    for k in klines_ext:
        buy_vol = k[6]
        sell_vol = k[5] - k[6]
        cumulative += buy_vol - sell_vol
        cvd.append(cumulative)
    return cvd


def _cvd_divergence(klines_ext: list[tuple]) -> Optional[str]:
    """
    Bearish divergence: price Higher High but CVD Lower High → 'bearish_div'.
    Bullish divergence: price Lower Low but CVD Higher Low → 'bullish_div'.
    """
    if len(klines_ext) < 3:
        return None
    cvd = _cvd_series(klines_ext)
    highs = [k[2] for k in klines_ext]
    lows = [k[3] for k in klines_ext]
    if highs[-1] > highs[-2] and cvd[-1] < cvd[-2]:
        return "bearish_div"
    if lows[-1] < lows[-2] and cvd[-1] > cvd[-2]:
        return "bullish_div"
    return None


def _absorption_check(klines_ext: list[tuple]) -> Optional[dict]:
    """
    Detects absorption in the last bar: volume > 2x avg AND spread < 0.5x avg.
    Returns {"location": "resistance"|"support", "vol_ratio": float} or None.
    """
    if len(klines_ext) < 3:
        return None
    ref = klines_ext[:-1]
    avg_vol = sum(k[5] for k in ref) / len(ref)
    avg_spread = sum(k[2] - k[3] for k in ref) / len(ref)
    if avg_vol <= 0 or avg_spread <= 0:
        return None
    last = klines_ext[-1]
    vol, spread = last[5], last[2] - last[3]
    if vol > avg_vol * 2.0 and spread < avg_spread * 0.5:
        buy_vol = last[6]
        sell_vol = vol - buy_vol
        location = "support" if buy_vol > sell_vol else "resistance"
        return {"location": location, "vol_ratio": round(vol / avg_vol, 2)}
    return None


def _poc_position_approx(bar: tuple) -> str:
    """
    Approximate POC position from candle anatomy (close position within range).
    Returns 'top', 'middle', or 'bottom'.
    bar = (ts, O, H, L, C, vol, taker_buy_vol)
    """
    _, _, h, l, c = bar[0], bar[1], bar[2], bar[3], bar[4]
    bar_range = h - l
    if bar_range < 1e-10:
        return "middle"
    close_pct = (c - l) / bar_range
    return "top" if close_pct > 0.7 else ("bottom" if close_pct < 0.3 else "middle")


def _trapped_traders(bar: tuple) -> Optional[str]:
    """
    Detects trapped traders from wick anatomy.
    Trapped buyers: bullish bar with upper wick > 40% of range.
    Trapped sellers: bearish bar with lower wick > 40% of range.
    bar = (ts, O, H, L, C, vol, taker_buy_vol)
    """
    _, o, h, l, c = bar[0], bar[1], bar[2], bar[3], bar[4]
    bar_range = h - l
    if bar_range < 1e-10:
        return None
    is_bullish = c >= o
    upper_wick = (h - c) / bar_range
    lower_wick = (c - l) / bar_range
    if is_bullish and upper_wick > 0.4:
        return "buyers"
    if not is_bullish and lower_wick > 0.4:
        return "sellers"
    return None


def _norm_cdf(x: float) -> float:
    import math
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

if __name__ == "__main__":
    import asyncio
    import logging

    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")

    test_questions = [
        "BTC Up or Down Daily",
        "ETH Up or Down Daily",
        "Solana Up or Down Daily",
        "BNB Up or Down Daily",
        "Will BTC be above $80,000 on May 5?",
        "XRP Up or Down Daily",
    ]

    print("=== detect_updown_market ===")
    for q in test_questions:
        result = detect_updown_market(q)
        print(f"  {q[:40]:<42} -> {result}")

    print("\n=== calculate_updown_probability ===")

    async def _smoke():
        vol_data = {"BTC": 0.30, "ETH": 0.50, "SOL": 0.40, "BNB": 0.25, "DEFAULT": 0.40}
        end_date = datetime.now(timezone.utc).replace(hour=23, minute=59, second=59)

        async with aiohttp.ClientSession() as session:
            for symbol in ["BTC", "ETH", "SOL", "BNB"]:
                prob_up = await calculate_updown_probability(
                    symbol, session, vol_data, end_date
                )
                if prob_up is not None:
                    print(f"  {symbol}: P(Up)={prob_up:.3f}  P(Down)={1-prob_up:.3f}")
                else:
                    print(f"  {symbol}: gagal fetch data")

    asyncio.run(_smoke())
