import logging

import aiohttp


class BinanceREST:
    """
    Spot price + klines. WebSocket streaming will land in a sibling
    module once a strategy actually needs sub-second prices; for now
    REST is plenty.
    """

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.log = logging.getLogger("api.binance")
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "BinanceREST":
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def get_price(self, symbol: str) -> float:
        assert self._session is not None, "use 'async with BinanceREST(...)'"
        url = f"{self.base_url}/api/v3/ticker/price"
        async with self._session.get(url, params={"symbol": symbol.upper()}) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return float(data["price"])
