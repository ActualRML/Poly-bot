"""
src/api/gamma_client.py
Gamma API — market discovery & metadata (no auth required)
Docs: https://gamma-api.polymarket.com

Async version: pakai aiohttp untuk non-blocking HTTP calls.
Tetap sediakan sync fallback untuk script standalone (monitor.py, dll).
"""

import asyncio
import aiohttp
import requests
import logging
from typing import Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class GammaClient:
    """
    Read-only client untuk Gamma API Polymarket.

    Dua mode:
    - Async: pakai aiohttp session (dari main loop)
    - Sync:  pakai requests (untuk script standalone)
    """

    def __init__(self, host: str = "https://gamma-api.polymarket.com"):
        self.host = host.rstrip("/")
        # Sync session — untuk backward compatibility
        self._sync_session = requests.Session()
        self._sync_session.headers.update({
            "Accept": "application/json",
            "User-Agent": "polymarket-bot/1.0"
        })

    # ─────────────────────────────────────────────
    # ASYNC HTTP
    # ─────────────────────────────────────────────

    async def _aget(
        self,
        endpoint: str,
        session: aiohttp.ClientSession,
        params: dict = None,
        _retries: int = 2,
    ) -> dict | list:
        """Async GET request dengan retry untuk timeout transient."""
        url = f"{self.host}{endpoint}"
        for attempt in range(_retries + 1):
            try:
                async with session.get(
                    url,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    resp.raise_for_status()
                    return await resp.json()
            except asyncio.TimeoutError:
                if attempt < _retries:
                    wait = 2 ** attempt  # 1s, 2s
                    logger.warning(f"Gamma timeout {endpoint} (attempt {attempt+1}), retry in {wait}s")
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"Gamma API timeout {endpoint} setelah {_retries+1} attempts")
                raise
            except Exception as e:
                logger.error(f"Gamma API async error endpoint={endpoint}: {e}")
                raise

    # ─────────────────────────────────────────────
    # SYNC HTTP (fallback)
    # ─────────────────────────────────────────────

    def _get(self, endpoint: str, params: dict = None) -> dict | list:
        """Sync GET request — untuk script standalone."""
        url = f"{self.host}{endpoint}"
        try:
            resp = self._sync_session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Gamma API error endpoint={endpoint}: {e}")
            raise

    # ─────────────────────────────────────────────
    # MARKET FETCHING — ASYNC
    # ─────────────────────────────────────────────

    async def aget_markets(
        self,
        session: aiohttp.ClientSession,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
        closed: bool = False,
        order: str = "volume24hr",
        ascending: bool = False,
    ) -> list[dict]:
        """Async: Ambil daftar market."""
        params = {
            "limit": limit,
            "offset": offset,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "order": order,
            "ascending": str(ascending).lower(),
        }
        return await self._aget("/markets", session, params=params)

    async def ascan_opportunities(
        self,
        session: aiohttp.ClientSession,
        min_volume: float = 10_000,
        min_liquidity: float = 5_000,
        max_days_to_resolve: int = 30,
        min_days_to_resolve: int = 1,
        limit: int = 100,
    ) -> list[dict]:
        """Async: Scan market aktif dan filter."""
        markets = await self.aget_markets(session, limit=limit, active=True)
        return self._filter_markets(
            markets, min_volume, min_liquidity,
            max_days_to_resolve, min_days_to_resolve
        )

    async def ascan_hourly_opportunities(
        self,
        session: aiohttp.ClientSession,
        min_volume: float = 500,
        min_liquidity: float = 200,
        max_minutes_to_resolve: int = 90,
        min_minutes_to_resolve: int = 5,
        limit: int = 500,
    ) -> list[dict]:
        """Async: Scan hourly markets — filter berdasarkan menit, bukan hari."""
        markets = await self.aget_markets(session, limit=limit, active=True)
        return self._filter_markets_hourly(
            markets, min_volume, min_liquidity,
            max_minutes_to_resolve, min_minutes_to_resolve,
        )

    # ─────────────────────────────────────────────
    # MARKET FETCHING — SYNC (backward compat)
    # ─────────────────────────────────────────────

    def get_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
        closed: bool = False,
        order: str = "volume24hr",
        ascending: bool = False,
    ) -> list[dict]:
        params = {
            "limit": limit,
            "offset": offset,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "order": order,
            "ascending": str(ascending).lower(),
        }
        return self._get("/markets", params=params)

    def scan_opportunities(
        self,
        min_volume: float = 10_000,
        min_liquidity: float = 5_000,
        max_days_to_resolve: int = 30,
        min_days_to_resolve: int = 1,
        limit: int = 100,
    ) -> list[dict]:
        """Sync: Scan market aktif dan filter."""
        markets = self.get_markets(limit=limit, active=True)
        return self._filter_markets(
            markets, min_volume, min_liquidity,
            max_days_to_resolve, min_days_to_resolve
        )

    # ─────────────────────────────────────────────
    # SHARED FILTER LOGIC
    # ─────────────────────────────────────────────

    # Kategori yang tidak bisa dimodel — skip untuk hemat compute
    SKIP_CATEGORIES = {
        "sports", "entertainment", "music", "awards",
        "tv", "movies", "gaming", "esports",
    }

    def _filter_markets_hourly(
        self,
        markets: list[dict],
        min_volume: float,
        min_liquidity: float,
        max_minutes_to_resolve: int,
        min_minutes_to_resolve: int,
    ) -> list[dict]:
        """Filter hourly markets — berbasis menit bukan hari."""
        now = datetime.now(timezone.utc)
        results = []

        for m in markets:
            try:
                category = (m.get("category") or "").lower().strip()
                if any(cat in category for cat in self.SKIP_CATEGORIES):
                    continue

                volume = float(m.get("volume", 0) or 0)
                if volume < min_volume:
                    continue

                liquidity = float(m.get("liquidity", 0) or 0)
                if liquidity < min_liquidity:
                    continue

                end_date_str = m.get("endDate") or m.get("end_date_iso")
                if not end_date_str:
                    continue

                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                minutes_to_resolve = (end_date - now).total_seconds() / 60

                if minutes_to_resolve < min_minutes_to_resolve:
                    continue
                if minutes_to_resolve > max_minutes_to_resolve:
                    continue

                m["minutes_to_resolve"] = round(minutes_to_resolve, 1)
                m["days_to_resolve"]    = minutes_to_resolve / 1440.0
                m["scan_timestamp"]     = now.isoformat()
                results.append(m)

            except (ValueError, TypeError, KeyError) as e:
                logger.debug(f"Skip market {m.get('id', '?')}: {e}")
                continue

        logger.debug(f"Hourly scan: {len(results)}/{len(markets)} market lolos filter")
        return results

    def _filter_markets(
        self,
        markets: list[dict],
        min_volume: float,
        min_liquidity: float,
        max_days_to_resolve: int,
        min_days_to_resolve: int,
    ) -> list[dict]:
        """Filter logic — sama untuk sync dan async."""
        now = datetime.now(timezone.utc)
        results = []
        skipped_category = 0

        for m in markets:
            try:
                # Skip kategori yang tidak bisa dimodel
                category = (m.get("category") or "").lower().strip()
                if any(cat in category for cat in self.SKIP_CATEGORIES):
                    skipped_category += 1
                    continue

                volume = float(m.get("volume", 0) or 0)
                if volume < min_volume:
                    continue

                liquidity = float(m.get("liquidity", 0) or 0)
                if liquidity < min_liquidity:
                    continue

                end_date_str = m.get("endDate") or m.get("end_date_iso")
                if not end_date_str:
                    continue

                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                days_to_resolve = (end_date - now).days

                if days_to_resolve < min_days_to_resolve:
                    continue
                if days_to_resolve > max_days_to_resolve:
                    continue

                m["days_to_resolve"] = days_to_resolve
                m["scan_timestamp"]  = now.isoformat()
                results.append(m)

            except (ValueError, TypeError, KeyError) as e:
                logger.debug(f"Skip market {m.get('id', '?')}: {e}")
                continue

        if skipped_category:
            logger.debug(f"Skip {skipped_category} market kategori non-modelable")
        logger.debug(f"Scan selesai: {len(results)}/{len(markets)} market lolos filter")
        return results

    # ─────────────────────────────────────────────
    # TOKEN & PRICE HELPERS (tidak butuh async)
    # ─────────────────────────────────────────────

    def extract_token_ids(self, market: dict) -> list[dict]:
        """Ekstrak token_id dari market untuk dipakai di CLOB API."""
        import json
        tokens         = []
        outcomes       = market.get("outcomes", [])
        clob_token_ids = market.get("clobTokenIds", [])

        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except Exception:
                outcomes = []

        if isinstance(clob_token_ids, str):
            try:
                clob_token_ids = json.loads(clob_token_ids)
            except Exception:
                clob_token_ids = []

        for i, outcome in enumerate(outcomes):
            token_id = clob_token_ids[i] if i < len(clob_token_ids) else None
            tokens.append({
                "outcome":      outcome,
                "token_id":     token_id,
                "market_slug":  market.get("slug", ""),
                "condition_id": market.get("conditionId", ""),
            })

        return tokens

    def get_token_prices(self, market: dict) -> dict:
        """Ambil harga dari market data Gamma. Return: {"Yes": 0.72, "No": 0.28}"""
        import json
        prices         = {}
        outcomes       = market.get("outcomes", [])
        outcome_prices = market.get("outcomePrices", [])

        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except Exception:
                outcomes = []

        if isinstance(outcome_prices, str):
            try:
                outcome_prices = json.loads(outcome_prices)
            except Exception:
                outcome_prices = []

        for i, outcome in enumerate(outcomes):
            try:
                price = float(outcome_prices[i]) if i < len(outcome_prices) else None
                prices[outcome] = price
            except (ValueError, TypeError):
                prices[outcome] = None

        return prices

    def get_market(self, condition_id: str) -> dict:
        return self._get(f"/markets/{condition_id}")

    async def aget_market(
        self,
        condition_id: str,
        session: aiohttp.ClientSession,
    ) -> Optional[dict]:
        """
        Async: Fetch single market by condition_id (the 0x... on-chain id).

        Gamma `/markets/{id}` minta numeric id internal — gak cocok buat condition_id.
        Pakai list endpoint + filter `condition_ids` (plural). Default endpoint
        skip closed markets, jadi try `closed=false` dulu, fallback `closed=true`.
        Return None kalau gak ketemu di kedua state.
        """
        for closed_flag in ("false", "true"):
            try:
                results = await self._aget(
                    "/markets",
                    session,
                    params={"condition_ids": condition_id, "closed": closed_flag},
                )
            except Exception:
                continue
            if isinstance(results, list) and results:
                return results[0]
        return None

    def format_market_summary(self, market: dict) -> str:
        title     = market.get("question", market.get("title", "Unknown"))[:60]
        volume    = float(market.get("volume", 0) or 0)
        liquidity = float(market.get("liquidity", 0) or 0)
        days      = market.get("days_to_resolve", "?")
        prices    = self.get_token_prices(market)
        price_str = " | ".join(f"{k}: {v:.2f}" for k, v in prices.items() if v)
        return (
            f"📊 {title}\n"
            f"   Vol: ${volume:,.0f} | Liq: ${liquidity:,.0f} | "
            f"Resolve: {days}d | {price_str}"
        )


# ─────────────────────────────────────────────
# QUICK TEST
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    client = GammaClient()

    print("=" * 60)
    print("TEST SYNC: Top 5 markets")
    print("=" * 60)
    markets = client.get_markets(limit=5)
    for m in markets:
        print(client.format_market_summary(m))
        print()

    print("=" * 60)
    print("TEST ASYNC: Scan opportunities")
    print("=" * 60)

    async def test_async():
        async with aiohttp.ClientSession() as session:
            opps = await client.ascan_opportunities(session, limit=50)
            print(f"Ditemukan {len(opps)} market layak")
            for m in opps[:3]:
                print(client.format_market_summary(m))

    asyncio.run(test_async())