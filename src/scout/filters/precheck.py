from __future__ import annotations

from src.scout.context import ScoutContext
from src.scout.filters import Filter
from src.scout.result import FilterResult


class AlreadyClosedFilter(Filter):
    name = "already_closed"

    def evaluate(self, ctx: ScoutContext) -> FilterResult:
        if ctx.condition_id in ctx.closed_this_cycle:
            return FilterResult.fail("closed this cycle, no re-entry")
        return FilterResult.pass_()


class ProfitLockedFilter(Filter):
    name = "profit_locked"

    def evaluate(self, ctx: ScoutContext) -> FilterResult:
        if ctx.condition_id in ctx.profit_locked_markets:
            return FilterResult.fail("profit locked this session, no re-entry")
        return FilterResult.pass_()


class CandleOpenDelayFilter(Filter):
    name = "candle_open_delay"

    def evaluate(self, ctx: ScoutContext) -> FilterResult:
        from src.utils.config import config
        delay = getattr(config, "UPDOWN_HOURLY_CANDLE_OPEN_MIN", 10)
        run_min = ctx.candle_running_min
        if run_min < delay:
            return FilterResult.fail(
                f"candle running {run_min:.1f}m < {delay}m delay"
            )
        return FilterResult.pass_(value=run_min)


class MinTimeFloorFilter(Filter):
    name = "min_time_floor"

    def evaluate(self, ctx: ScoutContext) -> FilterResult:
        from src.utils.config import config
        floor_min = getattr(config, "UPDOWN_HOURLY_MIN_T_MINUTES", 20)
        if ctx.t_min < floor_min:
            return FilterResult.fail(
                f"{ctx.t_min:.1f}m tersisa < {floor_min}m floor"
            )
        return FilterResult.pass_(value=ctx.t_min)


class EventHorizonTierFilter(Filter):
    """Classifies time-to-resolve tier and stores result on ctx for sizing."""

    name = "event_horizon"

    def evaluate(self, ctx: ScoutContext) -> FilterResult:
        from src.utils.config import config
        from src.scout.regime import classify_event_horizon

        floor_min = float(getattr(config, "UPDOWN_HOURLY_MIN_T_MINUTES", 20))
        t_gate = classify_event_horizon(
            t_min              = ctx.t_min,
            strategy           = "momentum",
            floor_min          = floor_min,
            contrarian_min     = getattr(config, "UPDOWN_HOURLY_CONTRARIAN_MIN_T", 20.0),
            tight_max          = getattr(config, "UPDOWN_HOURLY_T_TIER_TIGHT_MAX", 35.0),
            critical_max       = getattr(config, "UPDOWN_HOURLY_T_TIER_CRITICAL_MAX", 25.0),
            edge_mult_tight    = getattr(config, "UPDOWN_HOURLY_T_EDGE_MULT_TIGHT", 1.5),
            edge_mult_critical = getattr(config, "UPDOWN_HOURLY_T_EDGE_MULT_CRITICAL", 2.0),
        )
        ctx.event_horizon = t_gate
        if not t_gate["allowed"]:
            return FilterResult.fail(
                f"tier {t_gate['tier']}: {t_gate['reason']}", value=t_gate
            )
        return FilterResult.pass_(reason=t_gate["reason"], value=t_gate)


class MomentumDataAvailableFilter(Filter):
    name = "momentum_data"

    def evaluate(self, ctx: ScoutContext) -> FilterResult:
        if ctx.sym_mtf is None:
            if ctx.binance_full_pause:
                return FilterResult.fail("binance FULL_PAUSE active")
            return FilterResult.fail("no momentum data (binance fetch failed?)")
        return FilterResult.pass_()


PRECHECK_FILTERS: list[Filter] = [
    AlreadyClosedFilter(),
    ProfitLockedFilter(),
    CandleOpenDelayFilter(),
    MinTimeFloorFilter(),
    EventHorizonTierFilter(),
    MomentumDataAvailableFilter(),
]
