"""
src/logic/exit_strategy.py
==========================
Exit strategy — kapan keluar dari posisi.

Dua mekanisme utama:
1. Trailing Stop  — protect dari reversal, exit kalau harga turun > 15% dari peak
2. Resolve Awareness — hold vs lock profit tergantung proximity ke resolve date
"""

from decimal import Decimal
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from src.logic.pricing import ke_decimal, validasi_harga


# ─────────────────────────────────────────────
# TYPES
# ─────────────────────────────────────────────

class ExitSignal(Enum):
    HOLD             = "hold"              # Tahan posisi
    EXIT_TRAILING    = "exit_trailing"     # Trailing stop triggered
    EXIT_LOCK_PROFIT = "exit_lock_profit"  # Lock profit, redeploy modal
    HOLD_TO_RESOLVE  = "hold_to_resolve"   # Hampir resolve, tahan sampai $1
    EXIT_STALE       = "exit_stale"        # Posisi terlalu lama, tidak bergerak


@dataclass
class Position:
    """Representasi satu posisi aktif."""
    condition_id: str
    outcome: str                      # "Yes" atau "No"
    entry_price: Decimal              # harga saat beli
    current_price: Decimal            # harga terkini
    highest_price: Decimal            # harga tertinggi sejak entry
    shares: Decimal                   # jumlah shares
    capital_at_risk: Decimal          # USDC yang diinvestasikan
    resolve_date: datetime            # kapan market resolve
    entry_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    question: str = ""
    token_id: str = ""

    @property
    def days_to_resolve(self) -> int:
        now = datetime.now(timezone.utc)
        delta = self.resolve_date - now
        return max(0, delta.days)

    @property
    def days_held(self) -> int:
        now = datetime.now(timezone.utc)
        return (now - self.entry_time).days

    @property
    def unrealized_pnl_pct(self) -> Decimal:
        """PnL unrealized dalam persen dari entry."""
        if self.entry_price == Decimal("0"):
            return Decimal("0")
        return ((self.current_price - self.entry_price) / self.entry_price * 100
                ).quantize(Decimal("0.01"))

    @property
    def drawdown_from_peak(self) -> Decimal:
        """Drawdown dari harga tertinggi (0-1)."""
        if self.highest_price == Decimal("0"):
            return Decimal("0")
        return ((self.highest_price - self.current_price) / self.highest_price
                ).quantize(Decimal("0.0001"))


@dataclass
class ExitDecision:
    """Output dari evaluasi exit strategy."""
    signal: ExitSignal
    should_exit: bool
    position: Position

    # Detail
    reason: str
    trailing_stop_price: Optional[Decimal] = None   # harga trigger trailing stop
    suggested_exit_price: Optional[Decimal] = None  # rekomendasi exit price
    estimated_pnl_usdc: Optional[Decimal] = None    # estimasi PnL kalau exit

    def __str__(self) -> str:
        emoji = "🔴" if self.should_exit else "🟢"
        return (
            f"{emoji} [{self.signal.value.upper()}] "
            f"{self.position.question[:45]} | "
            f"{self.reason}"
        )


# ─────────────────────────────────────────────
# CORE EXIT EVALUATOR
# ─────────────────────────────────────────────

class ExitEvaluator:
    """
    Evaluasi kapan harus keluar dari posisi.

    Rules (urutan prioritas):
    1. Trailing stop — kalau turun > trailing_pct dari peak → EXIT
    2. Resolve awareness — kalau harga > profit_threshold DAN hampir resolve → HOLD_TO_RESOLVE
    3. Lock profit — kalau harga > profit_threshold DAN masih jauh dari resolve → EXIT_LOCK_PROFIT
    4. Stale check — kalau posisi terlalu lama tidak bergerak → EXIT_STALE
    5. Default → HOLD
    """

    def __init__(
        self,
        trailing_stop_pct: float = 0.15,       # exit kalau turun 15% dari peak
        profit_threshold: float = 0.85,         # harga "sudah profit besar"
        tight_trailing_stop_pct: float = 0.07,  # trailing stop diperketat saat profit zone
        days_hold_to_resolve: int = 3,          # kalau ≤ N hari → hold to resolve
        max_days_stale: int = 21,               # max hari hold tanpa movement
        stale_movement_threshold: float = 0.05, # movement < 5% dianggap stale
    ):
        self.trailing_stop_pct = ke_decimal(trailing_stop_pct)
        self.profit_threshold = ke_decimal(profit_threshold)
        self.tight_trailing_stop_pct = ke_decimal(tight_trailing_stop_pct)
        self.days_hold_to_resolve = days_hold_to_resolve
        self.max_days_stale = max_days_stale
        self.stale_movement_threshold = ke_decimal(stale_movement_threshold)

    def evaluate(self, pos: Position) -> ExitDecision:
        """
        Evaluasi posisi dan return ExitDecision.
        Rules dievaluasi secara berurutan — pertama yang trigger menang.
        """
        # ── Rule 1 & 2: Profit Zone — tight trailing stop ─────────────────
        if pos.current_price >= self.profit_threshold:

            # ≤ 3 hari ke resolve → tahan, tunggu settle ke $1
            if pos.days_to_resolve <= self.days_hold_to_resolve:
                return ExitDecision(
                    signal=ExitSignal.HOLD_TO_RESOLVE,
                    should_exit=False,
                    position=pos,
                    suggested_exit_price=Decimal("1.0"),
                    estimated_pnl_usdc=self._calc_pnl(pos, exit_price=Decimal("0.9999")),
                    reason=(
                        f"Harga {float(pos.current_price):.3f} ≥ threshold, "
                        f"resolve {pos.days_to_resolve}d lagi → HOLD, tunggu $1"
                    ),
                )

            # > 3 hari → tight trailing stop aktif, biarkan profit jalan
            tight_stop = (pos.highest_price * (Decimal("1") - self.tight_trailing_stop_pct)
                          ).quantize(Decimal("0.0001"))

            if pos.current_price <= tight_stop:
                pnl = self._calc_pnl(pos)
                return ExitDecision(
                    signal=ExitSignal.EXIT_LOCK_PROFIT,
                    should_exit=True,
                    position=pos,
                    trailing_stop_price=tight_stop,
                    suggested_exit_price=pos.current_price,
                    estimated_pnl_usdc=pnl,
                    reason=(
                        f"Tight trailing stop hit! "
                        f"Peak {float(pos.highest_price):.3f} → "
                        f"Current {float(pos.current_price):.3f} "
                        f"(turun {float(pos.drawdown_from_peak):.1%}) | "
                        f"Tight stop @ {float(tight_stop):.3f}"
                    ),
                )

            # Profit zone, tight stop belum kena → let it run
            return ExitDecision(
                signal=ExitSignal.HOLD,
                should_exit=False,
                position=pos,
                trailing_stop_price=tight_stop,
                estimated_pnl_usdc=self._calc_pnl(pos),
                reason=(
                    f"Profit zone | Price {float(pos.current_price):.3f} | "
                    f"PnL {float(pos.unrealized_pnl_pct):+.1f}% | "
                    f"Tight stop @ {float(tight_stop):.3f} | "
                    f"Resolve {pos.days_to_resolve}d"
                ),
            )

        # ── Rule 3: Normal Trailing Stop (di luar profit zone) ────────────
        trailing_stop_price = (pos.highest_price * (Decimal("1") - self.trailing_stop_pct)
                               ).quantize(Decimal("0.0001"))

        if pos.current_price <= trailing_stop_price:
            pnl = self._calc_pnl(pos)
            return ExitDecision(
                signal=ExitSignal.EXIT_TRAILING,
                should_exit=True,
                position=pos,
                trailing_stop_price=trailing_stop_price,
                suggested_exit_price=pos.current_price,
                estimated_pnl_usdc=pnl,
                reason=(
                    f"Trailing stop hit! "
                    f"Peak {float(pos.highest_price):.3f} → "
                    f"Current {float(pos.current_price):.3f} "
                    f"(turun {float(pos.drawdown_from_peak):.1%} dari peak) | "
                    f"Stop @ {float(trailing_stop_price):.3f}"
                ),
            )

        # ── Rule 4: Stale Position ─────────────────────────────────────────
        movement = abs(pos.current_price - pos.entry_price) / pos.entry_price if pos.entry_price else Decimal("0")
        if (pos.days_held >= self.max_days_stale
                and movement < self.stale_movement_threshold):
            pnl = self._calc_pnl(pos)
            return ExitDecision(
                signal=ExitSignal.EXIT_STALE,
                should_exit=True,
                position=pos,
                suggested_exit_price=pos.current_price,
                estimated_pnl_usdc=pnl,
                reason=(
                    f"Posisi {pos.days_held}d, movement hanya "
                    f"{float(movement):.1%} — modal stuck, exit & redeploy"
                ),
            )

        # ── Default: HOLD ──────────────────────────────────────────────────
        return ExitDecision(
            signal=ExitSignal.HOLD,
            should_exit=False,
            position=pos,
            trailing_stop_price=trailing_stop_price,
            reason=(
                f"Hold | Price {float(pos.current_price):.3f} | "
                f"PnL {float(pos.unrealized_pnl_pct):+.1f}% | "
                f"Stop @ {float(trailing_stop_price):.3f} | "
                f"Resolve {pos.days_to_resolve}d"
            ),
        )

    def update_highest_price(self, pos: Position) -> Position:
        """
        Update highest_price kalau current_price lebih tinggi.
        Panggil ini setiap kali ada price update.
        """
        if pos.current_price > pos.highest_price:
            pos.highest_price = pos.current_price
        return pos

    def _calc_pnl(
        self,
        pos: Position,
        exit_price: Optional[Decimal] = None,
    ) -> Decimal:
        """Estimasi PnL kalau exit sekarang (dalam USDC)."""
        price = exit_price or pos.current_price
        return ((price - pos.entry_price) * pos.shares).quantize(Decimal("0.01"))


# ─────────────────────────────────────────────
# PORTFOLIO-LEVEL EVALUATOR
# ─────────────────────────────────────────────

class PortfolioExitManager:
    """
    Evaluasi semua posisi aktif sekaligus.
    Wrap ExitEvaluator untuk multi-position management.
    """

    def __init__(self, evaluator: Optional[ExitEvaluator] = None):
        self.evaluator = evaluator or ExitEvaluator()

    def evaluate_all(self, positions: list[Position]) -> list[ExitDecision]:
        """Evaluasi semua posisi, return sorted: exit dulu baru hold."""
        decisions = []
        for pos in positions:
            pos = self.evaluator.update_highest_price(pos)
            decision = self.evaluator.evaluate(pos)
            decisions.append(decision)

        # Sort: yang perlu exit duluan
        decisions.sort(key=lambda d: (not d.should_exit, d.signal.value))
        return decisions

    def get_exits(self, positions: list[Position]) -> list[ExitDecision]:
        """Return hanya posisi yang harus di-exit."""
        return [d for d in self.evaluate_all(positions) if d.should_exit]

    def summary(self, decisions: list[ExitDecision]) -> str:
        exits = sum(1 for d in decisions if d.should_exit)
        holds = len(decisions) - exits
        total_pnl = sum(
            float(d.estimated_pnl_usdc or 0) for d in decisions if d.should_exit
        )
        return (
            f"Portfolio: {len(decisions)} posisi | "
            f"EXIT {exits} | HOLD {holds} | "
            f"Est. PnL exits: ${total_pnl:+.2f}"
        )


# ─────────────────────────────────────────────
# QUICK TEST — python src/logic/exit_strategy.py
# ─────────────────────────────────────────────

if __name__ == "__main__":
    from datetime import timedelta

    evaluator = ExitEvaluator()

    def make_pos(current, highest, entry, days_to_resolve, days_held=5, question="Test market?"):
        return Position(
            condition_id="0xTEST",
            question=question,
            outcome="Yes",
            entry_price=ke_decimal(entry),
            current_price=ke_decimal(current),
            highest_price=ke_decimal(highest),
            shares=ke_decimal(100),
            capital_at_risk=ke_decimal(entry * 100),
            resolve_date=datetime.now(timezone.utc) + timedelta(days=days_to_resolve),
            entry_time=datetime.now(timezone.utc) - timedelta(days=days_held),
        )

    print("=" * 65)
    print("TEST 1: Trailing stop — harga turun dari peak")
    print("=" * 65)
    pos1 = make_pos(current=0.58, highest=0.75, entry=0.45, days_to_resolve=10)
    d1 = evaluator.evaluate(pos1)
    print(d1)

    print()
    print("=" * 65)
    print("TEST 2: Hold to resolve — harga tinggi, resolve 2 hari lagi")
    print("=" * 65)
    pos2 = make_pos(current=0.92, highest=0.92, entry=0.45, days_to_resolve=2)
    d2 = evaluator.evaluate(pos2)
    print(d2)

    print()
    print("=" * 65)
    print("TEST 3: Lock profit — harga tinggi, resolve masih 20 hari")
    print("=" * 65)
    pos3 = make_pos(current=0.88, highest=0.88, entry=0.45, days_to_resolve=20)
    d3 = evaluator.evaluate(pos3)
    print(d3)
    print(f"  Est. PnL: ${float(d3.estimated_pnl_usdc):.2f}")

    print()
    print("=" * 65)
    print("TEST 4: Hold — posisi normal, belum ada trigger")
    print("=" * 65)
    pos4 = make_pos(current=0.62, highest=0.65, entry=0.45, days_to_resolve=15)
    d4 = evaluator.evaluate(pos4)
    print(d4)

    print()
    print("=" * 65)
    print("TEST 5: Portfolio — evaluasi semua sekaligus")
    print("=" * 65)
    manager = PortfolioExitManager(evaluator)
    all_decisions = manager.evaluate_all([pos1, pos2, pos3, pos4])
    print(manager.summary(all_decisions))
    print()
    for d in all_decisions:
        print(f"  {d}")