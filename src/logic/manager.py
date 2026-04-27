"""
src/logic/manager.py
====================
Position Manager — gatekeeper sebelum bot buka posisi baru.

Tugasnya:
1. Cek database: sudah ada posisi di market ini belum?
2. Cek kapasitas: sudah berapa posisi open? Masih boleh buka lagi?
3. Convert MispricingResult + KellyResult → Position object (siap pakai ExitEvaluator)
4. Sync harga terkini ke database
5. Evaluasi exit untuk semua posisi open
"""

import logging
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional

from src.logic.exit_strategy import Position, ExitEvaluator, ExitDecision, PortfolioExitManager
from src.models.database import (
    init_db,
    save_position,
    update_position_price,
    close_position,
    get_open_positions,
    get_position,
    get_position_by_market,
    count_open_positions,
    log_trade,
    get_stats,
    resolve_prediction,
)
from src.logic.pricing import ke_decimal

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

MAX_OPEN_POSITIONS = 5       # Maksimal posisi bersamaan
MAX_CAPITAL_PER_MARKET = 15  # Persen maksimal modal di satu market


# ─────────────────────────────────────────────
# POSITION MANAGER
# ─────────────────────────────────────────────

class PositionManager:
    """
    Gatekeeper antara logic bot dan database.

    Setiap kali bot mau buka posisi baru, harus lewat sini dulu.
    Manager yang memutuskan: boleh masuk atau tidak.
    """

    def __init__(
        self,
        max_open_positions: int = MAX_OPEN_POSITIONS,
        max_capital_per_market: float = MAX_CAPITAL_PER_MARKET,
        exit_evaluator: Optional[ExitEvaluator] = None,
    ):
        self.max_open = max_open_positions
        self.max_capital_per_market = max_capital_per_market
        self.exit_evaluator = exit_evaluator or ExitEvaluator()
        self.portfolio_manager = PortfolioExitManager(self.exit_evaluator)
        init_db()
        logger.info(f"PositionManager ready | max_open={self.max_open}")

    # ─────────────────────────────────────────────
    # ENTRY GATE
    # ─────────────────────────────────────────────

    def can_open(
        self,
        condition_id: str,
        outcome: str,
        bet_usdc: Decimal,
        total_capital: Decimal,
    ) -> tuple[bool, str]:
        """
        Cek apakah bot boleh buka posisi baru.

        Returns:
            (True, "") kalau boleh
            (False, alasan) kalau tidak boleh
        """
        # Sudah ada posisi di market ini (YES atau NO)?
        # Cegah beli kedua sisi market yang sama — guaranteed loss.
        existing_market = get_position_by_market(condition_id)
        if existing_market:
            return False, f"Sudah ada posisi open di market {condition_id[:8]}... ({existing_market['outcome']})"

        # Kapasitas penuh?
        open_count = count_open_positions()
        if open_count >= self.max_open:
            return False, f"Max posisi tercapai ({open_count}/{self.max_open})"

        # Over-concentration? (terlalu banyak modal di satu market)
        if total_capital > Decimal("0"):
            pct = float(bet_usdc / total_capital * 100)
            if pct > self.max_capital_per_market:
                return False, f"Bet {pct:.1f}% melebihi max {self.max_capital_per_market:.0f}% per market"

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
    ) -> bool:
        """
        Buka posisi baru — simpan ke database dan log trade entry.
        Returns True kalau berhasil.
        """
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

    # ─────────────────────────────────────────────
    # EXIT EVALUATION
    # ─────────────────────────────────────────────

    def evaluate_exits(self, current_prices: dict[str, dict[str, Decimal]]) -> list[ExitDecision]:
        """
        Evaluasi semua posisi open apakah perlu di-exit.

        Args:
            current_prices: {condition_id: {"Yes": Decimal, "No": Decimal}}

        Returns:
            List ExitDecision yang should_exit=True
        """
        raw_positions = get_open_positions()
        if not raw_positions:
            return []

        positions = []
        for row in raw_positions:
            cid = row["condition_id"]
            outcome = row["outcome"]

            # Update harga terkini kalau ada
            new_price = (current_prices.get(cid) or {}).get(outcome)
            if new_price:
                update_position_price(cid, outcome, new_price)
                current = new_price
            else:
                current = ke_decimal(row["current_price"])

            pos = self._row_to_position(row, current)
            positions.append(pos)

        exits = self.portfolio_manager.get_exits(positions)

        # Proses exits
        for decision in exits:
            self._process_exit(decision)

        return exits

    def _process_exit(self, decision: ExitDecision):
        """Tutup posisi di database dan log trade exit."""
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

    # ─────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────

    def _row_to_position(self, row: dict, current_price: Decimal) -> Position:
        """Convert database row → Position object untuk ExitEvaluator."""
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
        )

    def _process_exit_manual(
        self,
        condition_id: str,
        outcome: str,
        exit_price: Decimal,
        pnl: Decimal,
        reason: str,
    ):
        """Close posisi secara manual — dipakai resolve checker."""
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
        """Total unrealized PnL dari semua posisi open (negatif = rugi)."""
        positions = get_open_positions()
        return sum(
            (float(p["current_price"]) - float(p["entry_price"])) * float(p["shares"])
            for p in positions
        )

    def get_summary(self) -> str:
        """Ringkasan status portfolio saat ini."""
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