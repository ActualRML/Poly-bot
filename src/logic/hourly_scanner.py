from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import aiohttp

from src.api.gamma_client import GammaClient
from src.utils import config

logger = logging.getLogger(__name__)

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

    min_min = getattr(config, "HOURLY_MIN_MINUTES_TO_RESOLVE", 5)
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
