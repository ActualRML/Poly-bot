from __future__ import annotations

import asyncio
import math
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

BINANCE_HOST = "https://api.binance.com"

SYMBOL_MAP = {
    "BTC":  "BTCUSDT",
    "ETH":  "ETHUSDT",
    "SOL":  "SOLUSDT",
    "BNB":  "BNBUSDT",
    "XRP":  "XRPUSDT",
    "DOGE": "DOGEUSDT",
}

_price_cache: dict[str, float] = {}
_price_cache_time: dict[str, float] = {}
_PRICE_TTL = 30

_vol_cache: dict[str, float] = {}
_vol_cache_time: dict[str, float] = {}
_VOL_TTL = 300

_tech_cache: dict[str, dict] = {}
_tech_cache_time: dict[str, float] = {}
_TECH_TTL = 60  # 1 min — technical signals refresh each minute

_ban_until: float = 0.0
_BAN_COOLDOWN = 300

# ── Rate-limit shield ─────────────────────────────────────────────────────────
_rate_weight_1m: int = 0          # last observed X-MBX-USED-WEIGHT-1M value
_WEIGHT_LIMIT: int = 1200         # Binance spot default limit per minute
_THROTTLE_PCT: float = 0.85       # reduce polling above this fraction
_PAUSE_PCT: float = 0.95          # full pause above this fraction
_rate_limit_status: str = "OK"    # "OK" | "THROTTLE" | "FULL_PAUSE"
_rate_limit_set_at: float = 0.0   # monotonic timestamp when status was last elevated
_RATE_WINDOW_S: float = 65.0      # Binance 1-min window + 5s buffer

def _check_rate_auto_reset() -> None:
    """Auto-reset FULL_PAUSE/THROTTLE after one Binance rate-limit window (65s)."""
    global _rate_limit_status
    import time as _time
    if _rate_limit_status != "OK" and _time.monotonic() - _rate_limit_set_at >= _RATE_WINDOW_S:
        logger.info(f"[BINANCE] Rate-limit window elapsed — reset status to OK")
        _rate_limit_status = "OK"

def _update_rate_weight(headers) -> None:
    global _rate_weight_1m, _rate_limit_status, _rate_limit_set_at
    import time as _time
    raw = headers.get("X-MBX-USED-WEIGHT-1M") or headers.get("x-mbx-used-weight-1m")
    if raw is None:
        return
    try:
        _rate_weight_1m = int(raw)
    except ValueError:
        return
    fraction = _rate_weight_1m / _WEIGHT_LIMIT
    if fraction >= _PAUSE_PCT:
        if _rate_limit_status != "FULL_PAUSE":
            logger.warning(
                f"[BINANCE] Rate weight {_rate_weight_1m}/{_WEIGHT_LIMIT} "
                f"({fraction:.0%}) — FULL_PAUSE to avoid IP ban"
            )
            _rate_limit_set_at = _time.monotonic()
        _rate_limit_status = "FULL_PAUSE"
    elif fraction >= _THROTTLE_PCT:
        if _rate_limit_status not in ("THROTTLE", "FULL_PAUSE"):
            logger.warning(
                f"[BINANCE] Rate weight {_rate_weight_1m}/{_WEIGHT_LIMIT} "
                f"({fraction:.0%}) — entering THROTTLE mode"
            )
            _rate_limit_set_at = _time.monotonic()
        _rate_limit_status = "THROTTLE"
    else:
        _rate_limit_status = "OK"

def get_rate_limit_status() -> dict:
    """Return current rate-limit consumption and status for health monitoring."""
    _check_rate_auto_reset()
    return {
        "weight_used": _rate_weight_1m,
        "weight_limit": _WEIGHT_LIMIT,
        "fraction": round(_rate_weight_1m / _WEIGHT_LIMIT, 4),
        "status": _rate_limit_status,
    }

_price_locks: dict[str, asyncio.Lock] = {}
_vol_locks:   dict[str, asyncio.Lock] = {}

def _get_price_lock(symbol: str) -> asyncio.Lock:
    if symbol not in _price_locks:
        _price_locks[symbol] = asyncio.Lock()
    return _price_locks[symbol]

def _get_vol_lock(cache_key: str) -> asyncio.Lock:
    if cache_key not in _vol_locks:
        _vol_locks[cache_key] = asyncio.Lock()
    return _vol_locks[cache_key]

async def fetch_price(symbol: str, session: aiohttp.ClientSession) -> Optional[float]:
    global _ban_until
    symbol = symbol.upper()
    ticker = SYMBOL_MAP.get(symbol)
    if not ticker:
        return None

    now = datetime.now(timezone.utc).timestamp()

    if symbol in _price_cache and now - _price_cache_time.get(symbol, 0) < _PRICE_TTL:
        return _price_cache[symbol]

    _check_rate_auto_reset()
    if now < _ban_until or _rate_limit_status == "FULL_PAUSE":
        logger.debug(f"[BINANCE] {symbol} ban/pause aktif, pakai stale cache")
        return _price_cache.get(symbol)

    async with _get_price_lock(symbol):
        now = datetime.now(timezone.utc).timestamp()
        if symbol in _price_cache and now - _price_cache_time.get(symbol, 0) < _PRICE_TTL:
            return _price_cache[symbol]
        if now < _ban_until or _rate_limit_status == "FULL_PAUSE":
            return _price_cache.get(symbol)

        try:
            async with session.get(
                f"{BINANCE_HOST}/api/v3/ticker/price",
                params={"symbol": ticker},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status in (418, 429):
                    _ban_until = now + _BAN_COOLDOWN
                    logger.warning(
                        f"[BINANCE] Rate limit ({resp.status}) — pause {_BAN_COOLDOWN}s. "
                        f"Pakai stale cache kalau ada."
                    )
                    return _price_cache.get(symbol)
                _update_rate_weight(resp.headers)
                resp.raise_for_status()
                data = await resp.json()
                price = float(data["price"])
                _price_cache[symbol] = price
                _price_cache_time[symbol] = now
                logger.debug(f"[BINANCE] {symbol} = ${price:,.4f}")
                return price
        except Exception as e:
            logger.warning(f"[BINANCE] Price fetch gagal {symbol}: {e}")
            return _price_cache.get(symbol)

async def fetch_klines(
    symbol: str,
    session: aiohttp.ClientSession,
    interval: str = "1h",
    limit: int = 48,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> list[tuple[datetime, float, float, float, float]]:
    global _ban_until
    ticker = SYMBOL_MAP.get(symbol.upper())
    if not ticker:
        return []

    params: dict = {"symbol": ticker, "interval": interval, "limit": limit}
    if start_ms is not None:
        params["startTime"] = start_ms
    if end_ms is not None:
        params["endTime"] = end_ms

    now = datetime.now(timezone.utc).timestamp()
    _check_rate_auto_reset()
    if now < _ban_until or _rate_limit_status == "FULL_PAUSE":
        logger.debug(f"[BINANCE] Klines {symbol} skip — ban/pause aktif")
        return []

    try:
        async with session.get(
            f"{BINANCE_HOST}/api/v3/klines",
            params=params,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status in (418, 429):
                _ban_until = now + _BAN_COOLDOWN
                logger.warning(f"[BINANCE] Rate limit ({resp.status}) klines — pause {_BAN_COOLDOWN}s")
                return []
            _update_rate_weight(resp.headers)
            resp.raise_for_status()
            data = await resp.json()

        result = []
        for row in data:
            ts = datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc)
            o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
            result.append((ts, o, h, l, c))
        return result
    except Exception as e:
        logger.warning(f"[BINANCE] Klines fetch gagal {symbol}: {e}")
        return []


async def fetch_klines_extended(
    symbol: str,
    session: aiohttp.ClientSession,
    interval: str = "1m",
    limit: int = 30,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> list[tuple]:
    """
    Same as fetch_klines but also captures volume and taker-buy volume.
    Returns (ts, open, high, low, close, volume, taker_buy_vol) tuples.
    taker_buy_vol is the market-buy (aggressive buyer) volume per bar.
    """
    global _ban_until
    ticker = SYMBOL_MAP.get(symbol.upper())
    if not ticker:
        return []

    params: dict = {"symbol": ticker, "interval": interval, "limit": limit}
    if start_ms is not None:
        params["startTime"] = start_ms
    if end_ms is not None:
        params["endTime"] = end_ms

    now = datetime.now(timezone.utc).timestamp()
    if now < _ban_until or _rate_limit_status == "FULL_PAUSE":
        logger.debug(f"[BINANCE] Klines ext {symbol} skip — ban/pause aktif")
        return []

    try:
        async with session.get(
            f"{BINANCE_HOST}/api/v3/klines",
            params=params,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status in (418, 429):
                _ban_until = now + _BAN_COOLDOWN
                logger.warning(f"[BINANCE] Rate limit ({resp.status}) klines ext — pause {_BAN_COOLDOWN}s")
                return []
            _update_rate_weight(resp.headers)
            resp.raise_for_status()
            data = await resp.json()

        result = []
        for row in data:
            ts = datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc)
            o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
            vol = float(row[5])
            taker_buy_vol = float(row[9]) if len(row) > 9 else vol / 2
            result.append((ts, o, h, l, c, vol, taker_buy_vol))
        return result
    except Exception as e:
        logger.warning(f"[BINANCE] Klines extended fetch gagal {symbol}: {e}")
        return []

async def fetch_spot_depth(
    symbol: str,
    session: aiohttp.ClientSession,
    limit: int = 20,
) -> dict:
    """
    Fetch Binance spot order book depth via GET /api/v3/depth.
    Returns {"bids": [(price, size), ...], "asks": [(price, size), ...]}
    sorted descending for bids, ascending for asks.
    """
    global _ban_until
    ticker = SYMBOL_MAP.get(symbol.upper())
    if not ticker:
        return {"bids": [], "asks": []}

    now = datetime.now(timezone.utc).timestamp()
    if now < _ban_until or _rate_limit_status == "FULL_PAUSE":
        return {"bids": [], "asks": []}

    try:
        async with session.get(
            f"{BINANCE_HOST}/api/v3/depth",
            params={"symbol": ticker, "limit": limit},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status in (418, 429):
                _ban_until = now + _BAN_COOLDOWN
                logger.warning(f"[BINANCE] Rate limit ({resp.status}) depth — pause {_BAN_COOLDOWN}s")
                return {"bids": [], "asks": []}
            _update_rate_weight(resp.headers)
            resp.raise_for_status()
            data = await resp.json()

        bids = sorted(
            [(float(p), float(s)) for p, s in data.get("bids", [])],
            key=lambda x: x[0], reverse=True,
        )
        asks = sorted(
            [(float(p), float(s)) for p, s in data.get("asks", [])],
            key=lambda x: x[0],
        )
        return {"bids": bids, "asks": asks}
    except Exception as e:
        logger.warning(f"[BINANCE] Depth fetch gagal {symbol}: {e}")
        return {"bids": [], "asks": []}


def _compute_annualized_vol(closes: list[float]) -> Optional[float]:
    if len(closes) < 4:
        return None
    log_returns = [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
        if closes[i - 1] > 0 and closes[i] > 0
    ]
    if len(log_returns) < 3:
        return None
    n = len(log_returns)
    mean = sum(log_returns) / n
    variance = sum((r - mean) ** 2 for r in log_returns) / max(n - 1, 1)
    annualized = math.sqrt(variance) * math.sqrt(8760)
    return annualized if 0.02 <= annualized <= 10.0 else None

async def fetch_realized_vol(
    symbol: str,
    session: aiohttp.ClientSession,
    hours: int = 24,
) -> Optional[float]:
    symbol = symbol.upper()
    cache_key = f"{symbol}_{hours}h_vol"
    now = datetime.now(timezone.utc).timestamp()

    if cache_key in _vol_cache and now - _vol_cache_time.get(cache_key, 0) < _VOL_TTL:
        return _vol_cache[cache_key]

    if now < _ban_until:
        logger.debug(f"[BINANCE] {symbol} vol ban aktif, pakai stale cache")
        return _vol_cache.get(cache_key)

    async with _get_vol_lock(cache_key):
        now = datetime.now(timezone.utc).timestamp()
        if cache_key in _vol_cache and now - _vol_cache_time.get(cache_key, 0) < _VOL_TTL:
            return _vol_cache[cache_key]
        if now < _ban_until:
            return _vol_cache.get(cache_key)

        klines = await fetch_klines(symbol, session, interval="1h", limit=hours + 1)
        vol = _compute_annualized_vol([k[4] for k in klines])
        if vol is None:
            return _vol_cache.get(cache_key)

        _vol_cache[cache_key] = vol
        _vol_cache_time[cache_key] = now
        logger.info(f"[BINANCE] {symbol} realized vol {hours}h = {vol:.1%} (annualized)")
        return vol

async def fetch_technical_signals(
    symbol: str,
    session: aiohttp.ClientSession,
    rsi_period: int = 14,
    zscore_window: int = 20,
    vol_spike_mult: float = 3.0,
    trend_hours: int = 4,
) -> dict:
    """
    Returns RSI, Z-score, volume-spike, and trend from 1h Binance klines.
    Cached for _TECH_TTL seconds. Returns {} on fetch failure (caller skips filter).
    """
    from src.logic.technical import compute_rsi, compute_zscore, detect_volume_spike, compute_trend

    symbol = symbol.upper()
    now = datetime.now(timezone.utc).timestamp()
    cache_key = f"{symbol}_tech_{rsi_period}_{zscore_window}_{trend_hours}"

    if cache_key in _tech_cache and now - _tech_cache_time.get(cache_key, 0) < _TECH_TTL:
        return _tech_cache[cache_key]

    _check_rate_auto_reset()
    if now < _ban_until or _rate_limit_status == "FULL_PAUSE":
        return _tech_cache.get(cache_key) or {}

    limit = max(zscore_window, rsi_period, trend_hours) + 3
    klines = await fetch_klines_extended(symbol, session, interval="1h", limit=limit)
    if not klines:
        return {}

    closes  = [k[4] for k in klines]
    volumes = [k[5] for k in klines] if len(klines[0]) > 5 else []

    result: dict = {
        "rsi":       compute_rsi(closes, rsi_period),
        "zscore":    compute_zscore(closes, zscore_window),
        "vol_spike": detect_volume_spike(volumes, vol_spike_mult) if volumes else False,
        "trend_4h":  compute_trend(closes, trend_hours),
    }
    _tech_cache[cache_key] = result
    _tech_cache_time[cache_key] = now
    logger.debug(
        f"[BINANCE] {symbol} tech — RSI={result['rsi']} Z={result['zscore']} "
        f"trend={result['trend_4h']:.2%} spike={result['vol_spike']}"
        if result.get("trend_4h") is not None else
        f"[BINANCE] {symbol} tech — RSI={result['rsi']} Z={result['zscore']} spike={result['vol_spike']}"
    )
    return result


async def fetch_trend_bias(
    symbol: str,
    session: aiohttp.ClientSession,
) -> float:
    """
    Fetch 28 bars of 1h klines and return EMA-based trend bias in [-0.10, +0.10].
    Returns 0.0 on fetch failure (neutral — no bias applied).
    """
    from src.logic.technical import compute_trend_bias as _ctb

    symbol = symbol.upper()
    cache_key = f"{symbol}_trend_bias"
    now = datetime.now(timezone.utc).timestamp()

    if cache_key in _tech_cache and now - _tech_cache_time.get(cache_key, 0) < _TECH_TTL:
        cached = _tech_cache[cache_key]
        return cached.get("bias", 0.0)

    _check_rate_auto_reset()
    if now < _ban_until or _rate_limit_status == "FULL_PAUSE":
        cached = _tech_cache.get(cache_key) or {}
        return cached.get("bias", 0.0)

    klines = await fetch_klines_extended(symbol, session, interval="1h", limit=28)
    if not klines or len(klines) < 25:
        return 0.0

    closes = [k[4] for k in klines]
    bias = _ctb(closes)
    _tech_cache[cache_key] = {"bias": bias}
    _tech_cache_time[cache_key] = now
    logger.debug(f"[BINANCE] {symbol} trend_bias={bias:+.3f}")
    return bias


async def fetch_historical_realized_vol(
    symbol: str,
    session: aiohttp.ClientSession,
    at_time: datetime,
    hours: int = 24,
) -> Optional[float]:
    end_ms   = int(at_time.timestamp() * 1000)
    klines   = await fetch_klines(symbol, session, interval="1h", limit=hours + 1, end_ms=end_ms)
    return _compute_annualized_vol([k[4] for k in klines])

async def fetch_short_drift(
    symbol: str,
    session: aiohttp.ClientSession,
    hours: int = 4,
) -> float:
    klines = await fetch_klines(symbol, session, interval="1h", limit=hours + 1)
    if len(klines) < 2:
        return 0.0

    start_price = klines[0][4]
    end_price   = klines[-1][4]

    if start_price <= 0 or end_price <= 0:
        return 0.0

    hours_elapsed = len(klines) - 1
    if hours_elapsed <= 0:
        return 0.0

    drift = math.log(end_price / start_price) / (hours_elapsed / 8760.0)
    return max(-5.0, min(5.0, drift))

async def fetch_historical_short_drift(
    symbol: str,
    session: aiohttp.ClientSession,
    at_time: datetime,
    hours: int = 4,
) -> float:
    end_ms = int(at_time.timestamp() * 1000)
    klines = await fetch_klines(symbol, session, interval="1h", limit=hours + 1, end_ms=end_ms)
    if len(klines) < 2:
        return 0.0

    start_price = klines[0][4]
    end_price   = klines[-1][4]
    if start_price <= 0 or end_price <= 0:
        return 0.0

    hours_elapsed = len(klines) - 1
    if hours_elapsed <= 0:
        return 0.0

    drift = math.log(end_price / start_price) / (hours_elapsed / 8760.0)
    return max(-5.0, min(5.0, drift))

async def _smoke_test():
    import asyncio
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    async with aiohttp.ClientSession() as session:
        for symbol in ["BTC", "ETH", "SOL", "BNB"]:
            price = await fetch_price(symbol, session)
            vol   = await fetch_realized_vol(symbol, session, hours=24)
            drift = await fetch_short_drift(symbol, session, hours=4)
            print(f"{symbol}: price=${price:,.2f}  vol_24h={vol:.1%}  drift_4h={drift:+.0%}")

if __name__ == "__main__":
    import asyncio
    asyncio.run(_smoke_test())
