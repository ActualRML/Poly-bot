from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

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
_UPDOWN_HOURLY_SKIP_MARKERS = (
    "-5m-", "-15m-", "-4h-",
    "updown-5m", "updown-15m", "updown-4h",
    "updown-1m", "updown-30m",
)

# Fallback keyword matching — handles slug format variations (e.g. "btc-up-or-down-" vs "bitcoin-up-or-down-")
_UPDOWN_HOURLY_SLUG_KEYWORDS = {
    "BTC":  ("bitcoin-", "btc-"),
    "ETH":  ("ethereum-", "eth-"),
    "SOL":  ("solana-", "sol-"),
    "XRP":  ("xrp-",),
    "DOGE": ("dogecoin-", "doge-"),
    "BNB":  ("bnb-",),
}


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
                "limit":        500,
                "order":        "endDate",
                "ascending":    "true",
                "end_date_min": end_min,
                "end_date_max": end_max,
            },
        )
    except Exception as e:
        logger.warning(f"[UPDOWN HOURLY] Gagal fetch events: {e}")
        return results

    if not isinstance(batch, list):
        logger.warning(f"[UPDOWN HOURLY] Gamma response bukan list: {type(batch)}")
        return results

    all_events = list(batch)
    logger.debug(
        f"[UPDOWN HOURLY] Gamma returned {len(batch)} events "
        f"| window [{end_min[11:16]}–{end_max[11:16]}] UTC"
    )

    if len(batch) >= 100:
        last_end = max((e.get("endDate") or "") for e in batch if e.get("endDate"))
        if last_end and last_end < end_max:
            try:
                last_dt  = datetime.fromisoformat(last_end.replace("Z", "+00:00"))
                next_min = (last_dt + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
                batch2   = await gamma._aget(
                    "/events",
                    session,
                    params={
                        "closed":       "false",
                        "limit":        500,
                        "order":        "endDate",
                        "ascending":    "true",
                        "end_date_min": next_min,
                        "end_date_max": end_max,
                    },
                )
                if isinstance(batch2, list) and batch2:
                    logger.debug(f"[UPDOWN HOURLY] Page 2: {len(batch2)} events from {next_min[11:16]} UTC")
                    all_events.extend(batch2)
            except Exception as e:
                logger.warning(f"[UPDOWN HOURLY] Pagination gagal: {e}")

    _r = {"slug": 0, "skip": 0, "marker": 0, "parse": 0, "time": 0, "candle": 0, "outcome": 0}

    for event in all_events:
        slug = (event.get("slug") or "").lower()

        symbol = None
        for sym, prefix in _UPDOWN_HOURLY_SLUG_PREFIXES.items():
            if slug.startswith(prefix):
                symbol = sym
                break
        if symbol is None and ("up-or-down" in slug or "-updown-" in slug):
            for sym, keywords in _UPDOWN_HOURLY_SLUG_KEYWORDS.items():
                if any(kw in slug for kw in keywords):
                    symbol = sym
                    break
        if not symbol:
            _r["slug"] += 1
            continue

        _skip_syms = {s.strip().upper() for s in getattr(config, "UPDOWN_HOURLY_SKIP_SYMBOLS", "").split(",") if s.strip()}
        if symbol in _skip_syms:
            _r["skip"] += 1
            continue

        if any(m in slug for m in _UPDOWN_HOURLY_SKIP_MARKERS):
            _r["marker"] += 1
            continue

        end_date_str   = event.get("endDate") or ""
        start_date_str = event.get("startDate") or ""
        try:
            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        except Exception:
            _r["parse"] += 1
            continue

        try:
            start_date = datetime.fromisoformat(start_date_str.replace("Z", "+00:00"))
        except Exception:
            start_date = end_date - timedelta(hours=1)

        minutes_left = (end_date - now).total_seconds() / 60
        if minutes_left < min_min or minutes_left > max_min:
            _r["time"] += 1
            continue

        mkts = event.get("markets", [])
        if not mkts:
            _r["outcome"] += 1
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
            _r["outcome"] += 1
            continue

        mkt["_symbol"]     = symbol
        mkt["_start_date"] = start_date.isoformat()
        mkt["endDate"]     = end_date_str
        results.append(mkt)

    if sum(_r.values()):
        logger.info(f"[UPDOWN HOURLY] Filtered: {_r} → {len(results)} passed")

    return results
