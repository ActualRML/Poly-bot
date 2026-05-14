from datetime import datetime, timezone

from src.scout.regime import (
    classify_correlation,
    classify_htf_trend,
    composite_regime,
    session_bias,
)


def test_majority_up_trending():
    moves = [("BTC", 0.01), ("ETH", 0.008), ("SOL", 0.012), ("XRP", 0.005), ("DOGE", -0.002), ("BNB", 0.007)]
    r = classify_correlation(moves)
    assert r["trending"] is True
    assert r["direction"] == "up"


def test_majority_down_trending():
    moves = [("BTC", -0.01), ("ETH", -0.008), ("SOL", -0.012), ("XRP", -0.005), ("DOGE", -0.006), ("BNB", 0.001)]
    r = classify_correlation(moves)
    assert r["trending"] is True
    assert r["direction"] == "down"


def test_mixed_not_trending():
    moves = [("BTC", 0.01), ("ETH", -0.01), ("SOL", 0.005), ("XRP", -0.005), ("DOGE", 0.001), ("BNB", -0.003)]
    r = classify_correlation(moves)
    assert r["trending"] is False


def test_insufficient_data():
    r = classify_correlation([("BTC", 0.01), ("ETH", 0.01)])
    assert r["trending"] is False
    assert r["reason"] == "insufficient_data"


def test_aligned_up_trend():
    closes_1h = [100.0, 101.0, 102.0, 103.0, 105.0]
    closes_4h = [100.0, 102.0, 104.0, 107.0]
    r = classify_htf_trend(closes_1h, closes_4h)
    assert r["aligned"] is True
    assert r["tf_1h"] == "up"


def test_aligned_down_trend():
    closes_1h = [105.0, 104.0, 103.0, 102.0, 100.0]
    closes_4h = [107.0, 105.0, 103.0, 100.0]
    r = classify_htf_trend(closes_1h, closes_4h)
    assert r["aligned"] is True
    assert r["tf_1h"] == "down"


def test_conflicting_timeframes_not_aligned():
    closes_1h = [105.0, 104.0, 103.0, 102.0, 100.0]  # down
    closes_4h = [100.0, 102.0, 104.0, 107.0]           # up
    r = classify_htf_trend(closes_1h, closes_4h)
    assert r["aligned"] is False


def test_insufficient_htf_data():
    r = classify_htf_trend([100.0, 101.0], [])
    assert r["aligned"] is False


def test_us_open_trending():
    dt = datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc)
    r = session_bias(dt)
    assert r["session"] == "US_OPEN"
    assert r["trending_likely"] is True


def test_asia_not_trending():
    dt = datetime(2026, 5, 14, 2, 0, tzinfo=timezone.utc)
    r = session_bias(dt)
    assert r["session"] == "ASIA"
    assert r["trending_likely"] is False


def test_us_main_not_trending():
    dt = datetime(2026, 5, 14, 18, 0, tzinfo=timezone.utc)
    r = session_bias(dt)
    assert r["session"] == "US_MAIN"
    assert r["trending_likely"] is False


def test_high_score_skip_contrarian():
    cross = {"trending": True, "direction": "up"}
    htf = {"aligned": True, "tf_1h": "up"}
    sess = {"trending_likely": True}
    r = composite_regime(cross, htf, sess)
    assert r["skip_contrarian"] is True
    assert r["trend_score"] >= 4


def test_low_score_no_skip():
    cross = {"trending": False, "direction": None}
    htf = {"aligned": False, "tf_1h": "flat"}
    sess = {"trending_likely": False}
    r = composite_regime(cross, htf, sess)
    assert r["skip_contrarian"] is False


def test_conflicting_htf_reduces_score():
    cross = {"trending": True, "direction": "up"}
    htf = {"aligned": True, "tf_1h": "down"}  # conflicts with cross → -1
    sess = {"trending_likely": False}
    r = composite_regime(cross, htf, sess)
    assert r["trend_score"] == 1  # +2 cross, -1 conflict = 1
    assert r["skip_contrarian"] is False
