import time

from src.execute.decision import Action, Decision
from src.strategy.base import Strategy
from src.strategy.params import StrategyParams

DEBOUNCE_SECONDS = 60


class Plugin(Strategy):
    name = "contrarian"
    # Explicit risk knobs for the live strategy — pin its own entry floor + bet
    # sizing rather than silently inheriting whatever the framework default is
    # (matches today's values; a test pins them against a silent revert).
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

        # price is YES-perspective (orchestrator normalized NO -> 1-p).
        if snapshot.price_zone == "extreme_low" and snapshot.vol_regime == "low_vol":
            side = "YES"
        elif snapshot.price_zone == "extreme_high" and snapshot.vol_regime == "low_vol":
            side = "NO"
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
        # Record the side's TRUE cost: NO costs 1 - YES-price. price_zone above
        # still keys off the YES-perspective price (signal logic unchanged).
        entry = snapshot.price if side == "YES" else 1.0 - snapshot.price
        return Decision(
            action=Action.BUY,
            strategy=self.name,
            side=side,
            price=entry,
            market_id=snapshot.market_id,
            reason=f"zone={snapshot.price_zone} vol={snapshot.vol_regime}",
        )
