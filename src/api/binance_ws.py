"""
Binance WebSocket tick stream for latency arbitrage.

BinanceTickBuffer  — in-memory ring of (price, ts_ms) ticks.
stream_binance_ticks — async generator that feeds the buffer via aggTrade stream,
                       with a 2-second ghosting watchdog.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)

_WS_BASE = "wss://stream.binance.com:9443/ws"
GHOST_TIMEOUT = 2.0  # seconds without a message → assume ghost connection


class BinanceTickBuffer:
    """In-memory ring buffer of (price, exchange_ts_ms) tuples."""

    def __init__(self, maxlen: int = 500) -> None:
        self._buf: deque[tuple[float, int]] = deque(maxlen=maxlen)
        self._last_wall_ts: Optional[float] = None  # monotonic clock of last on_tick

    def on_tick(self, price: float, ts_ms: int) -> None:
        self._buf.append((price, ts_ms))
        self._last_wall_ts = time.monotonic()

    def latest_price(self) -> Optional[float]:
        if not self._buf:
            return None
        return self._buf[-1][0]

    def is_stale(self, timeout_s: float = GHOST_TIMEOUT) -> bool:
        """True if no tick has been received for longer than timeout_s."""
        if self._last_wall_ts is None:
            return True
        return time.monotonic() - self._last_wall_ts > timeout_s

    def latency_ms(self) -> Optional[float]:
        """
        Approximate latency: wall-clock time minus last exchange timestamp.
        Returns None if buffer is empty.
        """
        if not self._buf:
            return None
        exchange_ts_ms = self._buf[-1][1]
        wall_ms = time.time() * 1000.0
        return round(max(0.0, wall_ms - exchange_ts_ms), 1)

    def heartbeat_status(self) -> dict:
        """
        Returns a health snapshot for logging/monitoring.
        Keys: ok, last_seen_s, is_stale, latency_ms, warning
        """
        stale = self.is_stale()
        last_seen_s = (
            round(time.monotonic() - self._last_wall_ts, 2)
            if self._last_wall_ts is not None
            else None
        )
        warning = None
        if stale:
            age = f"{last_seen_s}s" if last_seen_s is not None else "never"
            warning = f"[WS] STALE CONNECTION — last tick {age} ago"
        return {
            "ok": not stale,
            "last_seen_s": last_seen_s,
            "is_stale": stale,
            "latency_ms": self.latency_ms(),
            "warning": warning,
        }

    def get_move_pct(self, seconds: float = 3.0) -> float:
        """% price change from `seconds` ago to now. Returns 0.0 if not enough data."""
        if not self._buf:
            return 0.0
        now_ms = self._buf[-1][1]
        cutoff_ms = now_ms - int(seconds * 1000)
        ref_price: Optional[float] = None
        for price, ts in self._buf:
            if ts >= cutoff_ms:
                ref_price = price
                break
        if ref_price is None or ref_price <= 0:
            return 0.0
        current = self._buf[-1][0]
        return (current - ref_price) / ref_price * 100.0

    def get_elapsed_since_move(self, threshold_pct: float = 0.1) -> Optional[float]:
        """
        Seconds since the last tick that triggered a move ≥ threshold_pct%.
        Returns None if no such move has been observed.
        """
        if not self._buf:
            return None
        now_ms = self._buf[-1][1]
        baseline: Optional[float] = None
        for price, ts in self._buf:
            if baseline is None:
                baseline = price
                continue
            if baseline > 0 and abs(price - baseline) / baseline * 100.0 >= threshold_pct:
                elapsed_s = (now_ms - ts) / 1000.0
                return max(0.0, elapsed_s)
        return None

    def __len__(self) -> int:
        return len(self._buf)


async def stream_binance_ticks(
    symbol: str,
    buf: BinanceTickBuffer,
    session,
    max_ticks: Optional[int] = None,
) -> None:
    """
    Connects to Binance aggTrade WebSocket stream and pushes ticks into `buf`.

    Ghosting watchdog: if no message arrives within GHOST_TIMEOUT seconds,
    forces a hard-disconnect and immediate reconnect without waiting for OS timeout.

    symbol    : e.g. "btcusdt"
    buf       : BinanceTickBuffer instance to populate
    session   : aiohttp.ClientSession (caller owns lifecycle)
    max_ticks : stop after N ticks (useful for tests); None = run forever
    """
    url = f"{_WS_BASE}/{symbol.lower()}@aggTrade"
    backoff = 1.0
    count = 0

    while True:
        try:
            async with session.ws_connect(url) as ws:
                backoff = 1.0
                ws_iter = ws.__aiter__()
                while True:
                    try:
                        msg = await asyncio.wait_for(
                            ws_iter.__anext__(), timeout=GHOST_TIMEOUT
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "[WS] Connection ghosting detected — no data for "
                            f"{GHOST_TIMEOUT}s on {symbol}, forcing hard-reconnect"
                        )
                        await ws.close()
                        break
                    except StopAsyncIteration:
                        break

                    if msg.type != 1:  # aiohttp.WSMsgType.TEXT == 1
                        continue
                    try:
                        data = json.loads(msg.data)
                        price = float(data["p"])
                        ts_ms = int(data["T"])
                        buf.on_tick(price, ts_ms)
                        count += 1
                        if max_ticks is not None and count >= max_ticks:
                            return
                    except (KeyError, ValueError):
                        continue
        except asyncio.CancelledError:
            return
        except Exception:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
