from datetime import datetime, timezone

from src.data.db import Database
from src.execute.fill import simulate_taker_fill, simulate_taker_sell
from src.execute.labels import format_market
from src.monitor.logger import get_logger
from src.strategy.params import StrategyParams

# GLOBAL safety limits — NOT per-strategy. A strategy can never loosen these
# (they're deliberately absent from StrategyParams). Entry floor + bet sizing
# ARE per-strategy and live in StrategyParams, whose defaults are the single
# source of truth for what used to be MIN_ENTRY_PRICE / BET_FRACTION here.
MIN_BET_USDC = 1.0   # below this is dust — don't open
MIN_TIME_TO_RESOLVE_SEC = 120  # don't open if market resolves in under 2 min (or already past)
# A decision can fire on a price_change event (which carries no book of its own),
# so we fill against the freshest CACHED book. If that book is older than this, it
# no longer reflects the market the signal saw: on illiquid markets (e.g. BNB) the
# price_change feed flickers to extremes while the last real book sits minutes
# stale, which produced fictional walked fills (entry ~0.7 off a stale ~0.5 book).
# No fresh book within this window => skip rather than fill against fiction.
MAX_BOOK_AGE_SEC = 30

# DEPRECATED. The old flat slippage fudge. Execution cost is now modelled
# HONESTLY at entry: open_position records the realistic taker fill (ask + depth
# walk, src/execute/fill.py), so resolve_position adds NO extra buffer — stacking
# one on top would double-count the spread. Kept at 0.0 only so legacy importers
# (a historical research script) don't break; not used by the pnl math anymore.
SLIPPAGE_BUFFER = 0.0

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

    async def is_held(self, market_id, strategy) -> bool:
        """True if THIS strategy already holds an open position on the market.
        Per-STRATEGY, not per-market: independent strategies can each hold the same
        market without blocking one another — e.g. contrarian (longshot side) and
        the momentum canary (favorite side) co-existing on one extreme market. At
        most one open row per (market_id, strategy)."""
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM positions "
            "WHERE market_id = ? AND status = 'open' AND strategy = ?",
            (market_id, strategy),
        )
        return bool(row and row["n"] > 0)

    async def get_open(self, market_id, strategy=None) -> dict | None:
        """The single open position on this market (or None). Unlike ``is_held``
        (a bool), this returns the held SIDE + entry the stop-loss trigger needs to
        mark the position to market. Pass ``strategy`` to disambiguate now that a
        market may hold one position PER strategy (contrarian + momentum canary):
        the exit overlays scope to their own strategy so they never close another
        strategy's side. ``strategy=None`` returns any one open row (legacy callers)."""
        if strategy is None:
            row = await self.db.fetchone(
                "SELECT * FROM positions WHERE market_id = ? AND status = 'open' LIMIT 1",
                (market_id,),
            )
        else:
            row = await self.db.fetchone(
                "SELECT * FROM positions WHERE market_id = ? AND status = 'open' "
                "AND strategy = ? LIMIT 1",
                (market_id, strategy),
            )
        return dict(row) if row else None

    async def open_position(self, decision, snapshot, book=None, book_ts=None) -> bool:
        """Open a simulated position at a REALISTIC taker fill.

        ``book`` is the freshest YES-perspective top-of-book (``fill.YesBook``)
        for this market, supplied by the orchestrator, captured at ``book_ts``
        (the book event's timestamp). We size the order off the bankroll, then ask
        ``simulate_taker_fill`` what that order actually costs against the real
        book (lift the ask, walk depth). The recorded ``entry_price`` is that
        effective fill — NOT the YES mid the strategy saw, which a taker can never
        get — and ``size_usdc`` is the dollars actually filled (a partial when
        depth runs out). No book / no quote / a STALE book (older than
        ``MAX_BOOK_AGE_SEC`` vs the decision) => no fill => skip (this blocks both
        fictional wick entries and stale-book walked fills)."""
        # Safety net — main already checks is_held, but guard the insert path too.
        # Per-strategy: blocks only a SECOND open of the SAME strategy on this
        # market; a different strategy may legitimately hold the other side.
        if await self.is_held(decision.market_id, decision.strategy):
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
        ceiling = params.entry_ceiling

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
        intended_size = balance * frac
        if intended_size < MIN_BET_USDC or balance <= 0:
            self.log.warning(
                "skip open: insufficient balance",
                extra={"balance": balance, "size": intended_size},
            )
            return False

        # Reject a STALE book. The cached book may predate the decision (a
        # price_change fired with no book of its own); if it is too old it no
        # longer reflects the market the signal saw — filling against it fabricates
        # an entry (see MAX_BOOK_AGE_SEC). Only enforce when we know both times;
        # an absent book_ts (e.g. unit tests) leaves the gate inert.
        if book_ts is not None and snapshot.ts is not None:
            book_age = (snapshot.ts - book_ts).total_seconds()
            if book_age > MAX_BOOK_AGE_SEC:
                self.log.info(
                    "skip open: stale book",
                    extra={
                        "market": format_market(decision.market_id, self.market_meta),
                        "book_age_sec": int(book_age),
                        "max_age_sec": MAX_BOOK_AGE_SEC,
                        "signal_price": decision.price,
                    },
                )
                return False

        # What does this order ACTUALLY cost as a taker? Lift the ask and walk the
        # captured depth, but never past the strategy's entry_ceiling (the walk is
        # limit-priced, so the effective fill can't climb into expensive shares).
        # decision.price (the YES mid the strategy saw) is the SIGNAL, never the
        # fill — a taker can't buy at the mid.
        fill = simulate_taker_fill(decision.side, intended_size, book, max_price=ceiling)
        if fill.flag == "nofill":
            self.log.info(
                "skip open: no fill available",
                extra={
                    "market": format_market(decision.market_id, self.market_meta),
                    "side": decision.side,
                    "signal_price": decision.price,
                    "entry_ceiling": ceiling,
                },
            )
            return False
        if fill.filled_usdc < MIN_BET_USDC:
            self.log.info(
                "skip open: fillable size below dust after depth walk",
                extra={
                    "market": format_market(decision.market_id, self.market_meta),
                    "filled_usdc": fill.filled_usdc,
                    "intended_usdc": intended_size,
                },
            )
            return False

        entry_price = fill.avg_price
        size = fill.filled_usdc

        await self.db.execute(
            "UPDATE balance SET balance_usdc = balance_usdc - ? WHERE id = 1",
            (size,),
        )
        await self.db.execute(
            """
            INSERT INTO positions
                (ts, market_id, symbol, side, entry_price, size_usdc, status,
                 resolve_time, strategy, fill_flag, intended_size_usdc)
            VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
            """,
            (
                snapshot.ts.isoformat(),
                decision.market_id,
                snapshot.symbol,
                decision.side,
                entry_price,
                size,
                rt.isoformat() if rt else None,
                decision.strategy,
                fill.flag,
                intended_size,
            ),
        )
        self.log.info(
            "opened position",
            extra={
                "market": format_market(decision.market_id, self.market_meta),
                "side": decision.side,
                "signal_price": decision.price,
                "entry_price": entry_price,
                "size_usdc": size,
                "fill": fill.flag,
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
            # entry_price is ALREADY the realistic taker fill (ask + depth walk,
            # set in open_position), so no extra slippage buffer here — that cost
            # is paid once, at entry. Hold-to-resolution: each share redeems at $1.
            payout = size / entry if entry else 0.0
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

    async def close_position(self, position_id, book, reason: str = "time_gated_sl") -> bool:
        """Exit a still-open position EARLY by selling the held side into ``book``
        (the time-gated stop-loss canary). A taker SELL hits the bid and walks bid
        depth (``simulate_taker_sell``) — the honest exit mirror of the open path,
        so live and the research that validated the rule share one cost model.

        Returns True only if the position was actually closed. We close ONLY on a
        FULL fill: a ``no_exit`` (empty/one-sided bid — can't sell) or a depth-
        exhausted ``partial`` leaves the position OPEN to resolve normally. That is
        deliberately conservative — live we don't yet know the eventual settlement
        of any unsold shares (the market hasn't resolved), and full-fill-or-hold
        avoids both partial-position bookkeeping and amplifying the rare killed
        winner (the search killed only 1 of 55 stops). Mirrors resolve_position's
        two-UPDATE bookkeeping and void_position's still-'open' guard so a double
        call can't double-credit."""
        row = await self.db.fetchone(
            "SELECT size_usdc, entry_price, side, market_id FROM positions "
            "WHERE id = ? AND status = 'open'",
            (position_id,),
        )
        if row is None:
            self.log.warning("close: position not open, skipped", extra={"position_id": position_id})
            return False

        size = float(row["size_usdc"])
        entry = float(row["entry_price"])
        side = row["side"]
        # Shares held = stake / effective entry — the SAME basis resolve_position
        # uses for its winner payout, so a close and a hold value the same lot.
        shares = size / entry if entry else 0.0
        if shares <= 0:
            return False

        sell = simulate_taker_sell(side, shares, book)
        if sell.flag in ("no_exit", "partial"):
            # Can't (fully) sell — hold to resolution; the resolver settles it.
            self.log.info(
                "stop-loss skipped: no full exit",
                extra={
                    "position_id": position_id,
                    "market": format_market(row["market_id"], self.market_meta),
                    "side": side,
                    "sell_flag": sell.flag,
                },
            )
            return False

        proceeds = sell.proceeds
        pnl = proceeds - size
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            """
            UPDATE positions
               SET status = 'closed', exit_price = ?, pnl_usdc = ?, resolved_ts = ?,
                   closed_reason = ?
             WHERE id = ? AND status = 'open'
            """,
            (sell.avg_price, pnl, now, reason, position_id),
        )
        await self.db.execute(
            "UPDATE balance SET balance_usdc = balance_usdc + ? WHERE id = 1",
            (proceeds,),
        )
        self.log.info(
            "stop-loss closed position",
            extra={
                "position_id": position_id,
                "market": format_market(row["market_id"], self.market_meta),
                "side": side,
                "exit_price": sell.avg_price,
                "proceeds": proceeds,
                "pnl_usdc": pnl,
                "reason": reason,
            },
        )
        return True
