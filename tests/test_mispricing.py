import pytest
from hypothesis import given, strategies as st

from src.logic.mispricing import MispricingDetector, BaseRate, MispricingDirection

detector = MispricingDetector(threshold=0.15)


def make_rate(rate: float, confidence: float = 0.8) -> BaseRate:
    return BaseRate(source="test", rate=rate, confidence=confidence)


@given(
    rates=st.lists(
        st.tuples(
            st.floats(0.0, 1.0, allow_nan=False, allow_infinity=False),
            st.floats(0.001, 1.0, allow_nan=False, allow_infinity=False),
        ),
        min_size=1,
        max_size=5,
    )
)
def test_blend_base_rates_in_bounds(rates):
    base_rates = [make_rate(r, c) for r, c in rates]
    blended, confidence = detector._blend_base_rates(base_rates)
    all_rates = [r.rate for r in base_rates]
    assert min(all_rates) - 5e-4 <= blended <= max(all_rates) + 5e-4
    assert 0.0 <= confidence <= 1.0

@given(
    market_price=st.floats(0.01, 0.99, allow_nan=False, allow_infinity=False),
    base_rate=st.floats(0.01, 0.99, allow_nan=False, allow_infinity=False),
    threshold=st.floats(0.01, 0.50, allow_nan=False, allow_infinity=False),
)
def test_analyze_direction_consistent_with_gap(market_price, base_rate, threshold):
    result = detector.analyze("0x1", "Test?", "Yes", market_price, [make_rate(base_rate)], threshold=threshold)
    if result.gap > threshold:
        assert result.direction == MispricingDirection.UNDERPRICED
        assert result.is_mispriced
    elif result.gap < -threshold:
        assert result.direction == MispricingDirection.OVERPRICED
        assert result.is_mispriced
    else:
        assert result.direction == MispricingDirection.FAIR
        assert not result.is_mispriced

@given(
    market_price=st.floats(0.01, 0.99, allow_nan=False, allow_infinity=False),
    base_rate=st.floats(0.01, 0.99, allow_nan=False, allow_infinity=False),
)
def test_analyze_gap_pct_always_non_negative(market_price, base_rate):
    result = detector.analyze("0x1", "Test?", "Yes", market_price, [make_rate(base_rate)])
    assert result.gap_pct >= 0.0

@given(
    rates=st.lists(
        st.floats(0.01, 0.99, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=5,
    )
)
def test_invert_base_rates_sums_to_one(rates):
    base_rates = [make_rate(r) for r in rates]
    inverted = detector._invert_base_rates(base_rates)
    for orig, inv in zip(base_rates, inverted):
        assert abs(orig.rate + inv.rate - 1.0) < 5e-4
        assert orig.confidence == inv.confidence

def test_analyze_raises_on_empty_rates():
    with pytest.raises(ValueError):
        detector.analyze("0x1", "Test?", "Yes", 0.50, [])

def test_underpriced_detected():
    result = detector.analyze("0x1", "Q?", "Yes", 0.40, [make_rate(0.70)], threshold=0.15)
    assert result.is_mispriced
    assert result.direction == MispricingDirection.UNDERPRICED

def test_overpriced_detected():
    result = detector.analyze("0x1", "Q?", "Yes", 0.80, [make_rate(0.40)], threshold=0.15)
    assert result.is_mispriced
    assert result.direction == MispricingDirection.OVERPRICED

def test_fair_when_within_threshold():
    result = detector.analyze("0x1", "Q?", "Yes", 0.50, [make_rate(0.55)], threshold=0.15)
    assert not result.is_mispriced
    assert result.direction == MispricingDirection.FAIR
