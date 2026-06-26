"""VolatilityClassifier — warmup, regime thresholds, per-symbol isolation, config.

vol = pstdev of simple returns over the rolling window. Crafted series give a
deterministic vol so the low/mid/high boundaries can be pinned.
"""
from src.classify.volatility import VolatilityClassifier


def _feed(clf, symbol, prices):
    for p in prices:
        clf.update(symbol, float(p))


def test_warmup_returns_unknown_below_min_samples():
    clf = VolatilityClassifier(min_samples=10)
    _feed(clf, "BTC", [100.0] * 5)          # only 5 prices < 10 required
    assert clf.volatility("BTC") is None
    assert clf.get_regime("BTC") == "unknown"


def test_flat_series_is_low_vol():
    clf = VolatilityClassifier(min_samples=10)
    _feed(clf, "BTC", [100.0] * 20)         # zero returns -> vol 0 <= low_vol_max
    assert clf.get_regime("BTC") == "low_vol"


def test_big_swings_are_high_vol():
    clf = VolatilityClassifier(min_samples=3, low_vol_max=0.0001, high_vol_min=0.05)
    _feed(clf, "BTC", [100, 120, 100, 120, 100])   # ~±18% returns -> pstdev ~0.18
    assert clf.get_regime("BTC") == "high_vol"


def test_per_symbol_isolation():
    clf = VolatilityClassifier(min_samples=3, low_vol_max=0.0001, high_vol_min=0.05)
    _feed(clf, "BTC", [100, 120, 100, 120, 100])
    _feed(clf, "ETH", [100, 100, 100, 100, 100])
    assert clf.get_regime("BTC") == "high_vol"
    assert clf.get_regime("ETH") == "low_vol"


def test_snapshot_vols_only_includes_ready_symbols():
    clf = VolatilityClassifier(min_samples=5)
    _feed(clf, "BTC", [100, 101, 102, 103, 104, 105])   # >=5 -> ready
    _feed(clf, "ETH", [100, 101])                        # <5 -> not ready
    vols = clf.snapshot_vols()
    assert "BTC" in vols
    assert "ETH" not in vols


def test_thresholds_are_configurable():
    prices = [100, 101, 100, 101, 100, 101]   # ~1% alternating returns -> pstdev ~0.0099
    loose = VolatilityClassifier(min_samples=3, low_vol_max=0.0001, high_vol_min=0.5)
    strict = VolatilityClassifier(min_samples=3, low_vol_max=0.0001, high_vol_min=0.005)
    _feed(loose, "BTC", prices)
    _feed(strict, "BTC", prices)
    assert loose.get_regime("BTC") == "mid_vol"     # 0.0099 < 0.5 and > 0.0001
    assert strict.get_regime("BTC") == "high_vol"   # 0.0099 >= 0.005
