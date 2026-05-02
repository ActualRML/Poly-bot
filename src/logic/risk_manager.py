"""
src/logic/risk_manager.py
=========================
Adaptive risk management untuk hourly scalping strategy.

Dua fungsi publik:
  get_dynamic_stop_loss()   — trailing stop berbasis binary probability P
  calculate_position_size() — adaptive bet size berbasis trade streak
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# BATAS HARD
# ─────────────────────────────────────────────

MIN_STOP_FRACTION: float    = 0.05   # 5%  — tidak pernah lebih ketat dari ini
MAX_STOP_FRACTION: float    = 0.45   # 45% — tidak pernah lebih longgar dari ini

MIN_POSITION_USDC: float    = 10.0
MAX_POSITION_USDC: float    = 30.0
BASE_POSITION_USDC: float   = 20.0


# ─────────────────────────────────────────────
# DYNAMIC STOP LOSS
# ─────────────────────────────────────────────

def get_dynamic_stop_loss(
    current_P: float,
    vol_annual: Optional[float] = None,
) -> float:
    """
    Hitung trailing stop fraction berdasarkan harga token P sekarang.

    Formula dasar:
        stop_fraction = P * (1 - P) * 2.0

    Intuisi: P*(1-P) adalah variance Bernoulli. Saat P mendekati 0 atau 1
    (market hampir settled), variance kecil → stop ketat. Saat P=0.5
    (market sangat tidak pasti), variance maksimal → stop paling lebar.
    Faktor 2.0 = buffer 2-sigma dari distribusi biner.

    Scaling vol (opsional):
        vol tinggi (60%+ annual) → stop sedikit lebih lebar (+20% max)
        vol rendah (< 20% annual) → stop sedikit lebih ketat (-20% max)
        Referensi: vol_normal = 40% annualized → scale = 1.0

    Args:
        current_P  : Harga token saat ini (0–1), bukan winrate entry
        vol_annual : Realized vol annualized dari Binance (misal 0.40 = 40%)
                     None → tidak ada vol scaling

    Returns:
        Trailing stop fraction (0.05 – 0.45).
        Dipakai sebagai: stop_price = peak_price * (1 - stop_fraction)

    Contoh:
        P=0.90, vol=40% → base=0.18 → scale 1.0 → final 0.18 (ketat)
        P=0.70, vol=40% → base=0.42 → scale 1.0 → final 0.42
        P=0.70, vol=80% → base=0.42 → scale 1.2 → final 0.45 (capped)
        P=0.50, vol=30% → base=0.50 → scale 0.9 → final 0.45 (capped)
    """
    base_stop = current_P * (1.0 - current_P) * 2.0

    if vol_annual is not None:
        # Scale: normal vol (40%) = 1.0, double = 1.2, half = 0.8
        vol_scale = max(0.80, min(1.20, vol_annual / 0.40))
        base_stop *= vol_scale

    result = max(MIN_STOP_FRACTION, min(MAX_STOP_FRACTION, base_stop))

    logger.debug(
        f"[DYNAMIC STOP] P={current_P:.3f} "
        f"→ base={current_P*(1-current_P)*2:.3f} "
        f"→ final={result:.3f} ({result:.0%})"
    )
    return result


# ─────────────────────────────────────────────
# ADAPTIVE POSITION SIZE
# ─────────────────────────────────────────────

def calculate_position_size(
    last_5_trades: list[dict],
    base_size: float = BASE_POSITION_USDC,
) -> float:
    """
    Adaptive position size berdasarkan streak dari 5 trade terakhir.

    Rules (urutan prioritas):
      1. 2+ consecutive losses di akhir list → MIN ($10) — capital protection
      2. 3+ consecutive wins  di akhir list  → MAX ($30) — ride hot streak
      3. Default                             → base_size ($20)

    Args:
        last_5_trades : List[dict] dengan key "pnl" (float).
                        Ordered terlama [0] → terbaru [-1].
                        Ambil dari: get_recent_closed_pnls(limit=5) di database.py
        base_size     : Default bet size USDC (bisa di-override dari Kelly result)

    Returns:
        Batas atas posisi dalam USDC (10.0 – 30.0).
        Dipakai sebagai CAP terhadap Kelly: actual_bet = min(kelly_bet, max_size)

    Contoh:
        [win, win, win, win, win]   → $30 (5 berturut menang)
        [win, loss, loss, -, -]     → $10 (2 rugi berturut)
        [win, loss, win, loss, win] → $20 (mixed, default)
        []                          → $20 (no history)
    """
    if not last_5_trades:
        return base_size

    # Hitung streak dari ujung (trade paling baru = index [-1])
    consecutive_wins   = 0
    consecutive_losses = 0

    for trade in reversed(last_5_trades):
        pnl = float(trade.get("pnl", 0))
        if pnl > 0:
            if consecutive_losses > 0:
                break
            consecutive_wins += 1
        else:
            if consecutive_wins > 0:
                break
            consecutive_losses += 1

    if consecutive_losses >= 2:
        logger.info(
            f"[RISK] {consecutive_losses} consecutive losses "
            f"→ posisi turun ke ${MIN_POSITION_USDC:.0f}"
        )
        return MIN_POSITION_USDC

    if consecutive_wins >= 3:
        # +$5 per win di atas 2, capped di MAX
        bonus    = min((consecutive_wins - 2) * 5.0, MAX_POSITION_USDC - base_size)
        new_size = min(base_size + bonus, MAX_POSITION_USDC)
        logger.info(
            f"[RISK] {consecutive_wins} consecutive wins "
            f"→ posisi naik ke ${new_size:.0f}"
        )
        return new_size

    return base_size
