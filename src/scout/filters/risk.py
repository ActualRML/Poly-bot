from __future__ import annotations

from src.scout.context import ScoutContext
from src.scout.filters import Filter
from src.scout.result import FilterResult


class SlotOpenCapFilter(Filter):
    name = "slot_open_cap"

    def evaluate(self, ctx: ScoutContext) -> FilterResult:
        from src.utils.config import config
        cap = config.MAX_POSITIONS_PER_SLOT
        if cap > 0 and ctx.slot_open_count >= cap:
            return FilterResult.fail(
                f"{ctx.slot_open_count}/{cap} OPEN di slot "
                f"{ctx.end_date.strftime('%H:%M')} UTC"
            )
        return FilterResult.pass_(value=ctx.slot_open_count)


class SlotCumulativeCapFilter(Filter):
    name = "slot_cumulative_cap"

    def evaluate(self, ctx: ScoutContext) -> FilterResult:
        from src.utils.config import config
        cap = config.UPDOWN_HOURLY_MAX_ENTRIES_PER_SLOT
        if ctx.slot_history_count >= cap:
            return FilterResult.fail(
                f"{ctx.slot_history_count}/{cap} CUMULATIVE entries di slot "
                f"{ctx.end_date.strftime('%H:%M')} UTC (slot exhausted)"
            )
        return FilterResult.pass_(value=ctx.slot_history_count)


class SymbolBlacklistFilter(Filter):
    name = "symbol_blacklist"

    def evaluate(self, ctx: ScoutContext) -> FilterResult:
        from src.risk.blacklist import check_symbol_blacklist
        if check_symbol_blacklist(ctx.symbol):
            return FilterResult.fail(
                f"{ctx.symbol} blacklisted (3 consecutive losses)"
            )
        return FilterResult.pass_()


class CircuitBreakerFilter(Filter):
    """Per-market CB check. Gated by CB_ENABLED."""

    name = "circuit_breaker"

    def evaluate(self, ctx: ScoutContext) -> FilterResult:
        from src.utils.config import config
        if not config.CB_ENABLED:
            return FilterResult.pass_(reason="cb_disabled")
        if ctx.breaker is None or ctx.manager is None:
            return FilterResult.pass_(reason="no breaker/manager (skipped)")
        try:
            status = ctx.breaker.check(unrealized_pnl=ctx.manager.get_unrealized_pnl())
        except Exception as e:
            return FilterResult.pass_(reason=f"cb check error: {e}")
        if not status.can_trade:
            return FilterResult.fail(f"breaker tripped: {status}")
        return FilterResult.pass_()


RISK_FILTERS: list[Filter] = [
    SlotOpenCapFilter(),
    SlotCumulativeCapFilter(),
    SymbolBlacklistFilter(),
    CircuitBreakerFilter(),
]
