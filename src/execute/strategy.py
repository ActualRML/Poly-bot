
import math
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

MIN_THRESHOLD: float       = 0.06
MAX_THRESHOLD: float       = 0.25
DEFAULT_VOL_ANNUAL: float  = 0.40
FORCE_EXIT_MINUTES: float  = 10.0

def get_dynamic_threshold(
    asset: str,
    vol_data: dict,
    multiplier: float = 1.5,
) -> float:

    vol_annual = float(
        vol_data.get(asset.upper())
        or vol_data.get("DEFAULT")
        or DEFAULT_VOL_ANNUAL
    )

    vol_scaled = vol_annual / math.sqrt(24)
    threshold  = vol_scaled * multiplier
    result     = max(MIN_THRESHOLD, min(MAX_THRESHOLD, threshold))

    logger.debug(
        f"[THRESHOLD] {asset} vol={vol_annual:.0%} "
        f"→ scaled={vol_scaled:.1%} × {multiplier} "
        f"= {threshold:.1%} → clamped {result:.1%}"
    )
    return result

def should_force_exit(
    expiry_time: datetime,
    buffer_minutes: float = FORCE_EXIT_MINUTES,
) -> bool:

    if expiry_time.tzinfo is None:
        expiry_time = expiry_time.replace(tzinfo=timezone.utc)

    now               = datetime.now(timezone.utc)
    minutes_remaining = (expiry_time - now).total_seconds() / 60.0

    if minutes_remaining <= 0:
        return True

    should_exit = minutes_remaining < buffer_minutes
    if should_exit:
        logger.info(
            f"[FORCE EXIT] {minutes_remaining:.1f} menit tersisa "
            f"< buffer {buffer_minutes:.0f} menit → trigger force sell"
        )
    return should_exit
