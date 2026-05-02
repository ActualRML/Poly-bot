"""
src/logic/circuit_breaker.py
=============================
Circuit Breaker — 3 saklar pengaman sebelum bot trade.

Saklar 1: MAX_DAILY_LOSS    → pause hari ini, reset besok otomatis
Saklar 2: MAX_CONSEC_LOSS   → pause sampai manual reset
Saklar 3: MAX_DRAWDOWN      → stop total, emergency brake

Cara pakai:
    breaker = CircuitBreaker(starting_capital=120)
    
    # Sebelum setiap trade
    status = breaker.check()
    if not status.can_trade:
        log.warning(status.reason)
        return
    
    # Setelah trade selesai (win atau loss)
    breaker.record_trade(pnl_usdc=-14.0)  # negatif = loss
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Path file state — persist across restarts
STATE_FILE = Path(__file__).resolve().parents[2] / "data" / "circuit_breaker.json"


# ─────────────────────────────────────────────
# TYPES
# ─────────────────────────────────────────────

@dataclass
class CircuitStatus:
    """Status circuit breaker saat ini (PnL-based)."""
    can_trade: bool
    reason: str
    saklar_1_triggered: bool = False   # daily loss
    saklar_2_triggered: bool = False   # consecutive loss
    saklar_3_triggered: bool = False   # max drawdown

    def __str__(self) -> str:
        if self.can_trade:
            return "Circuit breaker: OK — boleh trade"
        icons = []
        if self.saklar_1_triggered:
            icons.append("Daily loss limit")
        if self.saklar_2_triggered:
            icons.append("Consecutive loss limit")
        if self.saklar_3_triggered:
            icons.append("Max drawdown — STOP TOTAL")
        return f"HALT | {' | '.join(icons)} | {self.reason}"


@dataclass
class SafetyStatus:
    """
    Status market-condition check (independent dari PnL).
    HANYA blokir entry baru — exit posisi tetap berjalan.
    """
    halt_new_entries: bool
    trigger: str          # "vol_extreme" | "drawdown_daily" | "ok"
    reason: str
    current_vol: float
    daily_drawdown: float

    def __str__(self) -> str:
        if not self.halt_new_entries:
            return (
                f"Safety OK — "
                f"vol={self.current_vol:.0%} | "
                f"drawdown={self.daily_drawdown:.1%}"
            )
        return (
            f"[SAFETY HALT] trigger={self.trigger} | "
            f"vol={self.current_vol:.0%} | "
            f"drawdown={self.daily_drawdown:.1%} | "
            f"{self.reason}"
        )


@dataclass
class CircuitState:
    """State internal circuit breaker — disimpan ke file."""
    starting_capital: float         # modal awal
    current_capital: float          # modal sekarang (estimasi)
    daily_loss: float               # total loss hari ini
    daily_date: str                 # tanggal daily_loss dihitung
    consecutive_losses: int         # streak loss berturut
    saklar_2_triggered: bool        # manual reset required
    saklar_3_triggered: bool        # emergency stop
    total_trades: int               # total trade sejak start
    total_pnl: float                # total PnL sejak start


# ─────────────────────────────────────────────
# CIRCUIT BREAKER
# ─────────────────────────────────────────────

class CircuitBreaker:
    """
    3 saklar pengaman bot trading.

    Saklar 1 — Daily Loss Limit:
        Reset otomatis setiap hari baru.
        Trigger: loss harian > max_daily_loss_pct × modal

    Saklar 2 — Consecutive Loss:
        Harus di-reset manual via reset_consecutive().
        Trigger: kalah N kali berturut-turut.

    Saklar 3 — Max Drawdown:
        Emergency brake. Harus di-reset manual via reset_drawdown().
        Trigger: modal turun > max_drawdown_pct dari starting capital.
    """

    def __init__(
        self,
        starting_capital: float,
        max_drawdown_pct: float = 0.30,
        max_daily_loss_pct: float = 0.10,
        max_consecutive_losses: int = 3,
    ):
        self.starting_capital       = starting_capital
        self.max_drawdown_pct       = max_drawdown_pct
        self.max_daily_loss_pct     = max_daily_loss_pct
        self.max_consecutive_losses = max_consecutive_losses

        # Load atau init state
        self.state = self._load_state()

        logger.info(
            f"CircuitBreaker ready | "
            f"Capital: ${starting_capital:.2f} | "
            f"Drawdown limit: {max_drawdown_pct:.0%} | "
            f"Daily loss limit: {max_daily_loss_pct:.0%} | "
            f"Consec loss limit: {max_consecutive_losses}x"
        )

    # ─────────────────────────────────────────────
    # MAIN CHECK — panggil sebelum setiap trade
    # ─────────────────────────────────────────────

    def check(self, unrealized_pnl: float = 0.0) -> CircuitStatus:
        """
        Cek apakah bot boleh trade sekarang.
        Panggil ini sebelum setiap eksekusi order.

        Args:
            unrealized_pnl: Total unrealized PnL dari posisi open (negatif = rugi).

        Returns:
            CircuitStatus dengan can_trade=True/False dan alasan
        """
        self._reset_daily_if_new_day()

        # ── Saklar 3: Max Drawdown (paling prioritas) ─────────────
        if self.state.saklar_3_triggered:
            return CircuitStatus(
                can_trade=False,
                reason=(
                    f"EMERGENCY STOP — Modal turun "
                    f"{self.max_drawdown_pct:.0%} dari awal. "
                    f"Jalankan: breaker.reset_drawdown() setelah evaluasi."
                ),
                saklar_3_triggered=True,
            )

        # Cek drawdown real-time (include unrealized PnL posisi open)
        effective_capital = self.state.current_capital + unrealized_pnl
        drawdown = (self.starting_capital - effective_capital) / self.starting_capital
        if drawdown >= self.max_drawdown_pct:
            self.state.saklar_3_triggered = True
            self._save_state()
            logger.critical(
                f"🔴 SAKLAR 3 TRIGGERED — Drawdown {drawdown:.1%} "
                f"melebihi limit {self.max_drawdown_pct:.0%}"
            )
            return CircuitStatus(
                can_trade=False,
                reason=f"Modal turun {drawdown:.1%} — melebihi limit {self.max_drawdown_pct:.0%}",
                saklar_3_triggered=True,
            )

        # ── Saklar 2: Consecutive Loss ────────────────────────────
        if self.state.saklar_2_triggered:
            return CircuitStatus(
                can_trade=False,
                reason=(
                    f"Kalah {self.max_consecutive_losses}x berturut. "
                    f"Review strategi, lalu jalankan: breaker.reset_consecutive()"
                ),
                saklar_2_triggered=True,
            )

        if self.state.consecutive_losses >= self.max_consecutive_losses:
            self.state.saklar_2_triggered = True
            self._save_state()
            logger.warning(
                f"🟠 SAKLAR 2 TRIGGERED — {self.state.consecutive_losses}x kalah berturut"
            )
            return CircuitStatus(
                can_trade=False,
                reason=f"Kalah {self.state.consecutive_losses}x berturut — perlu evaluasi manual",
                saklar_2_triggered=True,
            )

        # ── Saklar 1: Daily Loss ──────────────────────────────────
        daily_limit = self.starting_capital * self.max_daily_loss_pct
        if abs(self.state.daily_loss) >= daily_limit:
            logger.warning(
                f"🟡 SAKLAR 1 TRIGGERED — Daily loss "
                f"${abs(self.state.daily_loss):.2f} melebihi limit ${daily_limit:.2f}"
            )
            return CircuitStatus(
                can_trade=False,
                reason=(
                    f"Daily loss ${abs(self.state.daily_loss):.2f} "
                    f"melebihi limit ${daily_limit:.2f} — pause sampai besok"
                ),
                saklar_1_triggered=True,
            )

        return CircuitStatus(can_trade=True, reason="OK")

    # ─────────────────────────────────────────────
    # SAFETY CHECK — market-condition, pre-entry only
    # ─────────────────────────────────────────────

    def check_safety_thresholds(
        self,
        current_vol: float,
        daily_drawdown: float,
    ) -> SafetyStatus:
        """
        Cek kondisi pasar eksternal sebelum membuka entry baru.
        Independent dari PnL check() — selalu jalankan keduanya.

        PENTING: fungsi ini HANYA memblokir _analyze_market().
        Exit posisi (evaluate_exits, _resolve_checker, force_exit)
        tetap harus berjalan meskipun safety halt aktif.

        Args:
            current_vol    : Realized vol annualized dari Binance
                             (mis. 0.80 = 80%). Ambil dari vol_data["BTC"].
            daily_drawdown : Fraction rugi hari ini vs starting capital.
                             Negatif = rugi. Hitung:
                             breaker.state.daily_loss / breaker.starting_capital

        Returns:
            SafetyStatus dengan halt_new_entries=True/False.

        Threshold:
            current_vol    > 1.0  (> 100% annual) → HALT: vol ekstrem
            daily_drawdown < -0.15 (> 15% loss hari ini) → HALT: drawdown harian
        """
        # ── Cek 1: Extreme Volatility ─────────────────────────────
        if current_vol > 1.0:
            reason = (
                f"Volatilitas ekstrem: {current_vol:.0%} annualized "
                f"melebihi batas 100% — model log-normal tidak reliable, "
                f"edge kalkulasi probabilitas tidak bisa diandalkan"
            )
            logger.critical(f"[SAFETY HALT] VOL_EXTREME — {reason}")
            self._log_safety_event("vol_extreme", current_vol, daily_drawdown, reason)
            return SafetyStatus(
                halt_new_entries=True,
                trigger="vol_extreme",
                reason=reason,
                current_vol=current_vol,
                daily_drawdown=daily_drawdown,
            )

        # ── Cek 2: Daily Drawdown Limit ───────────────────────────
        if daily_drawdown < -0.15:
            reason = (
                f"Daily drawdown {daily_drawdown:.1%} "
                f"melebihi batas -15% — "
                f"stop entry baru, tunggu hari berikutnya"
            )
            logger.critical(f"[SAFETY HALT] DRAWDOWN_DAILY — {reason}")
            self._log_safety_event("drawdown_daily", current_vol, daily_drawdown, reason)
            return SafetyStatus(
                halt_new_entries=True,
                trigger="drawdown_daily",
                reason=reason,
                current_vol=current_vol,
                daily_drawdown=daily_drawdown,
            )

        # ── OK ────────────────────────────────────────────────────
        logger.debug(
            f"[SAFETY] OK — vol={current_vol:.0%} "
            f"drawdown={daily_drawdown:.1%}"
        )
        return SafetyStatus(
            halt_new_entries=False,
            trigger="ok",
            reason="Market conditions normal",
            current_vol=current_vol,
            daily_drawdown=daily_drawdown,
        )

    def _log_safety_event(
        self,
        trigger: str,
        vol: float,
        drawdown: float,
        reason: str,
    ) -> None:
        """
        Tulis audit log ke file terpisah agar mudah di-grep.
        Format: JSON-per-line di data/safety_halt.log
        """
        try:
            log_path = STATE_FILE.parent / "safety_halt.log"
            entry = json.dumps({
                "ts":       datetime.now(timezone.utc).isoformat(),
                "trigger":  trigger,
                "vol":      round(vol, 4),
                "drawdown": round(drawdown, 4),
                "reason":   reason,
            })
            with log_path.open("a", encoding="utf-8") as f:
                f.write(entry + "\n")
        except Exception as e:
            logger.warning(f"[SAFETY] Gagal tulis audit log: {e}")

    # ─────────────────────────────────────────────
    # RECORD TRADE — panggil setelah trade selesai
    # ─────────────────────────────────────────────

    def record_trade(self, pnl_usdc: float):
        """
        Catat hasil trade ke circuit breaker.

        Args:
            pnl_usdc: PnL dalam USDC. Negatif = loss, positif = win.
        """
        self._reset_daily_if_new_day()

        self.state.total_trades += 1
        self.state.total_pnl    += pnl_usdc
        self.state.current_capital = max(0, self.state.current_capital + pnl_usdc)

        if pnl_usdc < 0:
            # Loss
            self.state.daily_loss        += pnl_usdc
            self.state.consecutive_losses += 1
            logger.info(
                f"[CB] Trade loss: ${pnl_usdc:.2f} | "
                f"Daily: ${self.state.daily_loss:.2f} | "
                f"Streak: {self.state.consecutive_losses}x"
            )
        else:
            # Win — reset consecutive loss streak
            self.state.consecutive_losses = 0
            logger.info(
                f"[CB] Trade win: +${pnl_usdc:.2f} | "
                f"Streak reset"
            )

        self._save_state()

    # ─────────────────────────────────────────────
    # MANUAL RESET
    # ─────────────────────────────────────────────

    def reset_consecutive(self, reason: str = "manual reset"):
        """Reset saklar 2 — panggil setelah evaluasi strategi."""
        self.state.consecutive_losses  = 0
        self.state.saklar_2_triggered  = False
        self._save_state()
        logger.info(f"🟠 Saklar 2 di-reset: {reason}")

    def reset_drawdown(self, new_capital: float, reason: str = "manual reset"):
        """
        Reset saklar 3 — panggil setelah deposit atau evaluasi serius.

        Args:
            new_capital: Modal baru setelah evaluasi/deposit
            reason: Alasan reset untuk logging
        """
        self.state.saklar_3_triggered = False
        self.state.current_capital    = new_capital
        self.state.starting_capital   = new_capital
        self.starting_capital         = new_capital
        self._save_state()
        logger.info(f"🔴 Saklar 3 di-reset: {reason} | Modal baru: ${new_capital:.2f}")

    def reset_daily(self):
        """Force reset daily loss — biasanya tidak perlu, otomatis tiap hari baru."""
        today = date.today().isoformat()
        self.state.daily_loss = 0.0
        self.state.daily_date = today
        self._save_state()
        logger.info("🟡 Daily loss counter di-reset manual")

    # ─────────────────────────────────────────────
    # STATUS DISPLAY
    # ─────────────────────────────────────────────

    def get_summary(self, unrealized_pnl: float = 0.0) -> str:
        """Summary singkat untuk logging."""
        effective_capital = self.state.current_capital + unrealized_pnl
        drawdown = (self.starting_capital - effective_capital) / self.starting_capital
        daily_limit = self.starting_capital * self.max_daily_loss_pct
        status = self.check(unrealized_pnl=unrealized_pnl)

        return (
            f"CB: {'🔴 STOP' if self.state.saklar_3_triggered else '🟠 PAUSE' if self.state.saklar_2_triggered else '🟡 DAILY' if not status.can_trade else '✅ OK'} | "
            f"Drawdown: {drawdown:.1%}/{self.max_drawdown_pct:.0%} | "
            f"Daily loss: ${abs(self.state.daily_loss):.2f}/${daily_limit:.2f} | "
            f"Streak: {self.state.consecutive_losses}/{self.max_consecutive_losses}"
        )

    # ─────────────────────────────────────────────
    # STATE PERSISTENCE
    # ─────────────────────────────────────────────

    def _load_state(self) -> CircuitState:
        """Load state dari file, atau init baru kalau belum ada."""
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text())
                state = CircuitState(**data)
                logger.debug(f"CircuitBreaker state loaded dari {STATE_FILE}")
                return state
            except Exception as e:
                logger.warning(f"Gagal load circuit breaker state: {e} — init baru")

        # Init state baru
        return CircuitState(
            starting_capital    = self.starting_capital,
            current_capital     = self.starting_capital,
            daily_loss          = 0.0,
            daily_date          = date.today().isoformat(),
            consecutive_losses  = 0,
            saklar_2_triggered  = False,
            saklar_3_triggered  = False,
            total_trades        = 0,
            total_pnl           = 0.0,
        )

    def _save_state(self):
        """Simpan state ke file secara atomic (write-then-rename)."""
        try:
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state.__dict__, indent=2))
            tmp.replace(STATE_FILE)
        except Exception as e:
            logger.error(f"Gagal simpan circuit breaker state: {e}")

    def _reset_daily_if_new_day(self):
        """Auto-reset daily loss kalau sudah hari baru."""
        today = date.today().isoformat()
        if self.state.daily_date != today:
            old_loss = self.state.daily_loss
            self.state.daily_loss = 0.0
            self.state.daily_date = today
            self._save_state()
            if old_loss < 0:
                logger.info(f"🟡 Daily loss counter auto-reset (kemarin: ${old_loss:.2f})")


# ─────────────────────────────────────────────
# QUICK TEST
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import shutil
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    # Backup state file kalau ada
    if STATE_FILE.exists():
        shutil.copy(STATE_FILE, str(STATE_FILE) + ".bak")
        STATE_FILE.unlink()

    breaker = CircuitBreaker(
        starting_capital       = 120.0,
        max_drawdown_pct       = 0.30,
        max_daily_loss_pct     = 0.10,
        max_consecutive_losses = 3,
    )

    print("\n" + "=" * 55)
    print("TEST 1: Normal — boleh trade")
    print("=" * 55)
    status = breaker.check()
    print(status)

    print("\n" + "=" * 55)
    print("TEST 2: Saklar 1 — daily loss limit")
    print("=" * 55)
    breaker.record_trade(-7.0)
    breaker.record_trade(-6.0)  # total -13 > 10% × 120 = -12
    status = breaker.check()
    print(status)
    print(breaker.get_summary())

    print("\n" + "=" * 55)
    print("TEST 3: Reset daily → Saklar 2 — consecutive loss")
    print("=" * 55)
    breaker.reset_daily()
    breaker.record_trade(-5.0)  # loss 1
    breaker.record_trade(-5.0)  # loss 2
    breaker.record_trade(-5.0)  # loss 3 → trigger
    status = breaker.check()
    print(status)

    print("\n" + "=" * 55)
    print("TEST 4: Reset consecutive → Saklar 3 — max drawdown")
    print("=" * 55)
    breaker.reset_consecutive("test")
    breaker.reset_daily()
    # Simulasi drawdown besar
    breaker.state.current_capital = 83.0  # turun $37 dari $120 = 30.8%
    breaker._save_state()
    status = breaker.check()
    print(status)
    print(breaker.get_summary())

    # Cleanup test
    if STATE_FILE.exists():
        STATE_FILE.unlink()
    bak = Path(str(STATE_FILE) + ".bak")
    if bak.exists():
        shutil.copy(bak, STATE_FILE)
        bak.unlink()

    print("\n✅ Semua test selesai")