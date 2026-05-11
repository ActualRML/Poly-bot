import logging
import math
import time
import pytest

import src.api.binance_client as _bc

from src.logic.oracle_arb import (
    time_urgency,
    detect_latency_arb,
    gbm_prob_above,
    gbm_prob_below,
    gbm_mc_prob_above,
    gbm_mc_with_audit,
    sync_strike_price,
    order_book_imbalance,
    sell_wall_check,
    arb_edge,
    alpha_signal,
    silent_execution,
)
from src.api.binance_ws import BinanceTickBuffer, GHOST_TIMEOUT

def test_urgency_normal():
    assert time_urgency(60.0) == "NORMAL"

def test_urgency_high_alert():
    assert time_urgency(30.0) == "HIGH_ALERT"
    assert time_urgency(15.0) == "HIGH_ALERT"

def test_urgency_final_snipe():
    assert time_urgency(10.0) == "FINAL_SECOND_SNIPE"
    assert time_urgency(1.0) == "FINAL_SECOND_SNIPE"
    assert time_urgency(0.0) == "FINAL_SECOND_SNIPE"

def test_latency_arb_detected_up():
    r = detect_latency_arb(binance_move_pct=0.3, elapsed_s=2.0)
    assert r["detected"]
    assert r["direction"] == "up"
    assert r["label"] == "ARB_DETECTED"

def test_latency_arb_detected_down():
    r = detect_latency_arb(binance_move_pct=-0.5, elapsed_s=1.0)
    assert r["detected"]
    assert r["direction"] == "down"

def test_latency_arb_not_detected_small_move():
    r = detect_latency_arb(binance_move_pct=0.05, elapsed_s=1.0)
    assert not r["detected"]
    assert r["label"] == "NO_ARB"

def test_latency_arb_not_detected_expired():
    r = detect_latency_arb(binance_move_pct=0.5, elapsed_s=6.0, max_lag_s=5.0)
    assert not r["detected"]

def test_latency_arb_already_priced_in():
    r = detect_latency_arb(binance_move_pct=0.3, elapsed_s=2.0, polymarket_move_pct=0.3)
    assert not r["detected"]

def test_latency_arb_gap_pct():
    r = detect_latency_arb(binance_move_pct=0.5, elapsed_s=1.0, polymarket_move_pct=0.1)
    assert abs(r["gap_pct"] - 0.4) < 1e-4

def test_gbm_prob_above_at_strike():
    p = gbm_prob_above(100.0, 100.0, vol_annual=1.0, time_remaining_s=3600)
    assert 0.4 < p < 0.6

def test_gbm_prob_above_well_above_strike():
    p = gbm_prob_above(110.0, 100.0, vol_annual=0.5, time_remaining_s=60)
    assert p > 0.85

def test_gbm_prob_above_well_below_strike():
    p = gbm_prob_above(90.0, 100.0, vol_annual=0.5, time_remaining_s=60)
    assert p < 0.15

def test_gbm_prob_below_complement():
    above = gbm_prob_above(100.0, 100.0, vol_annual=0.8, time_remaining_s=7200)
    below = gbm_prob_below(100.0, 100.0, vol_annual=0.8, time_remaining_s=7200)
    assert abs(above + below - 1.0) < 1e-10

def test_gbm_prob_zero_time():
    p = gbm_prob_above(100.0, 99.0, vol_annual=0.5, time_remaining_s=0)
    assert p == 0.5

def test_gbm_prob_zero_vol():
    p = gbm_prob_above(100.0, 99.0, vol_annual=0.0, time_remaining_s=60)
    assert p == 0.5

def test_gbm_prob_longer_time_more_uncertain():
    p_short = gbm_prob_above(105.0, 100.0, vol_annual=2.0, time_remaining_s=10)
    p_long  = gbm_prob_above(105.0, 100.0, vol_annual=2.0, time_remaining_s=3600)
    assert p_short > p_long

def test_gbm_mc_seed_reproducible():
    p1 = gbm_mc_prob_above(100.0, 100.0, 0.8, 60, seed=42)
    p2 = gbm_mc_prob_above(100.0, 100.0, 0.8, 60, seed=42)
    assert p1 == p2

def test_gbm_mc_prob_above_far_above_strike():
    p = gbm_mc_prob_above(110.0, 100.0, 0.5, 10, n_paths=1000, seed=0)
    assert p > 0.85

def test_gbm_mc_prob_above_far_below_strike():
    p = gbm_mc_prob_above(90.0, 100.0, 0.5, 10, n_paths=1000, seed=0)
    assert p < 0.15

def test_gbm_mc_roughly_matches_closed_form():
    closed = gbm_prob_above(100.0, 102.0, 1.0, 60)
    mc     = gbm_mc_prob_above(100.0, 102.0, 1.0, 60, n_paths=5000, seed=1)
    assert abs(closed - mc) < 0.05

def test_gbm_mc_zero_time():
    p = gbm_mc_prob_above(100.0, 99.0, 0.5, 0, n_paths=100)
    assert p == 0.5

def test_obi_buy_pressure():
    bids = [(100.0, 500.0), (99.5, 200.0)]
    asks = [(100.5, 50.0)]
    r = order_book_imbalance(bids, asks, 100.0, range_pct=0.01)
    assert r["signal"] == "buy_pressure"
    assert r["ratio"] >= 2.0

def test_obi_sell_pressure():
    bids = [(100.0, 50.0)]
    asks = [(100.5, 500.0), (101.0, 200.0)]
    r = order_book_imbalance(bids, asks, 100.0, range_pct=0.01)
    assert r["signal"] == "sell_pressure"

def test_obi_neutral():
    bids = [(100.0, 100.0)]
    asks = [(100.5, 100.0)]
    r = order_book_imbalance(bids, asks, 100.0, range_pct=0.01)
    assert r["signal"] == "neutral"

def test_obi_empty_books():
    r = order_book_imbalance([], [], 100.0)
    assert r["signal"] == "neutral"
    assert r["ratio"] == 1.0

def test_obi_out_of_range_excluded():
    bids = [(200.0, 9999.0)]
    asks = [(100.5, 100.0)]
    r = order_book_imbalance(bids, asks, 100.0, range_pct=0.01)
    assert r["bid_vol"] == 0.0

def test_sell_wall_detected():
    asks = [(100.1, 10.0), (100.2, 500.0), (100.3, 8.0)]
    r = sell_wall_check(asks, current_price=100.0, strike_price=100.5, wall_ratio=5.0)
    assert r["wall_detected"]
    assert r["contrarian_signal"] == "down"
    assert r["wall_price"] == 100.2

def test_sell_wall_not_detected():
    asks = [(100.1, 10.0), (100.2, 11.0), (100.3, 9.0)]
    r = sell_wall_check(asks, current_price=100.0, strike_price=100.5)
    assert not r["wall_detected"]
    assert r["contrarian_signal"] is None

def test_sell_wall_empty_asks():
    r = sell_wall_check([], current_price=100.0, strike_price=101.0)
    assert not r["wall_detected"]
    assert r["wall_size"] == 0.0

def test_sell_wall_no_asks_in_zone():
    asks = [(102.0, 1000.0)]
    r = sell_wall_check(asks, current_price=100.0, strike_price=100.5, scan_range_pct=0.003)
    assert not r["wall_detected"]

def test_arb_edge_positive():
    e = arb_edge(simulated_prob=0.75, polymarket_price=0.50, taker_fee=0.018)
    assert e > 0

def test_arb_edge_negative():
    e = arb_edge(simulated_prob=0.50, polymarket_price=0.60, taker_fee=0.018)
    assert e < 0

def test_arb_edge_exact():
    e = arb_edge(0.70, 0.50, taker_fee=0.018)
    assert abs(e - (0.70 - 0.50 - 0.018)) < 1e-4

def test_arb_edge_zero_fee():
    e = arb_edge(0.60, 0.50, taker_fee=0.0)
    assert abs(e - 0.10) < 1e-4

def _arb(detected=True, direction="up", gap=0.2):
    return {"detected": detected, "direction": direction, "gap_pct": gap, "label": "ARB_DETECTED"}

def _wall(detected=False):
    return {
        "wall_detected": detected,
        "wall_price": 100.2 if detected else None,
        "wall_size": 500.0 if detected else 0.0,
        "baseline_avg": 10.0,
        "contrarian_signal": "down" if detected else None,
    }

def test_alpha_signal_enter():
    r = alpha_signal(_arb(), edge=0.08, wall=_wall(), urgency="NORMAL")
    assert r["action"] == "ENTER"
    assert r["direction"] == "up"

def test_alpha_signal_edge_too_small():
    r = alpha_signal(_arb(), edge=0.01, wall=_wall(), urgency="NORMAL")
    assert r["action"] == "WAIT"
    assert r["label"] == "EDGE_TOO_SMALL"

def test_alpha_signal_sell_wall_blocks():
    r = alpha_signal(_arb(direction="up"), edge=0.10, wall=_wall(detected=True), urgency="HIGH_ALERT")
    assert r["action"] == "SKIP"
    assert r["label"] == "SELL_WALL_BLOCK"

def test_alpha_signal_final_snipe_label():
    r = alpha_signal(_arb(), edge=0.08, wall=_wall(), urgency="FINAL_SECOND_SNIPE")
    assert r["label"] == "FINAL_SECOND_SNIPE"

def test_alpha_signal_high_alert_label():
    r = alpha_signal(_arb(), edge=0.08, wall=_wall(), urgency="HIGH_ALERT")
    assert r["label"] == "HIGH_ALERT"

def test_alpha_signal_confidence_boosted_by_arb():
    r_arb    = alpha_signal(_arb(detected=True),  edge=0.05, wall=_wall(), urgency="NORMAL")
    r_no_arb = alpha_signal(_arb(detected=False), edge=0.05, wall=_wall(), urgency="NORMAL")
    assert r_arb["confidence"] >= r_no_arb["confidence"]

def test_alpha_signal_wall_on_down_direction_does_not_block():
    r = alpha_signal(_arb(direction="down"), edge=0.08, wall=_wall(detected=True), urgency="NORMAL")
    assert r["action"] != "SKIP"

def test_silent_execution_restores_on_exception():
    root = logging.getLogger()
    original = root.level
    try:
        with silent_execution():
            raise ValueError("test")
    except ValueError:
        pass
    assert root.level == original

def test_tick_buffer_empty():
    buf = BinanceTickBuffer()
    assert buf.latest_price() is None
    assert buf.get_move_pct() == 0.0
    assert buf.get_elapsed_since_move() is None

def test_tick_buffer_latest_price():
    buf = BinanceTickBuffer()
    buf.on_tick(100.0, 1000)
    buf.on_tick(101.0, 2000)
    assert buf.latest_price() == 101.0

def test_tick_buffer_move_pct():
    buf = BinanceTickBuffer()
    buf.on_tick(100.0, 0)
    buf.on_tick(101.0, 2000)
    pct = buf.get_move_pct(seconds=3.0)
    assert abs(pct - 1.0) < 1e-6

def test_tick_buffer_maxlen():
    buf = BinanceTickBuffer(maxlen=3)
    for i in range(10):
        buf.on_tick(float(i), i * 100)
    assert len(buf) == 3

def test_tick_buffer_elapsed_since_move():
    buf = BinanceTickBuffer()
    buf.on_tick(100.0, 0)
    buf.on_tick(100.2, 1000)
    buf.on_tick(100.2, 5000)
    elapsed = buf.get_elapsed_since_move(threshold_pct=0.1)
    assert elapsed is not None
    assert elapsed >= 0.0

def test_tick_buffer_stale_when_never_ticked():
    buf = BinanceTickBuffer()
    assert buf.is_stale(timeout_s=0.0)

def test_tick_buffer_not_stale_immediately_after_tick():
    buf = BinanceTickBuffer()
    buf.on_tick(100.0, int(time.time() * 1000))
    assert not buf.is_stale(timeout_s=2.0)

def test_tick_buffer_stale_after_timeout(monkeypatch):
    buf = BinanceTickBuffer()
    buf.on_tick(100.0, 1000)
    fake_time = buf._last_wall_ts + 3.0
    monkeypatch.setattr("src.api.binance_ws.time.monotonic", lambda: fake_time)
    assert buf.is_stale(timeout_s=2.0)

def test_tick_buffer_heartbeat_status_ok():
    buf = BinanceTickBuffer()
    buf.on_tick(100.0, int(time.time() * 1000))
    h = buf.heartbeat_status()
    assert h["ok"]
    assert not h["is_stale"]
    assert h["warning"] is None

def test_tick_buffer_heartbeat_status_stale(monkeypatch):
    buf = BinanceTickBuffer()
    buf.on_tick(100.0, int(time.time() * 1000))
    fake_time = buf._last_wall_ts + 5.0
    monkeypatch.setattr("src.api.binance_ws.time.monotonic", lambda: fake_time)
    h = buf.heartbeat_status()
    assert not h["ok"]
    assert h["is_stale"]
    assert "STALE" in h["warning"]

def test_gbm_mc_audit_keys():
    r = gbm_mc_with_audit(100.0, 100.0, 0.8, 60, seed=42)
    for k in ("prob", "seed_used", "n_paths", "current", "strike", "time_remaining_s"):
        assert k in r

def test_gbm_mc_audit_prob_matches_direct():
    seed = 12345
    direct = gbm_mc_prob_above(100.0, 100.0, 0.8, 60, seed=seed)
    via_audit = gbm_mc_with_audit(100.0, 100.0, 0.8, 60, seed=seed)
    assert abs(via_audit["prob"] - direct) < 1e-10

def test_sync_strike_price_ok():
    r = sync_strike_price(100.0, 100.1, tolerance=0.005)
    assert r["ok"]
    assert r["label"] == "SYNCED"

def test_sync_strike_price_mismatch():
    r = sync_strike_price(100.0, 101.5, tolerance=0.005)
    assert not r["ok"]
    assert r["label"] == "PRICE_MISMATCH"

def test_sync_strike_price_exact_tolerance_boundary():
    r = sync_strike_price(100.0, 100.5, tolerance=0.005)
    assert r["ok"]

def test_sync_strike_price_just_over_tolerance():
    r = sync_strike_price(100.0, 100.51, tolerance=0.005)
    assert not r["ok"]

def test_sync_strike_price_zero_ref():
    r = sync_strike_price(100.0, 0.0, tolerance=0.005)
    assert not r["ok"]
    assert r["label"] == "PRICE_MISMATCH"

def _arb_dict(detected=True, direction="up"):
    return {"detected": detected, "direction": direction, "gap_pct": 0.2, "label": "ARB_DETECTED"}

def _wall_dict(detected=False):
    return {"wall_detected": detected, "wall_price": None, "wall_size": 0.0,
            "baseline_avg": 0.0, "contrarian_signal": None}

def test_alpha_signal_audit_trail_in_final_snipe():
    audit = {"mc_seed": 999, "strike_synced": 50000.0}
    r = alpha_signal(_arb_dict(), edge=0.08, wall=_wall_dict(),
                     urgency="FINAL_SECOND_SNIPE", audit=audit)
    assert "audit_trail" in r
    assert r["audit_trail"]["mc_seed"] == 999
    assert r["audit_trail"]["strike_synced"] == 50000.0

def _reset_rate_state():
    _bc._rate_weight_1m = 0
    _bc._rate_limit_status = "OK"

def test_rate_limit_status_initial():
    _reset_rate_state()
    r = _bc.get_rate_limit_status()
    assert r["status"] == "OK"
    assert r["weight_used"] == 0

def test_rate_limit_update_throttle():
    _reset_rate_state()
    _bc._update_rate_weight({"X-MBX-USED-WEIGHT-1M": "1020"})
    r = _bc.get_rate_limit_status()
    assert r["status"] == "THROTTLE"

def test_rate_limit_update_full_pause():
    _reset_rate_state()
    _bc._update_rate_weight({"X-MBX-USED-WEIGHT-1M": "1140"})
    r = _bc.get_rate_limit_status()
    assert r["status"] == "FULL_PAUSE"

def test_rate_limit_lowercase_header():
    _reset_rate_state()
    _bc._update_rate_weight({"x-mbx-used-weight-1m": "800"})
    assert _bc._rate_weight_1m == 800

def test_rate_limit_fraction_computed():
    _reset_rate_state()
    _bc._update_rate_weight({"X-MBX-USED-WEIGHT-1M": "600"})
    r = _bc.get_rate_limit_status()
    assert abs(r["fraction"] - 0.5) < 1e-4
