from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class CycleDecision:
    enter_allowed: bool
    reason: str
    halt_alert: bool = False
    halt_reason: str = ""


class ScoutCycleGate:
    """
    Cycle-wide gate evaluated once before iterating markets each cycle.
    Houses flash crash hard skip, macro-regime contrarian skip, circuit
    breaker check, and safety thresholds. Returns CycleDecision; loop.py
    skips the per-market loop when enter_allowed=False.
    """

    @staticmethod
    def evaluate(
        *,
        market_regime: Optional[dict],
        breaker,
        manager,
        btc_vol: float,
    ) -> CycleDecision:
        from src.utils.config import config

        if config.CB_ENABLED and breaker is not None and manager is not None:
            try:
                cb_status = breaker.check(unrealized_pnl=manager.get_unrealized_pnl())
            except Exception as e:
                cb_status = None
                logger.debug(f"[CYCLE GATE] CB check error: {e}")
            if cb_status is not None and not cb_status.can_trade:
                return CycleDecision(
                    enter_allowed = False,
                    reason        = f"circuit breaker: {cb_status}",
                    halt_alert    = True,
                    halt_reason   = str(cb_status),
                )

            try:
                daily_drawdown = breaker.state.daily_loss / breaker.starting_capital
                safety = breaker.check_safety_thresholds(btc_vol, daily_drawdown)
            except Exception:
                safety = None
            if safety is not None and safety.halt_new_entries:
                return CycleDecision(
                    enter_allowed = False,
                    reason        = f"safety halt: {safety.reason}",
                    halt_alert    = True,
                    halt_reason   = safety.reason,
                )

        vol_state = (market_regime or {}).get("vol_state", "NORMAL")
        if (
            vol_state == "EXTREME_HIGH"
            and getattr(config, "FLASH_CRASH_HARD_SKIP", True)
        ):
            vol_pct = (market_regime or {}).get("vol_annual", 0) or 0
            return CycleDecision(
                enter_allowed = False,
                reason        = (
                    f"FLASH_CRASH: vol={vol_pct:.0%} annualized "
                    f"(EXTREME_HIGH) — hard skip all entries"
                ),
            )

        if (
            getattr(config, "UPDOWN_HOURLY_MACRO_TREND_GATE", False)
            and market_regime is not None
            and (
                market_regime.get("skip_contrarian")
                or vol_state == "EXTREME_HIGH"
            )
        ):
            return CycleDecision(
                enter_allowed = False,
                reason        = (
                    f"macro regime {market_regime.get('regime')} "
                    f"score={market_regime.get('trend_score')} vol={vol_state}"
                ),
            )

        return CycleDecision(enter_allowed=True, reason="ok")
