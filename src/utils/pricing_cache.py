from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

from src.utils.config import config
from src.utils.logger import log

logger = logging.getLogger(__name__)

_price_cache: dict = {}
_cache_time: dict = {}
_CACHE_TTL = 300
_price_lock = asyncio.Lock()
_open_position_lock = asyncio.Lock()
_cg_ban_until: float = 0.0
_CG_BAN_COOLDOWN = 120


async def fetch_crypto_price(symbol: str, session: aiohttp.ClientSession) -> float | None:
    from src.api.binance_client import fetch_price as _binance_price
    symbol = symbol.upper()

    price = await _binance_price(symbol, session)
    if price:
        return price

    id_map = {
        "BTC":   "bitcoin",
        "ETH":   "ethereum",
        "SOL":   "solana",
        "XRP":   "ripple",
        "DOGE":  "dogecoin",
        "BNB":   "binancecoin",
        "MATIC": "matic-network",
    }

    coin_id = id_map.get(symbol)
    if not coin_id:
        return None

    async with _price_lock:
        global _cg_ban_until
        now = datetime.now(timezone.utc).timestamp()
        if symbol in _price_cache and now - _cache_time.get(symbol, 0) < _CACHE_TTL:
            return _price_cache[symbol]

        if now < _cg_ban_until:
            return _price_cache.get(symbol)

        try:
            async with session.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": coin_id, "vs_currencies": "usd"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 429:
                    _cg_ban_until = now + _CG_BAN_COOLDOWN
                    logger.warning(f"[COINGECKO] 429 — pause {_CG_BAN_COOLDOWN}s, pakai stale cache")
                    return _price_cache.get(symbol)
                resp.raise_for_status()
                data  = await resp.json()
                price = data.get(coin_id, {}).get("usd")

                if price:
                    _price_cache[symbol] = float(price)
                    _cache_time[symbol]  = now
                    log.info(f"[PRICE] {symbol} CoinGecko = ${float(price):,.4f}")
                    return float(price)

        except Exception as e:
            logger.warning(f"Gagal fetch harga {symbol}: {e}")

    return None


async def prefetch_prices(session: aiohttp.ClientSession) -> None:
    await asyncio.gather(
        fetch_crypto_price("BTC", session),
        fetch_crypto_price("ETH", session),
        fetch_crypto_price("SOL", session),
        fetch_crypto_price("BNB", session),
        return_exceptions=True,
    )


async def build_vol_data(session: aiohttp.ClientSession, hours: int | None = None) -> dict:
    from src.api.binance_client import fetch_realized_vol
    if hours is None:
        hours = getattr(config, "HOURLY_VOL_HOURS", 4)

    symbols = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE"]
    results = await asyncio.gather(
        *(fetch_realized_vol(s, session, hours=hours) for s in symbols),
        return_exceptions=True,
    )
    vol_data: dict = {"DEFAULT": 0.40}
    for symbol, result in zip(symbols, results):
        if isinstance(result, float) and result > 0:
            vol_data[symbol] = result
    return vol_data
