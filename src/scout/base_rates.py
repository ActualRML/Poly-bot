from __future__ import annotations

import logging

import aiohttp

from src.utils.config import config
from src.utils.parsing import extract_price_target
from src.utils.pricing_cache import fetch_crypto_price

logger = logging.getLogger(__name__)


async def get_base_rates(
    market: dict, builder, session: aiohttp.ClientSession,
    vol_data: dict | None = None,
) -> list:
    from src.risk.probability import CryptoProbabilityCalculator
    from datetime import datetime, timezone

    question     = (market.get("question") or "").lower()
    calc         = CryptoProbabilityCalculator()
    end_date_str = market.get("endDate") or market.get("end_date_iso", "")

    days_remaining: float = 30.0
    try:
        end_date  = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        delta_sec = (end_date - datetime.now(timezone.utc)).total_seconds()
        days_remaining = max(0.001, delta_sec / 86400.0)
    except Exception:
        pass

    if any(k in question for k in ["between", "range", "dip to"]):
        return []

    if "up or down" in question:
        return []

    async def _check_crypto(symbol: str, keywords: list[str]) -> list | None:
        if not any(k in question for k in keywords):
            return None
        price = await fetch_crypto_price(symbol, session)
        if not price:
            return []
        target = extract_price_target(question)
        if not target:
            return []
        direction = "below" if any(k in question for k in ["dip", "drop", "fall", "below", "↓"]) else "above"
        use_barrier = " on " not in question

        if vol_data and symbol in vol_data:
            volatility = vol_data[symbol]
        else:
            from src.api.binance_client import fetch_realized_vol
            volatility = await fetch_realized_vol(symbol, session, hours=getattr(config, "HOURLY_VOL_HOURS", 4))

        result = await calc.calculate_async(
            symbol, price, target, days_remaining, session,
            direction=direction, use_barrier=use_barrier,
            volatility=volatility, drift=None,
        )
        if result.probability == 0.0:
            return []
        return [builder.from_manual(rate=result.probability, confidence=result.confidence, notes=result.notes)]

    for symbol, keywords in [
        ("BTC",  ["bitcoin", "btc"]),
        ("ETH",  ["ethereum", " eth ", "ether "]),
        ("SOL",  ["solana", " sol "]),
        ("BNB",  [" bnb ", "binance coin"]),
    ]:
        result = await _check_crypto(symbol, keywords)
        if result is not None:
            return result

    return []
