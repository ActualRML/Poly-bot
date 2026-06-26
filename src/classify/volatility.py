import statistics
from collections import defaultdict, deque

from src.monitor.logger import get_logger

# --- TUNE after observing real values (first run logs raw vol per symbol) ---
# Retuned to the observed per-tick return scale (~0.0000–0.0001+); the old
# 0.0015/0.0050 were ~15–30x too high so everything bucketed as low_vol.
VOL_WINDOW = 60         # recent spot prices kept per symbol
MIN_SAMPLES = 10        # fewer than this -> "unknown"
LOW_VOL_MAX = 0.00003   # vol <= this -> low_vol
HIGH_VOL_MIN = 0.00008  # vol >= this -> high_vol; in between -> mid_vol
# ----------------------------------------------------------------------------


class VolatilityClassifier:
    """
    Per-symbol rolling volatility from Binance spot.

    vol = pstdev of simple returns over the last VOL_WINDOW prices
    (return[i] = price[i]/price[i-1] - 1). Returns are unit-free, so one set
    of thresholds compares across BTC (~$79k) and DOGE (~$0.10). Stateful;
    fed by the binance feed in the orchestrator — strategies never see it.
    """

    def __init__(
        self,
        window: int = VOL_WINDOW,
        *,
        min_samples: int = MIN_SAMPLES,
        low_vol_max: float = LOW_VOL_MAX,
        high_vol_min: float = HIGH_VOL_MIN,
    ):
        self.window = window
        # Thresholds default to the module constants; the orchestrator passes
        # config.py values so they're tunable without editing this file.
        self.min_samples = min_samples
        self.low_vol_max = low_vol_max
        self.high_vol_min = high_vol_min
        self._prices: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=window))
        self.log = get_logger("regime")

    def update(self, symbol: str | None, price: float | None) -> None:
        if symbol and price and price > 0:
            self._prices[symbol].append(price)

    def volatility(self, symbol: str | None) -> float | None:
        prices = self._prices.get(symbol) if symbol else None
        if not prices or len(prices) < self.min_samples:
            return None
        returns = [prices[i] / prices[i - 1] - 1.0 for i in range(1, len(prices)) if prices[i - 1]]
        if len(returns) < 2:
            return None
        return statistics.pstdev(returns)

    def get_regime(self, symbol: str | None) -> str:
        vol = self.volatility(symbol)
        if vol is None:
            return "unknown"
        if vol <= self.low_vol_max:
            return "low_vol"
        if vol >= self.high_vol_min:
            return "high_vol"
        return "mid_vol"

    def snapshot_vols(self) -> dict[str, float]:
        """{symbol: vol} for symbols with enough data — drives the 60s log."""
        out: dict[str, float] = {}
        for sym in self._prices:
            v = self.volatility(sym)
            if v is not None:
                out[sym] = v
        return out
