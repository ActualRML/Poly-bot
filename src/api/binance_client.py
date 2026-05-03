"""
src/api/binance_client.py
=========================
Fetch harga dan realized volatility dari Binance public API.
No auth required — public endpoints only.

Public API:
    fetch_price(symbol, session) -> float | None
    fetch_klines(symbol, session, interval, limit, start_ms, end_ms) -> list[...]
    fetch_realized_vol(symbol, session, hours=24) -> float | None
    fetch_historical_realized_vol(symbol, session, at_time, hours=24) -> float | None
    fetch_short_drift(symbol, session, hours=4) -> float
"""

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
_PRICE_TTL = 30  # 30 detik — cukup untuk hourly trading

_vol_cache: dict[str, float] = {}
_vol_cache_time: dict[str, float] = {}
_VOL_TTL = 300  # 5 menit

# IP ban tracking — Binance returns 418 saat IP di-ban
# Setelah kena 418, tahan request selama _BAN_COOLDOWN detik
_ban_until: float = 0.0
_BAN_COOLDOWN = 300  # 5 menit cooldown per 418

# Per-symbol lock — cegah cache stampede saat 43 coroutine paralel
# cek cache expired bersamaan dan semua kirim HTTP request sekaligus
_price_locks: dict[str, asyncio.Lock] = {}


def _get_price_lock(symbol: str) -> asyncio.Lock:
    if symbol not in _price_locks:
        _price_locks[symbol] = asyncio.Lock()
    return _price_locks[symbol]


async def fetch_price(symbol: str, session: aiohttp.ClientSession) -> Optional[float]:
    """Real-time price dari Binance. Cache 30 detik. Stale fallback saat 418/429."""
    global _ban_until
    symbol = symbol.upper()
    ticker = SYMBOL_MAP.get(symbol)
    if not ticker:
        return None

    now = datetime.now(timezone.utc).timestamp()

    # Cache hit — cek sebelum acquire lock agar mayoritas request tidak block
    if symbol in _price_cache and now - _price_cache_time.get(symbol, 0) < _PRICE_TTL:
        return _price_cache[symbol]

    # IP masih di-ban — kembalikan stale cache daripada spam request
    if now < _ban_until:
        logger.debug(f"[BINANCE] {symbol} ban aktif, pakai stale cache")
        return _price_cache.get(symbol)

    # Lock per-symbol: cegah cache stampede dari asyncio.gather paralel
    async with _get_price_lock(symbol):
        # Re-cek setelah acquire lock — coroutine lain mungkin sudah update cache
        now = datetime.now(timezone.utc).timestamp()
        if symbol in _price_cache and now - _price_cache_time.get(symbol, 0) < _PRICE_TTL:
            return _price_cache[symbol]
        if now < _ban_until:
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
                resp.raise_for_status()
                data = await resp.json()
                price = float(data["price"])
                _price_cache[symbol] = price
                _price_cache_time[symbol] = now
                logger.debug(f"[BINANCE] {symbol} = ${price:,.4f}")
                return price
        except Exception as e:
            logger.warning(f"[BINANCE] Price fetch gagal {symbol}: {e}")
            return _price_cache.get(symbol)  # stale fallback


async def fetch_klines(
    symbol: str,
    session: aiohttp.ClientSession,
    interval: str = "1h",
    limit: int = 48,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> list[tuple[datetime, float, float, float, float]]:
    """
    Fetch klines dari Binance.
    Return list of (open_time, open, high, low, close).
    Support historical via start_ms/end_ms (epoch ms).
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
    if now < _ban_until:
        logger.debug(f"[BINANCE] Klines {symbol} skip — ban aktif")
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


def _compute_annualized_vol(closes: list[float]) -> Optional[float]:
    """Annualized realized vol dari list harga close (hourly)."""
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
    annualized = math.sqrt(variance) * math.sqrt(8760)  # hourly → annualize (24*365)
    # Lower bound 2% — vol bisa sangat rendah di market tenang malam hari.
    # Reject hanya kalau zero/near-zero (data buruk) atau >1000% (parsing error).
    return annualized if 0.02 <= annualized <= 10.0 else None


async def fetch_realized_vol(
    symbol: str,
    session: aiohttp.ClientSession,
    hours: int = 24,
) -> Optional[float]:
    """
    Annualized realized vol dari last `hours` jam klines 1h.
    Lebih relevan untuk 1-hour market vs Deribit DVOL 30d.
    Cache 5 menit.
    """
    symbol = symbol.upper()
    cache_key = f"{symbol}_{hours}h_vol"
    now = datetime.now(timezone.utc).timestamp()

    if cache_key in _vol_cache and now - _vol_cache_time.get(cache_key, 0) < _VOL_TTL:
        return _vol_cache[cache_key]

    klines = await fetch_klines(symbol, session, interval="1h", limit=hours + 1)
    vol = _compute_annualized_vol([k[4] for k in klines])
    if vol is None:
        return None

    _vol_cache[cache_key] = vol
    _vol_cache_time[cache_key] = now
    logger.info(f"[BINANCE] {symbol} realized vol {hours}h = {vol:.1%} (annualized)")
    return vol


async def fetch_historical_realized_vol(
    symbol: str,
    session: aiohttp.ClientSession,
    at_time: datetime,
    hours: int = 24,
) -> Optional[float]:
    """
    Annualized realized vol pada titik waktu historis `at_time`.
    Dipakai oleh backtest — fetch klines SEBELUM at_time.
    No in-memory cache (backtest pakai disk cache sendiri).
    """
    end_ms   = int(at_time.timestamp() * 1000)
    klines   = await fetch_klines(symbol, session, interval="1h", limit=hours + 1, end_ms=end_ms)
    return _compute_annualized_vol([k[4] for k in klines])


async def fetch_short_drift(
    symbol: str,
    session: aiohttp.ClientSession,
    hours: int = 4,
) -> float:
    """
    Annualized drift dari last `hours` jam klines 1h.
    Dipakai sebagai mu untuk 1-hour probability model.
    Return 0.0 kalau gagal.
    """
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
    """
    Annualized drift pada titik waktu historis `at_time`. Dipakai backtest.
    """
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


# ─────────────────────────────────────────────
# QUICK TEST
# ─────────────────────────────────────────────

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
