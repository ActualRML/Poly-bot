from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp

from src.api.gamma_client import GammaClient
from src.utils.config import config

logger = logging.getLogger(__name__)

# ── UP/DOWN Hourly scanner (unchanged) ────────────────────────────────────────

_UPDOWN_HOURLY_SLUG_PREFIXES = {
    "BTC":  "bitcoin-up-or-down-",
    "ETH":  "ethereum-up-or-down-",
    "SOL":  "solana-up-or-down-",
    "XRP":  "xrp-up-or-down-",
    "DOGE": "dogecoin-up-or-down-",
    "BNB":  "bnb-up-or-down-",
}
_UPDOWN_HOURLY_SKIP_MARKERS = ("-5m-", "-15m-", "-4h-", "updown-5m", "updown-15m", "updown-4h")


async def scan_updown_hourly_markets(session: aiohttp.ClientSession, gamma: GammaClient) -> list[dict]:
    import json as _json

    results = []
    now     = datetime.now(timezone.utc)

    min_min = getattr(config, "UPDOWN_HOURLY_MIN_T_MINUTES", 5)
    max_min = getattr(config, "UPDOWN_HOURLY_MAX_MINUTES", 90)
    end_min = (now + timedelta(minutes=min_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_max = (now + timedelta(minutes=max_min)).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        batch = await gamma._aget(
            "/events",
            session,
            params={
                "closed":       "false",
                "limit":        200,
                "order":        "endDate",
                "ascending":    "true",
                "end_date_min": end_min,
                "end_date_max": end_max,
            },
        )
    except Exception as e:
        logger.debug(f"[UPDOWN HOURLY] Gagal fetch events: {e}")
        return results

    if not isinstance(batch, list):
        return results

    for event in batch:
        slug = (event.get("slug") or "").lower()

        symbol = None
        for sym, prefix in _UPDOWN_HOURLY_SLUG_PREFIXES.items():
            if slug.startswith(prefix):
                symbol = sym
                break
        if not symbol:
            continue

        _skip_syms = {s.strip().upper() for s in getattr(config, "UPDOWN_HOURLY_SKIP_SYMBOLS", "").split(",") if s.strip()}
        if symbol in _skip_syms:
            continue

        if any(m in slug for m in _UPDOWN_HOURLY_SKIP_MARKERS):
            continue

        end_date_str   = event.get("endDate") or ""
        start_date_str = event.get("startDate") or ""
        try:
            end_date   = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            start_date = datetime.fromisoformat(start_date_str.replace("Z", "+00:00"))
        except Exception:
            continue

        minutes_left = (end_date - now).total_seconds() / 60
        if minutes_left < min_min or minutes_left > max_min:
            continue

        # Skip markets where the candle hasn't started yet (next-hour markets)
        candle_start = end_date - timedelta(hours=1)
        if candle_start > now:
            continue

        mkts = event.get("markets", [])
        if not mkts:
            continue
        mkt = mkts[0]

        outcomes = mkt.get("outcomes", [])
        if isinstance(outcomes, str):
            try: outcomes = _json.loads(outcomes)
            except: outcomes = []
        op = mkt.get("outcomePrices", [])
        if isinstance(op, str):
            try: op = _json.loads(op)
            except: op = []

        outcomes_lower = [str(o).lower() for o in outcomes]
        if "up" not in outcomes_lower or not op:
            continue

        mkt["_symbol"]     = symbol
        mkt["_start_date"] = start_date.isoformat()
        mkt["endDate"]     = end_date_str
        results.append(mkt)

    return results


# ── Scout AI scanner (Layer 1 -> Layer 2 -> Layer 3) ─────────────────────────

_SYSTEM_PROMPT = (
    "You are a Polymarket binary market analyst. "
    "Estimate the true probability (0.0-1.0) that the YES outcome resolves. "
    "The Polymarket taker fee is 1.8% in 2026. "
    "Be conservative - only flag genuine information edges, not noise. "
    "Return ONLY valid JSON with keys: probability_forecast, confidence_score, gemini_reasoning. "
    "gemini_reasoning must be under 200 characters."
)

_UPDOWN_SYSTEM_PROMPT = (
    "You are a crypto market analyst for short-term binary UP/DOWN markets. "
    "Estimate prob_up: probability the crypto price will be HIGHER at resolution than the candle open. "
    "Polymarket taker fee is 1.8%. Be conservative — only flag genuine directional bias. "
    "Return ONLY valid JSON: {\"prob_up\": 0.0-1.0, \"confidence\": 0.0-1.0, \"reasoning\": \"<80 chars\"}."
)

_gemini_client = None
_GEMINI_SEMAPHORE = asyncio.Semaphore(3)


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        from google import genai
        _gemini_client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _gemini_client


def _match_category(market: dict, categories: list[str]) -> Optional[str]:
    cats_lower = {c.lower(): c for c in categories}

    tags = market.get("tags") or []
    if isinstance(tags, list):
        for tag in tags:
            label = (tag.get("label") if isinstance(tag, dict) else str(tag)).lower()
            for key, original in cats_lower.items():
                if key in label:
                    return original

    event_list = market.get("events") or []
    if isinstance(event_list, list) and event_list:
        ev_cat = (event_list[0].get("category") or "").lower()
        for key, original in cats_lower.items():
            if key in ev_cat:
                return original

    import re as _re
    question = (market.get("question") or "").lower()
    if "crypto" in cats_lower:
        crypto_exact = {"bitcoin", "ethereum", "crypto", "solana", "dogecoin"}
        crypto_word = {"btc", "eth", "xrp", "doge", "bnb", "sol"}
        if any(kw in question for kw in crypto_exact) or any(
            _re.search(r"" + kw + r"", question) for kw in crypto_word
        ):
            return cats_lower["crypto"]
    if "politics" in cats_lower:
        politics_kw = {"president", "election", "senate", "congress", "minister", "political", "governor", "presidential"}
        politics_word = {"vote", "trump", "tariff"}
        if any(kw in question for kw in politics_kw) or any(
            _re.search(r"" + kw + r"", question) for kw in politics_word
        ):
            return cats_lower["politics"]

    return None


async def get_active_markets(
    session: aiohttp.ClientSession,
    gamma: GammaClient,
    clob,
    *,
    min_volume: Optional[float] = None,
    max_spread_pct: Optional[float] = None,
    categories: Optional[list[str]] = None,
) -> list[dict]:
    if min_volume is None:
        min_volume = getattr(config, "SCOUT_MIN_VOLUME_24H", 50_000.0)
    if max_spread_pct is None:
        max_spread_pct = getattr(config, "SCOUT_MAX_SPREAD_PCT", 0.03)
    if categories is None:
        raw = getattr(config, "SCOUT_CATEGORIES", "Crypto,Politics")
        categories = [c.strip() for c in raw.split(",") if c.strip()]

    try:
        markets = await gamma.aget_markets(session, limit=200, order="volume24hr", ascending=False)
    except Exception as e:
        logger.warning(f"[SCOUT] Gagal fetch markets: {e}")
        return []

    candidates: list[dict] = []
    _r = {"status": 0, "cat": 0, "vol": 0, "collat": 0, "token": 0, "snap": 0, "spread": 0}
    for m in markets:
        if m.get("closed") or m.get("resolved") or m.get("archived") or m.get("active") is False or m.get("enableOrderBook") is False:
            _r["status"] += 1
            continue

        cat = _match_category(m, categories)
        if not cat:
            _r["cat"] += 1
            continue

        vol = float(m.get("volume24hr") or m.get("volume") or 0)
        if vol < min_volume:
            _r["vol"] += 1
            continue

        col_raw = m.get("collateralToken")
        if col_raw:
            col_sym = (
                col_raw.get("symbol", "") if isinstance(col_raw, dict) else str(col_raw)
            ).lower()
            if col_sym and not any(k in col_sym for k in ("usdc", "pusdc", "pusd")):
                _r["collat"] += 1
                continue

        token_info = GammaClient.extract_token_ids(m)
        token_id = next((t["token_id"] for t in token_info if t.get("token_id")), None)
        if not token_id:
            _r["token"] += 1
            continue

        try:
            snap = clob.ambil_snapshot(token_id)
        except Exception:
            snap = None

        if not snap or not snap.valid:
            _r["snap"] += 1
            continue

        mid = float(snap.midpoint)
        if mid <= 0:
            _r["snap"] += 1
            continue
        spread_pct = float(snap.spread) / mid
        if spread_pct >= max_spread_pct:
            _r["spread"] += 1
            continue

        m["_category"]      = cat
        m["_spread_pct"]    = round(spread_pct, 5)
        m["_current_price"] = round(mid, 4)
        m["_token_id"]      = token_id
        candidates.append(m)

    logger.info(
        f"[SCOUT] Layer1: {len(candidates)}/{len(markets)} lolos "
        f"(vol>={min_volume:,.0f}, spread<{max_spread_pct:.0%}) | "
        f"reject: status={_r['status']} cat={_r['cat']} vol={_r['vol']} "
        f"collat={_r['collat']} token={_r['token']} snap={_r['snap']} spread={_r['spread']}"
    )
    return candidates


async def _enrich_one(market: dict, taker_fee: float):
    from src.models.scout import ScoutSignal
    async with _GEMINI_SEMAPHORE:
        prompt = json.dumps({
            "question": market.get("question", ""),
            "description": (market.get("description") or "")[:500],
            "current_implied_probability": market["_current_price"],
            "volume_24h_usd": float(market.get("volume24hr") or market.get("volume") or 0),
            "category": market.get("_category", ""),
            "task": (
                "Return JSON: probability_forecast (float 0-1), "
                "confidence_score (float 0-1), gemini_reasoning (string <200 chars)."
            ),
        })
        try:
            from google.genai import types as _gtypes
            client = _get_gemini_client()
            resp = await client.aio.models.generate_content(
                model=getattr(config, "SCOUT_GEMINI_MODEL", "gemini-2.0-flash"),
                contents=prompt,
                config=_gtypes.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.2,
                    system_instruction=_SYSTEM_PROMPT,
                ),
            )
            data = json.loads(resp.text)
            forecast = float(data["probability_forecast"])
            return ScoutSignal(
                market_id=market.get("conditionId", market.get("id", "")),
                question=market.get("question", ""),
                category=market.get("_category", ""),
                current_price=market["_current_price"],
                probability_forecast=forecast,
                confidence_score=float(data.get("confidence_score", 0.5)),
                gemini_reasoning=str(data.get("gemini_reasoning", "")),
                taker_fee_adjusted=abs(forecast - market["_current_price"]) > taker_fee,
                volume_24h=float(market.get("volume24hr") or market.get("volume") or 0),
                spread_pct=market["_spread_pct"],
            )
        except Exception as e:
            msg = str(e)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                logger.warning(
                    f"[GEMINI] Quota habis ({market.get('conditionId','?')[:8]}): "
                    f"set SCOUT_GEMINI_MODEL=gemini-2.0-flash-lite di .env.local"
                )
            else:
                logger.warning(f"[GEMINI] Enrich gagal {market.get('conditionId', '?')[:8]}: {e}")
            return None


async def enrich_with_gemini(
    candidates: list[dict],
    *,
    taker_fee: float = 0.018,
) -> list:
    max_calls = getattr(config, "SCOUT_GEMINI_MAX_CANDIDATES", 10)
    top = candidates[:max_calls]
    tasks = [_enrich_one(m, taker_fee) for m in top]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in results if r is not None and not isinstance(r, Exception)]


async def scan_with_ai(
    session: aiohttp.ClientSession,
    gamma: GammaClient,
    clob,
) -> list:
    if not getattr(config, "GEMINI_API_KEY", ""):
        logger.warning("[SCOUT AI] GEMINI_API_KEY tidak di-set, skip AI scan")
        return []

    geo_token = getattr(config, "POLYMARKET_GEO_TOKEN", "")
    if geo_token:
        session.headers.update({"Authorization": f"Bearer {geo_token}"})

    candidates = await get_active_markets(session, gamma, clob)
    if not candidates:
        logger.info("[SCOUT AI] Tidak ada kandidat lolos filter deterministik")
        return []

    logger.info(f"[SCOUT AI] {len(candidates)} kandidat -> Gemini")
    signals = await enrich_with_gemini(candidates)
    fee_adj = sum(1 for s in signals if s.taker_fee_adjusted)
    logger.info(f"[SCOUT AI] {len(signals)} signal | fee-adjusted: {fee_adj}")
    return signals


async def _enrich_updown_one(market: dict, vol_data: dict) -> None:
    import json as _json
    async with _GEMINI_SEMAPHORE:
        op = market.get("outcomePrices", [])
        if isinstance(op, str):
            try: op = _json.loads(op)
            except: op = []
        outcomes = market.get("outcomes", [])
        if isinstance(outcomes, str):
            try: outcomes = _json.loads(outcomes)
            except: outcomes = []
        outcomes_lower = [str(o).lower() for o in outcomes]
        try:
            up_idx = outcomes_lower.index("up")
            market_price_up = float(op[up_idx]) if op else 0.5
        except (ValueError, IndexError):
            market_price_up = 0.5

        end_str = market.get("endDate", "")
        try:
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            minutes_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 60
        except Exception:
            minutes_left = 30.0

        symbol = market.get("_symbol", "?")
        vol_annual = vol_data.get(symbol, vol_data.get("DEFAULT", 0.40))

        prompt = json.dumps({
            "symbol": symbol,
            "market_price_up": round(market_price_up, 3),
            "minutes_to_resolve": round(minutes_left, 1),
            "vol_annual": round(vol_annual, 3),
            "task": "Return JSON: prob_up (float 0-1), confidence (float 0-1), reasoning (string <80 chars).",
        })
        try:
            from google.genai import types as _gtypes
            client = _get_gemini_client()
            resp = await client.aio.models.generate_content(
                model=getattr(config, "SCOUT_GEMINI_MODEL", "gemini-1.5-flash"),
                contents=prompt,
                config=_gtypes.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                    system_instruction=_UPDOWN_SYSTEM_PROMPT,
                ),
            )
            data = json.loads(resp.text)
            market["_gemini_prob_up"]   = float(max(0.0, min(1.0, data["prob_up"])))
            market["_gemini_confidence"] = float(max(0.0, min(1.0, data.get("confidence", 0.5))))
            market["_gemini_reasoning"]  = str(data.get("reasoning", ""))[:80]
        except Exception as e:
            msg = str(e)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                logger.warning(f"[GEMINI] Quota habis ({symbol}): set SCOUT_GEMINI_MODEL=gemini-1.5-flash di .env.local")
            else:
                logger.warning(f"[GEMINI] UP/DOWN enrich gagal {symbol}: {e}")


async def enrich_updown_with_gemini(
    markets: list[dict],
    *,
    vol_data: dict | None = None,
) -> list[dict]:
    if not getattr(config, "GEMINI_API_KEY", "") or not getattr(config, "UPDOWN_HOURLY_USE_GEMINI", False):
        return markets
    tasks = [_enrich_updown_one(m, vol_data or {}) for m in markets]
    await asyncio.gather(*tasks, return_exceptions=True)
    n_enriched = sum(1 for m in markets if "_gemini_prob_up" in m)
    logger.info(f"[GEMINI] UP/DOWN enrichment: {n_enriched}/{len(markets)} markets")
    return markets
