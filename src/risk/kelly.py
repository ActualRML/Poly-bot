from decimal import Decimal
from dataclasses import dataclass
from typing import Optional

from src.risk.pricing import ke_decimal, hitung_midpoint, validasi_harga

MAX_KELLY_FRACTION: Decimal = Decimal("0.30")

KELLY_MULTIPLIER: Decimal = Decimal("0.7")

MIN_BET_USDC: Decimal = Decimal("5.0")

MIN_WINRATE: Decimal = Decimal("0.52")

@dataclass
class KellyResult:
    winrate: Decimal
    market_price: Decimal
    capital: Decimal

    odds: Decimal
    raw_kelly: Decimal
    half_kelly: Decimal
    capped_kelly: Decimal

    bet_fraction: Decimal
    bet_usdc: Decimal
    shares: Decimal

    is_positive_ev: bool
    expected_value: Decimal
    edge: Decimal
    reason: str

class KellySizer:

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
        p = ke_decimal(winrate)
        price = ke_decimal(market_price)
        cap = ke_decimal(capital)

        if not validasi_harga(price):
            return self._zero_result(p, price, cap, "Harga tidak valid (di luar 0.0001-0.9999)")

        if p < self.min_winrate:
            return self._zero_result(p, price, cap, f"Winrate {float(p):.1%} terlalu rendah (min {float(self.min_winrate):.1%})")

        if cap <= Decimal("0"):
            return self._zero_result(p, price, cap, "Modal 0 atau negatif")

        odds = (Decimal("1") - price) / price

        q = Decimal("1") - p

        raw_kelly = (p * odds - q) / odds

        ev = p * odds - q
        is_positive_ev = ev > Decimal("0")

        if not is_positive_ev:
            return self._zero_result(
                p, price, cap,
                f"EV negatif: {float(ev):.3f} — skip trade",
                odds=odds, raw_kelly=raw_kelly, ev=ev
            )

        half_kelly = raw_kelly * self.kelly_multiplier

        capped = min(half_kelly, self.max_fraction)
        capped = max(capped, Decimal("0"))

        bet_usdc = (cap * capped).quantize(Decimal("0.01"))

        if bet_usdc < self.min_bet_usdc:
            return self._zero_result(
                p, price, cap,
                f"Bet size ${float(bet_usdc):.2f} terlalu kecil (min ${float(self.min_bet_usdc):.2f})",
                odds=odds, raw_kelly=raw_kelly, ev=ev
            )

        shares = (bet_usdc / price).quantize(Decimal("0.0001"))

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
        mispricing_result,
        capital: float,
    ) -> KellyResult:
        return self.calculate(
            winrate=mispricing_result.base_rate,
            market_price=mispricing_result.market_price,
            capital=capital,
        )

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
        if r.bet_usdc == Decimal("0"):
            return f"❌ SKIP | {r.reason}"
        return (
            f"✅ BET ${float(r.bet_usdc):.2f} "
            f"({float(r.bet_fraction):.1%} of ${float(r.capital):.0f}) | "
            f"{float(r.shares):.2f} shares @ {float(r.market_price):.3f} | "
            f"{r.reason}"
        )

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
