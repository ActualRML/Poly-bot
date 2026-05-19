from __future__ import annotations

import logging

from src.scout.context import ScoutContext
from src.scout.filters import Filter
from src.scout.filters.discovery import DISCOVERY_FILTERS
from src.scout.filters.precheck import PRECHECK_FILTERS
from src.scout.filters.signal import SIGNAL_FILTERS
from src.scout.filters.risk import RISK_FILTERS
from src.scout.filters.exec import EXEC_FILTERS
from src.scout.result import ScoutDecision

logger = logging.getLogger(__name__)


def _run_stage(decision: ScoutDecision, ctx: ScoutContext, stage: list[Filter]) -> bool:
    for f in stage:
        result = f.evaluate(ctx)
        decision.add(f.name, result)
        if not result.passed:
            return False
    return True


async def evaluate_entry(ctx: ScoutContext) -> ScoutDecision:
    """
    Single gate for entry decisions. Runs all filter stages in order:
    discovery -> precheck -> signal -> risk -> exec.

    Short-circuits on first failure. Every filter (pass or fail) is recorded in
    `breakdown` so post-hoc audits can see the reason chain.

    Async to leave room for filters that need I/O (currently all filters are
    synchronous; signature kept async for forward compatibility).
    """
    decision = ScoutDecision(enter=False)

    if not _run_stage(decision, ctx, DISCOVERY_FILTERS):
        return decision
    if not _run_stage(decision, ctx, PRECHECK_FILTERS):
        return decision
    if not _run_stage(decision, ctx, SIGNAL_FILTERS):
        return decision
    if not _run_stage(decision, ctx, RISK_FILTERS):
        return decision
    if not _run_stage(decision, ctx, EXEC_FILTERS):
        return decision

    decision.enter = True
    return decision
