"""
src/logic/strategy.py
=====================
Adaptive signal filters untuk hourly scalping strategy.

Dua fungsi publik:
  get_dynamic_threshold() — mispricing gap threshold berbasis realized vol
  should_force_exit()     — force sell saat market mendekati expiry
"""

import math
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# KONSTANTA
# ─────────────────────────────────────────────

MIN_THRESHOLD: float       = 0.06    # 6%  — floor: di bawah ini terlalu banyak noise
MAX_THRESHOLD: float       = 0.25    # 25% — ceiling: di atas ini signal sangat langka
DEFAULT_VOL_ANNUAL: float  = 0.40    # fallback annualized vol jika data tidak tersedia
FORCE_EXIT_MINUTES: float  = 10.0    # menit sebelum expiry → force sell


# ─────────────────────────────────────────────
# DYNAMIC THRESHOLD
# ─────────────────────────────────────────────

def get_dynamic_threshold(
    asset: str,
    vol_data: dict,
    multiplier: float = 1.5,
) -> float:
    """
    Hitung mispricing gap threshold berbasis realized vol annualized dari Binance.

    Formula (kalibrasi empiris):
        vol_scaled = annualized_vol / sqrt(24)
        threshold  = vol_scaled * multiplier
        clamp      = [MIN_THRESHOLD, MAX_THRESHOLD]

    Faktor sqrt(24) di sini bukan konversi unit waktu apapun — ini scaling
    empiris yang menghasilkan threshold ~12% saat vol=40% (regime normal).
    Tujuannya: makin volatile pasar, makin lebar noise → butuh gap lebih
    besar untuk dianggap edge nyata. Multiplier 1.5 = lebih selektif.

    Kalibrasi (multiplier=1.5):
        vol=40%  annualized → threshold ≈ 12.2%
        vol=70%  annualized → threshold ≈ 21.4%
        vol=20%  annualized → threshold ≈  6.1%  (floored ke MIN)
        vol=100% annualized → threshold = 30.6%  (capped ke MAX)

    Args:
        asset      : "BTC", "ETH", "SOL", "BNB" — kunci di vol_data
        vol_data   : Dict {asset: annualized_vol_float, "DEFAULT": float}
        multiplier : Scaling factor (default 1.5 = agresif, 1.0 = konservatif)

    Returns:
        Threshold fraction (0.06 – 0.25), e.g. 0.12 = gap harus ≥ 12%
    """
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


# ─────────────────────────────────────────────
# FORCE EXIT
# ─────────────────────────────────────────────

def should_force_exit(
    expiry_time: datetime,
    buffer_minutes: float = FORCE_EXIT_MINUTES,
) -> bool:
    """
    Apakah posisi harus di-force sell karena mendekati expiry?

    Di menit-menit terakhir sebelum settlement, binary token Polymarket
    bergerak tajam ke 0 atau 1 dan bid/ask spread melebar drastis.
    Force exit sebelum window ini melindungi dari:
      - Settlement slippage (susah fill di harga wajar)
      - Liquidity kering (order tidak terisi)
      - Locked position sampai resolve (modal tidak bisa dideploy ulang)

    Args:
        expiry_time    : Datetime resolve market (timezone-aware UTC)
        buffer_minutes : Menit sebelum expiry untuk force exit (default 10)

    Returns:
        True  → jual sekarang di bid terbaik
        False → tahan posisi

    Contoh:
        Expiry 14:30 UTC, sekarang 14:22 →  8 menit → True  (force exit)
        Expiry 14:30 UTC, sekarang 14:19 → 11 menit → False (hold)
        Expiry 14:30 UTC, sekarang 14:35 →  sudah lewat → True
    """
    if expiry_time.tzinfo is None:
        expiry_time = expiry_time.replace(tzinfo=timezone.utc)

    now               = datetime.now(timezone.utc)
    minutes_remaining = (expiry_time - now).total_seconds() / 60.0

    if minutes_remaining <= 0:
        return True  # lewat expiry — seharusnya sudah di-resolve checker, jaga-jaga

    should_exit = minutes_remaining < buffer_minutes
    if should_exit:
        logger.info(
            f"[FORCE EXIT] {minutes_remaining:.1f} menit tersisa "
            f"< buffer {buffer_minutes:.0f} menit → trigger force sell"
        )
    return should_exit
