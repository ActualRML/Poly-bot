from datetime import datetime

from src.execute.decision import Action, Decision
from src.strategy.base import Strategy
from src.strategy.params import StrategyParams

DEBOUNCE_SECONDS = 60

# Mean-reversion needs RUNWAY: a fade bet only pays if the extreme price has time to
# revert before the market locks. Entering in the final minutes fades a price that is
# extreme precisely BECAUSE the outcome is nearly settled (low_vol + little time left =
# the price is informative, not noise) — empirically a 0-win tail (sub-10-min entries
# were 0/6, every one a full-stake loss, across days/coins/sides). So require MORE than
# this much life left to ENTER. Measured against snapshot.ts (event time), so it holds
# in live AND replay — same reason the debounce uses snapshot.ts, not the wall clock.
# NOTE this is a STRATEGY edge floor, distinct from portfolio's global FILL-realism
# MIN_TIME_TO_RESOLVE_SEC (120s): deliberately stricter and contrarian-specific —
# a FOLLOW-the-favorite strategy wins late, so this is contrarian-specific, never global.
MIN_RUNWAY_SECONDS = 600  # 10 min


class Plugin(Strategy):
    name = "contrarian"
    # Explicit risk knobs for the live strategy — pin its own entry floor + bet
    # sizing rather than silently inheriting whatever the framework default is
    # (matches today's values; a test pins them against a silent revert).
    params = StrategyParams(entry_floor=0.15, bet_fraction=0.02, entry_ceiling=0.30)

    def __init__(self) -> None:
        # market_id -> EVENT TIME (snapshot.ts) of last non-skip decision
        self._last_decision: dict[str, datetime] = {}

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

        # Reversion runway: don't fade an extreme with too little time left to bounce
        # back before the market locks (see MIN_RUNWAY_SECONDS). Measured event-time
        # (resolve_time - snapshot.ts) so it's correct live AND in replay. resolve_time
        # is None on feeds that don't carry it (a backtest that hasn't wired it) — leave
        # the gate INERT there rather than skipping blindly, mirroring portfolio's
        # MIN_TIME_TO_RESOLVE gate (which also only acts when resolve_time is known).
        if snapshot.resolve_time is not None:
            secs_left = (snapshot.resolve_time - snapshot.ts).total_seconds()
            if secs_left <= MIN_RUNWAY_SECONDS:
                return Decision.skip(
                    strategy=self.name,
                    reason=f"runway too short ({int(secs_left)}s <= {MIN_RUNWAY_SECONDS}s)",
                )

        # Debounce on EVENT time (snapshot.ts), not the wall clock: a backtest
        # replays a market's whole stream in milliseconds, so time.time() would
        # collapse to "one decision per market ever". snapshot.ts is real-time in
        # live and simulated-time in replay, so this is correct in both.
        now = snapshot.ts
        prev = self._last_decision.get(snapshot.market_id)
        if prev is not None and (now - prev).total_seconds() < DEBOUNCE_SECONDS:
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
