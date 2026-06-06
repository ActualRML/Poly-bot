from datetime import datetime, timezone

from src.data.db import Database
from src.execute.labels import format_market
from src.monitor.logger import get_logger
from src.strategy.params import StrategyParams

# GLOBAL safety limits — NOT per-strategy. A strategy can never loosen these
# (they're deliberately absent from StrategyParams). Entry floor + bet sizing
# ARE per-strategy and live in StrategyParams, whose defaults are the single
# source of truth for what used to be MIN_ENTRY_PRICE / BET_FRACTION here.
MIN_BET_USDC = 1.0   # below this is dust — don't open
SLIPPAGE_BUFFER = 0.03  # assume effective fill price is worse than logged entry
MIN_TIME_TO_RESOLVE_SEC = 120  # don't open if market resolves in under 2 min (or already past)

# Fallback when a decision's strategy isn't in the registry (unknown /
# unregistered) — the conservative house defaults.
DEFAULT_PARAMS = StrategyParams()


class Portfolio:
    """Simulated (dry-run) bankroll + position bookkeeping. No real orders."""

    def __init__(
        self,
        db: Database,
        market_meta: dict | None = None,
        strategy_params: dict[str, StrategyParams] | None = None,
    ):
        self.db = db
        self.market_meta = market_meta
        # name -> per-strategy knobs; an empty registry => everything uses defaults.
        self._strategy_params = strategy_params or {}
        self.log = get_logger("portfolio")

    def _params_for(self, strategy: str) -> StrategyParams:
        """Per-strategy entry floor + bet sizing. An unregistered or unknown
        strategy name falls back to conservative house defaults. Strategies can
        only set these two knobs — the GLOBAL safety limits (dust, slippage,
        time-to-resolve) are module constants and are never overridable."""
        return self._strategy_params.get(strategy, DEFAULT_PARAMS)

    async def get_balance(self) -> float:
        row = await self.db.fetchone("SELECT balance_usdc FROM balance WHERE id = 1")
        return float(row["balance_usdc"]) if row else 0.0

    async def is_held(self, market_id) -> bool:
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM positions WHERE market_id = ? AND status = 'open'",
            (market_id,),
        )
        return bool(row and row["n"] > 0)

    async def open_position(self, decision, snapshot) -> bool:
        # Safety net — main already checks is_held, but guard the insert path too.
        if await self.is_held(decision.market_id):
            self.log.debug(
                "position already open for market",
                extra={"market_id": decision.market_id},
            )
            return False

        # Per-strategy knobs: entry floor + bet sizing. An unknown / unregistered
        # strategy resolves to house defaults. The global limits below (timing,
        # dust) stay non-overridable.
        params = self._params_for(decision.strategy)
        floor = params.entry_floor
        frac = params.bet_fraction

        meta = (self.market_meta or {}).get(decision.market_id) or {}
        rt = meta.get("resolve_time")
        if rt is not None:
            secs_left = (rt - datetime.now(timezone.utc)).total_seconds()
            if secs_left < MIN_TIME_TO_RESOLVE_SEC:
                self.log.info(
                    "skip open: too close to resolution",
                    extra={
                        "market": format_market(decision.market_id, self.market_meta),
                        "secs_left": int(secs_left),
                    },
                )
                return False

        # At extreme-low prices the book is too thin to fill our size, and
        # theoretical payout (size/entry) explodes unrealistically.
        if decision.price is None or decision.price < floor:
            self.log.info(
                "skip open: entry price below realism floor",
                extra={
                    "market": format_market(decision.market_id, self.market_meta),
                    "entry_price": decision.price,
                    "floor": floor,
                },
            )
            return False

        balance = await self.get_balance()
        size = balance * frac
        if size < MIN_BET_USDC or balance <= 0:
            self.log.warning(
                "skip open: insufficient balance",
                extra={"balance": balance, "size": size},
            )
            return False

        await self.db.execute(
            "UPDATE balance SET balance_usdc = balance_usdc - ? WHERE id = 1",
            (size,),
        )
        await self.db.execute(
            """
            INSERT INTO positions
                (ts, market_id, symbol, side, entry_price, size_usdc, status, resolve_time, strategy)
            VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)
            """,
            (
                snapshot.ts.isoformat(),
                decision.market_id,
                snapshot.symbol,
                decision.side,
                decision.price,
                size,
                rt.isoformat() if rt else None,
                decision.strategy,
            ),
        )
        self.log.info(
            "opened position",
            extra={
                "market": format_market(decision.market_id, self.market_meta),
                "side": decision.side,
                "entry_price": decision.price,
                "size_usdc": size,
            },
        )
        return True

    async def list_open(self) -> list[dict]:
        rows = await self.db.fetchall("SELECT * FROM positions WHERE status = 'open'")
        return [dict(r) for r in rows]

    async def resolve_position(self, position_id, won: bool, exit_price: float) -> None:
        row = await self.db.fetchone(
            "SELECT size_usdc, entry_price, market_id FROM positions WHERE id = ?",
            (position_id,),
        )
        if row is None:
            self.log.warning("resolve: position not found", extra={"position_id": position_id})
            return

        size = float(row["size_usdc"])
        entry = float(row["entry_price"])
        if won:
            # Assume a worse effective fill than the logged entry (slippage).
            effective_entry = min(entry + SLIPPAGE_BUFFER, 1.0)
            payout = size / effective_entry if effective_entry else 0.0
            pnl = payout - size
        else:
            payout = 0.0
            pnl = -size

        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            """
            UPDATE positions
               SET status = 'resolved', exit_price = ?, pnl_usdc = ?, resolved_ts = ?
             WHERE id = ?
            """,
            (exit_price, pnl, now, position_id),
        )
        await self.db.execute(
            "UPDATE balance SET balance_usdc = balance_usdc + ? WHERE id = 1",
            (payout,),
        )
        self.log.info(
            "resolved position",
            extra={
                "position_id": position_id,
                "market": format_market(row["market_id"], self.market_meta),
                "won": won,
                "pnl_usdc": pnl,
                "payout": payout,
            },
        )

    async def void_position(self, position_id) -> None:
        """Cancel a position whose market can never resolve (stuck unresolved):
        refund the full stake, pnl=0, status='void'. A CANCEL, not a win/loss.

        Mirrors resolve_position's bookkeeping (two UPDATEs, no transaction
        wrapper — matching the rest of this class). Guarded to act only on a
        still-'open' row: the SELECT bails if it's not open and the UPDATE
        re-checks status='open', so a stray double-call cannot refund twice
        (a second call finds status='void' and returns before any write).
        DRY-RUN tidiness only; the caller (resolver) gates on dry_run."""
        row = await self.db.fetchone(
            "SELECT size_usdc, market_id FROM positions WHERE id = ? AND status = 'open'",
            (position_id,),
        )
        if row is None:
            # Not open (already voided/resolved or missing) — do NOT refund.
            self.log.warning("void: position not open, skipped", extra={"position_id": position_id})
            return

        size = float(row["size_usdc"])
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            """
            UPDATE positions
               SET status = 'void', exit_price = NULL, pnl_usdc = 0, resolved_ts = ?
             WHERE id = ? AND status = 'open'
            """,
            (now, position_id),
        )
        await self.db.execute(
            "UPDATE balance SET balance_usdc = balance_usdc + ? WHERE id = 1",
            (size,),
        )
        self.log.info(
            "voided position",
            extra={
                "position_id": position_id,
                "market": format_market(row["market_id"], self.market_meta),
                "stake_refunded": size,
                "pnl_usdc": 0,
            },
        )
