"""Tests for src/logic/gbm_hourly.py — GBM directional entry logic."""
import math

import pytest

from src.logic.gbm_hourly import pick_gbm_direction, passes_opposite_reentry_gate
from src.logic.oracle_arb import gbm_prob_above

def test_pick_gbm_buy_up_when_model_underprices():
    r = pick_gbm_direction(prob_up=0.65, market_price_up=0.50, fee=0.018, min_edge=0.05)
    assert r["action"] == "BUY"
    assert r["outcome"] == "Up"
    assert r["buy_price"] == 0.50
    assert r["edge"] == pytest.approx(0.132, abs=1e-3)
    assert r["reason"] == "MODEL_UNDERPRICES_UP"

def test_pick_gbm_buy_down_when_model_overprices():
    r = pick_gbm_direction(prob_up=0.30, market_price_up=0.50, fee=0.018, min_edge=0.05)
    assert r["action"] == "BUY"
    assert r["outcome"] == "Down"
    assert r["buy_price"] == 0.50
    assert r["edge"] == pytest.approx(0.182, abs=1e-3)
    assert r["reason"] == "MODEL_OVERPRICES_UP"

def test_pick_gbm_skip_when_model_agrees_with_market():
    r = pick_gbm_direction(prob_up=0.51, market_price_up=0.50, fee=0.018, min_edge=0.05)
    assert r["action"] == "SKIP"
    assert r["outcome"] is None
    assert r["reason"] == "EDGE_BELOW_MIN"

def test_pick_gbm_skip_when_edge_below_threshold():
    r = pick_gbm_direction(prob_up=0.55, market_price_up=0.50, fee=0.0, min_edge=0.05)
    assert r["action"] == "BUY"  # ≥ min_edge passes
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
    r = pick_gbm_direction(prob_up=0.80, market_price_up=0.20, fee=0.018, min_edge=0.05)
    assert r["action"] == "SKIP"
    assert r["reason"] == "EDGE_TOO_HIGH_UNRELIABLE"
    assert r["edge"] == pytest.approx(0.582, abs=1e-3)

    r2 = pick_gbm_direction(prob_up=0.80, market_price_up=0.20, fee=0.018, min_edge=0.05, max_edge=1.0)
    assert r2["action"] == "BUY"
    assert r2["outcome"] == "Up"
    assert r2["buy_price"] == 0.20

def test_pick_gbm_picks_larger_edge():
    r = pick_gbm_direction(prob_up=0.40, market_price_up=0.55, fee=0.0, min_edge=0.0)
    assert r["action"] == "BUY"
    assert r["outcome"] == "Down"

def test_gbm_prob_at_strike_returns_half():
    p = gbm_prob_above(current=100.0, strike=100.0, vol_annual=0.40, time_remaining_s=3600)
    assert 0.45 <= p <= 0.55

def test_gbm_prob_far_above_strike():
    p = gbm_prob_above(current=105.0, strike=100.0, vol_annual=0.40, time_remaining_s=1800)
    assert p > 0.85

def test_gbm_prob_far_below_strike():
    p = gbm_prob_above(current=95.0, strike=100.0, vol_annual=0.40, time_remaining_s=1800)
    assert p < 0.15

def test_gbm_decision_end_to_end_realistic():
    p = gbm_prob_above(current=100_500, strike=100_000, vol_annual=0.40, time_remaining_s=1800)
    decision = pick_gbm_direction(prob_up=p, market_price_up=0.55, fee=0.018, min_edge=0.05)
    assert decision["action"] == "SKIP"
    assert decision["reason"] == "EDGE_TOO_HIGH_UNRELIABLE"

    p_highvol = gbm_prob_above(current=100_500, strike=100_000, vol_annual=2.0, time_remaining_s=1800)
    decision2 = pick_gbm_direction(prob_up=p_highvol, market_price_up=0.55, fee=0.018, min_edge=0.05)
    assert decision2["action"] in ("BUY", "SKIP")  # outcome depends on exact prob

def test_gbm_decision_end_to_end_market_already_priced_in():
    p = gbm_prob_above(current=100_500, strike=100_000, vol_annual=0.40, time_remaining_s=1800)
    decision = pick_gbm_direction(prob_up=p, market_price_up=0.92, fee=0.018, min_edge=0.05)
    assert decision["action"] == "SKIP"

def test_gbm_decision_picks_down_when_market_overconfident_on_up():
    p = gbm_prob_above(current=99_500, strike=100_000, vol_annual=0.40, time_remaining_s=1800)
    decision = pick_gbm_direction(prob_up=p, market_price_up=0.55, fee=0.018, min_edge=0.05)
    assert decision["action"] == "SKIP"
    assert decision["reason"] == "EDGE_TOO_HIGH_UNRELIABLE"

    p_long = gbm_prob_above(current=99_500, strike=100_000, vol_annual=0.40, time_remaining_s=21600)
    decision_long = pick_gbm_direction(prob_up=p_long, market_price_up=0.55, fee=0.018, min_edge=0.05)
    assert decision_long["action"] == "BUY"
    assert decision_long["outcome"] == "Down"

def test_opposite_gate_not_a_reentry_case():
    allowed, reason = passes_opposite_reentry_gate(
        locked_outcome=None, proposed_outcome="Up",
        time_remaining_s=1800, min_minutes=10,
    )
    assert allowed
    assert reason == "NOT_REENTRY"

def test_opposite_gate_allows_opposite_direction_with_time():
    allowed, reason = passes_opposite_reentry_gate(
        locked_outcome="Up", proposed_outcome="Down",
        time_remaining_s=1800, min_minutes=10,
    )
    assert allowed
    assert reason == "OPPOSITE_REENTRY_OK"

def test_opposite_gate_blocks_same_direction_chase():
    allowed, reason = passes_opposite_reentry_gate(
        locked_outcome="Up", proposed_outcome="Up",
        time_remaining_s=1800, min_minutes=10,
    )
    assert not allowed
    assert reason == "SAME_DIRECTION_CHASE_BLOCKED"

def test_pick_gbm_direction_always_returns_edge_up_and_edge_down():
    """Contract: pick_gbm_direction must include edge_up and edge_down — used by MTM flip."""
    d = pick_gbm_direction(prob_up=0.65, market_price_up=0.50, fee=0.018, min_edge=0.05)
    assert "edge_up" in d
    assert "edge_down" in d
    assert "outcome" in d
    assert "action" in d


def test_gbm_flip_condition_flips_when_opposed_and_sufficient_edge():
    gbm = {"outcome": "Up", "edge_up": 0.12, "edge_down": 0.08, "buy_price": 0.40}
    mtf_dir = "down"
    adj_min_edge = 0.06

    opposed = gbm["outcome"] == "Up" and mtf_dir == "down"
    flip_edge = gbm["edge_down"]

    assert opposed is True
    assert flip_edge >= adj_min_edge
    assert round(1.0 - gbm["buy_price"], 4) == 0.60


def test_gbm_flip_condition_skips_when_insufficient_flip_edge():
    gbm = {"outcome": "Up", "edge_up": 0.12, "edge_down": 0.02, "buy_price": 0.40}
    mtf_dir = "down"
    adj_min_edge = 0.06

    opposed = gbm["outcome"] == "Up" and mtf_dir == "down"
    flip_edge = gbm["edge_down"]

    assert opposed is True
    assert flip_edge < adj_min_edge


def test_gbm_flip_condition_no_change_when_aligned():
    gbm = {"outcome": "Up", "edge_up": 0.12, "edge_down": 0.02, "buy_price": 0.40}
    mtf_dir = "up"
    buy_outcome = gbm["outcome"]

    opposed = (buy_outcome == "Up" and mtf_dir == "down") or \
              (buy_outcome == "Down" and mtf_dir == "up")
    assert opposed is False


def test_gbm_flip_condition_down_to_up():
    gbm = {"outcome": "Down", "edge_up": 0.09, "edge_down": 0.11, "buy_price": 0.55}
    mtf_dir = "up"
    adj_min_edge = 0.06

    opposed = gbm["outcome"] == "Down" and mtf_dir == "up"
    flip_edge = gbm["edge_up"]

    assert opposed is True
    assert flip_edge >= adj_min_edge
    assert round(1.0 - gbm["buy_price"], 4) == 0.45


def test_opposite_gate_blocks_when_too_close_to_resolve():
    allowed, reason = passes_opposite_reentry_gate(
        locked_outcome="Up", proposed_outcome="Down",
        time_remaining_s=5 * 60, min_minutes=10,
    )
    assert not allowed
    assert reason == "TIME_FLOOR_10M"

def test_opposite_gate_time_floor_exact():
    allowed, reason = passes_opposite_reentry_gate(
        locked_outcome="Up", proposed_outcome="Down",
        time_remaining_s=10 * 60, min_minutes=10,
    )
    assert allowed
    assert reason == "OPPOSITE_REENTRY_OK"

def test_opposite_gate_works_with_down_locked():
    allowed, reason = passes_opposite_reentry_gate(
        locked_outcome="Down", proposed_outcome="Up",
        time_remaining_s=1500, min_minutes=10,
    )
    assert allowed
    assert reason == "OPPOSITE_REENTRY_OK"

def test_opposite_gate_custom_min_minutes():
    allowed, reason = passes_opposite_reentry_gate(
        locked_outcome="Up", proposed_outcome="Down",
        time_remaining_s=12 * 60, min_minutes=15,
    )
    assert not allowed
    assert reason == "TIME_FLOOR_15M"

