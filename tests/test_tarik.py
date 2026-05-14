from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


def _run(coro):
    return asyncio.run(coro)


def _pos(cid: str = "abc123", outcome: str = "Up") -> dict:
    return {
        "condition_id":  cid,
        "outcome":       outcome,
        "current_price": "0.60",
        "entry_price":   "0.50",
        "shares":        "10",
        "token_id":      "tok1",
        "question":      "BTC Up or Down?",
    }


def make_manager():
    m = MagicMock()
    m._process_exit_manual = MagicMock()
    return m


def test_execute_tarik_no_positions():
    mgr = make_manager()
    with patch("src.models.database.get_open_positions", return_value=[]):
        from src.execute.tarik import execute_tarik
        result = _run(execute_tarik(mgr, MagicMock(), MagicMock()))
    assert "tidak ada" in result.lower()
    mgr._process_exit_manual.assert_not_called()


def test_execute_tarik_closes_all_positions():
    mgr = make_manager()
    pos = _pos("aaa")
    with patch("src.models.database.get_open_positions", return_value=[pos]), \
         patch("src.risk.pricing.ke_decimal", side_effect=lambda x: float(x)):
        from src.execute.tarik import execute_tarik
        result = _run(execute_tarik(mgr, MagicMock(), MagicMock()))
    mgr._process_exit_manual.assert_called_once()
    assert "TARIK 1" in result


def test_execute_tarik_condition_ids_filter():
    mgr = make_manager()
    pos_a = _pos("aaa")
    pos_b = _pos("bbb")
    with patch("src.models.database.get_open_positions", return_value=[pos_a, pos_b]), \
         patch("src.risk.pricing.ke_decimal", side_effect=lambda x: float(x)):
        from src.execute.tarik import execute_tarik
        result = _run(execute_tarik(mgr, MagicMock(), MagicMock(), condition_ids=["aaa"]))
    assert mgr._process_exit_manual.call_count == 1
    call_kwargs = mgr._process_exit_manual.call_args[1]
    assert call_kwargs["condition_id"] == "aaa"
    assert "1 posisi lain" in result
