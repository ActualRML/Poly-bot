import pytest
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import src.models.database as db_mod

@pytest.fixture
def manager(tmp_path, monkeypatch):
    db_path = tmp_path / "test_manager.db"
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    from src.logic.manager import PositionManager
    return PositionManager()

def _make_row(
    condition_id="0xabc",
    outcome="Yes",
    entry_price="0.65",
    current_price="0.65",
    highest_price="0.70",
    shares="15.38",
    capital_at_risk="10.00",
    resolve_offset=timedelta(hours=2),
    entry_offset=timedelta(hours=-1),
    naive=False,
    token_id="tok_001",
):
    resolve_dt = datetime.now(timezone.utc) + resolve_offset
    entry_dt = datetime.now(timezone.utc) + entry_offset
    if naive:
        resolve_dt = resolve_dt.replace(tzinfo=None)
        entry_dt = entry_dt.replace(tzinfo=None)
    return {
        "condition_id": condition_id,
        "question": "Will BTC be above $100k?",
        "outcome": outcome,
        "entry_price": entry_price,
        "current_price": current_price,
        "highest_price": highest_price,
        "shares": shares,
        "capital_at_risk": capital_at_risk,
        "resolve_date": resolve_dt.isoformat(),
        "entry_time": entry_dt.isoformat(),
        "token_id": token_id,
    }

def test_row_to_position_aware_datetime(manager):
    pos = manager._row_to_position(_make_row(), Decimal("0.65"))
    assert pos.resolve_date.tzinfo is not None
    assert pos.entry_time.tzinfo is not None

def test_row_to_position_naive_datetime_gets_utc(manager):
    pos = manager._row_to_position(_make_row(naive=True), Decimal("0.70"))
    assert pos.resolve_date.tzinfo is not None
    assert pos.entry_time.tzinfo is not None

def test_row_to_position_fields_correct(manager):
    pos = manager._row_to_position(
        _make_row(entry_price="0.65", shares="15.38", token_id="tok_001"),
        Decimal("0.70"),
    )
    assert pos.condition_id == "0xabc"
    assert pos.outcome == "Yes"
    assert pos.entry_price == Decimal("0.65")
    assert pos.current_price == Decimal("0.70")
    assert pos.token_id == "tok_001"

def test_row_to_position_highest_price(manager):
    pos = manager._row_to_position(_make_row(highest_price="0.80"), Decimal("0.70"))
    assert pos.highest_price == Decimal("0.80")

def test_can_open_allows_new_position(manager):
    with (
        patch("src.logic.manager.get_position_by_market", return_value=None),
        patch("src.logic.manager.count_open_positions", return_value=0),
    ):
        ok, reason = manager.can_open("0xnew", "Yes", Decimal("10"), Decimal("100"))
    assert ok is True
    assert reason == ""

def test_can_open_blocks_duplicate_market(manager):
    existing = {"condition_id": "0xabc", "outcome": "No"}
    with patch("src.logic.manager.get_position_by_market", return_value=existing):
        ok, reason = manager.can_open("0xabc", "Yes", Decimal("10"), Decimal("100"))
    assert ok is False
    assert "0xabc" in reason

def test_can_open_blocks_when_max_positions_reached(manager):
    with (
        patch("src.logic.manager.get_position_by_market", return_value=None),
        patch("src.logic.manager.count_open_positions", return_value=5),
    ):
        ok, reason = manager.can_open("0xnew", "Yes", Decimal("10"), Decimal("100"))
    assert ok is False
    assert "Max" in reason

def test_can_open_blocks_oversized_bet(manager):
    with (
        patch("src.logic.manager.get_position_by_market", return_value=None),
        patch("src.logic.manager.count_open_positions", return_value=0),
    ):
        ok, reason = manager.can_open("0xnew", "Yes", Decimal("40"), Decimal("100"))
    assert ok is False
    assert "%" in reason

def test_can_open_allows_at_max_pct(manager):
    with (
        patch("src.logic.manager.get_position_by_market", return_value=None),
        patch("src.logic.manager.count_open_positions", return_value=0),
    ):
        ok, _ = manager.can_open("0xnew", "Yes", Decimal("30"), Decimal("100"))
    assert ok is True

def test_can_open_skips_pct_check_when_zero_capital(manager):
    with (
        patch("src.logic.manager.get_position_by_market", return_value=None),
        patch("src.logic.manager.count_open_positions", return_value=0),
    ):
        ok, _ = manager.can_open("0xnew", "Yes", Decimal("50"), Decimal("0"))
    assert ok is True

def test_get_unrealized_pnl_positive(manager):
    with patch("src.logic.manager.get_open_positions", return_value=[{
        "entry_price": "0.60",
        "current_price": "0.80",
        "shares": "10.0",
    }]):
        pnl = manager.get_unrealized_pnl()
    assert pnl == pytest.approx(2.0)

def test_get_unrealized_pnl_negative(manager):
    with patch("src.logic.manager.get_open_positions", return_value=[{
        "entry_price": "0.80",
        "current_price": "0.60",
        "shares": "10.0",
    }]):
        pnl = manager.get_unrealized_pnl()
    assert pnl == pytest.approx(-2.0)

def test_get_unrealized_pnl_empty(manager):
    with patch("src.logic.manager.get_open_positions", return_value=[]):
        assert manager.get_unrealized_pnl() == 0.0

def test_get_unrealized_pnl_multiple_positions(manager):
    with patch("src.logic.manager.get_open_positions", return_value=[
        {"entry_price": "0.60", "current_price": "0.70", "shares": "10.0"},
        {"entry_price": "0.50", "current_price": "0.40", "shares": "5.0"},
    ]):
        pnl = manager.get_unrealized_pnl()
    assert pnl == pytest.approx(0.5)

