from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch


def _run(coro):
    return asyncio.run(coro)


def test_fetch_current_prices_empty_positions():
    with patch("src.models.database.get_open_positions", return_value=[]):
        from src.execute.reconciliation import fetch_current_prices
        result = _run(fetch_current_prices(MagicMock(), MagicMock()))
    assert result == {}


def test_fetch_current_prices_skips_missing_token_id():
    pos = {
        "condition_id": "abc",
        "outcome":      "Up",
        "token_id":     "",
        "entry_price":  "0.50",
        "shares":       "10",
    }
    clob = MagicMock()
    with patch("src.models.database.get_open_positions", return_value=[pos]):
        from src.execute.reconciliation import fetch_current_prices
        result = _run(fetch_current_prices(clob, MagicMock()))
    clob.ambil_snapshot.assert_not_called()
    assert result == {}
