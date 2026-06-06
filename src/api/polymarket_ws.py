import json
import logging
from contextlib import suppress

from src.api.ws_base import BaseWSClient


class PolymarketWSClient(BaseWSClient):
    """
    Polymarket CLOB market channel. On every (re)connect, send ONE
    `{"type":"market","assets_ids":[...]}` frame for the full subscription set.
    Add tokens live with `add_assets` (operation:subscribe, no reconnect); drop
    dead ones with `resubscribe` (clean reconnect). Receive arrays of book /
    price_change / last_trade_price / tick_size_change events.
    """

    def __init__(
        self,
        url: str,
        *,
        ping_interval: float = 20.0,
        max_backoff: float = 60.0,
        recv_timeout: float | None = 90.0,
        logger: logging.Logger | None = None,
    ):
        # 90s: above the normal quiet gap on thin markets between book updates,
        # well below the multi-hour stall the protocol-ping keepalive missed.
        super().__init__(
            url,
            ping_interval=ping_interval,
            max_backoff=max_backoff,
            recv_timeout=recv_timeout,
            logger=logger or logging.getLogger("ws"),
        )
        # Authoritative live subscription set, replayed on every (re)connect.
        self._assets: set[str] = set()

    @property
    def assets(self) -> set[str]:
        return set(self._assets)

    def set_assets(self, token_ids: list[str]) -> None:
        """Replace the set without touching the socket. Startup only: run()
        connects later and _on_connected sends it."""
        self._assets = set(token_ids)

    async def add_assets(self, token_ids: list[str]) -> None:
        """Add tokens live on the open socket — no reconnect. Records them first
        so a future reconnect includes them."""
        new = [t for t in token_ids if t not in self._assets]
        if not new:
            return
        self._assets.update(new)
        if self._ws is not None:
            await self._send_json({"assets_ids": new, "operation": "subscribe"})
            self.log.info("subscribed (live add)", extra={"count": len(new)})

    async def resubscribe(self, token_ids: list[str]) -> None:
        """Replace the set with only these tokens and force a clean reconnect, so
        the fresh socket subscribes exactly this set (drops dead tokens that
        operation:unsubscribe can't remove)."""
        self._assets = set(token_ids)
        if self._ws is not None:
            with suppress(Exception):
                await self._ws.close()

    def _connected_summary(self) -> str:
        return "polymarket"

    async def _on_connected(self) -> None:
        if not self._assets:
            return
        await self._send_json({"type": "market", "assets_ids": list(self._assets)})
        self.log.debug("subscribed (full set)", extra={"count": len(self._assets)})

    def _parse_frame(self, raw: str) -> list[tuple[str, dict]]:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self.log.warning("non-json frame", extra={"raw": str(raw)[:200]})
            return []
        # Polymarket batches: frames are JSON arrays; unwrap so handlers
        # only ever see one event at a time.
        events = payload if isinstance(payload, list) else [payload]
        out: list[tuple[str, dict]] = []
        for event in events:
            if isinstance(event, dict):
                event_type = event.get("event_type") or event.get("type") or "unknown"
                out.append((event_type, event))
        return out
