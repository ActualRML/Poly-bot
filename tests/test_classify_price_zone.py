"""price_zone classifier — zones, boundary semantics, input validation, overrides.

The boundary is left-closed/right-open via strict `<`: a price sitting exactly on a
threshold belongs to the UPPER zone (0.20 -> low, not extreme_low). None / NaN /
out-of-[0,1] map to "unknown" rather than silently bucketing.
"""
import pytest

from src.classify.price_zone import (
    DEFAULT_ZONES,
    ZONE_EXTREME_LOW,
    ZONE_HIGH,
    ZONE_LOW,
    ZONE_UNCERTAIN,
    ZoneThresholds,
    classify_price_zone,
)


@pytest.mark.parametrize(
    "price, zone",
    [
        (0.0, "extreme_low"),
        (0.10, "extreme_low"),
        (0.30, "low"),
        (0.50, "uncertain"),
        (0.70, "high"),
        (0.90, "extreme_high"),
        (1.0, "extreme_high"),
    ],
)
def test_each_zone(price, zone):
    assert classify_price_zone(price) == zone


@pytest.mark.parametrize(
    "price, zone",
    [
        (0.20, "low"),            # exactly on a boundary -> upper zone
        (0.40, "uncertain"),
        (0.60, "high"),
        (0.80, "extreme_high"),
    ],
)
def test_boundary_belongs_to_upper_zone(price, zone):
    assert classify_price_zone(price) == zone


@pytest.mark.parametrize("bad", [None, float("nan"), -0.01, 1.01, 1.5, -5.0])
def test_invalid_inputs_are_unknown(bad):
    assert classify_price_zone(bad) == "unknown"


def test_custom_thresholds_override_the_bands():
    wide = ZoneThresholds(0.10, 0.30, 0.70, 0.90)
    # 0.15: extreme_low under defaults, but 'low' once the extreme band tightens
    assert classify_price_zone(0.15) == "extreme_low"
    assert classify_price_zone(0.15, wide) == "low"
    # 0.85: extreme_high under defaults, but 'high' under the wider extreme band
    assert classify_price_zone(0.85) == "extreme_high"
    assert classify_price_zone(0.85, wide) == "high"


def test_default_zones_match_module_constants():
    assert DEFAULT_ZONES == ZoneThresholds(
        ZONE_EXTREME_LOW, ZONE_LOW, ZONE_UNCERTAIN, ZONE_HIGH
    )
