import logging
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional

from src.execute.exit import Position, ExitEvaluator, ExitDecision, PortfolioExitManager
from src.models.database import (
    init_db,
    save_position,
    update_position_price,
    close_position,
    get_open_positions,
    get_position,
    get_position_by_market,
    count_open_positions,
    count_open_by_direction,
    log_trade,
    get_stats,
    resolve_prediction,
)
from src.risk.pricing import ke_decimal

logger = logging.getLogger(__name__)

MAX_OPEN_POSITIONS = 10
MAX_CAPITAL_PER_MARKET = 75

class PositionManager:

    def __init__(
        self,
        max_open_positions: int = MAX_OPEN_POSITIONS,
        max_capital_per_market: float = MAX_CAPITAL_PER_MARKET,
        exit_evaluator: Optional[ExitEvaluator] = None,
        max_same_direction: int = 0,
    ):
        self.max_open = max_open_positions
        self.max_capital_per_market = max_capital_per_market
        self.exit_evaluator = exit_evaluator or ExitEvaluator()
        self.portfolio_manager = PortfolioExitManager(self.exit_evaluator)
        self.max_same_direction = max_same_direction
        init_db()
        logger.info(f"PositionManager ready | max_open={self.max_open}")

    def can_open(
        self,
        condition_id: str,
        outcome: str,
        bet_usdc: Decimal,
        total_capital: Decimal,
    ) -> tuple[bool, str]:
        existing_market = get_position_by_market(condition_id)
        if existing_market:
            return False, f"Sudah ada posisi open di market {condition_id[:8]}... ({existing_market['outcome']})"

        open_count = count_open_positions()
        if open_count >= self.max_open:
            return False, f"Max posisi tercapai ({open_count}/{self.max_open})"

        if total_capital > Decimal("0"):
            pct = float(bet_usdc / total_capital * 100)
            if pct > self.max_capital_per_market:
                return False, f"Bet {pct:.1f}% melebihi max {self.max_capital_per_market:.0f}% per market"

        if self.max_same_direction > 0 and outcome in ("Up", "Down"):
            direction_count = count_open_by_direction(outcome)
            if direction_count >= self.max_same_direction:
                return False, f"Max posisi {outcome} tercapai ({direction_count}/{self.max_same_direction})"

        return True, ""

    def open_position(
        self,
        condition_id: str,
        question: str,
        outcome: str,
        entry_price: Decimal,
        shares: Decimal,
        capital_at_risk: Decimal,
        resolve_date: datetime,
        gap_pct: float = 0.0,
        kelly_fraction: float = 0.0,
        strategy_mode: str = "mispricing",
        token_id: str = "",
        sym_m5m: Optional[float] = None,
        sym_m15m: Optional[float] = None,
        sym_m30m: Optional[float] = None,
        vol_ratio: Optional[float] = None,
        btc_m15m: Optional[float] = None,
        scout_score: Optional[int] = None,
        mtf_aligned: Optional[int] = None,
    ) -> bool:
        now = datetime.now(timezone.utc)

        pos_data = {
            "condition_id":   condition_id,
            "question":       question,
            "outcome":        outcome,
            "entry_price":    entry_price,
            "current_price":  entry_price,
            "highest_price":  entry_price,
            "shares":         shares,
            "capital_at_risk": capital_at_risk,
            "resolve_date":   resolve_date,
            "entry_time":     now,
            "token_id":       token_id,
            "gap_pct":        str(round(gap_pct, 4)),
            "kelly_fraction": str(round(kelly_fraction, 4)),
            "strategy_mode":  strategy_mode,
            "sym_m5m":        sym_m5m,
            "sym_m15m":       sym_m15m,
            "sym_m30m":       sym_m30m,
            "vol_ratio":      vol_ratio,
            "btc_m15m":       btc_m15m,
            "scout_score":   scout_score,
            "mtf_aligned":    mtf_aligned,
        }

        try:
            save_position(pos_data)
            log_trade({
                "condition_id":   condition_id,
                "question":       question,
                "outcome":        outcome,
                "action":         "buy",
                "price":          entry_price,
                "shares":         shares,
                "usdc_amount":    capital_at_risk,
                "strategy_mode":  strategy_mode,
                "gap_pct":        str(round(gap_pct, 4)),
                "kelly_fraction": str(round(kelly_fraction, 4)),
            })
            logger.info(
                f"[OPEN] {question[:45]} | {outcome} @ {float(entry_price):.3f} | "
                f"${float(capital_at_risk):.2f} | gap={gap_pct:.1%} kelly={kelly_fraction:.1%}"
            )
            return True
        except Exception as e:
            logger.error(f"Gagal open posisi {condition_id}: {e}")
            return False

    def evaluate_exits(self, current_prices: dict[str, dict[str, Decimal]]) -> list[ExitDecision]:
        raw_positions = get_open_positions()
        if not raw_positions:
            return []

        positions = []
        for row in raw_positions:
            cid = row["condition_id"]
            outcome = row["outcome"]

            new_price = (current_prices.get(cid) or {}).get(outcome)
            if new_price:
                update_position_price(cid, outcome, new_price)
                current = new_price
            else:
                current = ke_decimal(row["current_price"])

            pos = self._row_to_position(row, current)
            positions.append(pos)

        exits = self.portfolio_manager.get_exits(positions)

        for decision in exits:
            self._process_exit(decision)

        return exits

    def _process_exit(self, decision: ExitDecision):
        pos = decision.position
        pnl = decision.estimated_pnl_usdc or Decimal("0")

        close_position(
            condition_id=pos.condition_id,
            outcome=pos.outcome,
            exit_price=pos.current_price,
            exit_reason=decision.signal.value,
            pnl_usdc=pnl,
        )
        log_trade({
            "condition_id": pos.condition_id,
            "question":     pos.question,
            "outcome":      pos.outcome,
            "action":       "exit",
            "price":        pos.current_price,
            "shares":       pos.shares,
            "usdc_amount":  float(pnl),
            "notes":        decision.reason[:200],
        })
        resolve_prediction(
            condition_id  = pos.condition_id,
            outcome       = pos.outcome,
            won           = float(pnl) > 0,
            resolve_price = float(pos.current_price),
        )
        logger.info(
            f"[EXIT] {pos.question[:45]} | {pos.outcome} | "
            f"{decision.signal.value} | PnL: ${float(pnl):+.2f}"
        )

    def _row_to_position(self, row: dict, current_price: Decimal) -> Position:
        resolve_date = datetime.fromisoformat(row["resolve_date"])
        if resolve_date.tzinfo is None:
            resolve_date = resolve_date.replace(tzinfo=timezone.utc)

        entry_time = datetime.fromisoformat(row["entry_time"])
        if entry_time.tzinfo is None:
            entry_time = entry_time.replace(tzinfo=timezone.utc)

        return Position(
            condition_id=row["condition_id"],
            question=row["question"],
            outcome=row["outcome"],
            entry_price=ke_decimal(row["entry_price"]),
            current_price=current_price,
            highest_price=ke_decimal(row["highest_price"]),
            shares=ke_decimal(row["shares"]),
            capital_at_risk=ke_decimal(row["capital_at_risk"]),
            resolve_date=resolve_date,
            entry_time=entry_time,
            token_id=row.get("token_id") or "",
            strategy_mode=row.get("strategy_mode") or "",
        )

    def _process_exit_manual(
        self,
        condition_id: str,
        outcome: str,
        exit_price: Decimal,
        pnl: Decimal,
        reason: str,
    ):
        pos_row = get_position(condition_id, outcome)
        question = pos_row["question"] if pos_row else ""
        shares   = ke_decimal(pos_row["shares"]) if pos_row else Decimal("0")

        close_position(
            condition_id=condition_id,
            outcome=outcome,
            exit_price=exit_price,
            exit_reason=reason,
            pnl_usdc=pnl,
        )
        log_trade({
            "condition_id": condition_id,
            "question":     question,
            "outcome":      outcome,
            "action":       "exit",
            "price":        exit_price,
            "shares":       shares,
            "usdc_amount":  float(pnl),
            "notes":        reason,
        })
        resolve_prediction(
            condition_id  = condition_id,
            outcome       = outcome,
            won           = float(pnl) > 0,
            resolve_price = float(exit_price),
        )

    def get_unrealized_pnl(self) -> float:
        positions = get_open_positions()
        return sum(
            (float(p["current_price"]) - float(p["entry_price"])) * float(p["shares"])
            for p in positions
        )

    def get_summary(self) -> str:
        open_count = count_open_positions()
        stats = get_stats()
        total = stats.get("total_trades") or 0
        pnl = stats.get("total_pnl") or 0
        winrate = stats.get("winrate") or 0
        return (
            f"Portfolio: {open_count}/{self.max_open} posisi open | "
            f"Closed: {total} trades | "
            f"Total PnL: ${pnl:+.2f} | "
            f"Winrate: {winrate:.1f}%"
        )
