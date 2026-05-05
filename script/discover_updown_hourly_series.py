import sys
import asyncio
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiohttp
from src.api.gamma_client import GammaClient

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

HOURLY_KEYWORDS = ["up or down -", "up or down–", "up or down—"]
DAILY_KEYWORDS  = ["up or down on", "up or down daily"]

ASSETS = ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp", "ripple", "bnb"]

async def main():
    gamma = GammaClient()
    found: dict[str, dict] = {}

    async with aiohttp.ClientSession() as session:
        for closed in ("false", "true"):
            batch = await gamma._aget(
                "/events",
                session,
                params={
                    "limit":     200,
                    "offset":    0,
                    "closed":    closed,
                    "order":     "startDate",
                    "ascending": "false",
                },
            )
            if not isinstance(batch, list):
                continue

            for event in batch:
                title = (event.get("title") or "").lower()

                is_hourly = any(kw in title for kw in HOURLY_KEYWORDS)
                is_daily  = any(kw in title for kw in DAILY_KEYWORDS)

                if not is_hourly or is_daily:
                    continue

                has_asset = any(a in title for a in ASSETS)
                if not has_asset:
                    continue

                series_id = str(event.get("seriesId") or event.get("series_id") or "")
                slug      = event.get("slug") or ""
                raw_title = event.get("title") or ""

                key = series_id or slug
                if key and key not in found:
                    found[key] = {
                        "series_id": series_id,
                        "slug":      slug,
                        "title":     raw_title,
                        "closed":    closed,
                    }

    print("\n=== HOURLY UP/DOWN SERIES DITEMUKAN ===\n")
    if not found:
        print("Tidak ada market hourly ditemukan.")
        print("Coba cek: apakah market sudah expired semua atau keyword berbeda.")
        return

    for key, info in sorted(found.items()):
        print(f"  series_id : {info['series_id'] or '(kosong)'}")
        print(f"  slug      : {info['slug']}")
        print(f"  title     : {info['title']}")
        print(f"  closed    : {info['closed']}")
        print()

asyncio.run(main())
