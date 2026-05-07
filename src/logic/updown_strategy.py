
from __future__ import annotations

import re
import math
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

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

    symbol   = symbol.upper()
    start_ms = int(start_date.timestamp() * 1000)
    end_ms   = start_ms + 3_600_000

    klines = await fetch_klines(symbol, session, interval="1h", limit=1,
                                 start_ms=start_ms, end_ms=end_ms)
    if not klines:
        logger.warning(f"[UPDOWN HOURLY] Gagal fetch reference price {symbol}")
        return None

    ref_price = klines[0][1]
    if ref_price <= 0:
        return None

    logger.debug(f"[UPDOWN HOURLY] {symbol} reference (1h open @ {start_date.strftime('%H:%M UTC')}) = ${ref_price:,.4f}")
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
