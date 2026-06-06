# --- TUNE after observing (more intuitive than vol, but kept configurable) --
ZONE_EXTREME_LOW = 0.20  # price < this        -> extreme_low
ZONE_LOW = 0.40          # [extreme_low, this) -> low
ZONE_UNCERTAIN = 0.60    # [low, this)         -> uncertain
ZONE_HIGH = 0.80         # [uncertain, this)   -> high; >= this -> extreme_high
# ----------------------------------------------------------------------------

# Display order for the zone-distribution summary.
ZONE_ORDER = ("extreme_low", "low", "uncertain", "high", "extreme_high", "unknown")


def classify_price_zone(price: float | None) -> str:
    if price is None:
        return "unknown"
    if price < ZONE_EXTREME_LOW:
        return "extreme_low"
    if price < ZONE_LOW:
        return "low"
    if price < ZONE_UNCERTAIN:
        return "uncertain"
    if price < ZONE_HIGH:
        return "high"
    return "extreme_high"
