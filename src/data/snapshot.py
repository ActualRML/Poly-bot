from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class MarketSnapshot:
    """
    One normalized, point-in-time view of a market, handed to strategies.

    Two very different raw feeds (Polymarket CLOB book/trade events and
    Binance miniTicker frames) are flattened into this single shape so a
    strategy never has to branch on `source`-specific JSON layouts. The
    isolation contract holds: strategies see ONLY this — no WS handle, no
    HTTP, no DB. Anything a strategy needs must be materialized into a
    field here by the parser/orchestrator first (keeps strategies
    mock-free and unit-testable).

    `raw` keeps the original dict so a strategy can reach fields we
    haven't promoted yet; new needs should add a typed field rather than
    digging into raw.
    """

    ts: datetime                    # capture time, UTC, tz-aware
    source: str                     # "polymarket" | "binance"
    event_type: str                 # book | price_change | last_trade_price | ticker
    symbol: str | None              # BTC/ETH/SOL/XRP/DOGE/BNB, None if unmappable
    market_id: str | None           # polymarket condition_id / market hash (None for binance)
    asset_id: str | None            # polymarket token id (None for binance / when absent)
    price: float | None             # poly yes-price / binance spot
    best_bid: float | None          # polymarket book top bid (None otherwise)
    best_ask: float | None          # polymarket book top ask (None otherwise)
    # Outcome side of asset_id, labeled by the orchestrator from discovery.
    # When "NO", the orchestrator normalizes `price` to YES-perspective (1-p).
    outcome: str | None = None      # "YES" | "NO" | None
    # Tagged by the orchestrator's classifier (observation only, not decisions):
    vol_regime: str | None = None   # low_vol | mid_vol | high_vol | unknown
    price_zone: str | None = None   # extreme_low | low | uncertain | high | extreme_high | unknown
    raw: dict[str, Any] = field(default_factory=dict)
