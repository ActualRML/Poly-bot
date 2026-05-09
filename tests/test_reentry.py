import pytest

from src.logic.reentry import (
    estimate_fair_value,
    check_reentry_signal,
    validate_reentry_orderbook,
    passes_time_gate,
)


# ── estimate_fair_value ───────────────────────────────────────────────────────

def _scalp(confidence=0.65, momentum_score=1.0):
    return {"confidence": confidence, "momentum_score": momentum_score}

def _mtf(m_15m=0.005):
    return {"m_15m": m_15m}

def test_fair_value_thesis_still_holds_down():
    # Momentum positive → contrarian bet was Down. m_15m still positive → still favors Down.
    fv = estimate_fair_value("Down", _scalp(confidence=0.65, momentum_score=1.0), _mtf(m_15m=0.005))
    assert fv >= 0.50

def test_fair_value_thesis_still_holds_up():
    # Momentum negative → contrarian bet was Up. m_15m still negative → still favors Up.
    fv = estimate_fair_value("Up", _scalp(confidence=0.65, momentum_score=-1.0), _mtf(m_15m=-0.005))
    assert fv >= 0.50

def test_fair_value_thesis_reversed():
    # Momentum was up (we bet Down), now flipped down (would expect Up). Thesis weakened.
    fv = estimate_fair_value("Down", _scalp(confidence=0.65, momentum_score=-1.0), _mtf(m_15m=-0.005))
    assert fv < 0.50

def test_fair_value_returns_none_on_missing_data():
    assert estimate_fair_value("Up", None, _mtf()) is None
    assert estimate_fair_value("Up", _scalp(), None) is None

def test_fair_value_clamped():
    fv = estimate_fair_value("Down", _scalp(confidence=0.99, momentum_score=1.0), _mtf(m_15m=0.005))
    assert fv <= 0.85


# ── check_reentry_signal ──────────────────────────────────────────────────────

def test_reentry_should_fire():
    r = check_reentry_signal(exit_price=0.65, current_market_price=0.40, fair_value=0.60)
    assert r["should_reenter"]
    assert r["drop_pct"] >= 0.30
    assert r["edge"] >= 0.05

def test_reentry_blocked_no_drop():
    r = check_reentry_signal(exit_price=0.65, current_market_price=0.55, fair_value=0.70)
    # drop = (0.65 - 0.55) / 0.65 = 0.154 — below 30% threshold
    assert not r["should_reenter"]
    assert "drop" in r["reason"]

def test_reentry_blocked_insufficient_edge():
    r = check_reentry_signal(exit_price=0.65, current_market_price=0.40, fair_value=0.45)
    # drop OK, but edge = 0.45 - 0.40 - 0.018 = 0.032 < 0.05
    assert not r["should_reenter"]
    assert "edge" in r["reason"]

def test_reentry_invalid_prices():
    r = check_reentry_signal(exit_price=0.0, current_market_price=0.40, fair_value=0.60)
    assert not r["should_reenter"]

def test_reentry_drop_pct_calculated():
    r = check_reentry_signal(exit_price=0.80, current_market_price=0.40, fair_value=0.65)
    assert abs(r["drop_pct"] - 0.50) < 1e-4

def test_reentry_custom_thresholds():
    # 15% drop, custom threshold 10% — should pass
    r = check_reentry_signal(
        exit_price=0.65, current_market_price=0.55, fair_value=0.70,
        drop_threshold=0.10, min_edge=0.05,
    )
    assert r["should_reenter"]


# ── validate_reentry_orderbook ────────────────────────────────────────────────

def test_orderbook_ok():
    bids = [(0.39, 100.0)]
    asks = [(0.41, 100.0)]
    r = validate_reentry_orderbook(bids, asks, capital_required=20.0, spread_max=0.05)
    assert r["ok"]

def test_orderbook_wide_spread():
    bids = [(0.30, 100.0)]
    asks = [(0.50, 100.0)]
    # spread = (0.50 - 0.30) / 0.50 = 0.40 = 40%
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


# ── passes_time_gate ──────────────────────────────────────────────────────────

def test_time_gate_pass():
    assert passes_time_gate(20.0, min_minutes=15.0)

def test_time_gate_fail():
    assert not passes_time_gate(10.0, min_minutes=15.0)

def test_time_gate_boundary():
    assert passes_time_gate(15.0, min_minutes=15.0)  # >= boundary OK
