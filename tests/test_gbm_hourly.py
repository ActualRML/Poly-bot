"""Tests for src/logic/gbm_hourly.py — GBM directional entry logic."""
import math

import pytest

from src.logic.gbm_hourly import pick_gbm_direction, passes_opposite_reentry_gate
from src.logic.oracle_arb import gbm_prob_above


# ── pick_gbm_direction (pure) ─────────────────────────────────────────────────

def test_pick_gbm_buy_up_when_model_underprices():
    # Model says P(Up)=0.65, market prices Up at 0.50
    # edge_up = 0.65 - 0.50 - 0.018 = 0.132 → BUY Up
    r = pick_gbm_direction(prob_up=0.65, market_price_up=0.50, fee=0.018, min_edge=0.05)
    assert r["action"] == "BUY"
    assert r["outcome"] == "Up"
    assert r["buy_price"] == 0.50
    assert r["edge"] == pytest.approx(0.132, abs=1e-3)
    assert r["reason"] == "MODEL_UNDERPRICES_UP"


def test_pick_gbm_buy_down_when_model_overprices():
    # Model says P(Up)=0.30, market prices Up at 0.50
    # P(Down)=0.70, market_price_down=0.50
    # edge_down = 0.70 - 0.50 - 0.018 = 0.182 → BUY Down
    r = pick_gbm_direction(prob_up=0.30, market_price_up=0.50, fee=0.018, min_edge=0.05)
    assert r["action"] == "BUY"
    assert r["outcome"] == "Down"
    assert r["buy_price"] == 0.50
    assert r["edge"] == pytest.approx(0.182, abs=1e-3)
    assert r["reason"] == "MODEL_OVERPRICES_UP"


def test_pick_gbm_skip_when_model_agrees_with_market():
    # P(Up)=0.51, market=0.50 → tiny edge, fee eats it
    # edge_up = 0.51 - 0.50 - 0.018 = -0.008
    # edge_down = 0.49 - 0.50 - 0.018 = -0.028
    r = pick_gbm_direction(prob_up=0.51, market_price_up=0.50, fee=0.018, min_edge=0.05)
    assert r["action"] == "SKIP"
    assert r["outcome"] is None
    assert r["reason"] == "EDGE_BELOW_MIN"


def test_pick_gbm_skip_when_edge_below_threshold():
    # P(Up)=0.55, market=0.50, fee=0
    # edge_up = 0.55 - 0.50 - 0 = 0.05 — exactly at threshold (not < min_edge)
    r = pick_gbm_direction(prob_up=0.55, market_price_up=0.50, fee=0.0, min_edge=0.05)
    assert r["action"] == "BUY"  # ≥ min_edge passes
    # bump to require strictly above
    r2 = pick_gbm_direction(prob_up=0.54, market_price_up=0.50, fee=0.0, min_edge=0.05)
    assert r2["action"] == "SKIP"


def test_pick_gbm_invalid_prob():
    r = pick_gbm_direction(prob_up=-0.1, market_price_up=0.5)
    assert r["action"] == "SKIP"
    assert r["reason"] == "INVALID_PROB"
    r2 = pick_gbm_direction(prob_up=1.5, market_price_up=0.5)
    assert r2["action"] == "SKIP"
    assert r2["reason"] == "INVALID_PROB"


def test_pick_gbm_invalid_market_price():
    for bad in (0.0, 1.0, -0.5, 1.5):
        r = pick_gbm_direction(prob_up=0.5, market_price_up=bad)
        assert r["action"] == "SKIP"
        assert r["reason"] == "INVALID_MARKET_PRICE"


def test_pick_gbm_extreme_underpriced_up():
    # Market says Up=0.20 but model says P(Up)=0.80 (huge edge)
    r = pick_gbm_direction(prob_up=0.80, market_price_up=0.20, fee=0.018, min_edge=0.05)
    assert r["action"] == "BUY"
    assert r["outcome"] == "Up"
    assert r["buy_price"] == 0.20
    assert r["edge"] == pytest.approx(0.582, abs=1e-3)


def test_pick_gbm_picks_larger_edge():
    # Both edge_up and edge_down can't simultaneously beat threshold
    # (edge_up + edge_down = -2·fee when market sums to 1) — verify the larger one wins
    r = pick_gbm_direction(prob_up=0.40, market_price_up=0.55, fee=0.0, min_edge=0.0)
    # edge_up   = 0.40 - 0.55 - 0   = -0.15
    # edge_down = 0.60 - 0.45 - 0   = +0.15
    assert r["action"] == "BUY"
    assert r["outcome"] == "Down"


# ── Integration with gbm_prob_above ───────────────────────────────────────────

def test_gbm_prob_at_strike_returns_half():
    # Current == strike → P(Up) ≈ 0.5 (with negligible drift adjustment)
    p = gbm_prob_above(current=100.0, strike=100.0, vol_annual=0.40, time_remaining_s=3600)
    assert 0.45 <= p <= 0.55


def test_gbm_prob_far_above_strike():
    # Current 5% above strike with 30 min left, vol 40% — should be > 0.85
    p = gbm_prob_above(current=105.0, strike=100.0, vol_annual=0.40, time_remaining_s=1800)
    assert p > 0.85


def test_gbm_prob_far_below_strike():
    # Current 5% below strike with 30 min left
    p = gbm_prob_above(current=95.0, strike=100.0, vol_annual=0.40, time_remaining_s=1800)
    assert p < 0.15


def test_gbm_decision_end_to_end_realistic():
    # BTC 30m to resolve, vol=40% annualized, current=$100,500, strike=$100,000
    # Market prices Up at 0.55 (fairly high)
    # GBM should give P(Up) very high, edge positive → BUY Up
    p = gbm_prob_above(current=100_500, strike=100_000, vol_annual=0.40, time_remaining_s=1800)
    decision = pick_gbm_direction(prob_up=p, market_price_up=0.55, fee=0.018, min_edge=0.05)
    assert decision["action"] == "BUY"
    assert decision["outcome"] == "Up"


def test_gbm_decision_end_to_end_market_already_priced_in():
    # Same setup but market already prices Up at 0.92 — no edge left
    p = gbm_prob_above(current=100_500, strike=100_000, vol_annual=0.40, time_remaining_s=1800)
    decision = pick_gbm_direction(prob_up=p, market_price_up=0.92, fee=0.018, min_edge=0.05)
    # If P(Up)≈0.93 vs market 0.92, edge_up ≈ 0.93 - 0.92 - 0.018 = -0.008 → skip
    assert decision["action"] == "SKIP"


def test_gbm_decision_picks_down_when_market_overconfident_on_up():
    # Current=$99,500 (below strike), but market still prices Up at 0.55 (over-confident)
    # P(Up) should be ~0.30, market_down=0.45, edge_down = 0.70 - 0.45 - 0.018 = 0.232
    p = gbm_prob_above(current=99_500, strike=100_000, vol_annual=0.40, time_remaining_s=1800)
    decision = pick_gbm_direction(prob_up=p, market_price_up=0.55, fee=0.018, min_edge=0.05)
    assert decision["action"] == "BUY"
    assert decision["outcome"] == "Down"


# ── passes_opposite_reentry_gate ──────────────────────────────────────────────

def test_opposite_gate_not_a_reentry_case():
    # locked_outcome=None → never blocked here (normal entry path)
    allowed, reason = passes_opposite_reentry_gate(
        locked_outcome=None, proposed_outcome="Up",
        time_remaining_s=1800, min_minutes=10,
    )
    assert allowed
    assert reason == "NOT_REENTRY"


def test_opposite_gate_allows_opposite_direction_with_time():
    # Locked Up, GBM picks Down, 30m left — classic fakeout reversal scenario
    allowed, reason = passes_opposite_reentry_gate(
        locked_outcome="Up", proposed_outcome="Down",
        time_remaining_s=1800, min_minutes=10,
    )
    assert allowed
    assert reason == "OPPOSITE_REENTRY_OK"


def test_opposite_gate_blocks_same_direction_chase():
    # Locked Up, GBM picks Up again → same-direction chase blocked
    allowed, reason = passes_opposite_reentry_gate(
        locked_outcome="Up", proposed_outcome="Up",
        time_remaining_s=1800, min_minutes=10,
    )
    assert not allowed
    assert reason == "SAME_DIRECTION_CHASE_BLOCKED"


def test_opposite_gate_blocks_when_too_close_to_resolve():
    # Opposite direction but only 5m left — time floor blocks
    allowed, reason = passes_opposite_reentry_gate(
        locked_outcome="Up", proposed_outcome="Down",
        time_remaining_s=5 * 60, min_minutes=10,
    )
    assert not allowed
    assert reason == "TIME_FLOOR_10M"


def test_opposite_gate_time_floor_exact():
    # Exactly at floor — should pass (>= comparison)
    allowed, reason = passes_opposite_reentry_gate(
        locked_outcome="Up", proposed_outcome="Down",
        time_remaining_s=10 * 60, min_minutes=10,
    )
    assert allowed
    assert reason == "OPPOSITE_REENTRY_OK"


def test_opposite_gate_works_with_down_locked():
    # Locked Down, GBM picks Up → allowed
    allowed, reason = passes_opposite_reentry_gate(
        locked_outcome="Down", proposed_outcome="Up",
        time_remaining_s=1500, min_minutes=10,
    )
    assert allowed
    assert reason == "OPPOSITE_REENTRY_OK"


def test_opposite_gate_custom_min_minutes():
    # Custom 15m floor — 12m should fail
    allowed, reason = passes_opposite_reentry_gate(
        locked_outcome="Up", proposed_outcome="Down",
        time_remaining_s=12 * 60, min_minutes=15,
    )
    assert not allowed
    assert reason == "TIME_FLOOR_15M"
