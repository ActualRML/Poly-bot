from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ScoutResult:
    score: int
    max_score: int = 5
    breakdown: dict = field(default_factory=dict)

    @property
    def passes(self) -> bool:
        from src.utils.config import config
        return self.score >= getattr(config, "UPDOWN_SCOUT_MIN_SCORE", 3)


def score_updown_market(
    symbol: str,
    buy_outcome: str,
    gbm_edge: float,
    adj_min_edge: float,
    sym_mtf: dict | None,
    vol_annual: float,
    minutes_left: float,
    macro_htf_dir: str | None,
) -> ScoutResult:
    breakdown: dict = {}
    score = 0

    ok = gbm_edge >= adj_min_edge * 2
    breakdown["edge"] = ok
    score += int(ok)

    if sym_mtf:
        is_up = buy_outcome == "Up"
        aligned = sum([
            (sym_mtf.get("m_1m",  0) > 0) == is_up,
            (sym_mtf.get("m_5m",  0) > 0) == is_up,
            (sym_mtf.get("m_15m", 0) > 0) == is_up,
        ])
        ok = aligned >= 2
        breakdown["momentum"] = ok
        score += int(ok)
    else:
        breakdown["momentum"] = None

    if macro_htf_dir and macro_htf_dir != "flat":
        ok = (buy_outcome == "Up") == (macro_htf_dir == "up")
        breakdown["macro"] = ok
        score += int(ok)
    else:
        breakdown["macro"] = None

    ok = 0.20 <= vol_annual <= 0.80
    breakdown["vol_regime"] = ok
    score += int(ok)

    ok = 20 <= minutes_left <= 50
    breakdown["time"] = ok
    score += int(ok)

    max_score = sum(1 for v in breakdown.values() if v is not None)
    return ScoutResult(score=score, max_score=max_score, breakdown=breakdown)


async def evaluate_updown_scout(
    symbol: str,
    buy_outcome: str,
    gbm_edge: float,
    adj_min_edge: float,
    minutes_left: float,
    session,
) -> ScoutResult:
    from src.api.binance_client import fetch_realized_vol
    from src.execute.updown import calculate_multi_tf_momentum
    from src.scout.regime import detect_market_regime
    vol_annual    = (await fetch_realized_vol(symbol, session)) or 0.40
    sym_mtf       = await calculate_multi_tf_momentum(symbol, session)
    _regime       = await detect_market_regime(session)
    macro_htf_dir = _regime.get("direction") if _regime else None
    return score_updown_market(
        symbol        = symbol,
        buy_outcome   = buy_outcome,
        gbm_edge      = gbm_edge,
        adj_min_edge  = adj_min_edge,
        sym_mtf       = sym_mtf,
        vol_annual    = vol_annual,
        minutes_left  = minutes_left,
        macro_htf_dir = macro_htf_dir,
    )
