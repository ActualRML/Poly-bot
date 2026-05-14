import asyncio
import aiohttp
import requests
import logging
from typing import Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

class GammaClient:

    def __init__(self, host: str = "https://gamma-api.polymarket.com"):
        self.host = host.rstrip("/")
        self._sync_session = requests.Session()
        self._sync_session.headers.update({
            "Accept": "application/json",
            "User-Agent": "polymarket-bot/1.0"
        })

    async def _aget(
        self,
        endpoint: str,
        session: aiohttp.ClientSession,
        params: dict = None,
        _retries: int = 2,
    ) -> dict | list:
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
                    wait = 2 ** attempt
                    logger.warning(f"Gamma timeout {endpoint} (attempt {attempt+1}), retry in {wait}s")
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"Gamma API timeout {endpoint} setelah {_retries+1} attempts")
                raise
            except aiohttp.ClientResponseError as e:
                if e.status in (403, 429, 500, 502, 503) and attempt < _retries:
                    wait = 2 ** attempt
                    logger.warning(f"Gamma HTTP {e.status} {endpoint} (attempt {attempt+1}), retry in {wait}s")
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"Gamma API HTTP {e.status} endpoint={endpoint}: {e}")
                raise
            except Exception as e:
                logger.error(f"Gamma API async error endpoint={endpoint}: {e}")
                raise

    def _get(self, endpoint: str, params: dict = None) -> dict | list:
        url = f"{self.host}{endpoint}"
        try:
            resp = self._sync_session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Gamma API error endpoint={endpoint}: {e}")
            raise

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
        markets = await self.aget_markets(session, limit=limit, active=True)
        return self._filter_markets(
            markets, min_volume, min_liquidity,
            max_days_to_resolve, min_days_to_resolve
        )

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
        markets = self.get_markets(limit=limit, active=True)
        return self._filter_markets(
            markets, min_volume, min_liquidity,
            max_days_to_resolve, min_days_to_resolve
        )

    SKIP_SERIES_KEYWORDS = {
        "league",
        "ligue",
        "liga",
        "serie-a",
        "nba",
        "mlb",
        "nfl",
        "nhl",
        "mls",
        "ucl",
        "atp",
        "wta",
        "ipl",
        "cricket",
        "counter-strike",
        "dota",
        "valorant",
        "esports",
        "ufc",
        "mma",
        "boxing",
        "golf",
        "rugby",
        "nascar",
        "formula",
    }

    def _filter_markets(
        self,
        markets: list[dict],
        min_volume: float,
        min_liquidity: float,
        max_days_to_resolve: int,
        min_days_to_resolve: int,
    ) -> list[dict]:
        now = datetime.now(timezone.utc)
        results = []
        skip = {"status": 0, "orderbook": 0, "category": 0, "volume": 0, "liquidity": 0, "time": 0}

        for m in markets:
            mid = (m.get("conditionId") or m.get("id") or "?")[:8]
            try:
                if m.get("closed") is True:
                    skip["status"] += 1
                    continue
                if m.get("active") is False:
                    skip["status"] += 1
                    continue
                if m.get("archived") is True:
                    skip["status"] += 1
                    continue
                if m.get("resolved") is True:
                    skip["status"] += 1
                    continue

                if m.get("enableOrderBook") is False:
                    skip["orderbook"] += 1
                    continue

                events_list = m.get("events") or []
                series_slug = ""
                if events_list and isinstance(events_list, list):
                    series_slug = (events_list[0].get("seriesSlug") or "").lower()
                if series_slug and any(kw in series_slug for kw in self.SKIP_SERIES_KEYWORDS):
                    skip["category"] += 1
                    continue

                volume = float(m.get("volume", 0) or 0)
                if volume < min_volume:
                    skip["volume"] += 1
                    continue

                liquidity = float(m.get("liquidity", 0) or 0)
                if liquidity < min_liquidity:
                    skip["liquidity"] += 1
                    continue

                end_date_str = m.get("endDate") or m.get("end_date_iso")
                if not end_date_str:
                    skip["time"] += 1
                    continue

                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                days_to_resolve = (end_date - now).days

                if days_to_resolve < min_days_to_resolve:
                    skip["time"] += 1
                    continue
                if days_to_resolve > max_days_to_resolve:
                    skip["time"] += 1
                    continue

                m["days_to_resolve"] = days_to_resolve
                m["scan_timestamp"]  = now.isoformat()
                results.append(m)

            except (ValueError, TypeError, KeyError) as e:
                logger.debug(f"Skip {mid}: parse error: {e}")
                continue

        logger.debug(
            f"Scan: {len(results)}/{len(markets)} lolos | "
            f"skip: status={skip['status']} orderbook={skip['orderbook']} "
            f"category={skip['category']} vol={skip['volume']} "
            f"liq={skip['liquidity']} time={skip['time']}"
        )
        return results

    @staticmethod
    def extract_token_ids(market: dict) -> list[dict]:
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

    @staticmethod
    def get_token_prices(market: dict) -> dict:
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
