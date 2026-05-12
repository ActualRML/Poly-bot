"""Tests for binance_client.py stale cache fallback on FULL_PAUSE."""
import time
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock
import src.api.binance_client as bmod
from src.api.binance_client import fetch_klines, fetch_klines_extended

_async = pytest.mark.asyncio(loop_scope="function")


@pytest.fixture(autouse=True)
def reset_state():
    bmod._klines_cache.clear()
    bmod._rate_limit_status = "OK"
    bmod._rate_limit_set_at = 0.0
    bmod._ban_until = 0.0
    yield
    bmod._klines_cache.clear()
    bmod._rate_limit_status = "OK"
    bmod._rate_limit_set_at = 0.0
    bmod._ban_until = 0.0


def _session():
    s = MagicMock()
    s.get = MagicMock(side_effect=AssertionError("HTTP should not be called during FULL_PAUSE"))
    return s


_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_STALE_KLINES = [(_TS, 100.0, 105.0, 95.0, 102.0)]
_STALE_EXT    = [(_TS, 3000.0, 3100.0, 2900.0, 3050.0, 1000.0, 500.0)]


@_async
async def test_fetch_klines_full_pause_returns_stale_cache():
    ck = "BTC_1h_48_klines"
    bmod._klines_cache[ck] = (_STALE_KLINES, time.time() - 200)
    bmod._rate_limit_status = "FULL_PAUSE"
    bmod._rate_limit_set_at = time.monotonic()

    result = await fetch_klines("BTC", _session(), interval="1h", limit=48)
    assert result == _STALE_KLINES


@_async
async def test_fetch_klines_full_pause_returns_empty_when_no_cache():
    bmod._rate_limit_status = "FULL_PAUSE"
    bmod._rate_limit_set_at = time.monotonic()

    result = await fetch_klines("BTC", _session(), interval="1h", limit=48)
    assert result == []


@_async
async def test_fetch_klines_extended_full_pause_returns_stale_cache():
    ck = "ETH_1m_30_ext"
    bmod._klines_cache[ck] = (_STALE_EXT, time.time() - 200)
    bmod._rate_limit_status = "FULL_PAUSE"
    bmod._rate_limit_set_at = time.monotonic()

    result = await fetch_klines_extended("ETH", _session(), interval="1m", limit=30)
    assert result == _STALE_EXT


@_async
async def test_fetch_klines_extended_full_pause_returns_empty_when_no_cache():
    bmod._rate_limit_status = "FULL_PAUSE"
    bmod._rate_limit_set_at = time.monotonic()

    result = await fetch_klines_extended("ETH", _session(), interval="1m", limit=30)
    assert result == []


@_async
async def test_fetch_klines_with_start_ms_does_not_serve_stale_cache():
    ck = "BTC_1h_48_klines"
    bmod._klines_cache[ck] = (_STALE_KLINES, time.time() - 200)
    bmod._rate_limit_status = "FULL_PAUSE"
    bmod._rate_limit_set_at = time.monotonic()

    result = await fetch_klines("BTC", _session(), interval="1h", limit=48, start_ms=1000000)
    assert result == []
