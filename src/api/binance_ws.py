import json
import logging

from src.api.ws_base import BaseWSClient


def build_stream_url(base: str, symbols: list[str], stream: str = "miniTicker") -> str:
    """Combined-stream URL — subscription is baked into the path, no SUBSCRIBE frame needed."""
    streams = "/".join(f"{s.lower()}@{stream}" for s in symbols)
    return f"{base.rstrip('/')}/stream?streams={streams}"


class BinanceWSClient(BaseWSClient):
    """
    Binance public combined-stream client. All symbols ride one connection;
    subscription is in the URL so reconnect = same URL, no replay needed.

    miniTicker frame shape:
      {"stream":"btcusdt@miniTicker",
       "data":{"e":"24hrMiniTicker","s":"BTCUSDT","c":"79500.00", ...}}

    Handlers register on the configured stream name (default "miniTicker")
    and receive the inner `data` dict — branch on `data["s"]` for symbol.
    """

    def __init__(
        self,
        base_url: str,
        symbols: list[str],
        *,
        stream: str = "miniTicker",
        ping_interval: float = 20.0,
        max_backoff: float = 60.0,
        logger: logging.Logger | None = None,
    ):
        self.symbols = [s.lower() for s in symbols]
        self.stream = stream
        url = build_stream_url(base_url, self.symbols, stream)
        super().__init__(
            url,
            ping_interval=ping_interval,
            max_backoff=max_backoff,
            logger=logger or logging.getLogger("binance"),
        )

    def _connected_summary(self) -> str:
        return f"{len(self.symbols)} streams"

    def _parse_frame(self, raw: str) -> list[tuple[str, dict]]:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self.log.warning("non-json frame", extra={"raw": str(raw)[:200]})
            return []
        if isinstance(payload, dict) and "data" in payload:
            return [(self.stream, payload["data"])]
        # Non-stream frames (e.g. subscription acks) — log but don't dispatch.
        if isinstance(payload, dict):
            self.log.debug("control frame", extra={"payload": payload})
        return []
