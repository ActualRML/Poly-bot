import time

from src.execute.decision import Action, Decision
from src.strategy.base import Strategy
from src.strategy.params import StrategyParams

DEBOUNCE_SECONDS = 60


class Plugin(Strategy):
    name = "momentum"
    # Risk knobs match the house defaults (0.15 / 0.02). entry_floor does not
    # bind here: momentum trades MODERATE favorites priced ~0.60-0.80, well
    # above the floor. (Same two knobs as contrarian, but the entry signal
    # below differs - momentum is no longer a mirror of contrarian.)
    params = StrategyParams(entry_floor=0.15, bet_fraction=0.02)

    def __init__(self) -> None:
        # market_id -> timestamp of last non-skip decision
        self._last_decision: dict[str, float] = {}

    async def evaluate(self, snapshot) -> Decision:
        if snapshot.source != "polymarket":
            return Decision.skip(strategy=self.name, reason="not polymarket")
        if snapshot.price is None or snapshot.price_zone is None or snapshot.vol_regime is None:
            return Decision.skip(strategy=self.name, reason="incomplete snapshot")
        if snapshot.market_id is None:
            return Decision.skip(strategy=self.name, reason="no market_id")
        if snapshot.outcome is None:
            return Decision.skip(strategy=self.name, reason="unknown outcome")

        # price is YES-perspective (orchestrator normalized NO -> 1-p). momentum
        # trades MODERATE favorites in low volatility, following the favored
        # side: it buys whichever side sits in the high/low zone (cost ~0.60-
        # 0.80). It deliberately AVOIDS the extreme zones (>=0.80), where the
        # 0.03 slippage buffer erases the win margin.
        if snapshot.price_zone == "high" and snapshot.vol_regime == "low_vol":
            side = "YES"  # YES is the favorite (0.60-0.80); follow the trend up
        elif snapshot.price_zone == "low" and snapshot.vol_regime == "low_vol":
            side = "NO"   # NO is the favorite (YES 0.20-0.40); follow the trend
        else:
            return Decision.skip(
                strategy=self.name,
                reason=f"no signal zone={snapshot.price_zone} vol={snapshot.vol_regime}",
            )

        now = time.time()
        prev = self._last_decision.get(snapshot.market_id)
        if prev is not None and now - prev < DEBOUNCE_SECONDS:
            return Decision.skip(strategy=self.name, reason="debounce")

        self._last_decision[snapshot.market_id] = now
        # Record the side's TRUE cost: NO costs 1 - YES-price. In both branches
        # the favorite's cost lands ~0.60-0.80 - a real margin over the 0.03
        # slippage buffer (unlike the extreme zones this strategy now avoids).
        entry = snapshot.price if side == "YES" else 1.0 - snapshot.price
        return Decision(
            action=Action.BUY,
            strategy=self.name,
            side=side,
            price=entry,
            market_id=snapshot.market_id,
            reason=f"zone={snapshot.price_zone} vol={snapshot.vol_regime}",
        )
