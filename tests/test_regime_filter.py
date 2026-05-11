from datetime import datetime, timezone

import pytest

from src.logic.regime_filter import (
    classify_correlation,
    classify_htf_trend,
    composite_regime,
    session_bias,
)

def test_correlation_majority_up():
    moves = [("BTC", 0.005), ("ETH", 0.006), ("SOL", 0.008),
             ("XRP", 0.004), ("DOGE", 0.003), ("BNB", -0.001)]
    r = classify_correlation(moves, move_threshold_pct=0.002, align_threshold=0.7)
    assert r["trending"]
    assert r["direction"] == "up"
    assert r["aligned_count"] == 5
    assert r["total_count"] == 6

def test_correlation_majority_down():
    moves = [("BTC", -0.005), ("ETH", -0.006), ("SOL", -0.008),
             ("XRP", -0.004), ("DOGE", 0.001), ("BNB", -0.003)]
    r = classify_correlation(moves, move_threshold_pct=0.002, align_threshold=0.7)
    assert r["trending"]
    assert r["direction"] == "down"

def test_correlation_mixed():
    moves = [("BTC", 0.005), ("ETH", -0.005), ("SOL", 0.001),
             ("XRP", -0.001), ("DOGE", 0.003), ("BNB", -0.003)]
    r = classify_correlation(moves, move_threshold_pct=0.002, align_threshold=0.7)
    assert not r["trending"]
    assert r["direction"] is None
    assert r["reason"] == "mixed"

def test_correlation_below_min_assets():
    moves = [("BTC", 0.005), ("ETH", 0.006)]  # only 2
    r = classify_correlation(moves, min_assets=3)
    assert not r["trending"]
    assert r["reason"] == "insufficient_data"

def test_correlation_threshold_filters_noise():
    moves = [("BTC", 0.001), ("ETH", 0.0015), ("SOL", 0.001),
             ("XRP", 0.001), ("DOGE", 0.001), ("BNB", 0.001)]
    r = classify_correlation(moves, move_threshold_pct=0.003, align_threshold=0.7)
    assert not r["trending"]

def test_correlation_avg_move_pct_computed():
    moves = [("BTC", 0.01), ("ETH", 0.02), ("SOL", 0.03)]
    r = classify_correlation(moves, move_threshold_pct=0.001, align_threshold=0.5)
    assert abs(r["avg_move_pct"] - 0.02) < 1e-5

def test_correlation_align_threshold_strict():
    moves = [("BTC", 0.005), ("ETH", 0.005), ("SOL", 0.005), ("XRP", 0.005),
             ("DOGE", -0.005), ("BNB", -0.005)]
    r = classify_correlation(moves, move_threshold_pct=0.002, align_threshold=0.7)
    assert not r["trending"]

def test_htf_aligned_up():
    closes_1h = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 110.0]
    closes_4h = [100.0, 102.0, 104.0, 110.0]
    r = classify_htf_trend(closes_1h, closes_4h, flat_threshold_pct=0.005)
    assert r["aligned"]
    assert r["tf_1h"] == "up"
    assert r["tf_4h"] == "up"
    assert r["label"] == "trending_up"

def test_htf_aligned_down():
    closes_1h = [110.0, 108.0, 106.0, 104.0, 102.0, 100.0, 98.0, 95.0]
    closes_4h = [110.0, 105.0, 100.0, 95.0]
    r = classify_htf_trend(closes_1h, closes_4h)
    assert r["aligned"]
    assert r["label"] == "trending_down"

def test_htf_mixed():
    closes_1h = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 110.0]  # up
    closes_4h = [110.0, 105.0, 100.0, 95.0]  # down
    r = classify_htf_trend(closes_1h, closes_4h)
    assert not r["aligned"]
    assert r["label"] == "mixed"

def test_htf_ranging():
    flat_1h = [100.0] * 8
    flat_4h = [100.0] * 4
    r = classify_htf_trend(flat_1h, flat_4h)
    assert not r["aligned"]
    assert r["label"] == "ranging"

def test_htf_insufficient_data():
    r = classify_htf_trend([100.0], [100.0])
    assert r["tf_1h"] is None
    assert r["label"] == "no_data"

def test_session_us_open():
    t = datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc)  # 14:00 UTC
    r = session_bias(t)
    assert r["session"] == "US_OPEN"
    assert r["trending_likely"]

def test_session_us_main():
    t = datetime(2026, 5, 8, 19, 0, tzinfo=timezone.utc)  # 19:00 UTC = today's losses
    r = session_bias(t)
    assert r["session"] == "US_MAIN"
    assert not r["trending_likely"]

def test_session_asia():
    t = datetime(2026, 5, 8, 3, 0, tzinfo=timezone.utc)  # 03:00 UTC
    r = session_bias(t)
    assert r["session"] == "ASIA"
    assert not r["trending_likely"]

def test_session_eu():
    t = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    r = session_bias(t)
    assert r["session"] == "EU"

def test_session_us_open_lower_boundary():
    t = datetime(2026, 5, 8, 13, 30, tzinfo=timezone.utc)
    r = session_bias(t)
    assert r["session"] == "US_OPEN"

def test_session_us_open_upper_boundary():
    t = datetime(2026, 5, 8, 15, 29, tzinfo=timezone.utc)
    r = session_bias(t)
    assert r["session"] == "US_OPEN"

def test_session_late_night_asia():
    t = datetime(2026, 5, 8, 23, 30, tzinfo=timezone.utc)
    r = session_bias(t)
    assert r["session"] == "ASIA"

def _cross(trending=False, direction=None):
    return {"trending": trending, "direction": direction,
            "aligned_count": 5, "total_count": 6, "avg_move_pct": 0.005,
            "moves": [], "reason": "majority_up" if trending else "mixed"}

def _htf(aligned=False, tf="up"):
    return {"tf_1h": tf, "tf_4h": tf, "aligned": aligned,
            "label": f"trending_{tf}" if aligned else "mixed"}

def _sess(trending=False):
    return {"session": "US_OPEN" if trending else "US_MAIN",
            "bias_strength": "high" if trending else "medium",
            "trending_likely": trending}

def test_composite_strong_trending_up():
    cross = _cross(trending=True, direction="up")
    htf   = _htf(aligned=True, tf="up")
    sess  = _sess(trending=True)
    r = composite_regime(cross, htf, sess)
    assert r["trend_score"] == 4  # 2 + 1 + 1
    assert r["skip_contrarian"]
    assert r["regime"] == "TRENDING_UP"
    assert r["direction"] == "up"

def test_composite_cross_alone_borderline():
    r = composite_regime(_cross(trending=True, direction="up"),
                         _htf(aligned=False), _sess(trending=False))
    assert r["trend_score"] == 2
    assert not r["skip_contrarian"]
    assert r["regime"] == "MIXED"

def test_composite_cross_plus_htf_no_skip_at_threshold_4():
    r = composite_regime(_cross(trending=True, direction="up"),
                         _htf(aligned=True, tf="up"), _sess(trending=False))
    assert r["trend_score"] == 3
    assert not r["skip_contrarian"]
    assert r["regime"] == "MIXED"

def test_composite_cross_plus_htf_plus_session_skip():
    r = composite_regime(_cross(trending=True, direction="up"),
                         _htf(aligned=True, tf="up"), _sess(trending=True))
    assert r["trend_score"] == 4
    assert r["skip_contrarian"]
    assert r["regime"] == "TRENDING_UP"

def test_composite_htf_conflicts_cross():
    r = composite_regime(_cross(trending=True, direction="up"),
                         _htf(aligned=True, tf="down"), _sess(trending=False))
    assert r["trend_score"] == 1
    assert not r["skip_contrarian"]

def test_composite_ranging():
    r = composite_regime(_cross(trending=False), _htf(aligned=False), _sess(trending=False))
    assert r["trend_score"] == 0
    assert r["regime"] == "RANGING"
    assert not r["skip_contrarian"]

def test_composite_us_open_alone_not_enough():
    r = composite_regime(_cross(trending=False), _htf(aligned=False), _sess(trending=True))
    assert r["trend_score"] == 1
    assert not r["skip_contrarian"]

def test_composite_today_scenario():
    """
    2PM ET (19:00 UTC = US_MAIN, no session bias) + cross trending +
    HTF aligned. Score=3 → MIXED (threshold=4 sekarang lebih longgar,
    skip cuma saat full-alignment cross+HTF+US_OPEN).
    """
    cross = _cross(trending=True, direction="up")
    htf   = _htf(aligned=True, tf="up")
    sess  = _sess(trending=False)  # US_MAIN, no high session bias
    r = composite_regime(cross, htf, sess)
    assert r["trend_score"] == 3  # 2 (cross) + 1 (htf) + 0 (session)
    assert not r["skip_contrarian"]
    assert r["regime"] == "MIXED"

