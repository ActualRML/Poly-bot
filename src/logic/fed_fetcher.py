"""
src/logic/fed_fetcher.py
========================
Auto-fetch data Fed dari FRED API + Polymarket price sebagai base rate.

Cara kerja:
1. Fetch current Fed Funds Rate dari FRED API (akurat, resmi)
2. Fetch harga market Fed dari Polymarket (pakai sebagai base rate)
3. Bot bandingkan base rate vs market price → deteksi mispricing

Kenapa Polymarket price sebagai base rate?
- Polymarket Fed market sangat liquid ($127M volume)
- Price-nya sangat akurat (99.4% no change = CME FedWatch)
- Kalau ada market Fed lain yang price berbeda → ada edge!

Cache: 1 jam untuk Fed rate, 5 menit untuk Polymarket price
"""

import asyncio
import aiohttp
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Cache
_cache: dict      = {}
_cache_time: dict = {}
_RATE_TTL         = 3600   # 1 jam untuk Fed rate
_PRICE_TTL        = 300    # 5 menit untuk Polymarket price
_fed_lock         = asyncio.Lock()  # prevent race condition saat parallel fetch


# ─────────────────────────────────────────────
# DATA CLASS
# ─────────────────────────────────────────────

@dataclass
class FedData:
    """Data Fed yang sudah di-fetch."""
    current_rate: float        # Rate sekarang dari FRED (misal 3.75)
    source: str                # "fred" atau "fallback"
    notes: str


@dataclass 
class FedMarketPrice:
    """Harga market Fed dari Polymarket."""
    question: str
    yes_price: float           # Harga Yes di Polymarket
    outcome_type: str          # "no_change", "cut_25", "cut_50", "hike_25"
    condition_id: str


# ─────────────────────────────────────────────
# FETCH CURRENT FED RATE (FRED API)
# ─────────────────────────────────────────────

async def fetch_current_fed_rate(
    session: aiohttp.ClientSession,
    api_key: str,
) -> FedData:
    """
    Fetch current Fed Funds Rate dari FRED API.
    Series: DFEDTARU (Fed Funds Upper Target Rate)
    
    Returns FedData dengan current rate.
    Fallback ke 3.75% kalau fetch gagal.
    """
    now = datetime.now(timezone.utc).timestamp()

    if "fed_rate" in _cache:
        if now - _cache_time.get("fed_rate", 0) < _RATE_TTL:
            return _cache["fed_rate"]

    if not api_key:
        logger.warning("[FED] FRED_API_KEY tidak ada, pakai fallback")
        return _fallback_fed_data()

    try:
        async with session.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id":  "DFEDTARU",
                "api_key":    api_key,
                "file_type":  "json",
                "limit":      1,
                "sort_order": "desc",
            },
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            obs  = data.get("observations", [])

            if not obs:
                return _fallback_fed_data()

            rate   = float(obs[0]["value"])
            date   = obs[0]["date"]
            result = FedData(
                current_rate = rate,
                source       = "fred",
                notes        = f"FRED DFEDTARU per {date}: {rate}%",
            )

            _cache["fed_rate"]      = result
            _cache_time["fed_rate"] = now

            logger.info(f"[FED] Current rate: {rate}% (FRED)")
            return result

    except Exception as e:
        logger.warning(f"[FED] Gagal fetch FRED: {e}")
        return _fallback_fed_data()


def _fallback_fed_data() -> FedData:
    return FedData(
        current_rate = 3.75,
        source       = "fallback",
        notes        = "Hardcoded fallback — update FRED_API_KEY di .env",
    )


# ─────────────────────────────────────────────
# FETCH FED MARKET PRICES FROM POLYMARKET
# ─────────────────────────────────────────────

async def fetch_fed_market_prices(
    session: aiohttp.ClientSession,
) -> list[FedMarketPrice]:
    """
    Fetch harga market Fed dari Gamma API Polymarket.
    
    Ini yang dipakai sebagai BASE RATE di bot:
    - Market "no change" price = probabilitas no change
    - Market "decrease 25bps" price = probabilitas cut
    - dll
    
    Returns list FedMarketPrice yang bisa dipakai sebagai base rate.
    """
    now = datetime.now(timezone.utc).timestamp()

    # Cek cache dulu tanpa lock (fast path)
    if "fed_prices" in _cache:
        if now - _cache_time.get("fed_prices", 0) < _PRICE_TTL:
            return _cache["fed_prices"]

    # Lock untuk prevent multiple fetch sekaligus
    async with _fed_lock:
        # Double-check setelah dapat lock (mungkin sudah di-fetch coroutine lain)
        if "fed_prices" in _cache:
            if now - _cache_time.get("fed_prices", 0) < _PRICE_TTL:
                return _cache["fed_prices"]

        try:
            async with session.get(
                "https://gamma-api.polymarket.com/markets",
                params={
                    "limit":     50,
                    "active":    "true",
                    "order":     "volume24hr",
                    "ascending": "false",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                resp.raise_for_status()
                markets = await resp.json()

            import json
            results = []

            for m in markets:
                q = (m.get("question") or "").lower()

                # Filter Fed market
                if not any(k in q for k in ["fed", "federal", "interest rate", "bps"]):
                    continue

                # Ambil harga Yes
                outcome_prices = m.get("outcomePrices", [])
                if isinstance(outcome_prices, str):
                    try:
                        outcome_prices = json.loads(outcome_prices)
                    except Exception:
                        continue

                if not outcome_prices:
                    continue

                yes_price    = float(outcome_prices[0])
                condition_id = m.get("conditionId", m.get("id", ""))

                # Classify outcome type
                if "no change" in q or "unchanged" in q:
                    outcome_type = "no_change"
                elif "decrease" in q and "25" in q:
                    outcome_type = "cut_25"
                elif "decrease" in q and "50" in q:
                    outcome_type = "cut_50"
                elif "increase" in q:
                    outcome_type = "hike_25"
                else:
                    continue

                results.append(FedMarketPrice(
                    question     = m.get("question", ""),
                    yes_price    = yes_price,
                    outcome_type = outcome_type,
                    condition_id = condition_id,
                ))

                logger.debug(
                    f"[FED PRICE] {outcome_type}: {yes_price:.3f} | "
                    f"{m.get('question','')[:50]}"
                )

            if results:
                _cache["fed_prices"]      = results
                _cache_time["fed_prices"] = now
                logger.info(f"[FED] Fetched {len(results)} Fed market prices dari Polymarket")

            return results

        except Exception as e:
            logger.warning(f"[FED] Gagal fetch Fed market prices: {e}")
            return []


# ─────────────────────────────────────────────
# GET BASE RATE UNTUK MARKET TERTENTU
# ─────────────────────────────────────────────

async def get_fed_base_rate(
    question: str,
    session: aiohttp.ClientSession,
) -> Optional[float]:
    """
    Get base rate untuk satu Fed market question.
    
    Logika:
    - Fetch semua harga Fed market dari Polymarket
    - Match question dengan outcome type
    - Return harga Yes sebagai base rate
    
    Args:
        question: Pertanyaan market (lowercase)
        session : aiohttp session
    
    Returns:
        float: Base rate (0-1) atau None kalau tidak ketemu
    """
    fed_prices = await fetch_fed_market_prices(session)

    if not fed_prices:
        # Fallback ke hardcoded kalau fetch gagal
        return _hardcoded_fed_rate(question)

    # Match question ke outcome type
    if "no change" in question or "unchanged" in question:
        target = "no_change"
    elif "decrease" in question and "25" in question:
        target = "cut_25"
    elif "decrease" in question and "50" in question:
        target = "cut_50"
    elif "increase" in question:
        target = "hike_25"
    else:
        return None

    # Cari market yang matching
    for fp in fed_prices:
        if fp.outcome_type == target:
            logger.debug(
                f"[FED BASE RATE] {target}: {fp.yes_price:.3f} "
                f"(dari Polymarket — auto)"
            )
            return fp.yes_price

    # Fallback kalau tidak ketemu
    return _hardcoded_fed_rate(question)


_FOMC_FALLBACK_DATE = "2026-04-23"

def _hardcoded_fed_rate(question: str) -> Optional[float]:
    """
    Hardcoded fallback — hanya dipakai kalau semua sumber live gagal.
    Update nilai ini setelah setiap FOMC meeting.
    """
    import logging
    from datetime import date
    logger = logging.getLogger(__name__)

    fallback_date = date.fromisoformat(_FOMC_FALLBACK_DATE)
    days_stale = (date.today() - fallback_date).days
    if days_stale > 45:
        logger.warning(
            f"[FED] Fallback rate sudah {days_stale} hari stale (per {_FOMC_FALLBACK_DATE}). "
            "Update _hardcoded_fed_rate() setelah FOMC meeting berikutnya."
        )

    q = question.lower()
    if "no change" in q or "unchanged" in q:
        return 0.989
    if "decrease" in q and "25" in q:
        return 0.008
    if "decrease" in q and "50" in q:
        return 0.001
    if "increase" in q:
        return 0.002
    return None


# ─────────────────────────────────────────────
# QUICK TEST
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    from pathlib import Path

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    FRED_KEY = os.getenv("FRED_API_KEY", "")

    async def test():
        print("=" * 60)
        print("FED FETCHER TEST — FRED + POLYMARKET")
        print("=" * 60)

        async with aiohttp.ClientSession() as session:
            # Test 1: FRED rate
            fed_data = await fetch_current_fed_rate(session, FRED_KEY)
            print(f"\n[FRED] {fed_data.notes}")

            # Test 2: Polymarket prices
            print("\n[POLYMARKET] Fed market prices:")
            prices = await fetch_fed_market_prices(session)
            for p in prices:
                print(f"  {p.outcome_type:12} → yes={p.yes_price:.3f} | {p.question[:50]}")

            # Test 3: Base rate untuk specific question
            print("\n[BASE RATE] Test per question:")
            test_questions = [
                "will there be no change in fed interest rates after the april",
                "will the fed decrease interest rates by 25 bps after the april",
                "will the fed increase interest rates by 25+ bps after the april",
            ]
            for q in test_questions:
                rate = await get_fed_base_rate(q, session)
                print(f"  {q[:50]} → {rate:.3f}" if rate else f"  {q[:50]} → None")

    asyncio.run(test())