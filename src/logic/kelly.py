"""
src/logic/kelly.py
==================
Kelly Criterion — dynamic position sizing berdasarkan confidence level.

Selalu pakai Half Kelly untuk safety, hard cap 30% modal per trade.
Integrate dengan pricing.py untuk presisi Decimal yang konsisten.
"""

from decimal import Decimal
from dataclasses import dataclass
from typing import Optional

from src.logic.pricing import ke_decimal, hitung_midpoint, validasi_harga


# ─────────────────────────────────────────────
# KONSTANTA
# ─────────────────────────────────────────────

# Hard cap maksimal per trade — tidak peduli Kelly hasilnya berapa
MAX_KELLY_FRACTION: Decimal = Decimal("0.30")

# Default pakai Half Kelly
KELLY_MULTIPLIER: Decimal = Decimal("0.5")

# Minimum bet size yang masuk akal (dalam USDC)
MIN_BET_USDC: Decimal = Decimal("5.0")

# Minimum winrate yang bisa dipakai (di bawah ini → skip)
MIN_WINRATE: Decimal = Decimal("0.52")


# ─────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────

@dataclass
class KellyResult:
    """Output dari kalkulasi Kelly sizing."""
    # Input
    winrate: Decimal          # probabilitas menang (0-1)
    market_price: Decimal     # harga beli di market (0-1)
    capital: Decimal          # modal tersedia (USDC)

    # Intermediate
    odds: Decimal             # (1 - price) / price — berapa kali modal kembali
    raw_kelly: Decimal        # full Kelly fraction sebelum multiplier
    half_kelly: Decimal       # setelah × 0.5
    capped_kelly: Decimal     # setelah hard cap 30%

    # Output utama
    bet_fraction: Decimal     # fraction akhir yang dipakai
    bet_usdc: Decimal         # nominal USDC yang di-bet
    shares: Decimal           # jumlah shares yang dibeli (bet_usdc / price)

    # Meta
    is_positive_ev: bool      # apakah trade ini EV positif?
    expected_value: Decimal   # EV per unit modal
    edge: Decimal             # winrate - market_price (seberapa besar edge kita)
    reason: str               # penjelasan keputusan


# ─────────────────────────────────────────────
# CORE CALCULATOR
# ─────────────────────────────────────────────

class KellySizer:
    """
    Dynamic position sizing pakai Kelly Criterion.

    Formula Kelly standar untuk binary outcome:
        f* = (p * odds - q) / odds
        di mana:
            p    = winrate (probabilitas menang)
            q    = 1 - p (probabilitas kalah)
            odds = (1 - price) / price

    Selalu pakai Half Kelly (× 0.5) dan hard cap 30%.
    """

    def __init__(
        self,
        kelly_multiplier: float = 0.5,
        max_fraction: float = 0.30,
        min_bet_usdc: float = 5.0,
        min_winrate: float = 0.52,
    ):
        self.kelly_multiplier = ke_decimal(kelly_multiplier)
        self.max_fraction = ke_decimal(max_fraction)
        self.min_bet_usdc = ke_decimal(min_bet_usdc)
        self.min_winrate = ke_decimal(min_winrate)

    def calculate(
        self,
        winrate: float,
        market_price: float,
        capital: float,
    ) -> KellyResult:
        """
        Hitung bet size optimal untuk satu trade.

        Args:
            winrate     : Probabilitas menang berdasarkan analisis kita (0-1)
            market_price: Harga beli di Polymarket (0-1)
            capital     : Modal tersedia dalam USDC

        Returns:
            KellyResult dengan bet_usdc dan shares siap dipakai
        """
        p = ke_decimal(winrate)
        price = ke_decimal(market_price)
        cap = ke_decimal(capital)

        # Validasi input
        if not validasi_harga(price):
            return self._zero_result(p, price, cap, "Harga tidak valid (di luar 0.0001-0.9999)")

        if p < self.min_winrate:
            return self._zero_result(p, price, cap, f"Winrate {float(p):.1%} terlalu rendah (min {float(self.min_winrate):.1%})")

        if cap <= Decimal("0"):
            return self._zero_result(p, price, cap, "Modal 0 atau negatif")

        # Odds: berapa kali modal kembali kalau menang
        # Contoh: beli YES di harga 0.40 → kalau benar dapat 1/0.40 = 2.5x
        # Net odds (profit saja, tidak termasuk modal) = (1 - 0.40) / 0.40 = 1.5x
        odds = (Decimal("1") - price) / price

        q = Decimal("1") - p

        # Full Kelly
        raw_kelly = (p * odds - q) / odds

        # EV check — kalau negatif, jangan bet
        ev = p * odds - q
        is_positive_ev = ev > Decimal("0")

        if not is_positive_ev:
            return self._zero_result(
                p, price, cap,
                f"EV negatif: {float(ev):.3f} — skip trade",
                odds=odds, raw_kelly=raw_kelly, ev=ev
            )

        # Half Kelly
        half_kelly = raw_kelly * self.kelly_multiplier

        # Hard cap
        capped = min(half_kelly, self.max_fraction)
        capped = max(capped, Decimal("0"))  # jangan negatif

        # Nominal bet
        bet_usdc = (cap * capped).quantize(Decimal("0.01"))

        # Minimum bet check
        if bet_usdc < self.min_bet_usdc:
            return self._zero_result(
                p, price, cap,
                f"Bet size ${float(bet_usdc):.2f} terlalu kecil (min ${float(self.min_bet_usdc):.2f})",
                odds=odds, raw_kelly=raw_kelly, ev=ev
            )

        # Shares
        shares = (bet_usdc / price).quantize(Decimal("0.0001"))

        # Edge = seberapa jauh winrate kita di atas harga pasar
        edge = p - price

        reason = (
            f"Kelly {float(raw_kelly):.1%} → Half Kelly {float(half_kelly):.1%} "
            f"→ Capped {float(capped):.1%} | "
            f"Edge {float(edge):.1%} | EV {float(ev):.3f}"
        )

        return KellyResult(
            winrate=p,
            market_price=price,
            capital=cap,
            odds=odds,
            raw_kelly=raw_kelly,
            half_kelly=half_kelly,
            capped_kelly=capped,
            bet_fraction=capped,
            bet_usdc=bet_usdc,
            shares=shares,
            is_positive_ev=is_positive_ev,
            expected_value=ev,
            edge=edge,
            reason=reason,
        )

    def calculate_from_mispricing(
        self,
        mispricing_result,   # MispricingResult dari mispricing.py
        capital: float,
    ) -> KellyResult:
        """
        Shortcut: langsung dari MispricingResult.
        winrate = base_rate, market_price = market_price dari mispricing.
        """
        return self.calculate(
            winrate=mispricing_result.base_rate,
            market_price=mispricing_result.market_price,
            capital=capital,
        )

    # ─────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────

    def _zero_result(
        self,
        p: Decimal,
        price: Decimal,
        cap: Decimal,
        reason: str,
        odds: Optional[Decimal] = None,
        raw_kelly: Optional[Decimal] = None,
        ev: Optional[Decimal] = None,
    ) -> KellyResult:
        """Return result dengan bet 0 — artinya skip trade ini."""
        zero = Decimal("0")
        return KellyResult(
            winrate=p,
            market_price=price,
            capital=cap,
            odds=odds or zero,
            raw_kelly=raw_kelly or zero,
            half_kelly=zero,
            capped_kelly=zero,
            bet_fraction=zero,
            bet_usdc=zero,
            shares=zero,
            is_positive_ev=ev > Decimal("0") if ev else False,
            expected_value=ev or zero,
            edge=p - price,
            reason=f"SKIP — {reason}",
        )

    def format_result(self, r: KellyResult) -> str:
        """Pretty print KellyResult untuk logging."""
        if r.bet_usdc == Decimal("0"):
            return f"❌ SKIP | {r.reason}"
        return (
            f"✅ BET ${float(r.bet_usdc):.2f} "
            f"({float(r.bet_fraction):.1%} of ${float(r.capital):.0f}) | "
            f"{float(r.shares):.2f} shares @ {float(r.market_price):.3f} | "
            f"{r.reason}"
        )


# ─────────────────────────────────────────────
# QUICK TEST — python src/logic/kelly.py
# ─────────────────────────────────────────────

if __name__ == "__main__":
    sizer = KellySizer()

    print("=" * 65)
    print("TEST 1: Strong edge — winrate 70%, market 45%, modal $100")
    print("=" * 65)
    r = sizer.calculate(winrate=0.70, market_price=0.45, capital=100)
    print(sizer.format_result(r))
    print(f"  Raw Kelly: {float(r.raw_kelly):.1%}")
    print(f"  Half Kelly: {float(r.half_kelly):.1%}")
    print(f"  After cap: {float(r.capped_kelly):.1%}")

    print()
    print("=" * 65)
    print("TEST 2: Thin edge — winrate 58%, market 55%, modal $100")
    print("=" * 65)
    r2 = sizer.calculate(winrate=0.58, market_price=0.55, capital=100)
    print(sizer.format_result(r2))

    print()
    print("=" * 65)
    print("TEST 3: Negative EV — winrate 40%, market 55%, modal $100")
    print("=" * 65)
    r3 = sizer.calculate(winrate=0.40, market_price=0.55, capital=100)
    print(sizer.format_result(r3))

    print()
    print("=" * 65)
    print("TEST 4: High confidence — winrate 85%, market 50%, modal $120")
    print("=" * 65)
    r4 = sizer.calculate(winrate=0.85, market_price=0.50, capital=120)
    print(sizer.format_result(r4))
    print(f"  (Hard cap applied: {float(r4.half_kelly):.1%} → {float(r4.capped_kelly):.1%})")