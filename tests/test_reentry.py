import pytest

from src.logic.reentry import (
    estimate_fair_value,
    check_reentry_signal,
    validate_reentry_orderbook,
    passes_time_gate,
)

def _scalp(confidence=0.65, momentum_score=1.0):
    return {"confidence": confidence, "momentum_score": momentum_score}

def _mtf(m_15m=0.005):
    return {"m_15m": m_15m}

def test_fair_value_thesis_still_holds_down():
    fv = estimate_fair_value("Down", _scalp(confidence=0.65, momentum_score=1.0), _mtf(m_15m=0.005))
    assert fv >= 0.50

def test_fair_value_thesis_still_holds_up():
    fv = estimate_fair_value("Up", _scalp(confidence=0.65, momentum_score=-1.0), _mtf(m_15m=-0.005))
    assert fv >= 0.50

def test_fair_value_thesis_reversed():
    fv = estimate_fair_value("Down", _scalp(confidence=0.65, momentum_score=-1.0), _mtf(m_15m=-0.005))
    assert fv < 0.50

def test_fair_value_returns_none_on_missing_data():
    assert estimate_fair_value("Up", None, _mtf()) is None
    assert estimate_fair_value("Up", _scalp(), None) is None

def test_fair_value_clamped():
    fv = estimate_fair_value("Down", _scalp(confidence=0.99, momentum_score=1.0), _mtf(m_15m=0.005))
    assert fv <= 0.85

def test_reentry_should_fire():
    r = check_reentry_signal(exit_price=0.65, current_market_price=0.40, fair_value=0.60)
    assert r["should_reenter"]
    assert r["drop_pct"] >= 0.30
    assert r["edge"] >= 0.05

def test_reentry_blocked_no_drop():
    r = check_reentry_signal(exit_price=0.65, current_market_price=0.55, fair_value=0.70)
    assert not r["should_reenter"]
    assert "drop" in r["reason"]

def test_reentry_blocked_insufficient_edge():
    r = check_reentry_signal(exit_price=0.65, current_market_price=0.40, fair_value=0.45)
    assert not r["should_reenter"]
    assert "edge" in r["reason"]

def test_reentry_invalid_prices():
    r = check_reentry_signal(exit_price=0.0, current_market_price=0.40, fair_value=0.60)
    assert not r["should_reenter"]

def test_reentry_drop_pct_calculated():
    r = check_reentry_signal(exit_price=0.80, current_market_price=0.40, fair_value=0.65)
    assert abs(r["drop_pct"] - 0.50) < 1e-4

def test_reentry_custom_thresholds():
    r = check_reentry_signal(
        exit_price=0.65, current_market_price=0.55, fair_value=0.70,
        drop_threshold=0.10, min_edge=0.05,
    )
    assert r["should_reenter"]

def test_orderbook_ok():
    bids = [(0.39, 100.0)]
    asks = [(0.41, 100.0)]
    r = validate_reentry_orderbook(bids, asks, capital_required=20.0, spread_max=0.05)
    assert r["ok"]

def test_orderbook_wide_spread():
    bids = [(0.30, 100.0)]
    asks = [(0.50, 100.0)]
    r = validate_reentry_orderbook(bids, asks, capital_required=20.0, spread_max=0.05)
    assert not r["ok"]
    assert "spread" in r["reason"]

def test_orderbook_thin_liquidity():
    bids = [(0.39, 100.0)]
    asks = [(0.41, 5.0)]  # only 5 shares × 0.41 = $2.05 liquidity
    r = validate_reentry_orderbook(bids, asks, capital_required=20.0, spread_max=0.05)
    assert not r["ok"]
    assert "liquidity" in r["reason"]

def test_orderbook_walks_down_for_depth():
    bids = [(0.39, 50.0)]
    asks = [(0.41, 30.0), (0.415, 30.0), (0.42, 50.0)]  # Combined: $44+ if walk down
    r = validate_reentry_orderbook(bids, asks, capital_required=20.0, spread_max=0.10)
    assert r["ok"]

def test_orderbook_empty():
    r = validate_reentry_orderbook([], [], capital_required=20.0)
    assert not r["ok"]
    assert r["reason"] == "empty_book"

def test_time_gate_pass():
    assert passes_time_gate(20.0, min_minutes=15.0)

def test_time_gate_fail():
    assert not passes_time_gate(10.0, min_minutes=15.0)

def test_time_gate_boundary():
    assert passes_time_gate(15.0, min_minutes=15.0)  # >= boundary OK


def test_get_recent_closed_hourly_returns_pnl_key():
    """Regression: returned dicts must have key 'pnl', not 'pnl_usdc'.
    The reentry LOSS guard in main.py uses r.get('pnl') — key mismatch = silent bypass."""
    import sqlite3
    from contextlib import contextmanager
    from unittest.mock import patch

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE positions (
        id INTEGER PRIMARY KEY, question TEXT, outcome TEXT,
        entry_price REAL, current_price REAL, highest_price REAL,
        shares REAL, capital_at_risk REAL, resolve_date TEXT,
        entry_time TEXT, status TEXT, exit_price REAL, exit_time TEXT,
        pnl_usdc REAL, exit_reason TEXT, gap_pct REAL,
        kelly_fraction REAL, strategy_mode TEXT, token_id TEXT,
        condition_id TEXT
    )""")
    conn.execute(
        "INSERT INTO positions (question, status, pnl_usdc, strategy_mode, exit_time) "
        "VALUES ('BTC Up?', 'closed', -5.0, 'updown_hourly_dry_run', '2026-05-12T10:00:00')"
    )
    conn.commit()

    @contextmanager
    def _mock_conn():
        yield conn

    with patch("src.models.database.get_conn", _mock_conn):
        from src.models.database import get_recent_closed_hourly
        result = get_recent_closed_hourly(limit=10)

    assert len(result) == 1
    assert "pnl" in result[0], "key must be 'pnl', not 'pnl_usdc'"
    assert "pnl_usdc" not in result[0]
    assert result[0]["pnl"] == -5.0

