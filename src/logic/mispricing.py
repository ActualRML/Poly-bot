import logging
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

logger = logging.getLogger(__name__)

class MispricingDirection(Enum):
    UNDERPRICED = "underpriced"
    OVERPRICED  = "overpriced"
    FAIR        = "fair"

@dataclass
class BaseRate:
    source: str
    rate: float
    confidence: float = 0.8
    sample_size: Optional[int] = None
    notes: str = ""

@dataclass
class MispricingResult:
    condition_id: str
    question: str
    outcome: str
    market_price: float
    base_rate: float
    gap: float
    gap_pct: float
    direction: MispricingDirection
    is_mispriced: bool
    threshold_used: float
    confidence: float
    source: str
    notes: str = ""
    raw_base_rates: list[BaseRate] = field(default_factory=list)

class MispricingDetector:

    def __init__(self, threshold: float = 0.15):
        self.threshold = threshold

    def analyze(
        self,
        condition_id: str,
        question: str,
        outcome: str,
        market_price: float,
        base_rates: list[BaseRate],
        threshold: Optional[float] = None,
    ) -> MispricingResult:
        if not base_rates:
            raise ValueError("Perlu minimal 1 BaseRate untuk analisis")

        th = threshold if threshold is not None else self.threshold

        blended_rate, blended_confidence = self._blend_base_rates(base_rates)

        gap = blended_rate - market_price
        gap_pct = abs(gap) * 100

        if gap > th:
            direction = MispricingDirection.UNDERPRICED
            is_mispriced = True
        elif gap < -th:
            direction = MispricingDirection.OVERPRICED
            is_mispriced = True
        else:
            direction = MispricingDirection.FAIR
            is_mispriced = False

        sources = ", ".join(set(br.source for br in base_rates))

        notes = self._build_notes(direction, gap_pct, blended_rate, market_price, outcome)

        result = MispricingResult(
            condition_id=condition_id,
            question=question,
            outcome=outcome,
            market_price=market_price,
            base_rate=blended_rate,
            gap=gap,
            gap_pct=gap_pct,
            direction=direction,
            is_mispriced=is_mispriced,
            threshold_used=th,
            confidence=blended_confidence,
            source=sources,
            notes=notes,
            raw_base_rates=base_rates,
        )

        self._log_result(result)
        return result

    def analyze_market(
        self,
        market: dict,
        yes_base_rates: list[BaseRate],
        no_base_rates: Optional[list[BaseRate]] = None,
        threshold: Optional[float] = None,
        analyze_yes_only: bool = True,
    ) -> list[MispricingResult]:
        from src.api.gamma_client import GammaClient
        prices = GammaClient.get_token_prices(market)

        results = []
        condition_id = market.get("conditionId", market.get("id", "unknown"))
        question = market.get("question", market.get("title", "Unknown"))

        yes_price = prices.get("Yes")
        if yes_price is not None and yes_base_rates:
            results.append(self.analyze(
                condition_id=condition_id,
                question=question,
                outcome="Yes",
                market_price=yes_price,
                base_rates=yes_base_rates,
                threshold=threshold,
            ))

        if analyze_yes_only:
            return results

        no_price = prices.get("No")
        if no_price is not None:
            if no_base_rates is None and yes_base_rates:
                no_base_rates = self._invert_base_rates(yes_base_rates)
            if no_base_rates:
                results.append(self.analyze(
                    condition_id=condition_id,
                    question=question,
                    outcome="No",
                    market_price=no_price,
                    base_rates=no_base_rates,
                    threshold=threshold,
                ))

        return results

    def _blend_base_rates(self, base_rates: list[BaseRate]) -> tuple[float, float]:
        total_weight = sum(br.confidence for br in base_rates)
        if total_weight == 0:
            avg = sum(br.rate for br in base_rates) / len(base_rates)
            return avg, 0.5

        blended = sum(br.rate * br.confidence for br in base_rates) / total_weight
        avg_conf = total_weight / len(base_rates)

        rates = [br.rate for br in base_rates]
        spread = max(rates) - min(rates)
        convergence_bonus = max(0, 0.1 - spread)

        return round(blended, 4), round(min(avg_conf + convergence_bonus, 1.0), 4)

    def _invert_base_rates(self, base_rates: list[BaseRate]) -> list[BaseRate]:
        return [
            BaseRate(
                source=br.source,
                rate=round(1.0 - br.rate, 4),
                confidence=br.confidence,
                sample_size=br.sample_size,
                notes=f"inverted from YES: {br.notes}",
            )
            for br in base_rates
        ]

    def _build_notes(
        self,
        direction: MispricingDirection,
        gap_pct: float,
        base_rate: float,
        market_price: float,
        outcome: str,
    ) -> str:
        if direction == MispricingDirection.FAIR:
            return f"Gap {gap_pct:.1f}% — dalam threshold, skip."
        elif direction == MispricingDirection.UNDERPRICED:
            return (
                f"⚡ {outcome} UNDERPRICED — gap {gap_pct:.1f}%. "
                f"Base rate {base_rate:.0%} vs market {market_price:.0%}. "
                f"→ Pertimbangkan BUY {outcome}."
            )
        else:
            return (
                f"⚡ {outcome} OVERPRICED — gap {gap_pct:.1f}%. "
                f"Base rate {base_rate:.0%} vs market {market_price:.0%}. "
                f"→ Pertimbangkan BUY {'No' if outcome == 'Yes' else 'Yes'}."
            )

    def _log_result(self, r: MispricingResult) -> None:
        if r.is_mispriced:
            logger.info(
                f"[MISPRICING] {r.question[:50]} | {r.outcome} | "
                f"market={r.market_price:.2f} base={r.base_rate:.2f} "
                f"gap={r.gap_pct:.1f}% | {r.direction.value.upper()}"
            )
        else:
            logger.debug(
                f"[FAIR] {r.question[:50]} | {r.outcome} | gap={r.gap_pct:.1f}%"
            )

class BaseRateBuilder:

    @staticmethod
    def from_historical(rate: float, sample_size: int, notes: str = "") -> BaseRate:
        conf = min(0.5 + (sample_size / 200) * 0.4, 0.9)
        return BaseRate(
            source="historical",
            rate=rate,
            confidence=round(conf, 2),
            sample_size=sample_size,
            notes=notes,
        )

    @staticmethod
    def from_metaculus(rate: float, num_predictors: int = 50) -> BaseRate:
        conf = min(0.6 + (num_predictors / 500) * 0.25, 0.85)
        return BaseRate(
            source="metaculus",
            rate=rate,
            confidence=round(conf, 2),
            notes=f"{num_predictors} predictors",
        )

    @staticmethod
    def from_kalshi(rate: float) -> BaseRate:
        return BaseRate(
            source="kalshi",
            rate=rate,
            confidence=0.85,
            notes="Kalshi market price",
        )

    @staticmethod
    def from_cme_fedwatch(rate: float) -> BaseRate:
        return BaseRate(
            source="cme_fedwatch",
            rate=rate,
            confidence=0.90,
            notes="CME FedWatch implied probability",
        )

    @staticmethod
    def from_manual(rate: float, confidence: float = 0.7, notes: str = "") -> BaseRate:
        return BaseRate(
            source="manual",
            rate=rate,
            confidence=confidence,
            notes=notes,
        )

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    detector = MispricingDetector(threshold=0.15)
    builder = BaseRateBuilder()

    print("=" * 60)
    print("TEST 1: Fed Rate Cut — UNDERPRICED scenario")
    print("=" * 60)
    result = detector.analyze(
        condition_id="0xABC123",
        question="Will the Fed cut rates in June 2025?",
        outcome="Yes",
        market_price=0.45,
        base_rates=[
            builder.from_cme_fedwatch(0.70),
            builder.from_metaculus(0.65, num_predictors=120),
            builder.from_historical(0.68, sample_size=40, notes="Last 10 Fed cycles"),
        ]
    )
    print(f"Mispriced: {result.is_mispriced}")
    print(f"Direction: {result.direction.value}")
    print(f"Gap: {result.gap_pct:.1f}%")
    print(f"Blended base rate: {result.base_rate:.2%}")
    print(f"Notes: {result.notes}")

    print()
    print("=" * 60)
    print("TEST 2: Crypto market — FAIR scenario")
    print("=" * 60)
    result2 = detector.analyze(
        condition_id="0xDEF456",
        question="Will BTC reach $100k by end of 2025?",
        outcome="Yes",
        market_price=0.55,
        base_rates=[
            builder.from_kalshi(0.58),
            builder.from_manual(0.60, confidence=0.65, notes="On-chain analysis"),
        ]
    )
    print(f"Mispriced: {result2.is_mispriced}")
    print(f"Gap: {result2.gap_pct:.1f}%")
    print(f"Notes: {result2.notes}")

    print()
    print("=" * 60)
    print("TEST 3: OVERPRICED scenario")
    print("=" * 60)
    result3 = detector.analyze(
        condition_id="0xGHI789",
        question="Will Elon Musk resign from DOGE by March 2025?",
        outcome="Yes",
        market_price=0.80,
        base_rates=[
            builder.from_metaculus(0.40, num_predictors=200),
            builder.from_historical(0.35, sample_size=15, notes="Similar political events"),
        ]
    )
    print(f"Mispriced: {result3.is_mispriced}")
    print(f"Direction: {result3.direction.value}")
    print(f"Gap: {result3.gap_pct:.1f}%")
    print(f"Notes: {result3.notes}")
