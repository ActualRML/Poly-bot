from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


def _run(coro):
    return asyncio.run(coro)


def reset_cache():
    from src.utils import pricing_cache
    pricing_cache._price_cache.clear()
    pricing_cache._cache_time.clear()
    pricing_cache._cg_ban_until = 0.0


def test_fetch_crypto_price_returns_float_from_binance():
    reset_cache()
    session = MagicMock()
    with patch("src.api.binance_client.fetch_price", new=AsyncMock(return_value=50000.0)):
        from src.utils.pricing_cache import fetch_crypto_price
        result = _run(fetch_crypto_price("BTC", session))
    assert isinstance(result, float)
    assert result == 50000.0


def test_fetch_crypto_price_falls_back_to_coingecko():
    reset_cache()
    mock_resp = MagicMock()
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    mock_resp.status = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value={"ethereum": {"usd": 3000.0}})
    session = MagicMock()
    session.get = MagicMock(return_value=mock_resp)

    with patch("src.api.binance_client.fetch_price", new=AsyncMock(return_value=None)):
        from src.utils.pricing_cache import fetch_crypto_price
        result = _run(fetch_crypto_price("ETH", session))
    assert result == 3000.0


def test_fetch_crypto_price_uses_cache_on_second_call():
    reset_cache()
    from src.utils import pricing_cache
    pricing_cache._price_cache["BTC"] = 45000.0
    pricing_cache._cache_time["BTC"] = 1e15

    session = MagicMock()
    with patch("src.api.binance_client.fetch_price", new=AsyncMock(return_value=None)):
        from src.utils.pricing_cache import fetch_crypto_price
        result = _run(fetch_crypto_price("BTC", session))

    session.get.assert_not_called()
    assert result == 45000.0


def test_build_vol_data_returns_dict_with_default():
    reset_cache()
    with patch("src.api.binance_client.fetch_realized_vol", new=AsyncMock(return_value=0.44)):
        from src.utils.pricing_cache import build_vol_data
        result = _run(build_vol_data(MagicMock(), hours=4))
    assert isinstance(result, dict)
    assert "DEFAULT" in result
    assert "BTC" in result
    assert result["BTC"] == 0.44
