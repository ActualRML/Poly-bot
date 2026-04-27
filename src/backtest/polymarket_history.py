"""
src/backtest/polymarket_history.py
==================================
Fetch closed crypto markets dari Gamma + price history per token dari CLOB.

Disk cache di data/backtest_cache/ — re-run tidak hit API ulang.

Public API:
    fetch_closed_crypto_markets(days_back, session) -> list[ClosedMarket]
    fetch_price_history(token_id, session) -> list[PricePoint]
"""

from __future__ import annotations

import json
import asyncio
import logging
from pathlib import Path
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "backtest_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST  = "https://clob.polymarket.com"

CRYPTO_KEYWORDS: dict[str, list[str]] = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", " eth ", "ether "],
    "SOL": ["solana", " sol "],
}

SKIP_KEYWORDS = ["between", "range", "dip to", "up or down"]


@dataclass
class ClosedMarket:
    condition_id: str
    question: str
    asset: str                 # BTC / ETH / SOL
    yes_token_id: str
    no_token_id: str
    start_date: datetime
    end_date: datetime
    yes_outcome_price: float   # final outcome (1.0 = YES won, 0.0 = NO won)
    volume: float

    def to_dict(self) -> dict:
        d = asdict(self)
        d["start_date"] = self.start_date.isoformat()
        d["end_date"]   = self.end_date.isoformat()
        return d

    @staticmethod
    def from_dict(d: dict) -> ClosedMarket:
        return ClosedMarket(
            condition_id      = d["condition_id"],
            question          = d["question"],
            asset             = d["asset"],
            yes_token_id      = d["yes_token_id"],
            no_token_id       = d["no_token_id"],
            start_date        = datetime.fromisoformat(d["start_date"]),
            end_date          = datetime.fromisoformat(d["end_date"]),
            yes_outcome_price = float(d["yes_outcome_price"]),
            volume            = float(d["volume"]),
        )


@dataclass
class PricePoint:
    timestamp: datetime
    price: float


def _classify_asset(question: str) -> Optional[str]:
    q = question.lower()
    if any(k in q for k in SKIP_KEYWORDS):
        return None
    for asset, keywords in CRYPTO_KEYWORDS.items():
        if any(k in q for k in keywords):
            return asset
    return None


def _parse_outcome_prices(raw) -> Optional[list[float]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    if isinstance(raw, list):
        try:
            return [float(x) for x in raw]
        except (ValueError, TypeError):
            return None
    return None


def _parse_clob_token_ids(raw) -> Optional[list[str]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    if isinstance(raw, list):
        return [str(x) for x in raw]
    return None


# ─────────────────────────────────────────────
# CLOSED MARKETS — Gamma API
# ─────────────────────────────────────────────

async def _fetch_gamma_page(
    session: aiohttp.ClientSession, offset: int, limit: int
) -> list[dict]:
    params = {
        "limit":     limit,
        "offset":    offset,
        "closed":    "true",
        "active":    "false",
        "order":     "volumeNum",
        "ascending": "false",
    }
    async with session.get(
        f"{GAMMA_HOST}/markets",
        params=params,
        timeout=aiohttp.ClientTimeout(total=20),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return data if isinstance(data, list) else []


async def fetch_closed_crypto_markets(
    days_back: int,
    session: aiohttp.ClientSession,
    max_pages: int = 50,
    page_size: int = 100,
    use_cache: bool = True,
) -> list[ClosedMarket]:
    """
    Fetch closed crypto markets yang resolve dalam `days_back` hari terakhir.
    Paginated via Gamma API, filter keyword + tanggal.
    """
    cache_file = CACHE_DIR / f"closed_markets_{days_back}d.json"
    if use_cache and cache_file.exists():
        age_h = (datetime.now().timestamp() - cache_file.stat().st_mtime) / 3600
        if age_h < 24:
            logger.info(f"[CACHE] Load closed markets dari {cache_file.name} (age {age_h:.1f}h)")
            with open(cache_file, "r", encoding="utf-8") as f:
                return [ClosedMarket.from_dict(d) for d in json.load(f)]

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    results: list[ClosedMarket] = []
    seen_ids: set[str] = set()
    pages_empty = 0

    for page in range(max_pages):
        offset = page * page_size
        try:
            batch = await _fetch_gamma_page(session, offset, page_size)
        except Exception as e:
            logger.warning(f"[GAMMA] Gagal fetch page {page}: {e}")
            break

        if not batch:
            break

        page_added = 0
        for m in batch:
            try:
                end_date_str = m.get("endDate") or m.get("end_date_iso")
                if not end_date_str:
                    continue
                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))

                # Filter by cutoff: market must have resolved within window.
                # Use closedTime if available (actual resolve), else endDate (scheduled).
                actual_close = end_date
                closed_time_str = m.get("closedTime") or m.get("closed_time")
                if closed_time_str:
                    try:
                        actual_close = datetime.fromisoformat(closed_time_str.replace("Z", "+00:00"))
                    except (ValueError, TypeError):
                        pass

                if actual_close < cutoff:
                    continue

                question = m.get("question") or m.get("title") or ""
                asset = _classify_asset(question)
                if not asset:
                    continue

                condition_id = m.get("conditionId") or m.get("condition_id") or ""
                if not condition_id or condition_id in seen_ids:
                    continue

                outcome_prices = _parse_outcome_prices(m.get("outcomePrices"))
                token_ids      = _parse_clob_token_ids(m.get("clobTokenIds"))
                if not outcome_prices or not token_ids or len(outcome_prices) < 2 or len(token_ids) < 2:
                    continue

                yes_outcome_price = outcome_prices[0]
                if not (yes_outcome_price in (0.0, 1.0)):
                    # Skip kalau market ga clean-resolved (mid price aneh)
                    continue

                start_date_str = m.get("startDate") or m.get("start_date_iso")
                if start_date_str:
                    start_date = datetime.fromisoformat(start_date_str.replace("Z", "+00:00"))
                else:
                    # Fallback: estimate 7 days before end
                    start_date = end_date - timedelta(days=7)

                volume = float(m.get("volume") or m.get("volumeNum") or 0)

                results.append(ClosedMarket(
                    condition_id      = condition_id,
                    question          = question,
                    asset             = asset,
                    yes_token_id      = token_ids[0],
                    no_token_id       = token_ids[1],
                    start_date        = start_date,
                    end_date          = end_date,
                    yes_outcome_price = yes_outcome_price,
                    volume            = volume,
                ))
                seen_ids.add(condition_id)
                page_added += 1

            except (ValueError, TypeError, KeyError) as e:
                logger.debug(f"Skip market: {e}")
                continue

        # Stop kalau 3 page berturut-turut tidak ada hasil baru (likely di luar window)
        if page_added == 0:
            pages_empty += 1
            if pages_empty >= 3:
                logger.info(f"[GAMMA] {pages_empty} pages tanpa hasil — stop di page {page}")
                break
        else:
            pages_empty = 0

        # Throttle
        await asyncio.sleep(0.3)

    logger.info(f"[GAMMA] Found {len(results)} closed crypto markets dalam {days_back}d window")

    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump([m.to_dict() for m in results], f, indent=2)

    return results


# ─────────────────────────────────────────────
# PRICE HISTORY — CLOB /prices-history
# ─────────────────────────────────────────────

async def fetch_price_history(
    token_id: str,
    session: aiohttp.ClientSession,
    fidelity_minutes: int = 1440,    # 1440 = daily
    use_cache: bool = True,
) -> list[PricePoint]:
    """
    Fetch timeseries harga (mid) untuk satu token dari CLOB.
    Default daily granularity. Output: list[(timestamp, price)] sorted by time.
    """
    cache_file = CACHE_DIR / f"prices_{token_id[:16]}_{fidelity_minutes}.json"
    if use_cache and cache_file.exists():
        age_h = (datetime.now().timestamp() - cache_file.stat().st_mtime) / 3600
        if age_h < 24:
            with open(cache_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return [PricePoint(datetime.fromisoformat(r["timestamp"]), float(r["price"])) for r in raw]

    params = {
        "market":   token_id,
        "interval": "max",
        "fidelity": fidelity_minutes,
    }
    try:
        async with session.get(
            f"{CLOB_HOST}/prices-history",
            params=params,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                logger.warning(f"[CLOB] prices-history {resp.status} untuk token {token_id[:16]}")
                return []
            data = await resp.json()
    except Exception as e:
        logger.warning(f"[CLOB] Gagal fetch prices-history {token_id[:16]}: {e}")
        return []

    history = data.get("history", []) if isinstance(data, dict) else []
    points: list[PricePoint] = []
    for row in history:
        try:
            ts    = int(row["t"])
            price = float(row["p"])
            points.append(PricePoint(
                timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                price=price,
            ))
        except (KeyError, ValueError, TypeError):
            continue

    points.sort(key=lambda p: p.timestamp)

    serializable = [{"timestamp": p.timestamp.isoformat(), "price": p.price} for p in points]
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(serializable, f)

    return points


# ─────────────────────────────────────────────
# QUICK TEST — python -m src.backtest.polymarket_history
# ─────────────────────────────────────────────

async def _smoke_test():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    async with aiohttp.ClientSession() as session:
        markets = await fetch_closed_crypto_markets(days_back=30, session=session, use_cache=False)
        print(f"\nFound {len(markets)} closed crypto markets:")
        for m in markets[:5]:
            print(f"  [{m.asset}] {m.question[:60]} | end={m.end_date.date()} | YES={m.yes_outcome_price}")

        if markets:
            sample = markets[0]
            print(f"\nFetching price history untuk: {sample.question[:50]}")
            history = await fetch_price_history(sample.yes_token_id, session, use_cache=False)
            print(f"  {len(history)} price points dari {history[0].timestamp.date() if history else '?'} "
                  f"ke {history[-1].timestamp.date() if history else '?'}")
            for p in history[:3]:
                print(f"    {p.timestamp.date()}: {p.price:.4f}")


if __name__ == "__main__":
    asyncio.run(_smoke_test())
