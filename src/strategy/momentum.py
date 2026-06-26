"""momentum — follow-the-EXTREME-favorite canary (paper forward test).

The MIRROR of contrarian. On the SAME trigger (an extreme price + low_vol),
contrarian BUYS THE LONGSHOT (the cheap side, betting the extreme reverts);
momentum BUYS THE FAVORITE (the expensive side, ~0.80+, betting the extreme is
RIGHT and persists). The deliberate opposite bet on the same opportunity.

WHY a canary, with the prior that it is DEAD (FINDINGS *Session 2026-06-17/18*):
  * The earlier MODERATE-favorite momentum (high/low zones) ran 108 trades live
    and LOST -$187: WR 65% vs price paid 68% — favorites fairly priced, cost eats
    the rest. Removed 2026-06-17. THIS strategy trades the EXTREMES, not that band.
  * The EXTREME-favorite "edge" looks +5.6pp in the current (efficient) regime,
    but that is the arithmetic MIRROR of contrarian's -5.6pp loss on the same
    markets (binary: favorite_edge = -longshot_edge), it is NOT significant
    (z=1.3), and it is -15.5pp (z=5.0) in the reverting regime. Prior = "no edge
    after cost". This canary exists to MEASURE that forward, not because an edge
    is expected. FROZEN — score, don't tune (tuning voids the forward test).

Scoring (pre-registered, mirrors the SL / slow-rise canaries):
  Score per-strategy avg_roi in scripts/check_state.py (sizing-neutral) at
  n >= ~100 momentum resolves. KEEP only if avg_roi clearly >= 0 net of fills.
  KILL-LINE: if avg_roi <= 0 at n >= 100 (the expected outcome), remove from
  active_strategies — do NOT search params to rescue it (that manufactures a
  train-winner; see the "Don't" canon in CLAUDE.md).

Deliberate differences from contrarian (design, NOT knobs to search):
  * Buys the FAVORITE side; entry_ceiling is raised so an ~0.80 favorite can fill
    (contrarian's 0.30 cap would reject every favorite as "too expensive").
  * NO reversion-runway gate: a follow-the-favorite bet WINS late (the favorite
    resolving in its favored direction near the lock is the normal case) — the
    opposite of a fade — so the contrarian-specific runway floor must NOT apply.
    The global MIN_TIME_TO_RESOLVE_SEC (portfolio) still guards fill realism.

NOTE on running it: dedup is PER-STRATEGY (portfolio.is_held filters on strategy),
so this and contrarian can hold the SAME extreme market on OPPOSITE sides at once
without fighting — contrarian takes the longshot, momentum the favorite. The exit
overlays (SL / slow-rise) scope to contrarian (exits.EXIT_STRATEGY), so momentum
is held to resolution = a clean follow-vs-fade comparison. Enable by adding
"momentum" to active_strategies (e.g. ACTIVE_STRATEGIES=contrarian,momentum).

Isolation: sees only MarketSnapshot (no api/db imports), like every strategy.
"""
from datetime import datetime

from src.execute.decision import Action, Decision
from src.strategy.base import Strategy
from src.strategy.params import StrategyParams

DEBOUNCE_SECONDS = 60


class Plugin(Strategy):
    name = "momentum"
    # Buys the FAVORITE (~0.80+) on an extreme, so entry_ceiling must clear the
    # favorite's price for the depth-walk to fill it; contrarian's 0.30 cap would
    # reject every favorite. entry_floor pins entries to genuine favorites only.
    # bet_fraction = house default (scoring is the sizing-neutral avg_roi).
    params = StrategyParams(entry_floor=0.50, bet_fraction=0.02, entry_ceiling=0.85)

    def __init__(self) -> None:
        # market_id -> EVENT TIME (snapshot.ts) of last non-skip decision. ts, not
        # wall clock, so the debounce is correct in live AND replay (see contrarian).
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

        # price is YES-perspective (orchestrator normalized NO -> 1-p). FOLLOW the
        # favorite: on an extreme, buy the EXPENSIVE side — the exact mirror of
        # contrarian, which buys the cheap side on the SAME trigger. low_vol only,
        # matching contrarian's gate, so this is a clean follow-vs-fade comparison.
        if snapshot.price_zone == "extreme_high" and snapshot.vol_regime == "low_vol":
            side = "YES"  # YES is the favorite (>=0.80); follow it up
        elif snapshot.price_zone == "extreme_low" and snapshot.vol_regime == "low_vol":
            side = "NO"   # NO is the favorite (YES <=0.20); follow it
        else:
            return Decision.skip(
                strategy=self.name,
                reason=f"no signal zone={snapshot.price_zone} vol={snapshot.vol_regime}",
            )

        # NO reversion-runway gate here (unlike contrarian): a follow-the-favorite
        # bet wins late, so entering near the lock is fine.

        # Debounce on EVENT time (snapshot.ts), not the wall clock: a backtest
        # replays a market's whole stream in milliseconds, so time.time() would
        # collapse to "one decision per market ever".
        now = snapshot.ts
        prev = self._last_decision.get(snapshot.market_id)
        if prev is not None and (now - prev).total_seconds() < DEBOUNCE_SECONDS:
            return Decision.skip(strategy=self.name, reason="debounce")

        self._last_decision[snapshot.market_id] = now
        # Record the side's TRUE cost: NO costs 1 - YES-price. Both branches land
        # on the favorite (~0.80+).
        entry = snapshot.price if side == "YES" else 1.0 - snapshot.price
        return Decision(
            action=Action.BUY,
            strategy=self.name,
            side=side,
            price=entry,
            market_id=snapshot.market_id,
            reason=f"zone={snapshot.price_zone} vol={snapshot.vol_regime}",
        )
