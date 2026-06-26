import asyncio
from contextlib import suppress

from src.data.db import Database
from src.data.snapshot import MarketSnapshot
from src.monitor.logger import get_logger

_INSERT_SQL = """
    INSERT INTO snapshots
        (ts, source, event_type, symbol, market_id, asset_id, price, best_bid, best_ask,
         bid_size, ask_size, bid_depth, ask_depth, vol_regime, price_zone)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# Sample the high-frequency floods to ~1 row per key per interval; book and
# last_trade_price bypass (sparse, kept whole for backtest fidelity).
_THROTTLED_EVENTS = {"price_change", "ticker"}
_SAMPLE_INTERVAL_S = 10.0
# Markets we currently hold a position in get a finer sample interval so the
# intra-trade price path is dense enough for MFE/MAE analysis afterwards.
_PRIORITY_SAMPLE_INTERVAL_S = 1.0


class SnapshotWriter:
    """
    Buffers MarketSnapshot rows and flushes them to SQLite in batches.

    At thousands of msgs/min a per-message commit can't keep up, so `add()`
    only appends to an in-memory list (instant — never blocks the WS receive
    loop). A background `run()` loop flushes via executemany() + single commit
    either every `batch_size` rows or every `flush_interval_s` seconds,
    whichever comes first.
    """

    def __init__(
        self,
        db: Database,
        *,
        batch_size: int = 100,
        flush_interval_s: float = 5.0,
    ):
        self.db = db
        self.batch_size = batch_size
        self.flush_interval_s = flush_interval_s
        self._buf: list[tuple] = []
        self._last_ts: dict[str, float] = {}
        # market_ids of currently-open positions; refreshed by the orchestrator.
        # Snapshots for these are sampled finer (see add()). Empty => no change
        # from the plain throttle, so default behaviour is untouched.
        self.priority_markets: set[str] = set()
        self._stop = asyncio.Event()
        self._flush_now = asyncio.Event()
        self.log = get_logger("storage")
        # Rolling counters; the 60s summary reads + diffs these.
        self.rows_written = 0
        self.batches = 0

    def add(self, snapshot: MarketSnapshot) -> None:
        """Non-blocking enqueue from the dispatch path. Flood event types are
        sampled to ~1 row per key per _SAMPLE_INTERVAL_S; book and
        last_trade_price always pass through."""
        if snapshot.event_type in _THROTTLED_EVENTS:
            key = snapshot.asset_id or snapshot.symbol
            if key is not None:
                # Markets we hold are sampled finer for intra-trade analysis.
                interval = (
                    _PRIORITY_SAMPLE_INTERVAL_S
                    if snapshot.market_id and snapshot.market_id in self.priority_markets
                    else _SAMPLE_INTERVAL_S
                )
                now = snapshot.ts.timestamp()
                last = self._last_ts.get(key)
                if last is not None and now - last < interval:
                    return
                self._last_ts[key] = now
        self._buf.append(self._to_row(snapshot))
        if len(self._buf) >= self.batch_size:
            self._flush_now.set()

    @staticmethod
    def _to_row(s: MarketSnapshot) -> tuple:
        return (
            s.ts.isoformat(),
            s.source,
            s.event_type,
            s.symbol,
            s.market_id,
            s.asset_id,
            s.price,
            s.best_bid,
            s.best_ask,
            s.bid_size,
            s.ask_size,
            s.bid_depth,
            s.ask_depth,
            s.vol_regime,
            s.price_zone,
        )

    async def _flush(self) -> None:
        if not self._buf:
            return
        batch, self._buf = self._buf, []
        await self.db.executemany(_INSERT_SQL, batch)
        self.rows_written += len(batch)
        self.batches += 1

    async def run(self) -> None:
        while not self._stop.is_set():
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._flush_now.wait(), timeout=self.flush_interval_s)
            self._flush_now.clear()
            try:
                await self._flush()
            except Exception as e:
                self.log.exception("snapshot flush failed", extra={"error": str(e)})

    async def stop(self) -> None:
        """Final flush so the last partial batch isn't lost on shutdown."""
        self._stop.set()
        with suppress(Exception):
            await self._flush()
