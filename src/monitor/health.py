import asyncio
import time
from dataclasses import dataclass, field

from src.monitor.logger import get_logger


@dataclass
class Health:
    """
    Per-stream message counters + uptime. Plain dataclass, no locks —
    asyncio is single-threaded so counter bumps are safe to share.
    """
    started_at: float = field(default_factory=time.time)
    poly_last_at: float = 0.0
    poly_message_count: int = 0
    binance_last_at: float = 0.0
    binance_message_count: int = 0

    def mark_poly(self) -> None:
        self.poly_last_at = time.time()
        self.poly_message_count += 1

    def mark_binance(self) -> None:
        self.binance_last_at = time.time()
        self.binance_message_count += 1

    def uptime_s(self) -> float:
        return time.time() - self.started_at

    def _silent_s(self, last: float) -> float:
        if last == 0.0:
            return -1.0
        return time.time() - last

    def poly_silent_s(self) -> float:
        return self._silent_s(self.poly_last_at)

    def binance_silent_s(self) -> float:
        return self._silent_s(self.binance_last_at)


# Silent time is shown only when a stream looks stalled — clean lines stay quiet.
_SILENT_WARN_S = 5.0


def _silent_flag(s: float) -> str:
    if s < 0:
        return " SILENT n/a"
    if s > _SILENT_WARN_S:
        return f" SILENT {int(s)}s"
    return ""


async def heartbeat(health: Health, interval_s: float = 60.0) -> None:
    log = get_logger("health")
    while True:
        await asyncio.sleep(interval_s)
        # Numbers live in the message (not extra) so the line reads cleanly
        # without the formatter's key=value tail duplicating them.
        log.info(
            f"up={int(health.uptime_s())}s  "
            f"poly={health.poly_message_count}{_silent_flag(health.poly_silent_s())}  "
            f"binance={health.binance_message_count}{_silent_flag(health.binance_silent_s())}"
        )
