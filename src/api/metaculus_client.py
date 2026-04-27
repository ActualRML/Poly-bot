"""
src/api/metaculus_client.py
============================
Multi-source base probability fetcher untuk political/event markets.

Sources (paralel):
- Kalshi      (regulated US market, real money, no auth) — confidence 0.85
- Manifold    (play money, free public API)              — confidence 0.60

Catatan sumber yang dihapus:
- Metaculus: community_prediction deprecated/gated sejak Nov 2024
  (return null untuk free accounts).
- PredictIt: Cloudflare block traffic non-US, tidak bisa di-bypass
  tanpa proxy.

Returns multiple matches yang nanti di-blend oleh MispricingDetector.
File ini tetap pakai nama metaculus_client.py untuk backward-compat
dengan import yang sudah ada.
"""

import asyncio
import aiohttp
import logging
from datetime import datetime, timezone
from typing import Optional

try:
    from rapidfuzz import fuzz as _fuzz
    def _similarity(a: str, b: str) -> float:
        return _fuzz.token_set_ratio(a.lower(), b.lower()) / 100.0
except ImportError:
    from difflib import SequenceMatcher
    def _similarity(a: str, b: str) -> float:
        return SequenceMatcher(None, a.lower(), b.lower()).ratio()

logger = logging.getLogger(__name__)

_KALSHI_BASE   = "https://api.elections.kalshi.com/trade-api/v2"
_MANIFOLD_BASE = "https://api.manifold.markets/v0"

# Per-query result cache (final blended response)
_query_cache: dict[str, tuple[float, list[dict]]] = {}
_QUERY_CACHE_TTL = 1800  # 30 menit

# Bulk-fetch cache (Kalshi punya ratusan-ribuan market, fetch sekali
# dan filter di client lebih efisien daripada per-query)
_kalshi_cache: tuple[float, list[dict]] = (0.0, [])
_BULK_CACHE_TTL = 1800

# Browser-like UA untuk hindari beberapa CDN block
_DEFAULT_HEADERS = {
    "Accept":     "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0 Safari/537.36",
}


class MetaculusClient:
    """
    Multi-source fetcher: Kalshi + Manifold paralel.
    """

    async def search_all(
        self,
        query: str,
        session: aiohttp.ClientSession,
        min_similarity: float = 0.50,
        min_predictors: int = 5,
    ) -> list[dict]:
        """
        Cari di semua sumber paralel. Return list dari semua match yang valid.

        Each match: {title, probability, num_predictors, similarity, source}
        """
        cache_key = query.lower()[:80]
        now = datetime.now(timezone.utc).timestamp()

        if cache_key in _query_cache:
            ts, cached = _query_cache[cache_key]
            if now - ts < _QUERY_CACHE_TTL:
                return cached

        results = await asyncio.gather(
            self._search_kalshi(query, session, min_similarity),
            self._search_manifold(query, session, min_similarity, min_predictors),
            return_exceptions=True,
        )

        matches: list[dict] = []
        for r in results:
            if isinstance(r, Exception):
                logger.debug(f"[POLITICAL] Source error: {r}")
                continue
            if r is not None:
                matches.append(r)

        _query_cache[cache_key] = (now, matches)
        return matches

    # Backward-compat: caller lama yang ekspektasi single result
    async def search_question(
        self,
        query: str,
        session: aiohttp.ClientSession,
        min_similarity: float = 0.50,
        min_predictors: int = 5,
    ) -> Optional[dict]:
        matches = await self.search_all(query, session, min_similarity, min_predictors)
        if not matches:
            return None
        # Pilih yang similarity-nya paling tinggi
        return max(matches, key=lambda m: m["similarity"])

    # ──────────────────────────────────────────────────────────────
    # KALSHI — regulated US prediction market
    # ──────────────────────────────────────────────────────────────

    # Kategori event yang relevan untuk political/event markets di Polymarket
    _RELEVANT_CATEGORIES = {
        "Politics", "Elections", "World",
        "Climate and Weather", "Science and Technology", "Economics",
    }

    async def _get_kalshi_markets(
        self, session: aiohttp.ClientSession
    ) -> list[dict]:
        """
        Fetch events dengan nested markets, flatten jadi list market.
        Setiap market di-tag dengan event title untuk matching.
        """
        global _kalshi_cache
        now = datetime.now(timezone.utc).timestamp()
        ts, cached = _kalshi_cache
        if cached and now - ts < _BULK_CACHE_TTL:
            return cached

        flat: list[dict] = []
        cursor = ""
        try:
            for _ in range(10):  # max 10 pages
                params = {
                    "limit":   "200",
                    "status":  "open",
                    "with_nested_markets": "true",
                }
                if cursor:
                    params["cursor"] = cursor
                async with session.get(
                    f"{_KALSHI_BASE}/events",
                    params=params,
                    headers=_DEFAULT_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

                events = data.get("events", []) or []
                for ev in events:
                    if ev.get("category") not in self._RELEVANT_CATEGORIES:
                        continue
                    ev_title = ev.get("title") or ""
                    markets  = ev.get("markets") or []
                    is_binary = len(markets) == 1
                    for m in markets:
                        if m.get("status") != "active":
                            continue
                        flat.append({
                            "event_title":   ev_title,
                            "event_ticker":  ev.get("event_ticker"),
                            "category":      ev.get("category"),
                            "is_binary":     is_binary,
                            "market":        m,
                        })

                cursor = data.get("cursor", "") or ""
                if not cursor or not events:
                    break

            _kalshi_cache = (now, flat)
            logger.debug(f"[KALSHI] Cached {len(flat)} markets")
            return flat
        except Exception as e:
            logger.debug(f"[KALSHI] Bulk fetch error: {e}")
            return []

    @staticmethod
    def _kalshi_price(market: dict) -> Optional[float]:
        """
        Ekstrak fair probability dari Kalshi market.
        Field-nya '*_dollars' string (e.g. '0.5500' = 55%).
        """
        def _f(key: str) -> Optional[float]:
            v = market.get(key)
            if v is None or v == "":
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        bid = _f("yes_bid_dollars")
        ask = _f("yes_ask_dollars")
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            return (bid + ask) / 2.0

        last = _f("last_price_dollars")
        if last is not None and 0.0 < last < 1.0:
            return last
        return None

    async def _search_kalshi(
        self,
        query: str,
        session: aiohttp.ClientSession,
        min_similarity: float,
    ) -> Optional[dict]:
        flat = await self._get_kalshi_markets(session)
        if not flat:
            return None

        # Untuk binary event, match terhadap event title saja.
        # Untuk multi-outcome event, match terhadap "event title - outcome".
        # Skip yang price-nya invalid.
        best, best_price, best_score = None, None, 0.0
        for entry in flat:
            m = entry["market"]
            price = self._kalshi_price(m)
            if price is None:
                continue

            ev_title = entry["event_title"]
            if entry["is_binary"]:
                cmp_title = ev_title
            else:
                outcome = m.get("yes_sub_title") or m.get("title") or ""
                cmp_title = f"{ev_title} - {outcome}"

            score = _similarity(query, cmp_title)
            if score > best_score:
                best_score = score
                best = entry
                best_price = price

        if best is None or best_score < min_similarity:
            return None

        m = best["market"]
        prob = float(best_price)
        try:
            volume = int(float(m.get("volume_fp") or 0))
        except (TypeError, ValueError):
            volume = 0

        # Display title — kalau multi-outcome, sertakan outcome
        if best["is_binary"]:
            display_title = best["event_title"]
        else:
            display_title = f"{best['event_title']} — {m.get('yes_sub_title', '')}"

        result = {
            "title":          display_title,
            "probability":    prob,
            "num_predictors": volume,
            "similarity":     round(best_score, 2),
            "source":         "kalshi",
        }
        logger.info(
            f"[KALSHI] '{query[:45]}' -> {prob:.1%} "
            f"(vol={volume}, sim={best_score:.2f}, {best['category']})"
        )
        return result

    # ──────────────────────────────────────────────────────────────
    # MANIFOLD — play money, fallback / additional signal
    # ──────────────────────────────────────────────────────────────

    async def _search_manifold(
        self,
        query: str,
        session: aiohttp.ClientSession,
        min_similarity: float,
        min_predictors: int,
    ) -> Optional[dict]:
        try:
            async with session.get(
                f"{_MANIFOLD_BASE}/search-markets",
                params={
                    "term":         query[:100],
                    "limit":        5,
                    "filter":       "open",
                    "contractType": "BINARY",
                },
                headers=_DEFAULT_HEADERS,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                resp.raise_for_status()
                results = await resp.json()

            if not results:
                return None

            best, best_score = None, 0.0
            for r in results:
                score = _similarity(query, r.get("question", ""))
                if score > best_score:
                    best_score = score
                    best = r

            if best is None or best_score < min_similarity:
                return None

            prob = best.get("probability")
            if prob is None:
                return None

            num_traders = (
                best.get("uniqueBettorCount")
                or best.get("totalTraders")
                or 0
            )
            if num_traders < min_predictors:
                return None

            result = {
                "title":          best.get("question", ""),
                "probability":    float(prob),
                "num_predictors": int(num_traders),
                "similarity":     round(best_score, 2),
                "source":         "manifold",
            }
            logger.info(
                f"[MANIFOLD] '{query[:45]}' -> {float(prob):.1%} "
                f"({num_traders} traders, sim={best_score:.2f})"
            )
            return result

        except asyncio.TimeoutError:
            logger.warning(f"[MANIFOLD] Timeout: {query[:50]}")
        except Exception as e:
            logger.debug(f"[MANIFOLD] Error: {e}")
        return None


# Module-level singleton
metaculus_client = MetaculusClient()
