"""YES-price -> probability zone.

The default thresholds below are validated against `research/calibration.db`
(491 resolved markets): at T-30 they produce monotonic YES-win-rate buckets
(~10 / 31 / 53 / 82 / 98 %, Brier 0.131). See `research/calibrate_zones.py` for
the calibration analysis. Thresholds are tunable — the orchestrator builds a
`ZoneThresholds` from `config.py` and passes it in; the module-level constants
remain the defaults so existing callers/tests keep working unchanged.
"""
import math
from dataclasses import dataclass

# --- default thresholds (calibrated on 491 markets; TUNE via config.py) -----
ZONE_EXTREME_LOW = 0.20  # price < this        -> extreme_low
ZONE_LOW = 0.40          # [extreme_low, this) -> low
ZONE_UNCERTAIN = 0.60    # [low, this)         -> uncertain
ZONE_HIGH = 0.80         # [uncertain, this)   -> high; >= this -> extreme_high
# ----------------------------------------------------------------------------

# Display order for the zone-distribution summary.
ZONE_ORDER = ("extreme_low", "low", "uncertain", "high", "extreme_high", "unknown")


@dataclass(frozen=True)
class ZoneThresholds:
    """The four ascending price boundaries that split a YES price into 5 zones.
    Defaults are the calibrated values; override (e.g. from `config.py`) to retune.
    Frozen, so one instance is safely shareable across the whole process."""
    extreme_low: float = ZONE_EXTREME_LOW
    low: float = ZONE_LOW
    uncertain: float = ZONE_UNCERTAIN
    high: float = ZONE_HIGH


DEFAULT_ZONES = ZoneThresholds()


def classify_price_zone(price: float | None, zones: ZoneThresholds = DEFAULT_ZONES) -> str:
    # Only a real probability in [0,1] maps to a zone. None / NaN / out-of-range
    # -> "unknown" (defensive: never silently bucket a bad input as a real zone).
    if price is None or math.isnan(price) or price < 0.0 or price > 1.0:
        return "unknown"
    if price < zones.extreme_low:
        return "extreme_low"
    if price < zones.low:
        return "low"
    if price < zones.uncertain:
        return "uncertain"
    if price < zones.high:
        return "high"
    return "extreme_high"
