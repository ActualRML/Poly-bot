from __future__ import annotations

import json as _json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class ScoutContext:
    """Container for all data scout filters need. Built once per market analysis."""

    market: dict
    symbol: str
    condition_id: str
    question: str

    market_price_up: float
    start_date: datetime
    end_date: datetime
    delta_sec: float

    sym_mtf: Optional[dict]
    vol_annual: float
    vol_data: dict

    market_regime: Optional[dict]
    btc_scalp: Optional[dict]
    market_session: str
    btc_mtf: Optional[dict]

    closed_this_cycle: set
    profit_locked_markets: dict

    slot_open_count: int
    slot_history_count: int

    session: aiohttp.ClientSession
    capital: float

    binance_full_pause: bool = False

    buy_outcome: str = ""
    buy_price: float = 0.0
    # Set during DirectionalDecisionFilter via probability.calculate_winrate().
    # Default 0.0 until buy_outcome is finalized.
    buy_winrate: float = 0.0
    locked_outcome: Optional[str] = None

    event_horizon: Optional[dict] = None
    market_state: Optional[dict] = None

    sizer: Any = None
    gamma: Any = None
    clob: Any = None
    manager: Any = None
    breaker: Any = None

    kelly: Any = None
    scalp_kelly_mult: float = 1.0
    token_id: str = ""

    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def t_min(self) -> float:
        return self.delta_sec / 60.0

    @property
    def candle_running_min(self) -> float:
        return (datetime.now(timezone.utc) - self.start_date).total_seconds() / 60.0

    @classmethod
    async def build(
        cls,
        *,
        market: dict,
        session: aiohttp.ClientSession,
        capital: float,
        vol_data: dict,
        symbol_momentum_map: dict,
        market_regime: Optional[dict],
        btc_scalp: Optional[dict],
        market_session: str,
        closed_this_cycle: set,
        profit_locked_markets: dict,
    ) -> Optional["ScoutContext"]:
        """Build context from a market dict. Returns None if market is structurally invalid."""
        symbol = market.get("_symbol", "")
        if not symbol:
            return None

        condition_id = market.get("conditionId", market.get("id", ""))
        question = market.get("question", market.get("title", f"{symbol} Up or Down Hourly"))

        outcomes = market.get("outcomes", [])
        op = market.get("outcomePrices", [])
        if isinstance(outcomes, str):
            try: outcomes = _json.loads(outcomes)
            except Exception: outcomes = []
        if isinstance(op, str):
            try: op = _json.loads(op)
            except Exception: op = []

        outcomes_lower = [str(o).lower() for o in outcomes]
        if "up" not in outcomes_lower or not op:
            return None
        try:
            up_idx = outcomes_lower.index("up")
            market_price_up = float(op[up_idx])
        except (ValueError, IndexError):
            return None
        if not (0.0 < market_price_up < 1.0):
            return None

        end_date_str = market.get("endDate", "")
        start_date_str = market.get("_start_date", "")
        try:
            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            start_date = datetime.fromisoformat(start_date_str.replace("Z", "+00:00"))
        except Exception:
            return None

        now = datetime.now(timezone.utc)
        delta_sec = (end_date - now).total_seconds()
        if delta_sec <= 0:
            return None

        from src.risk.stagnation import track_market_price
        track_market_price(condition_id, market_price_up)

        sym_mtf = (symbol_momentum_map or {}).get(symbol.upper())
        binance_full_pause = False
        if sym_mtf is None:
            try:
                from src.api.binance_client import get_rate_limit_status
                binance_full_pause = get_rate_limit_status().get("status") == "FULL_PAUSE"
            except Exception:
                binance_full_pause = False

        vol_annual = (vol_data or {}).get(symbol.upper()) or (vol_data or {}).get("DEFAULT")
        if vol_annual is None:
            logger.warning(f"[VOL] {symbol} vol fetch failed, using fallback 0.40")
            vol_annual = 0.40

        btc_mtf = (symbol_momentum_map or {}).get("BTC")

        from src.models.database import count_open_by_resolve_slot
        from src.risk.slots import slot_history_count as _slot_hist
        slot_open_count = count_open_by_resolve_slot(end_date)
        slot_history_count = _slot_hist(end_date)

        locked_outcome: Optional[str] = None
        if profit_locked_markets and condition_id in profit_locked_markets:
            locked_outcome = (
                profit_locked_markets.get(condition_id)
                if isinstance(profit_locked_markets, dict) else None
            )

        return cls(
            market=market,
            symbol=symbol,
            condition_id=condition_id,
            question=question,
            market_price_up=market_price_up,
            start_date=start_date,
            end_date=end_date,
            delta_sec=delta_sec,
            sym_mtf=sym_mtf,
            vol_annual=vol_annual,
            vol_data=vol_data or {},
            market_regime=market_regime,
            btc_scalp=btc_scalp,
            market_session=market_session,
            btc_mtf=btc_mtf,
            closed_this_cycle=closed_this_cycle or set(),
            profit_locked_markets=profit_locked_markets or {},
            slot_open_count=slot_open_count,
            slot_history_count=slot_history_count,
            session=session,
            capital=capital,
            binance_full_pause=binance_full_pause,
            locked_outcome=locked_outcome,
        )
