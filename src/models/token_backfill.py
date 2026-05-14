from __future__ import annotations

import json
import logging
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


async def backfill_token_id(gamma, session: aiohttp.ClientSession, condition_id: str, outcome: str) -> str:
    try:
        market = await gamma.aget_market(condition_id, session)
        if not market:
            return ""
        tokens = gamma.extract_token_ids(market)
        token  = next((t for t in tokens if t["outcome"] == outcome), None)
        return str(token["token_id"]) if token and token.get("token_id") else ""
    except Exception as e:
        logger.debug(f"Gagal backfill token_id {condition_id[:8]}: {e}")
        return ""


async def backfill_missing_token_ids(gamma, session: aiohttp.ClientSession) -> None:
    from src.models.database import get_open_positions, update_position_token_id
    from src.utils.logger import log

    missing = [p for p in get_open_positions() if not (p.get("token_id") or "")]
    if not missing:
        return

    for pos in missing:
        cid     = pos["condition_id"]
        outcome = pos["outcome"]
        tid     = await backfill_token_id(gamma, session, cid, outcome)
        if tid:
            update_position_token_id(cid, outcome, tid)
            log.info(f"[BACKFILL] token_id {cid[:8]} {outcome} ✓")
        else:
            logger.debug(f"[BACKFILL] gagal {cid[:8]} {outcome} — market mungkin sudah closed di Gamma")


async def get_resolved_price_from_gamma(
    gamma, session: aiohttp.ClientSession, condition_id: str, outcome: str
) -> Optional[float]:
    try:
        market = await gamma.aget_market(condition_id, session)
        if not market:
            return None
        outcomes       = market.get("outcomes", [])
        outcome_prices = market.get("outcomePrices", [])
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if isinstance(outcome_prices, str):
            outcome_prices = json.loads(outcome_prices)
        if not outcome_prices:
            return None
        prices_float = [float(p) for p in outcome_prices]
        if outcome not in outcomes:
            return None
        idx = outcomes.index(outcome)
        if 0 <= idx < len(prices_float):
            return prices_float[idx]
    except Exception as e:
        logger.debug(f"Gagal fetch resolved price {condition_id[:8]} {outcome}: {e}")
    return None
