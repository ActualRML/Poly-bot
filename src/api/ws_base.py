import asyncio
import json
import logging
from collections import defaultdict
from contextlib import suppress
from typing import Any, Awaitable, Callable

import websockets

MessageHandler = Callable[[dict], Awaitable[None]]


class BaseWSClient:
    """
    Shared async WebSocket lifecycle: connect → init → receive → reconnect.

    Subclasses fill two hooks:
      - `_on_connected()` — send subscription frames after each successful connect
      - `_parse_frame(raw)` — split one raw frame into [(event_type, payload), ...]

    Everything else (backoff, dispatch, exception isolation, stop) is shared,
    which is what keeps Polymarket and Binance behaviour consistent.
    """

    def __init__(
        self,
        url: str,
        *,
        ping_interval: float = 20.0,
        max_backoff: float = 60.0,
        recv_timeout: float | None = None,
        logger: logging.Logger | None = None,
    ):
        self.url = url
        self.ping_interval = ping_interval
        self.max_backoff = max_backoff
        # None = no data-stall watchdog (preserves prior block-forever behavior).
        self.recv_timeout = recv_timeout
        self.log = logger or logging.getLogger("ws")

        self._handlers: dict[str, list[MessageHandler]] = defaultdict(list)
        self._stop = asyncio.Event()
        self._ws: Any = None

    def on(self, event_type: str) -> Callable[[MessageHandler], MessageHandler]:
        def register(fn: MessageHandler) -> MessageHandler:
            self._handlers[event_type].append(fn)
            return fn
        return register

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self.url,
                    ping_interval=self.ping_interval,
                ) as ws:
                    self._ws = ws
                    backoff = 1.0
                    summary = self._connected_summary()
                    self.log.info(f"connected ({summary})" if summary else "connected")
                    self.log.debug("connected url", extra={"url": self.url})
                    await self._on_connected()
                    await self._receive_loop(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log.warning(
                    "ws error, will reconnect",
                    extra={"error": str(e), "backoff_s": backoff},
                )
            finally:
                self._ws = None

            if self._stop.is_set():
                break

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                break
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2.0, self.max_backoff)

        self.log.info("ws run loop exited")

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            with suppress(Exception):
                await self._ws.close()

    async def _send_json(self, payload: dict) -> None:
        if self._ws is None:
            return
        await self._ws.send(json.dumps(payload))

    async def _on_connected(self) -> None:
        pass

    def _connected_summary(self) -> str:
        """Short, human-readable connect descriptor for the INFO log (no URL)."""
        return ""

    def _parse_frame(self, raw: str) -> list[tuple[str, dict]]:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self.log.warning("non-json frame", extra={"raw": str(raw)[:200]})
            return []
        if not isinstance(payload, dict):
            return []
        event_type = payload.get("event_type") or payload.get("type") or "unknown"
        return [(event_type, payload)]

    async def _receive_loop(self, ws) -> None:
        # No watchdog configured: original behavior, block until close/error.
        if self.recv_timeout is None:
            async for raw in ws:
                for event_type, event in self._parse_frame(raw):
                    await self._dispatch(event_type, event)
            return

        # Watchdog: a silent-but-open socket (server answers protocol pings but
        # sends no data) never raises, so bound each read. On stall, return
        # cleanly so run() reconnects through the normal _on_connected re-sub.
        while not self._stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=self.recv_timeout)
            except asyncio.TimeoutError:
                self.log.warning(
                    "recv stall: no data within timeout, forcing reconnect",
                    extra={"timeout_s": self.recv_timeout},
                )
                return
            for event_type, event in self._parse_frame(raw):
                await self._dispatch(event_type, event)

    async def _dispatch(self, event_type: str, event: dict) -> None:
        handlers = self._handlers.get(event_type, [])
        if not handlers:
            self.log.debug("no handler", extra={"event_type": event_type})
            return
        for handler in handlers:
            try:
                await handler(event)
            except Exception as e:
                # One bad handler must never silence the feed for siblings.
                self.log.exception(
                    "handler error",
                    extra={"event_type": event_type, "error": str(e)},
                )
