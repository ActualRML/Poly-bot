import asyncio
import importlib
import signal
import sys
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.api.binance_ws import BinanceWSClient
from src.api.polymarket import PolymarketREST, SYMBOL_HINTS
from src.api.polymarket_ws import PolymarketWSClient
from src.classify.price_zone import ZONE_ORDER, ZoneThresholds, classify_price_zone
from src.classify.volatility import VolatilityClassifier
from src.config import Settings
from src.data.db import Database
from src.data.parsers import parse_binance, parse_polymarket
from src.data.schema import create_tables
from src.data.writer import SnapshotWriter
from src.execute.decision import Action
from src.execute.executor import DryRunExecutor
from src.execute.exits import maybe_slow_rise_exit, maybe_stop_loss
from src.execute.fill import YesBook, yes_book_from_token
from src.execute.portfolio import Portfolio
from src.execute.resolver import resolve_loop
from src.monitor.health import Health, heartbeat
from src.monitor.logger import get_logger, separator, setup_logging
from src.strategy.base import Strategy

BINANCE_SYMBOLS = ["btcusdt", "ethusdt", "solusdt", "xrpusdt", "dogeusdt", "bnbusdt"]

# Fixed display order for the 60s status block (the 6 tracked coins).
STATUS_COINS = tuple(SYMBOL_HINTS)

# Re-discovery prune (Fase B): reconnect-clean the WS to drop tokens whose
# market has resolved. Fires when the live set nears the per-connection limit
# (~250 freezes the feed) OR dead tokens have lingered past PRUNE_MAX_INTERVAL.
PRUNE_THRESHOLD = 150
PRUNE_GRACE = timedelta(minutes=10)
PRUNE_MAX_INTERVAL = timedelta(hours=6)


def _load_strategies(names: list[str]) -> list[Strategy]:
    strategies: list[Strategy] = []
    for name in names:
        module = importlib.import_module(f"src.strategy.{name}")
        plugin_cls = getattr(module, "Plugin", None)
        if plugin_cls is None:
            raise ImportError(
                f"src.strategy.{name} must export a class named 'Plugin' "
                f"(see src/strategy/noop.py for the contract)"
            )
        instance = plugin_cls()
        strategies.append(instance)
    return strategies


async def _discover_polymarket(settings: Settings) -> list[dict]:
    log = get_logger("main")
    try:
        async with PolymarketREST(
            settings.polymarket_gamma_url,
            settings.polymarket_clob_url,
        ) as api:
            markets, mode = await api.discover_updown_markets()
    except Exception as e:
        log.exception("polymarket discovery failed", extra={"error": str(e)})
        return []

    if not markets:
        log.warning(
            "no active Up/Down markets — polymarket WS will not start",
            extra={"gamma_url": settings.polymarket_gamma_url},
        )
        return []

    symbols = sorted({m["symbol"] for m in markets if m.get("symbol")})
    log.info(f"discovered {len(markets)} {mode} markets: {symbols}")
    return markets


def _index_markets(
    markets: list[dict],
    symbol_lookup: dict[str, str],
    outcome_lookup: dict[str, str],
    market_meta: dict[str, dict],
) -> set[str]:
    """Index discovered markets into the shared lookup dicts (in place) and
    return their CLOB token_ids. Shared by startup + re-discovery so the
    indexing logic never drifts."""
    token_ids: set[str] = set()
    for m in markets:
        for tid, side in (m.get("token_outcomes") or {}).items():
            outcome_lookup[tid] = side
        for tid in m.get("token_ids", []):
            token_ids.add(tid)
        sym = m.get("symbol")
        if not sym:
            continue
        cid = m.get("condition_id")
        if cid:
            symbol_lookup[cid] = sym
            dt = None
            if m.get("end_date"):
                dt = datetime.fromisoformat(m["end_date"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            label = f"{sym} {dt.strftime('%H:%MZ')}" if dt else sym
            market_meta[cid] = {
                "label": label,
                "resolve_time": dt,
                "token_ids": list(m.get("token_ids", [])),
            }
        for tid in m.get("token_ids", []):
            symbol_lookup[tid] = sym
    return token_ids


async def _parse_summary(
    stats: dict[str, int],
    latest_yes: dict[str, float],
    latest_spot: dict[str, float],
) -> None:
    """Periodic INFO proof that raw -> MarketSnapshot conversion is working.

    Per-message parsing stays at DEBUG; this 60s rollup shows counts +
    one sample so the operator can confirm symbols are mapped and prices
    populated without the log flooding.
    """
    log = get_logger("parse")
    last = dict(stats)
    while True:
        await asyncio.sleep(60)
        delta = {k: stats[k] - last.get(k, 0) for k in stats}
        last = dict(stats)
        poly = delta["poly_book"] + delta["poly_price_change"] + delta["poly_last_trade_price"] + delta["poly_other"]

        sample = ""
        sample_sym = "BTC" if "BTC" in latest_spot else next(iter(latest_spot), None)
        if sample_sym:
            yes = latest_yes.get(sample_sym)
            yes_s = f"{yes:.4f}" if yes is not None else "n/a"
            spot_s = f"{latest_spot[sample_sym]:.0f}"
            sample = f"  sample {sample_sym} yes={yes_s} spot={spot_s}"

        # Counts live in the message; no extra= so the formatter's key=value
        # tail doesn't duplicate them.
        log.info(
            f"poly={poly} (book={delta['poly_book']} chg={delta['poly_price_change']} "
            f"trade={delta['poly_last_trade_price']})  binance={delta['binance']}{sample}"
        )


async def _storage_summary(writer: SnapshotWriter, db_path: Path) -> None:
    """60s proof that snapshots are landing in SQLite (rows + batches + size)."""
    log = get_logger("storage")
    last_rows, last_batches = 0, 0
    while True:
        await asyncio.sleep(60)
        rows = writer.rows_written - last_rows
        batches = writer.batches - last_batches
        last_rows, last_batches = writer.rows_written, writer.batches
        size_mb = 0.0
        with suppress(OSError):
            size_mb = db_path.stat().st_size / (1024 * 1024)
        log.info(f"wrote {rows} snapshots (batches: {batches})  db={size_mb:.1f}MB")


async def _refresh_priority_markets(
    writer: SnapshotWriter, db: Database, interval: int = 15
) -> None:
    """Keep the writer's fine-sampling set in sync with currently-open positions
    so the price path of markets we hold is captured at higher resolution for
    later MFE/MAE trade analysis. Read-only on the DB; no effect on decisions."""
    log = get_logger("storage")
    while True:
        try:
            rows = await db.fetchall(
                "SELECT DISTINCT market_id FROM positions WHERE status = 'open'"
            )
            writer.priority_markets = {r["market_id"] for r in rows if r["market_id"]}
        except Exception as e:
            log.debug("priority-market refresh failed", extra={"error": str(e)})
        await asyncio.sleep(interval)


# Maps the classifier's regime label to the human-readable vol word shown in the
# status line (the raw float already appears in the regime line above).
_VOL_DISPLAY = {"low_vol": "low", "mid_vol": "mid", "high_vol": "high", "unknown": "unknown"}


def _active_side(rows: list[dict], now: datetime) -> str:
    """The side to surface for a symbol's open positions: prefer the nearest
    running candle (smallest resolve_time still in the future), else the most
    recent past resolve_time, else just the first open row. resolve_time is
    parsed exactly like resolve_loop (fromisoformat + UTC fallback)."""
    if not rows:
        return "-"
    parsed: list[tuple[datetime | None, dict]] = []
    for r in rows:
        rt_raw = r.get("resolve_time")
        rt = datetime.fromisoformat(rt_raw) if rt_raw else None
        if rt is not None and rt.tzinfo is None:
            rt = rt.replace(tzinfo=timezone.utc)
        parsed.append((rt, r))
    future = [(rt, r) for rt, r in parsed if rt is not None and rt > now]
    if future:
        return min(future, key=lambda x: x[0])[1].get("side", "-")
    past = [(rt, r) for rt, r in parsed if rt is not None]
    if past:
        return max(past, key=lambda x: x[0])[1].get("side", "-")
    return rows[0].get("side", "-")


async def _regime_summary(
    classifier: VolatilityClassifier,
    zone_counts: dict[str, int],
    portfolio: Portfolio,
    latest_yes: dict[str, float],
    zones: ZoneThresholds,
) -> None:
    """60s proof both classifier dimensions work — with raw vol for tuning.
    Also emits a per-coin status block (pos + zone + vol)."""
    log_r = get_logger("regime")
    log_z = get_logger("zones")
    log_s = get_logger("status")
    last = dict(zone_counts)
    while True:
        await asyncio.sleep(60)
        vols = classifier.snapshot_vols()
        parts = " ".join(
            f"{sym}={classifier.get_regime(sym)}({vols[sym]:.7f})" for sym in sorted(vols)
        )
        log_r.info(parts or "(no symbols with enough data yet)")
        delta = {z: zone_counts.get(z, 0) - last.get(z, 0) for z in zone_counts}
        last = dict(zone_counts)
        log_z.info(" ".join(f"{z}={delta.get(z, 0)}" for z in ZONE_ORDER))

        # Guarded: a transient list_open() DB error must not kill the regime/zones
        # lines above. STATUS_COINS is fixed (the 6 tracked coins).
        try:
            now = datetime.now(timezone.utc)
            open_by_sym: dict[str, list[dict]] = {}
            for p in await portfolio.list_open():
                open_by_sym.setdefault(p.get("symbol"), []).append(p)
            for sym in STATUS_COINS:
                rows = open_by_sym.get(sym, [])
                pos = _active_side(rows, now) if rows else "skip"
                zone = classify_price_zone(latest_yes.get(sym), zones)
                vol = _VOL_DISPLAY.get(classifier.get_regime(sym), "unknown")
                log_s.info(f"{sym:<4} pos={pos:<4}  zone={zone:<12}  vol={vol}")
        except Exception:
            log_s.exception("status block failed")


async def rediscovery_loop(
    settings: Settings,
    poly_ws: PolymarketWSClient,
    symbol_lookup: dict[str, str],
    outcome_lookup: dict[str, str],
    market_meta: dict[str, dict],
    interval_sec: int = 300,
) -> None:
    """Add newly-listed hourly markets live (operation:subscribe, no reconnect);
    periodically reconnect-clean to drop tokens whose market has resolved."""
    log = get_logger("rediscovery")
    last_prune = datetime.now(timezone.utc)
    while True:
        await asyncio.sleep(interval_sec)
        try:
            markets = await _discover_polymarket(settings)
            new_markets = [m for m in markets if m.get("condition_id") not in market_meta]
            if new_markets:
                new_tokens = _index_markets(new_markets, symbol_lookup, outcome_lookup, market_meta)
                await poly_ws.add_assets(list(new_tokens))
                log.info("re-discovery: +%d new markets (%d tokens)", len(new_markets), len(new_tokens))
            else:
                log.debug("re-discovery: nothing new")

            now = datetime.now(timezone.utc)
            active = poly_ws.assets
            due = len(active) >= PRUNE_THRESHOLD or (now - last_prune) >= PRUNE_MAX_INTERVAL
            if due:
                dead: set[str] = set()
                for meta in market_meta.values():
                    rt = meta.get("resolve_time")
                    if rt is not None and rt + PRUNE_GRACE <= now:
                        dead.update(meta.get("token_ids", ()))
                stale = active & dead
                if stale:
                    live = active - dead
                    await poly_ws.resubscribe(list(live))
                    last_prune = now
                    log.info("prune: %d -> %d live (dropped %d) via reconnect",
                             len(active), len(live), len(stale))
                else:
                    log.debug("prune due but nothing resolved yet (%d active)", len(active))
        except Exception as e:
            log.exception("re-discovery tick failed", extra={"error": str(e)})


async def run() -> int:
    try:
        settings = Settings()
        settings.require_credentials()
    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 1

    setup_logging(settings.log_dir, settings.log_level)
    log = get_logger("main")
    # Mode is the very first INFO line; verbose detail stays at DEBUG.
    log.info("MODE: DRY-RUN (no real orders)" if settings.dry_run else "MODE: LIVE")
    log.debug(
        "starting",
        extra={
            "dry_run": settings.dry_run,
            "strategies": settings.active_strategies,
            "db": str(settings.db_path),
        },
    )

    db = Database(settings.db_path)
    await db.connect()
    await create_tables(db)

    strategies = _load_strategies(settings.active_strategies)
    log.info(f"strategies active: {[s.name for s in strategies]}")
    writer = SnapshotWriter(db)
    # Classifier knobs come from config (defaults = calibrated values).
    zones = ZoneThresholds(
        settings.zone_extreme_low,
        settings.zone_low,
        settings.zone_uncertain,
        settings.zone_high,
    )
    vol_classifier = VolatilityClassifier(
        settings.vol_window,
        min_samples=settings.vol_min_samples,
        low_vol_max=settings.vol_low_max,
        high_vol_min=settings.vol_high_min,
    )
    zone_counts: dict[str, int] = {z: 0 for z in ZONE_ORDER}
    health = Health()

    # --- Polymarket: discover, then build a WS client (always run; re-discovery
    # attaches new subscriptions to it). The shared lookups are mutated in place
    # by _index_markets so on_poly_event/portfolio/resolver see new markets. ---
    markets = await _discover_polymarket(settings)
    symbol_lookup: dict[str, str] = {}      # token_id & condition_id -> symbol
    outcome_lookup: dict[str, str] = {}     # token_id -> "YES"|"NO"
    market_meta: dict[str, dict] = {}       # condition_id -> {label, resolve_time}
    token_ids = _index_markets(markets, symbol_lookup, outcome_lookup, market_meta)

    # Built after discovery so the friendly-label meta is available to both.
    executor = DryRunExecutor(db=db, dry_run=settings.dry_run, market_meta=market_meta)
    # Per-strategy knobs (entry floor + sizing), keyed by strategy name. Portfolio
    # resolves decision.strategy -> these on open; an unknown name -> house defaults.
    strategy_params = {s.name: s.params for s in strategies}
    portfolio = Portfolio(db, market_meta=market_meta, strategy_params=strategy_params)

    # Shared parse-verification state (see _parse_summary).
    parse_stats = {"poly_book": 0, "poly_price_change": 0, "poly_last_trade_price": 0, "poly_other": 0, "binance": 0}
    latest_yes: dict[str, float] = {}   # symbol -> latest polymarket yes-price
    latest_spot: dict[str, float] = {}  # symbol -> latest binance spot
    # market_id -> latest YES-perspective top-of-book, for realistic taker-fill
    # costing at open. A decision can fire on a price_change event (no book), and
    # either token can tick, so we keep the freshest book per market here rather
    # than relying on the triggering snapshot. See src/execute/fill.py.
    latest_book: dict[str, YesBook] = {}
    # market_id -> ts of the cached book above, so open_position can reject a
    # stale book (a price_change can fire long after the last real book event).
    latest_book_ts: dict[str, datetime] = {}
    # market_ids whose slow-rise first-reach-of-V has been handled (decide once per
    # position; see exits.maybe_slow_rise_exit). Reset on restart; bounded by run length.
    slowrise_seen: set[str] = set()

    _POLY_STAT = {
        "book": "poly_book",
        "price_change": "poly_price_change",
        "last_trade_price": "poly_last_trade_price",
    }

    poly_ws = PolymarketWSClient(url=settings.polymarket_ws_url)

    async def on_poly_event(event: dict) -> None:
        health.mark_poly()
        snapshot = parse_polymarket(event, symbol_lookup)
        if snapshot is None:
            return  # bad message already logged at DEBUG by the parser
        parse_stats[_POLY_STAT.get(snapshot.event_type, "poly_other")] += 1
        # Label outcome, then normalize price to YES-perspective so zone +
        # strategy always reason in YES terms regardless of which token ticked.
        snapshot.outcome = outcome_lookup.get(snapshot.asset_id or "")
        if snapshot.outcome == "NO" and snapshot.price is not None:
            snapshot.price = 1.0 - snapshot.price
        if snapshot.symbol and snapshot.price is not None:
            latest_yes[snapshot.symbol] = snapshot.price
        # Cache the freshest YES-perspective book per market for realistic fill
        # costing at open. Only book events carry quotes, and only a known
        # outcome lets us reflect the raw (per-token) book into YES terms.
        if (
            snapshot.event_type == "book"
            and snapshot.market_id is not None
            and snapshot.outcome in ("YES", "NO")
            and (snapshot.best_bid is not None or snapshot.best_ask is not None)
        ):
            latest_book[snapshot.market_id] = yes_book_from_token(
                snapshot.outcome,
                snapshot.best_bid,
                snapshot.best_ask,
                snapshot.bid_size,
                snapshot.ask_size,
                snapshot.bid_depth,
                snapshot.ask_depth,
            )
            latest_book_ts[snapshot.market_id] = snapshot.ts
        # Copy the market's resolution time onto the snapshot so time-aware
        # strategies (contrarian's reversion-runway gate) can measure time-to-resolve
        # against snapshot.ts. market_meta is keyed by condition_id == market_id.
        snapshot.resolve_time = (market_meta.get(snapshot.market_id) or {}).get("resolve_time")
        # Tag both dimensions before dispatch/store (observation only).
        snapshot.vol_regime = vol_classifier.get_regime(snapshot.symbol)
        snapshot.price_zone = classify_price_zone(snapshot.price, zones)
        zone_counts[snapshot.price_zone] = zone_counts.get(snapshot.price_zone, 0) + 1
        writer.add(snapshot)  # persist every snapshot, regardless of decision
        # Exit overlays on held positions (book just cached → sell sees a fresh book):
        # time-gated SL (near-dead side in the final minutes) + slow-rise (weak riser
        # at 0.40). See src/execute/exits.py.
        await maybe_stop_loss(settings, snapshot, market_meta, portfolio, executor, latest_book)
        await maybe_slow_rise_exit(settings, snapshot, market_meta, portfolio, executor,
                                   latest_book, slowrise_seen)
        poly_ws.log.debug(
            "snapshot",
            extra={
                "event_type": snapshot.event_type,
                "symbol": snapshot.symbol,
                "asset_id": snapshot.asset_id,
                "price": snapshot.price,
            },
        )
        for strat in strategies:
            try:
                decision = await strat.evaluate(snapshot)
            except Exception as e:
                log.exception(
                    "strategy evaluate failed",
                    extra={"strategy": strat.name, "error": str(e)},
                )
                continue
            if decision.action is not Action.SKIP:
                held = await portfolio.is_held(decision.market_id, decision.strategy)
                await executor.execute(decision, snapshot, held=held)
                if not held:
                    await portfolio.open_position(
                        decision, snapshot,
                        latest_book.get(decision.market_id),
                        latest_book_ts.get(decision.market_id),
                    )
            else:
                await executor.execute(decision, snapshot)

    for et in ("book", "price_change", "last_trade_price", "tick_size_change"):
        poly_ws.on(et)(on_poly_event)

    poly_ws.set_assets(token_ids)

    # --- Binance: combined stream, no auth, always runs ---
    binance_ws = BinanceWSClient(settings.binance_ws_url, BINANCE_SYMBOLS)

    @binance_ws.on("miniTicker")
    async def on_binance_tick(data: dict) -> None:
        health.mark_binance()
        snapshot = parse_binance(data)
        if snapshot is None:
            return
        parse_stats["binance"] += 1
        if snapshot.symbol and snapshot.price is not None:
            latest_spot[snapshot.symbol] = snapshot.price
        # Feed the rolling window, then tag this snapshot's own regime.
        vol_classifier.update(snapshot.symbol, snapshot.price)
        snapshot.vol_regime = vol_classifier.get_regime(snapshot.symbol)
        writer.add(snapshot)  # persist every snapshot, regardless of decision
        binance_ws.log.debug("snapshot", extra={"symbol": snapshot.symbol, "price": snapshot.price})
        for strat in strategies:
            try:
                decision = await strat.evaluate(snapshot)
            except Exception as e:
                log.exception(
                    "strategy evaluate failed",
                    extra={"strategy": strat.name, "error": str(e)},
                )
                continue
            if decision.action is not Action.SKIP:
                held = await portfolio.is_held(decision.market_id, decision.strategy)
                await executor.execute(decision, snapshot, held=held)
                if not held:
                    await portfolio.open_position(
                        decision, snapshot,
                        latest_book.get(decision.market_id),
                        latest_book_ts.get(decision.market_id),
                    )
            else:
                await executor.execute(decision, snapshot)

    # --- Signal + task wiring ---
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_signal() -> None:
        log.info("shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, _on_signal)

    # Long-lived REST client for the resolver — discovery's was context-managed
    # and is already closed. Closed in the shutdown block below.
    rest = PolymarketREST(settings.polymarket_gamma_url, settings.polymarket_clob_url)
    await rest.__aenter__()

    tasks: list[asyncio.Task] = []
    # Always start poly_ws so re-discovery has a live socket to attach to.
    tasks.append(asyncio.create_task(poly_ws.run(), name="poly_ws"))
    tasks.append(asyncio.create_task(binance_ws.run(), name="binance_ws"))
    tasks.append(asyncio.create_task(writer.run(), name="snapshot_writer"))
    tasks.append(asyncio.create_task(_refresh_priority_markets(writer, db), name="priority_refresh"))
    tasks.append(asyncio.create_task(_parse_summary(parse_stats, latest_yes, latest_spot), name="parse_summary"))
    tasks.append(asyncio.create_task(_storage_summary(writer, settings.db_path), name="storage_summary"))
    tasks.append(asyncio.create_task(_regime_summary(vol_classifier, zone_counts, portfolio, latest_yes, zones), name="regime_summary"))
    tasks.append(asyncio.create_task(heartbeat(health), name="heartbeat"))
    tasks.append(asyncio.create_task(
        resolve_loop(portfolio, rest, market_meta=market_meta, dry_run=settings.dry_run),
        name="resolver",
    ))
    tasks.append(asyncio.create_task(
        rediscovery_loop(settings, poly_ws, symbol_lookup, outcome_lookup, market_meta),
        name="rediscovery",
    ))

    # Concise startup banner: subscriptions line, then the streaming separator.
    poly_part = (
        f"{len(token_ids)} polymarket tokens"
        if token_ids else "0 polymarket tokens (idle)"
    )
    log.info(f"subscribed: {poly_part} + {len(BINANCE_SYMBOLS)} binance tickers")
    log.debug(
        "endpoints",
        extra={
            "poly_ws": settings.polymarket_ws_url if token_ids else "(idle: no markets)",
            "binance_ws": settings.binance_ws_url,
        },
    )
    separator("streaming")

    try:
        await stop_event.wait()
    finally:
        log.info("shutting down")
        # Stop WS clients first so their run loops exit cleanly, then cancel
        # the auxiliary tasks (summary / heartbeat) which sleep forever.
        await poly_ws.stop()
        await binance_ws.stop()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await task
        # WS loops + writer loop are stopped: buffer is now stable. Flush the
        # last partial batch before the DB closes so no snapshots are lost.
        await writer.stop()
        await rest.__aexit__(None, None, None)
        await db.close()
        log.info("stopped")
    return 0


def main() -> None:
    try:
        sys.exit(asyncio.run(run()))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
