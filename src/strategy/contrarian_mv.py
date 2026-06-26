"""contrarian_mv — MID-VOL fade canary (LIVE forward-paper, BTC/ETH).

A ONE-VARIABLE mirror of contrarian: identical fade logic + params, the ONLY
signal difference being the vol gate — ``mid_vol`` instead of ``low_vol`` — plus a
BTC/ETH coin scope (below). Buys the cheap (≤0.30) longshot on an extreme, with
the same reversion-runway gate + debounce as contrarian.

WHY a canary (enabled 2026-06-20). The earlier "contrarian_mv is dead" verdict was
EFFICIENT-regime + the fill-DEPENDENT avg_roi. The 2026-06-20 regime-segmented
re-test (FINDINGS *2026-06-20*) showed, on the fill-INDEPENDENT edge, MV-REVERT
(BTC/ETH) +4.9pp (z=2.1, ≈breakeven after fills) ≥ LV-revert in the SAME
(understating) backtest lens → the backtest FAILS to kill it; MV is
regime-CONDITIONAL like LV (revert +, efficient dead). The backtest is nearly
blind to this edge (LV-revert scores +1.4pp there vs +15.5pp live), so the cell
can ONLY be closed by a LIVE read. This canary IS that read.

COIN SCOPE = BTC/ETH. Thin coins (XRP/BNB/DOGE) are MECHANICALLY dead for the
mid_vol fade (the book walks the fill toward the 0.30 cap; re-test edge −3 to
−4pp) — the same liquidity reason the SL canary scopes to BTC/ETH. A mechanical
exclusion, NOT signal-mining. Set ``SYMBOLS = None`` to open it up for all-coin
offline RESEARCH (the probes do this); the LIVE canary stays BTC/ETH.

SCORING — pre-registered, and DELIBERATELY different from the momentum/SL canaries.
MV bleeds in the efficient regime BY DESIGN (its edge is revert-only), so the
momentum-style "total avg_roi ≤ 0 at n≥100" kill-line would wrongly kill it before
a revert window ever arrives. Instead score the REVERT-SUBSET only (positions
opened in the reverting regime, post-hoc labeled from resolutions — see FINDINGS
for the date boundary): KEEP if the revert-subset edge is comparable to LV-revert;
KILL only if the revert-subset is CLEARLY negative. Do NOT kill for efficient-
regime bleed (that is expected). FROZEN — do not tune.

Runs ALONGSIDE contrarian without interfering (verified in code): ``is_held`` is
per-(market, strategy) so MV never steals LV's markets — LV's depth-imbalance
efficient-n keeps accumulating unaffected; exits scope to
``exits.EXIT_STRATEGY == "contrarian"`` (exact match) so MV is held-to-resolution.
Enable via ``ACTIVE_STRATEGIES=contrarian,contrarian_mv``.

Isolation: sees only MarketSnapshot (no api/db imports), like every strategy.
"""
from datetime import datetime

from src.execute.decision import Action, Decision
from src.strategy.base import Strategy
from src.strategy.params import StrategyParams

DEBOUNCE_SECONDS = 60
# Identical to contrarian: a fade only pays if the extreme has runway to revert
# before the lock. Measured event-time (resolve_time - snapshot.ts) → correct live
# AND in replay; inert when resolve_time is None.
MIN_RUNWAY_SECONDS = 600  # 10 min

# LIVE canary coin scope. Thin coins are mechanically dead for the mid_vol fade
# (fill walk), so trade only the liquid pair — same rationale as the SL canary.
# Set to None to disable the filter for all-coin offline RESEARCH (the probes do).
SYMBOLS: frozenset[str] | None = frozenset({"BTC", "ETH"})


class Plugin(Strategy):
    name = "contrarian_mv"
    # SAME knobs as contrarian — only the vol gate (mid_vol) + coin scope differ.
    # Pinned so the comparison to contrarian stays one-variable.
    params = StrategyParams(entry_floor=0.15, bet_fraction=0.02, entry_ceiling=0.30)

    def __init__(self) -> None:
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
        if SYMBOLS is not None and snapshot.symbol not in SYMBOLS:
            return Decision.skip(strategy=self.name, reason=f"coin {snapshot.symbol} out of scope")

        # price is YES-perspective; the ONLY signal change from contrarian is mid_vol.
        if snapshot.price_zone == "extreme_low" and snapshot.vol_regime == "mid_vol":
            side = "YES"
        elif snapshot.price_zone == "extreme_high" and snapshot.vol_regime == "mid_vol":
            side = "NO"
        else:
            return Decision.skip(
                strategy=self.name,
                reason=f"no signal zone={snapshot.price_zone} vol={snapshot.vol_regime}",
            )

        # Reversion runway (identical to contrarian): don't fade an extreme with too
        # little time left to bounce back before the lock.
        if snapshot.resolve_time is not None:
            secs_left = (snapshot.resolve_time - snapshot.ts).total_seconds()
            if secs_left <= MIN_RUNWAY_SECONDS:
                return Decision.skip(
                    strategy=self.name,
                    reason=f"runway too short ({int(secs_left)}s <= {MIN_RUNWAY_SECONDS}s)",
                )

        # Debounce on EVENT time (snapshot.ts) → correct live AND in replay.
        now = snapshot.ts
        prev = self._last_decision.get(snapshot.market_id)
        if prev is not None and (now - prev).total_seconds() < DEBOUNCE_SECONDS:
            return Decision.skip(strategy=self.name, reason="debounce")

        self._last_decision[snapshot.market_id] = now
        # NO costs 1 - YES-price; price_zone keys off the YES-perspective price.
        entry = snapshot.price if side == "YES" else 1.0 - snapshot.price
        return Decision(
            action=Action.BUY,
            strategy=self.name,
            side=side,
            price=entry,
            market_id=snapshot.market_id,
            reason=f"zone={snapshot.price_zone} vol={snapshot.vol_regime}",
        )
