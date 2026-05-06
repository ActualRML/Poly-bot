import sqlite3
import pytest
from decimal import Decimal
from datetime import datetime, timezone, timedelta

import src.models.database as db_mod


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_bot.db"
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    db_mod.init_db()
    yield db_path


def _sample_pos(condition_id="0xabc", outcome="Yes"):
    return {
        "condition_id": condition_id,
        "question": "Will BTC be above $100k?",
        "outcome": outcome,
        "entry_price": Decimal("0.65"),
        "current_price": Decimal("0.65"),
        "highest_price": Decimal("0.65"),
        "shares": Decimal("15.38"),
        "capital_at_risk": Decimal("10.00"),
        "resolve_date": datetime.now(timezone.utc) + timedelta(hours=1),
        "entry_time": datetime.now(timezone.utc),
        "token_id": "tok_yes_001",
        "gap_pct": "0.15",
        "kelly_fraction": "0.05",
        "strategy_mode": "mispricing",
    }


# ==============================================================================
# init_db / schema
# ==============================================================================

def test_init_db_creates_tables(tmp_path, monkeypatch):
    fresh_db = tmp_path / "fresh.db"
    monkeypatch.setattr(db_mod, "DB_PATH", fresh_db)
    db_mod.init_db()
    with sqlite3.connect(fresh_db) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    assert "positions" in tables
    assert "trades" in tables
    assert "predictions" in tables

def test_migrate_adds_token_id_column(tmp_path, monkeypatch):
    old_db = tmp_path / "old.db"
    monkeypatch.setattr(db_mod, "DB_PATH", old_db)
    with sqlite3.connect(old_db) as conn:
        conn.execute("""
            CREATE TABLE positions (
                id INTEGER PRIMARY KEY,
                condition_id TEXT, question TEXT, outcome TEXT,
                entry_price TEXT, current_price TEXT, highest_price TEXT,
                shares TEXT, capital_at_risk TEXT, resolve_date TEXT,
                entry_time TEXT, status TEXT DEFAULT 'open',
                exit_price TEXT, exit_time TEXT, pnl_usdc TEXT,
                exit_reason TEXT, gap_pct TEXT, kelly_fraction TEXT,
                strategy_mode TEXT,
                UNIQUE(condition_id, outcome)
            )
        """)
        conn.commit()
    db_mod.init_db()
    with sqlite3.connect(old_db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(positions)").fetchall()}
    assert "token_id" in cols


# ==============================================================================
# save_position / get_open_positions
# ==============================================================================

def test_save_position_returns_row_id():
    row_id = db_mod.save_position(_sample_pos())
    assert isinstance(row_id, int) and row_id > 0

def test_get_open_positions_after_save():
    db_mod.save_position(_sample_pos())
    positions = db_mod.get_open_positions()
    assert len(positions) == 1
    assert positions[0]["condition_id"] == "0xabc"
    assert positions[0]["outcome"] == "Yes"
    assert positions[0]["status"] == "open"

def test_save_position_upsert_does_not_duplicate():
    pos = _sample_pos()
    db_mod.save_position(pos)
    pos["entry_price"] = Decimal("0.70")
    db_mod.save_position(pos)
    positions = db_mod.get_open_positions()
    assert len(positions) == 1
    assert positions[0]["entry_price"] == "0.70"

def test_save_position_upsert_resets_exit_fields():
    db_mod.save_position(_sample_pos())
    db_mod.close_position("0xabc", "Yes", Decimal("0.90"), "test_exit", Decimal("3.85"))
    db_mod.save_position(_sample_pos())
    p = db_mod.get_open_positions()[0]
    assert p["status"] == "open"
    assert p["exit_price"] is None
    assert p["pnl_usdc"] is None

def test_two_different_markets_both_open():
    db_mod.save_position(_sample_pos("0x1", "Yes"))
    db_mod.save_position(_sample_pos("0x2", "Yes"))
    assert len(db_mod.get_open_positions()) == 2


# ==============================================================================
# close_position
# ==============================================================================

def test_close_position_removes_from_open():
    db_mod.save_position(_sample_pos())
    db_mod.close_position("0xabc", "Yes", Decimal("0.90"), "trailing_stop", Decimal("3.85"))
    assert db_mod.get_open_positions() == []

def test_close_position_stores_pnl():
    db_mod.save_position(_sample_pos())
    db_mod.close_position("0xabc", "Yes", Decimal("0.90"), "trailing_stop", Decimal("3.85"))
    with sqlite3.connect(db_mod.DB_PATH) as conn:
        row = conn.execute(
            "SELECT pnl_usdc FROM positions WHERE condition_id='0xabc'"
        ).fetchone()
    assert float(row[0]) == pytest.approx(3.85)

def test_close_position_only_affects_open_status():
    db_mod.save_position(_sample_pos())
    db_mod.close_position("0xabc", "Yes", Decimal("0.90"), "stop", Decimal("3.0"))
    # Second call has no effect — already closed
    db_mod.close_position("0xabc", "Yes", Decimal("0.50"), "stop2", Decimal("-5.0"))
    with sqlite3.connect(db_mod.DB_PATH) as conn:
        row = conn.execute(
            "SELECT pnl_usdc FROM positions WHERE condition_id='0xabc'"
        ).fetchone()
    assert float(row[0]) == pytest.approx(3.0)


# ==============================================================================
# get_stats
# ==============================================================================

def _close_with_trade(condition_id, outcome, exit_price, reason, pnl):
    db_mod.close_position(condition_id, outcome, exit_price, reason, pnl)
    db_mod.log_trade({
        "condition_id": condition_id,
        "question":     "Will BTC be above $100k?",
        "outcome":      outcome,
        "action":       "exit",
        "price":        str(exit_price),
        "shares":       "10",
        "usdc_amount":  float(pnl),
    })


def test_stats_empty_db():
    stats = db_mod.get_stats()
    assert stats["winrate"] == 0
    assert (stats["total_pnl"] or 0) == 0

def test_stats_with_mixed_closed_positions():
    for cid in ("0x1", "0x2", "0x3"):
        db_mod.save_position(_sample_pos(cid, "Yes"))
    _close_with_trade("0x1", "Yes", Decimal("0.90"), "stop", Decimal("5.00"))
    _close_with_trade("0x2", "Yes", Decimal("0.30"), "stop", Decimal("-3.00"))
    _close_with_trade("0x3", "Yes", Decimal("0.80"), "stop", Decimal("2.00"))
    stats = db_mod.get_stats()
    assert stats["total_trades"] == 3
    assert stats["wins"] == 2
    assert stats["winrate"] == pytest.approx(66.7)
    assert stats["total_pnl"] == pytest.approx(4.0)

def test_stats_open_positions_not_counted():
    db_mod.save_position(_sample_pos("0x1", "Yes"))
    stats = db_mod.get_stats()
    assert stats["total_trades"] == 0

def test_stats_survives_reopen():
    db_mod.save_position(_sample_pos("0xabc", "Yes"))
    _close_with_trade("0xabc", "Yes", Decimal("0.30"), "stop", Decimal("-4.00"))
    db_mod.save_position(_sample_pos("0xabc", "Yes"))  # re-enter same market
    _close_with_trade("0xabc", "Yes", Decimal("0.90"), "stop", Decimal("5.00"))
    stats = db_mod.get_stats()
    assert stats["total_trades"] == 2
    assert stats["wins"] == 1
    assert stats["total_pnl"] == pytest.approx(1.0)


# ==============================================================================
# count_open_positions / get_position_by_market
# ==============================================================================

def test_count_open_positions():
    assert db_mod.count_open_positions() == 0
    db_mod.save_position(_sample_pos("0x1", "Yes"))
    db_mod.save_position(_sample_pos("0x2", "Yes"))
    assert db_mod.count_open_positions() == 2

def test_get_position_by_market_found():
    db_mod.save_position(_sample_pos("0xabc", "Yes"))
    pos = db_mod.get_position_by_market("0xabc")
    assert pos is not None
    assert pos["condition_id"] == "0xabc"

def test_get_position_by_market_not_found():
    assert db_mod.get_position_by_market("0xnotexist") is None


# ==============================================================================
# log_prediction / resolve_prediction
# ==============================================================================

def test_log_prediction_and_resolve_won():
    db_mod.log_prediction({
        "condition_id": "0xpred",
        "question": "BTC up?",
        "outcome": "Yes",
        "predicted_prob": "0.75",
        "market_price": "0.60",
        "gap_pct": "0.15",
    })
    db_mod.resolve_prediction("0xpred", "Yes", won=True, resolve_price=0.95)
    with sqlite3.connect(db_mod.DB_PATH) as conn:
        row = conn.execute(
            "SELECT actual_outcome, resolve_price FROM predictions WHERE condition_id='0xpred'"
        ).fetchone()
    assert row[0] == 1
    assert float(row[1]) == pytest.approx(0.95)

def test_log_prediction_and_resolve_lost():
    db_mod.log_prediction({
        "condition_id": "0xlost",
        "question": "ETH up?",
        "outcome": "Yes",
        "predicted_prob": "0.70",
        "market_price": "0.60",
        "gap_pct": "0.10",
    })
    db_mod.resolve_prediction("0xlost", "Yes", won=False, resolve_price=0.10)
    with sqlite3.connect(db_mod.DB_PATH) as conn:
        row = conn.execute(
            "SELECT actual_outcome FROM predictions WHERE condition_id='0xlost'"
        ).fetchone()
    assert row[0] == 0

def test_log_prediction_duplicate_ignored():
    data = {
        "condition_id": "0xdup",
        "question": "Q?",
        "outcome": "Yes",
        "predicted_prob": "0.70",
        "market_price": "0.60",
        "gap_pct": "0.10",
    }
    db_mod.log_prediction(data)
    db_mod.log_prediction(data)  # ON CONFLICT DO NOTHING
    with sqlite3.connect(db_mod.DB_PATH) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM predictions WHERE condition_id='0xdup'"
        ).fetchone()[0]
    assert count == 1
