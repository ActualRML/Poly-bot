import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).resolve().parents[2] / "data" / "circuit_breaker.json"

@dataclass
class CircuitStatus:
    can_trade: bool
    reason: str
    saklar_1_triggered: bool = False
    saklar_2_triggered: bool = False
    saklar_3_triggered: bool = False

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
    halt_new_entries: bool
    trigger: str
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
    starting_capital: float
    current_capital: float
    daily_loss: float
    daily_date: str
    consecutive_losses: int
    saklar_2_triggered: bool
    saklar_3_triggered: bool
    total_trades: int
    total_pnl: float

class CircuitBreaker:

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

        self.state = self._load_state()

        logger.info(
            f"CircuitBreaker ready | "
            f"Capital: ${starting_capital:.2f} | "
            f"Drawdown limit: {max_drawdown_pct:.0%} | "
            f"Daily loss limit: {max_daily_loss_pct:.0%} | "
            f"Consec loss limit: {max_consecutive_losses}x"
        )

    def check(self, unrealized_pnl: float = 0.0) -> CircuitStatus:
        self._reset_daily_if_new_day()

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

    def check_safety_thresholds(
        self,
        current_vol: float,
        daily_drawdown: float,
    ) -> SafetyStatus:
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

    def record_trade(self, pnl_usdc: float):
        self._reset_daily_if_new_day()

        self.state.total_trades += 1
        self.state.total_pnl    += pnl_usdc
        self.state.current_capital = max(0, self.state.current_capital + pnl_usdc)

        if pnl_usdc < 0:
            self.state.daily_loss        += pnl_usdc
            self.state.consecutive_losses += 1
            logger.info(
                f"[CB] Trade loss: ${pnl_usdc:.2f} | "
                f"Daily: ${self.state.daily_loss:.2f} | "
                f"Streak: {self.state.consecutive_losses}x"
            )
        else:
            self.state.consecutive_losses = 0
            logger.info(
                f"[CB] Trade win: +${pnl_usdc:.2f} | "
                f"Streak reset"
            )

        self._save_state()

    def reset_consecutive(self, reason: str = "manual reset"):
        self.state.consecutive_losses  = 0
        self.state.saklar_2_triggered  = False
        self._save_state()
        logger.info(f"🟠 Saklar 2 di-reset: {reason}")

    def reset_drawdown(self, new_capital: float, reason: str = "manual reset"):
        self.state.saklar_3_triggered = False
        self.state.current_capital    = new_capital
        self.state.starting_capital   = new_capital
        self.starting_capital         = new_capital
        self._save_state()
        logger.info(f"🔴 Saklar 3 di-reset: {reason} | Modal baru: ${new_capital:.2f}")

    def reset_daily(self):
        today = date.today().isoformat()
        self.state.daily_loss = 0.0
        self.state.daily_date = today
        self._save_state()
        logger.info("🟡 Daily loss counter di-reset manual")

    def get_summary(self, unrealized_pnl: float = 0.0) -> str:
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

    def _load_state(self) -> CircuitState:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text())
                state = CircuitState(**data)
                logger.debug(f"CircuitBreaker state loaded dari {STATE_FILE}")
                return state
            except Exception as e:
                logger.warning(f"Gagal load circuit breaker state: {e} — init baru")

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
        try:
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state.__dict__, indent=2))
            tmp.replace(STATE_FILE)
        except Exception as e:
            logger.error(f"Gagal simpan circuit breaker state: {e}")

    def _reset_daily_if_new_day(self):
        today = date.today().isoformat()
        if self.state.daily_date != today:
            old_loss = self.state.daily_loss
            self.state.daily_loss = 0.0
            self.state.daily_date = today
            self._save_state()
            if old_loss < 0:
                logger.info(f"🟡 Daily loss counter auto-reset (kemarin: ${old_loss:.2f})")

if __name__ == "__main__":
    import shutil
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

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
    breaker.record_trade(-6.0)
    status = breaker.check()
    print(status)
    print(breaker.get_summary())

    print("\n" + "=" * 55)
    print("TEST 3: Reset daily → Saklar 2 — consecutive loss")
    print("=" * 55)
    breaker.reset_daily()
    breaker.record_trade(-5.0)
    breaker.record_trade(-5.0)
    breaker.record_trade(-5.0)
    status = breaker.check()
    print(status)

    print("\n" + "=" * 55)
    print("TEST 4: Reset consecutive → Saklar 3 — max drawdown")
    print("=" * 55)
    breaker.reset_consecutive("test")
    breaker.reset_daily()
    breaker.state.current_capital = 83.0
    breaker._save_state()
    status = breaker.check()
    print(status)
    print(breaker.get_summary())

    if STATE_FILE.exists():
        STATE_FILE.unlink()
    bak = Path(str(STATE_FILE) + ".bak")
    if bak.exists():
        shutil.copy(bak, STATE_FILE)
        bak.unlink()

    print("\n✅ Semua test selesai")
