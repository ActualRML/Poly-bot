from __future__ import annotations

from src.scout.context import ScoutContext
from src.scout.filters import Filter
from src.scout.result import FilterResult


class ScoutCompositeFilter(Filter):
    """
    Composite of 4 sub-checks (momentum / macro / vol_regime / time).
    Gate passes when score >= UPDOWN_SCOUT_MIN_SCORE.
    Edge sub-check dropped per refactor decision 4 (was structurally zero
    in non-GBM mode). max_score reduced from 5 → 4.
    Active only when UPDOWN_SCOUT_ENABLED=true; otherwise reports pass.
    """

    name = "scout_composite"

    def evaluate(self, ctx: ScoutContext) -> FilterResult:
        from src.utils.config import config

        if not getattr(config, "UPDOWN_SCOUT_ENABLED", False):
            return FilterResult.pass_(reason="scout_disabled")

        subs: dict = {}
        score = 0
        max_score = 0

        if ctx.sym_mtf:
            is_up = ctx.buy_outcome == "Up"
            aligned = sum([
                (ctx.sym_mtf.get("m_5m",  0) > 0) == is_up,
                (ctx.sym_mtf.get("m_15m", 0) > 0) == is_up,
                (ctx.sym_mtf.get("m_30m", 0) > 0) == is_up,
            ])
            mom_ok = aligned >= 2
            subs["momentum"] = mom_ok
            score += int(mom_ok)
            max_score += 1
        else:
            subs["momentum"] = None

        macro_dir = (ctx.market_regime or {}).get("direction")
        if macro_dir and macro_dir != "flat":
            macro_ok = (ctx.buy_outcome == "Up") == (macro_dir == "up")
            subs["macro"] = macro_ok
            score += int(macro_ok)
            max_score += 1
        else:
            subs["macro"] = None

        vol_ok = 0.20 <= ctx.vol_annual <= 0.80
        subs["vol_regime"] = vol_ok
        score += int(vol_ok)
        max_score += 1

        t_ok = 20 <= ctx.t_min <= 50
        subs["time"] = t_ok
        score += int(t_ok)
        max_score += 1

        threshold = getattr(config, "UPDOWN_SCOUT_MIN_SCORE", 3)
        passed = score >= threshold

        return FilterResult(
            passed=passed,
            reason=f"score={score}/{max_score} thr={threshold} subs={subs}",
            value={"score": score, "max_score": max_score, "subs": subs},
        )


SIGNAL_FILTERS: list[Filter] = [
    ScoutCompositeFilter(),
]
