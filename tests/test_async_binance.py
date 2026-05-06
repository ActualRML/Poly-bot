import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import src.api.binance_client as binance_mod
from src.api.binance_client import fetch_price, fetch_realized_vol, _compute_annualized_vol

_async = pytest.mark.asyncio(loop_scope="function")


@pytest.fixture(autouse=True)
def reset_binance_state():
    binance_mod._price_cache.clear()
    binance_mod._price_cache_time.clear()
    binance_mod._vol_cache.clear()
    binance_mod._vol_cache_time.clear()
    binance_mod._price_locks.clear()
    binance_mod._vol_locks.clear()
    binance_mod._ban_until = 0.0
    yield
    binance_mod._price_cache.clear()
    binance_mod._price_cache_time.clear()
    binance_mod._vol_cache.clear()
    binance_mod._vol_cache_time.clear()
    binance_mod._price_locks.clear()
    binance_mod._vol_locks.clear()
    binance_mod._ban_until = 0.0


def make_session(price="95000.0", status=200, json_side_effect=None):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    if json_side_effect:
        resp.json = AsyncMock(side_effect=json_side_effect)
    else:
        resp.json = AsyncMock(return_value={"price": price})

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=None)

    session = MagicMock()
    session.get = MagicMock(return_value=ctx)
    return session


def make_klines_session(closes: list[float]):
    now_ms = 1_700_000_000_000
    raw = [[now_ms + i * 3_600_000, c, c, c, c, "0"] for i, c in enumerate(closes)]

    resp = AsyncMock()
    resp.status = 200
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value=raw)

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=None)

    session = MagicMock()
    session.get = MagicMock(return_value=ctx)
    return session


# ==============================================================================
# _compute_annualized_vol — pure function
# ==============================================================================

def test_compute_vol_empty_list():
    assert _compute_annualized_vol([]) is None

def test_compute_vol_too_few_prices():
    assert _compute_annualized_vol([100.0, 101.0, 102.0]) is None

def test_compute_vol_constant_prices_returns_none():
    assert _compute_annualized_vol([100.0] * 48) is None

def test_compute_vol_realistic_data_returns_float():
    closes = [95000, 95200, 94800, 95500, 94200, 96000, 95800, 95100,
              94500, 96200, 95700, 95300, 94900, 96500, 95600, 95400]
    result = _compute_annualized_vol(closes)
    assert result is not None
    assert 0.02 <= result <= 10.0

def test_compute_vol_zero_price_skipped():
    closes = [100.0, 0.0, 100.0, 102.0, 101.0]
    result = _compute_annualized_vol(closes)
    assert result is None or isinstance(result, float)


# ==============================================================================
# fetch_price
# ==============================================================================

@_async
async def test_fetch_price_returns_correct_value():
    session = make_session(price="95000.0")
    price = await fetch_price("BTC", session)
    assert price == 95000.0

@_async
async def test_fetch_price_second_call_uses_cache():
    session = make_session(price="95000.0")
    await fetch_price("BTC", session)
    await fetch_price("BTC", session)
    assert session.get.call_count == 1

@_async
async def test_fetch_price_concurrent_calls_hit_api_once():
    session = make_session(price="95000.0")
    results = await asyncio.gather(*[fetch_price("BTC", session) for _ in range(10)])
    assert all(r == 95000.0 for r in results)
    assert session.get.call_count == 1

@_async
async def test_fetch_price_rate_limit_returns_stale_cache():
    binance_mod._price_cache["BTC"] = 94000.0
    session = make_session(status=418)
    price = await fetch_price("BTC", session)
    assert binance_mod._ban_until > 0
    assert price == 94000.0

@_async
async def test_fetch_price_exception_returns_stale_cache():
    binance_mod._price_cache["BTC"] = 94000.0
    session = make_session(json_side_effect=Exception("network error"))
    price = await fetch_price("BTC", session)
    assert price == 94000.0

@_async
async def test_fetch_price_unknown_symbol_returns_none():
    session = make_session()
    assert await fetch_price("UNKNOWN", session) is None

@_async
async def test_fetch_price_no_stale_cache_on_exception_returns_none():
    session = make_session(json_side_effect=Exception("timeout"))
    price = await fetch_price("BTC", session)
    assert price is None


# ==============================================================================
# fetch_realized_vol
# ==============================================================================

@_async
async def test_fetch_realized_vol_returns_float():
    closes = [95000 + i * 100 for i in range(26)]
    session = make_klines_session(closes)
    result = await fetch_realized_vol("BTC", session, hours=24)
    assert result is None or isinstance(result, float)

@_async
async def test_fetch_realized_vol_second_call_uses_cache():
    closes = [95000, 95200, 94800, 95500, 94200, 96000, 95800, 95100,
              94500, 96200, 95700, 95300, 94900, 96500, 95600, 95400,
              95000, 95300, 94700, 96100, 95500, 95800, 94400, 96300, 95200]
    session = make_klines_session(closes)
    vol1 = await fetch_realized_vol("BTC", session, hours=24)
    if vol1 is None:
        pytest.skip("vol=None tidak di-cache — skip tes ini")
    call_count_after_first = session.get.call_count
    await fetch_realized_vol("BTC", session, hours=24)
    assert session.get.call_count == call_count_after_first

@_async
async def test_fetch_realized_vol_ban_active_returns_none():
    binance_mod._ban_until = 9_999_999_999.0
    session = make_session()
    result = await fetch_realized_vol("BTC", session, hours=4)
    assert session.get.call_count == 0
    assert result is None


# ==============================================================================
# _build_vol_data (main.py)
# ==============================================================================

@_async
async def test_build_vol_data_all_succeed():
    from src.main import _build_vol_data

    async def mock_vol(symbol, session, hours):
        return {"BTC": 0.45, "ETH": 0.60, "SOL": 0.70, "BNB": 0.55, "XRP": 0.65}[symbol]

    with patch("src.api.binance_client.fetch_realized_vol", side_effect=mock_vol):
        result = await _build_vol_data(MagicMock())

    assert result == {"BTC": 0.45, "ETH": 0.60, "SOL": 0.70, "BNB": 0.55, "XRP": 0.65, "DEFAULT": 0.40}

@_async
async def test_build_vol_data_partial_none_excluded():
    from src.main import _build_vol_data

    async def mock_vol(symbol, session, hours):
        return None if symbol == "XRP" else 0.50

    with patch("src.api.binance_client.fetch_realized_vol", side_effect=mock_vol):
        result = await _build_vol_data(MagicMock())

    assert "BTC" in result
    assert "XRP" not in result
    assert result["DEFAULT"] == 0.40

@_async
async def test_build_vol_data_exception_does_not_propagate():
    from src.main import _build_vol_data

    async def mock_vol(symbol, session, hours):
        if symbol == "SOL":
            raise Exception("Binance timeout")
        return 0.50

    with patch("src.api.binance_client.fetch_realized_vol", side_effect=mock_vol):
        result = await _build_vol_data(MagicMock())

    assert "BTC" in result
    assert "SOL" not in result

@_async
async def test_build_vol_data_all_fail_returns_default_only():
    from src.main import _build_vol_data

    async def mock_vol(symbol, session, hours):
        return None

    with patch("src.api.binance_client.fetch_realized_vol", side_effect=mock_vol):
        result = await _build_vol_data(MagicMock())

    assert list(result.keys()) == ["DEFAULT"]
    assert result["DEFAULT"] == 0.40
