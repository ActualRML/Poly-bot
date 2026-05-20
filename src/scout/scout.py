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


def _mark_skipped(
    decision: ScoutDecision,
    stages: list[list[Filter]],
    from_stage_idx: int,
) -> None:
    """Tally every filter that didn't evaluate due to short-circuit."""
    for f in stages[from_stage_idx]:
        if f.name not in decision.breakdown:
            decision.skip(f.name)
    for later in stages[from_stage_idx + 1:]:
        for f in later:
            decision.skip(f.name)


async def evaluate_entry(ctx: ScoutContext) -> ScoutDecision:
    """
    Single gate for entry decisions. Runs all filter stages in order:
    discovery -> precheck -> signal -> risk -> exec.

    Short-circuits on first failure. Evaluated filters (pass or fail) are recorded in
    `breakdown`; remaining filters in unreached stages are tallied in `reasons_skipped`.
    max_score reflects the total filter count across the full pipeline (fixed across
    markets), preserving the invariant passed + failed + skipped == max_score.

    Async to leave room for filters that need I/O (currently all filters are
    synchronous; signature kept async for forward compatibility).
    """
    stages: list[list[Filter]] = [
        DISCOVERY_FILTERS,
        PRECHECK_FILTERS,
        SIGNAL_FILTERS,
        RISK_FILTERS,
        EXEC_FILTERS,
    ]
    total = sum(len(s) for s in stages)
    decision = ScoutDecision(enter=False, max_score=total)

    for i, stage in enumerate(stages):
        if not _run_stage(decision, ctx, stage):
            _mark_skipped(decision, stages, i)
            return decision

    decision.enter = True
    return decision
