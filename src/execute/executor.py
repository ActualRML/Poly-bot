from datetime import datetime, timezone

from src.data.db import Database
from src.data.snapshot import MarketSnapshot
from src.execute.decision import Action, Decision
from src.execute.labels import format_market
from src.monitor.logger import get_logger


class DryRunExecutor:
    """
    Logs decisions, persists non-SKIP ones to SQLite, never trades.

    Why a dedicated class rather than a function: the live executor will
    have the same `execute(decision, snapshot)` shape, so swapping is
    mechanical. Keeping the dry-run path as the only path until the
    foundation is shaken out is deliberate — the second-most-common bug
    in trading bots is "flipped DRY_RUN by accident."
    """

    def __init__(self, db: Database, *, dry_run: bool = True, market_meta: dict | None = None):
        self.db = db
        self.dry_run = dry_run
        self.market_meta = market_meta
        self.log = get_logger("executor")

    async def execute(self, decision: Decision, snapshot: MarketSnapshot, held: bool = False) -> None:
        if decision.action is Action.SKIP:
            # Don't spam the DB with SKIP rows; trace for debugging only.
            self.log.debug(
                "skip",
                extra={
                    "strategy": decision.strategy,
                    "market": format_market(snapshot.market_id, self.market_meta),
                    "reason": decision.reason,
                },
            )
            return

        # Repeat decisions on a held market drop to DEBUG to cut INFO noise.
        log_fn = self.log.debug if held else self.log.info
        log_fn(
            "decision",
            extra={
                "strategy": decision.strategy,
                "action": decision.action.value,
                "side": decision.side,
                "size_usdc": decision.size_usdc,
                "price": decision.price,
                "market": format_market(decision.market_id or snapshot.market_id, self.market_meta),
            },
        )

        await self.db.execute(
            """
            INSERT INTO decisions
                (ts, strategy, market_id, action, side, size_usdc, price, reason, dry_run)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                decision.strategy,
                decision.market_id or snapshot.market_id,
                decision.action.value,
                decision.side,
                decision.size_usdc,
                decision.price,
                decision.reason,
                1 if self.dry_run else 0,
            ),
        )

        if not self.dry_run:
            # Live trading is intentionally not wired yet. If you flipped
            # DRY_RUN=false expecting orders to fly, this warning is your
            # answer: the live path needs to be built (CLOB auth, order
            # signing, fill confirmation) before this branch does anything.
            self.log.warning(
                "live trading not implemented; decision logged only",
                extra={"strategy": decision.strategy},
            )
