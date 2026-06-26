"""Replay loop + simulated portfolio for the snapshot-replay backtest.

Replays every stored Polymarket snapshot (in ts order) through the REAL
``strategy.evaluate()`` and a pure, synchronous ``SimPortfolio``, then settles
each opened position against the offline-recovered outcome.

Two correctness invariants the live bot gets for free but a replay must enforce:

* **No look-ahead.** After a market settles the WS keeps streaming its dead token
  at ~0/1 for ~14 min. Feeding those to a strategy would look like a screaming
  high/low-zone signal on a known result. The loop drops every snapshot with
  ``ts >= resolve_ts`` so strategies only ever see the genuine pre-settlement book.
* **Simulated time.** ``SimPortfolio`` never reads the wall clock; "now" is the
  snapshot's ts. The production ``Portfolio.open_position`` time-to-resolve gate is
  wall-clock-bound and would reject every (year-old) market, so it can't be reused
  directly — the gate is re-implemented here against ``snap.ts``.

PnL math and the global gate constants are taken from
``src/execute/portfolio.py`` so they can't drift; the only deliberate departure is
**slippage is a parameter (default ~1 cent)** rather than the module's fixed 0.03 —
that recalibration is the whole point of Phase 1.

✅ **REALISTIC FILL (2026-06-16).** ``SimPortfolio.open`` now fills through the
SAME ``simulate_taker_fill`` the live path uses: it lifts the captured **ask**
and walks real depth, capped by the strategy's ``entry_ceiling``, and SKIPS when
there's no usable quote (``nofill``) or the cached book is staler than
``MAX_BOOK_AGE_SEC`` — byte-identical execution cost to the live ledger, so a
flat-slippage mirage (e.g. the contrarian_hv backtest +$879 that died 0/13 live)
now shows up *offline*. Two ingredients the old engine lacked, both built here:

* **per-market book cache** — only ``book`` events carry quotes; the replay
  reflects each into YES-perspective (``yes_book_from_token``) and caches it, so a
  decision fires against the freshest book exactly as live does.
* **per-token (YES/NO) normalization** — the snapshots table doesn't store which
  token ticked, so ``_build_outcome_map`` recovers it per ``asset_id`` from the
  data itself (a book row's YES-normalized ``price`` equals its raw ``best_bid``
  for a YES token, ``1 - best_bid`` for NO; majority vote, 0.5 rows abstain).
  Validated: 0 mixed / 0 ambiguous over 2,147 asset_ids.

The ``slippage`` param survives only as an OPTIONAL fill-stress knob (extra
adverse cents ON TOP of the realistic fill); default ``0.0`` = the honest fill.
For speed the replay reads only ``book``/``last_trade_price`` rows (decisions +
labels) — build the slim DB with ``scripts/make_bt_db.py``.
"""
import asyncio
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.backtest.recovery import MarketResolution, RecoveryResult, recover_resolutions
from src.data.snapshot import MarketSnapshot
from src.execute.decision import Action, Decision
from src.execute.fill import YesBook, simulate_taker_fill, yes_book_from_token
from src.execute.portfolio import (
    MAX_BOOK_AGE_SEC,
    MIN_BET_USDC,
    MIN_TIME_TO_RESOLVE_SEC,
)
from src.main import _load_strategies
from src.monitor.logger import get_logger
from src.strategy.params import StrategyParams

# Realistic fill already prices execution at the ask+walk, so the default score
# adds NOTHING on top (0.0). A small positive value stress-tests "the fill came in
# a cent worse than the captured book" — kept as an optional sensitivity knob.
DEFAULT_SLIPPAGE = 0.0
DEFAULT_SLIPPAGES = (0.0, 0.01)
STARTING_BALANCE = 1000.0


@dataclass
class SimPosition:
    """One simulated trade: opened during replay, scored at settle.

    ``entry_price`` is the realistic effective fill (ask + depth walk), ``size_usdc``
    the dollars actually DEPLOYED (< intended on a depth-capped partial), ``shares``
    the units bought. ``fill_flag`` records how it filled (ok/walk/partial)."""
    market_id: str
    symbol: str | None
    side: str
    entry_price: float                # effective taker fill (avg ask+walk), NOT the mid
    size_usdc: float                  # dollars actually filled (may be < intended)
    strategy: str
    opened_ts: datetime
    shares: float = 0.0               # units bought = size_usdc / entry_price
    intended_size_usdc: float = 0.0   # stake before depth capped it (bet_fraction*bal)
    fill_flag: str = "ok"             # ok | walk | partial (nofill -> never opened)
    price_zone: str | None = None     # zone at entry — for per-zone reporting
    vol_regime: str | None = None     # vol regime at entry — for per-regime reporting
    decision_price: float | None = None  # held-side MID at trigger (decision.price); the
                                         # fill-INDEPENDENT edge uses WR - avg(decision_price),
                                         # whereas entry_price above is the realistic ask+walk FILL
    won: bool | None = None           # filled on settle
    pnl_usdc: float | None = None     # filled on settle


@dataclass
class BacktestResult:
    slippage: float
    starting_balance: float
    final_balance: float
    settled: list[SimPosition]
    skipped: dict[str, int]           # gate reason -> count of blocked opens
    strategies: list[str]
    recovery: RecoveryResult


def win_pnl(entry: float, size: float, slippage: float) -> float:
    """Profit on a WON position. Mirrors ``Portfolio.resolve_position`` exactly:
    a worse effective fill than the logged entry (slippage), payout = size/eff."""
    eff = min(entry + slippage, 1.0)
    payout = size / eff if eff else 0.0
    return payout - size


class SimPortfolio:
    """Pure, synchronous dry-run bankroll for replay — no DB, no wall clock.

    Opening a position is slippage-independent (settlement happens after the
    whole stream, so stake sizing only ever sees earlier opens, never payouts);
    settlement is therefore split into a separate, slippage-parametrised pass so
    one replay can be scored at several slippages without re-replaying.
    """

    def __init__(self, strategy_params: dict[str, StrategyParams] | None = None,
                 *, starting_balance: float = STARTING_BALANCE,
                 fixed_bet_usdc: float | None = None):
        self._params = strategy_params or {}
        self.starting_balance = starting_balance
        self.balance = starting_balance          # decremented by stakes on open
        # When set, every trade stakes this FIXED amount instead of
        # balance*bet_fraction. For a sizing-NEUTRAL strategy comparison: it (a)
        # decouples trades (a losing run can't starve later ones via a shrinking
        # balance) and (b) keeps the order small enough to fill against real
        # Polymarket depth instead of walking it into all-`partial` fills (which a
        # huge balance*2% stake does, distorting avg-ROI pessimistically).
        self._fixed_bet = fixed_bet_usdc
        self._held: set[str] = set()
        self.open_positions: list[SimPosition] = []
        self.skipped: Counter[str] = Counter()   # gate reason -> count

    def _params_for(self, strategy: str) -> StrategyParams:
        return self._params.get(strategy, StrategyParams())

    def is_held(self, market_id: str | None) -> bool:
        return market_id in self._held

    def open(
        self,
        decision: Decision,
        snap: MarketSnapshot,
        resolve_ts: datetime,
        cached_book: tuple[YesBook, datetime] | None = None,
    ) -> bool:
        """Apply the same gates as ``Portfolio.open_position`` (per-strategy floor
        + sizing, global dust + time-to-resolve), using sim time = ``snap.ts``,
        THEN fill realistically against ``cached_book`` exactly as the live path:
        lift the ask + walk depth, limit-priced by ``entry_ceiling``; SKIP on a
        stale book (> ``MAX_BOOK_AGE_SEC``) or no usable quote (``nofill``).

        ``cached_book`` is ``(YES-perspective book, ts it was captured)`` — the
        freshest ``book`` event seen for this market during replay, or ``None`` if
        none yet."""
        if self.is_held(decision.market_id):
            return False
        params = self._params_for(decision.strategy)

        secs_left = (resolve_ts - snap.ts).total_seconds()
        if secs_left < MIN_TIME_TO_RESOLVE_SEC:
            self.skipped["too_close_to_resolution"] += 1
            return False
        if decision.price is None or decision.price < params.entry_floor:
            self.skipped["below_entry_floor"] += 1
            return False
        intended = self._fixed_bet if self._fixed_bet is not None else self.balance * params.bet_fraction
        if intended < MIN_BET_USDC or self.balance <= 0:
            self.skipped["insufficient_balance"] += 1
            return False

        # --- realistic fill (the whole point of this engine) -------------------
        if cached_book is None:
            self.skipped["no_book"] += 1
            return False
        book, book_ts = cached_book
        if (snap.ts - book_ts).total_seconds() > MAX_BOOK_AGE_SEC:
            self.skipped["stale_book"] += 1     # same gate as live open_position
            return False
        fill = simulate_taker_fill(decision.side, intended, book, max_price=params.entry_ceiling)
        if fill.flag == "nofill" or fill.shares <= 0 or fill.filled_usdc <= 0:
            self.skipped["nofill"] += 1         # no quote / priced out by entry_ceiling
            return False

        self.balance -= fill.filled_usdc
        self.open_positions.append(
            SimPosition(
                market_id=decision.market_id,
                symbol=snap.symbol,
                side=decision.side,
                entry_price=fill.avg_price,     # effective ask+walk fill, not the mid
                size_usdc=fill.filled_usdc,     # dollars actually deployed
                strategy=decision.strategy,
                opened_ts=snap.ts,
                shares=fill.shares,
                intended_size_usdc=intended,
                fill_flag=fill.flag,
                price_zone=snap.price_zone,
                vol_regime=snap.vol_regime,
                decision_price=decision.price,
            )
        )
        self._held.add(decision.market_id)
        return True

    def settle(self, resolutions: dict[str, MarketResolution], slippage: float) -> tuple[list[SimPosition], float]:
        """Score every open position at ``slippage`` against recovered outcomes.
        Returns ``(settled_positions, final_balance)``; does not mutate self
        (so it can be called once per slippage on the same replay)."""
        balance = self.balance
        settled: list[SimPosition] = []
        for p in self.open_positions:
            won = p.side == resolutions[p.market_id].outcome
            if won:
                pnl = win_pnl(p.entry_price, p.size_usdc, slippage)
                payout = p.size_usdc + pnl
            else:
                pnl = -p.size_usdc
                payout = 0.0
            balance += payout
            settled.append(
                SimPosition(
                    market_id=p.market_id, symbol=p.symbol, side=p.side,
                    entry_price=p.entry_price, size_usdc=p.size_usdc,
                    strategy=p.strategy, opened_ts=p.opened_ts, shares=p.shares,
                    intended_size_usdc=p.intended_size_usdc, fill_flag=p.fill_flag,
                    price_zone=p.price_zone, vol_regime=p.vol_regime,
                    decision_price=p.decision_price, won=won, pnl_usdc=pnl,
                )
            )
        return settled, balance


def _reconstruct(row: sqlite3.Row) -> MarketSnapshot:
    """Rebuild the dispatched MarketSnapshot from a stored row. The stored
    ``price`` is already YES-normalized and ``vol_regime``/``price_zone`` are the
    exact values the live bot tagged, so no re-parsing/re-classifying is needed.
    ``outcome`` is set to "YES" — strategies use it only as a non-None gate; the
    traded side is derived from ``price_zone``, not the outcome value."""
    return MarketSnapshot(
        ts=datetime.fromisoformat(row["ts"]),
        source="polymarket",
        event_type=row["event_type"],
        symbol=row["symbol"],
        market_id=row["market_id"],
        asset_id=row["asset_id"],
        price=row["price"],
        best_bid=row["best_bid"],
        best_ask=row["best_ask"],
        outcome="YES",
        vol_regime=row["vol_regime"],
        price_zone=row["price_zone"],
    )


# Replay EVERY priced poly event — book, price_change AND last_trade_price. This is
# not optional: the live strategy triggers on whichever event first carries an
# extreme zone, and that is very often a price_change (a deep level prints the
# extreme before top-of-book does). Dropping price_change made the replay catch a
# LATER/opposite extreme on markets that touched both — 29/115 wrong side, 11% vs
# 31% live WR (verified 2026-06-16). Only book events carry quotes, so only they
# update the fill cache; price_change/last_trade trigger, then fill against that cache.
_REPLAY_SQL = """
    SELECT ts, event_type, symbol, market_id, asset_id, price, best_bid, best_ask,
           bid_size, ask_size, bid_depth, ask_depth, vol_regime, price_zone
      FROM snapshots
     WHERE source = 'polymarket' AND market_id IS NOT NULL AND price IS NOT NULL
       AND event_type IN ('book', 'price_change', 'last_trade_price')
     ORDER BY ts
"""

_OUTCOME_SQL = """
    SELECT asset_id, price, best_bid, best_ask
      FROM snapshots
     WHERE source = 'polymarket' AND event_type = 'book'
       AND price IS NOT NULL AND asset_id IS NOT NULL
"""


def _build_outcome_map(conn: sqlite3.Connection) -> dict[str, str]:
    """Recover ``asset_id -> "YES"/"NO"`` from the data itself (the snapshots table
    never stored which token ticked, yet the fill must reflect the raw per-token
    book into YES terms). A ``book`` row's stored YES-normalized ``price`` equals
    its raw ``best_bid`` for a YES token and ``1 - best_bid`` for a NO token, so we
    vote per asset_id over all its book rows. A reference quote of exactly 0.5 is
    ambiguous (bid == 1-bid) and abstains. Verified on the live DB: 0 mixed /
    0 ambiguous over 2,147 asset_ids."""
    votes: dict[str, list[int]] = {}        # asset_id -> [yes_votes, no_votes]
    for r in conn.execute(_OUTCOME_SQL):
        ref = r["best_bid"] if r["best_bid"] is not None else r["best_ask"]
        if ref is None or abs(ref - 0.5) < 1e-9:
            continue
        p = r["price"]
        v = votes.setdefault(r["asset_id"], [0, 0])
        if abs(p - ref) < 1e-6:
            v[0] += 1
        elif abs(p - (1.0 - ref)) < 1e-6:
            v[1] += 1
    return {aid: ("YES" if y >= n else "NO") for aid, (y, n) in votes.items()}


async def _replay(db_path: Path, strategies, sim: SimPortfolio,
                  resolutions: dict[str, MarketResolution],
                  outcome_map: dict[str, str]) -> int:
    """Stream poly book/last_trade snapshots in ts order through the strategies;
    open on BUY against the freshest cached book. Returns the number of
    price-bearing snapshots actually dispatched.

    Mirrors the live orchestrator's order: cache the YES-perspective book from a
    ``book`` event BEFORE dispatching it, so a same-tick decision fills against the
    book it just saw (age 0), and a later last_trade decision fills against the
    last cached book (and is stale-gated if that book is too old)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    latest_book: dict[str, tuple[YesBook, datetime]] = {}
    dispatched = 0
    try:
        for row in conn.execute(_REPLAY_SQL):
            resol = resolutions.get(row["market_id"])
            if resol is None:
                continue                          # market not scoreable — skip
            snap = _reconstruct(row)
            # Give the snapshot the same resolve_time the live orchestrator copies
            # from market_meta, so a time-aware strategy (contrarian's reversion-
            # runway gate) sees identical input in replay and live — recovery already
            # knows resolve_ts, so the gate is no longer silently inert offline.
            snap.resolve_time = resol.resolve_ts
            if snap.ts >= resol.resolve_ts:
                continue                          # look-ahead guard
            if row["event_type"] == "book":
                outcome = outcome_map.get(row["asset_id"] or "")
                if outcome in ("YES", "NO") and (
                    row["best_bid"] is not None or row["best_ask"] is not None
                ):
                    latest_book[row["market_id"]] = (
                        yes_book_from_token(
                            outcome, row["best_bid"], row["best_ask"],
                            row["bid_size"], row["ask_size"],
                            row["bid_depth"], row["ask_depth"],
                        ),
                        snap.ts,
                    )
            dispatched += 1
            for strat in strategies:
                decision = await strat.evaluate(snap)
                if decision.action is Action.BUY:
                    sim.open(decision, snap, resol.resolve_ts,
                             latest_book.get(decision.market_id))
    finally:
        conn.close()
    return dispatched


def run_backtest(
    db_path: Path,
    strategy_names: list[str],
    slippages: tuple[float, ...] | list[float] = DEFAULT_SLIPPAGES,
    *,
    starting_balance: float = STARTING_BALANCE,
    fixed_bet_usdc: float | None = None,
    recovery: RecoveryResult | None = None,
) -> list[BacktestResult]:
    """Replay ``db_path`` through ``strategy_names`` once and score the resulting
    trades at each slippage in ``slippages``. Returns one BacktestResult per
    slippage (sharing the same opened trades + recovery).

    ``fixed_bet_usdc`` (optional): stake a flat amount per trade instead of
    balance*bet_fraction — a sizing-neutral mode for comparing strategies' avg-ROI
    without bankroll coupling or oversized-stake depth-walk distortion."""
    log = get_logger("backtest.engine")
    if recovery is None:
        recovery = recover_resolutions(db_path)
    strategies = _load_strategies(strategy_names)
    params = {s.name: s.params for s in strategies}

    # Recover token polarity once (needed to reflect each raw book into YES terms).
    omap_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    omap_conn.row_factory = sqlite3.Row
    try:
        outcome_map = _build_outcome_map(omap_conn)
    finally:
        omap_conn.close()

    sim = SimPortfolio(params, starting_balance=starting_balance, fixed_bet_usdc=fixed_bet_usdc)
    dispatched = asyncio.run(_replay(db_path, strategies, sim, recovery.usable, outcome_map))
    log.info(
        "replay: %d snapshots dispatched, %d positions opened across %d markets "
        "(%d tokens polarity-mapped); fill skips: %s",
        dispatched, len(sim.open_positions), len(recovery.usable),
        len(outcome_map), dict(sim.skipped),
    )

    results: list[BacktestResult] = []
    for slip in slippages:
        settled, final_balance = sim.settle(recovery.usable, slip)
        results.append(
            BacktestResult(
                slippage=slip,
                starting_balance=starting_balance,
                final_balance=final_balance,
                settled=settled,
                skipped=dict(sim.skipped),
                strategies=[s.name for s in strategies],
                recovery=recovery,
            )
        )
    return results
