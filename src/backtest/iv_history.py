"""
src/backtest/iv_history.py
==========================
Fetch historical implied volatility (DVOL) dari Deribit untuk range tanggal.

Endpoint: /public/get_volatility_index_data
- BTC + ETH only (Deribit DVOL).
- SOL → fallback realized vol (CoinGecko).

Cache di data/backtest_cache/. Re-run gak hit API ulang.

Public API:
    fetch_iv_history(asset, start, end, session) -> dict[date, iv]
"""

from __future__ import annotations

import json
import math
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta, date
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "backtest_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

DERIBIT_HOST = "https://www.deribit.com"
COINGECKO_HOST = "https://api.coingecko.com"

DERIBIT_SUPPORTED = {"BTC", "ETH"}
COINGECKO_IDS = {
    "BTC":  "bitcoin",
    "ETH":  "ethereum",
    "SOL":  "solana",
    "XRP":  "ripple",
    "DOGE": "dogecoin",
    "BNB":  "binancecoin",
}

VOLATILITAS_FALLBACK = {
    "BTC":  0.45,
    "ETH":  0.60,
    "SOL":  0.80,
    "XRP":  0.70,
    "DOGE": 0.95,
    "BNB":  0.55,
}


# ─────────────────────────────────────────────
# DERIBIT DVOL — BTC, ETH
# ─────────────────────────────────────────────

async def _fetch_deribit_dvol_chunk(
    asset: str, start_ms: int, end_ms: int, session: aiohttp.ClientSession,
) -> list[tuple[int, float]]:
    """Daily resolution = 86400 detik. Return [(timestamp_ms, iv_close)]."""
    params = {
        "currency":        asset,
        "resolution":      "86400",
        "start_timestamp": start_ms,
        "end_timestamp":   end_ms,
    }
    async with session.get(
        f"{DERIBIT_HOST}/api/v2/public/get_volatility_index_data",
        params=params,
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        resp.raise_for_status()
        data    = await resp.json()
        candles = data.get("result", {}).get("data", [])

    out = []
    for row in candles:
        # row format: [timestamp_ms, open, high, low, close]
        try:
            ts = int(row[0])
            iv_close = float(row[4]) / 100.0  # Deribit returns as percentage
            if 0.10 <= iv_close <= 5.0:
                out.append((ts, iv_close))
        except (IndexError, ValueError, TypeError):
            continue
    return out


async def _fetch_deribit_iv_history(
    asset: str, start: datetime, end: datetime, session: aiohttp.ClientSession,
) -> dict[date, float]:
    """Fetch full range, chunk per 90 hari biar gak kena rate limit."""
    out: dict[date, float] = {}
    cursor = start

    while cursor < end:
        chunk_end = min(cursor + timedelta(days=90), end)
        start_ms  = int(cursor.timestamp() * 1000)
        end_ms    = int(chunk_end.timestamp() * 1000)

        try:
            rows = await _fetch_deribit_dvol_chunk(asset, start_ms, end_ms, session)
            for ts_ms, iv in rows:
                d = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date()
                out[d] = iv
            logger.debug(f"[IV] {asset} chunk {cursor.date()}–{chunk_end.date()}: {len(rows)} points")
        except Exception as e:
            logger.warning(f"[IV] Deribit fail {asset} {cursor.date()}: {e}")

        cursor = chunk_end
        await asyncio.sleep(0.5)

    return out


# ─────────────────────────────────────────────
# REALIZED VOL FALLBACK (untuk SOL atau gap days)
# ─────────────────────────────────────────────

_coingecko_locks: dict[str, asyncio.Lock] = {}


async def _fetch_coingecko_history(
    asset: str, days: int, session: aiohttp.ClientSession,
    use_cache: bool = True,
) -> list[tuple[date, float]]:
    """Disk-cached + single-flight per (asset, days). Cache 24 jam."""
    asset = asset.upper()
    coin_id = COINGECKO_IDS.get(asset)
    if not coin_id:
        return []

    days = min(days, 365)
    cache_file = CACHE_DIR / f"spot_{asset}_{days}d.json"

    # Single-flight per asset+days
    lock_key = f"{asset}_{days}"
    lock = _coingecko_locks.setdefault(lock_key, asyncio.Lock())

    async with lock:
        if use_cache and cache_file.exists():
            age_h = (datetime.now().timestamp() - cache_file.stat().st_mtime) / 3600
            if age_h < 24:
                with open(cache_file, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                return [(date.fromisoformat(d), float(p)) for d, p in raw]

        params = {"vs_currency": "usd", "days": str(days), "interval": "daily"}
        # Retry dengan backoff untuk 429
        for attempt in range(3):
            try:
                async with session.get(
                    f"{COINGECKO_HOST}/api/v3/coins/{coin_id}/market_chart",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 429:
                        wait = (attempt + 1) * 30
                        logger.warning(f"[CG] 429 — backoff {wait}s (attempt {attempt+1}/3)")
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    data = await resp.json()
                    break
            except aiohttp.ClientResponseError as e:
                if e.status == 429 and attempt < 2:
                    wait = (attempt + 1) * 30
                    logger.warning(f"[CG] 429 retry {attempt+1}: wait {wait}s")
                    await asyncio.sleep(wait)
                    continue
                raise
        else:
            logger.error(f"[CG] {asset} gagal setelah 3 retry, return empty")
            return []

        out = []
        for ts_ms, price in data.get("prices", []):
            d = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date()
            out.append((d, float(price)))

        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump([[d.isoformat(), p] for d, p in out], f)

        return out


def _compute_rolling_realized_vol(
    prices: list[tuple[date, float]], window: int = 30,
) -> dict[date, float]:
    """Annualized realized vol dari rolling 30d log returns."""
    if len(prices) < 2:
        return {}

    log_returns = []
    for i in range(1, len(prices)):
        p_prev = prices[i - 1][1]
        p_curr = prices[i][1]
        if p_prev > 0 and p_curr > 0:
            log_returns.append((prices[i][0], math.log(p_curr / p_prev)))

    out = {}
    for i in range(len(log_returns)):
        start = max(0, i - window + 1)
        slice_returns = [r for _, r in log_returns[start:i + 1]]
        n = len(slice_returns)
        if n < 5:
            continue
        mean = sum(slice_returns) / n
        var = sum((r - mean) ** 2 for r in slice_returns) / max(n - 1, 1)
        annualized = math.sqrt(var) * math.sqrt(365)
        out[log_returns[i][0]] = annualized
    return out


# ─────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────

async def fetch_iv_history(
    asset: str,
    start: datetime,
    end: datetime,
    session: aiohttp.ClientSession,
    use_cache: bool = True,
) -> dict[date, float]:
    """
    Return dict[date → annualized IV] untuk asset di range [start, end].

    Strategy:
    - BTC/ETH: pakai Deribit DVOL historical
    - SOL/XRP/DOGE: pakai realized vol 30d rolling dari CoinGecko
    - Gap days (kalau Deribit miss): fallback realized vol
    """
    asset = asset.upper()
    cache_file = CACHE_DIR / f"iv_{asset}_{start.date()}_{end.date()}.json"

    if use_cache and cache_file.exists():
        age_h = (datetime.now().timestamp() - cache_file.stat().st_mtime) / 3600
        if age_h < 24 * 7:  # IV historis stabil — cache 7 hari
            with open(cache_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return {date.fromisoformat(k): float(v) for k, v in raw.items()}

    out: dict[date, float] = {}

    if asset in DERIBIT_SUPPORTED:
        out = await _fetch_deribit_iv_history(asset, start, end, session)
        logger.info(f"[IV] {asset} Deribit: {len(out)} days")

    # Realized vol fallback (untuk SOL, atau fill gap di BTC/ETH)
    span_days = (end - start).days + 60
    coingecko_window = min(span_days, 365)
    try:
        prices = await _fetch_coingecko_history(asset, coingecko_window, session)
        rvol = _compute_rolling_realized_vol(prices, window=30)

        for d, vol in rvol.items():
            if d not in out and start.date() <= d <= end.date():
                out[d] = vol
        logger.info(f"[IV] {asset} realized fallback fill: {len(rvol)} days available")
    except Exception as e:
        logger.warning(f"[IV] Realized vol fallback gagal {asset}: {e}")

    # Hard fallback — kalau tetep kosong, isi static
    if not out:
        static = VOLATILITAS_FALLBACK.get(asset, 0.65)
        d = start.date()
        while d <= end.date():
            out[d] = static
            d += timedelta(days=1)
        logger.warning(f"[IV] {asset} pakai static fallback {static:.0%}")

    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump({d.isoformat(): v for d, v in out.items()}, f)

    return out


def get_iv_at(iv_history: dict[date, float], target: date, fallback: float = 0.65) -> float:
    """Lookup IV pada tanggal target. Kalau tidak ada, cari nearest preceding."""
    if target in iv_history:
        return iv_history[target]
    # Walk backward max 7 hari
    for delta in range(1, 8):
        prev = target - timedelta(days=delta)
        if prev in iv_history:
            return iv_history[prev]
    return fallback


# ─────────────────────────────────────────────
# QUICK TEST
# ─────────────────────────────────────────────

async def _smoke_test():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=60)

    async with aiohttp.ClientSession() as session:
        for asset in ["BTC", "ETH", "SOL"]:
            iv = await fetch_iv_history(asset, start, end, session, use_cache=False)
            print(f"\n{asset}: {len(iv)} days")
            sample_dates = sorted(iv.keys())[:5]
            for d in sample_dates:
                print(f"  {d}: {iv[d]:.1%}")


if __name__ == "__main__":
    asyncio.run(_smoke_test())
