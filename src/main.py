"""
src/main.py
===========
Entry point — orchestrator utama bot (async version).
"""

import asyncio
import signal
import sys
import logging
import re
import aiohttp
from datetime import datetime, timezone
from typing import Optional

from src.api.clob_client import ClobClient
from src.api.gamma_client import GammaClient
from src.logic.strategy import ScalarStrategy, KonfigurasiStrategy, SinyalTrade
from src.logic.mispricing import MispricingDetector, BaseRateBuilder, MispricingDirection
from src.logic.kelly import KellySizer
from src.logic.exit_strategy import ExitEvaluator
from src.logic.manager import PositionManager
from src.logic.probability import CryptoProbabilityCalculator
from src.logic.circuit_breaker import CircuitBreaker
from src.logic.fed_fetcher import get_fed_base_rate
from src.models.types import SisiOrder
from src.models.database import log_prediction, resolve_prediction
from src.utils.config import config
from src.utils.logger import log, tampilkan_header, tampilkan_sinyal, tampilkan_trade
from src.utils.telegram_alert import init_telegram, get_alert

logger = logging.getLogger(__name__)

STRATEGY_MODE = getattr(config, "STRATEGY_MODE", "mispricing").lower()

_price_cache: dict = {}
_cache_time: dict  = {}
_CACHE_TTL         = 300
_price_lock        = asyncio.Lock()


async def _fetch_crypto_price(symbol: str, session: aiohttp.ClientSession) -> float | None:
    """Fetch harga crypto dari CoinGecko secara async. Cache 5 menit, single-flight per symbol."""
    symbol = symbol.upper()

    id_map = {
        "BTC":   "bitcoin",
        "ETH":   "ethereum",
        "SOL":   "solana",
        "XRP":   "ripple",
        "DOGE":  "dogecoin",
        "BNB":   "binancecoin",
        "MATIC": "matic-network",
    }

    coin_id = id_map.get(symbol)
    if not coin_id:
        return None

    # Lock cover seluruh fetch — concurrent call untuk symbol sama akan share hasil.
    async with _price_lock:
        now = datetime.now(timezone.utc).timestamp()
        if symbol in _price_cache and now - _cache_time.get(symbol, 0) < _CACHE_TTL:
            return _price_cache[symbol]

        try:
            async with session.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": coin_id, "vs_currencies": "usd"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                resp.raise_for_status()
                data  = await resp.json()
                price = data.get(coin_id, {}).get("usd")

                if price:
                    _price_cache[symbol] = float(price)
                    _cache_time[symbol]  = now
                    log.info(f"[PRICE] {symbol} = ${float(price):,.4f}")
                    return float(price)

        except Exception as e:
            logger.warning(f"Gagal fetch harga {symbol}: {e}")

        return None


async def _prefetch_prices(session: aiohttp.ClientSession) -> None:
    """Fetch semua harga crypto sekaligus secara paralel."""
    await asyncio.gather(
        _fetch_crypto_price("BTC", session),
        _fetch_crypto_price("ETH", session),
        _fetch_crypto_price("SOL", session),
        return_exceptions=True,
    )


async def _get_base_rates(market: dict, builder, session: aiohttp.ClientSession) -> list:
    """Auto-generate base rates berdasarkan kategori market (async)."""
    question       = (market.get("question") or "").lower()
    calc           = CryptoProbabilityCalculator()
    end_date_str   = market.get("endDate") or market.get("end_date_iso", "")
    days_remaining = 30
    try:
        end_date       = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        days_remaining = max(1, (end_date - datetime.now(timezone.utc)).days)
    except Exception:
        pass

    # ── Skip market yang tidak bisa di-model ──────────────────────
    if any(k in question for k in ["between", "range", "dip to"]):
        return []

    # ── Skip Up/Down market (butuh strategy berbeda) ──────────────
    if "up or down" in question:
        return []

    async def _check_crypto(symbol: str, keywords: list[str]) -> list | None:
        """Return base rate list jika question cocok, None jika tidak match."""
        if not any(k in question for k in keywords):
            return None
        price = await _fetch_crypto_price(symbol, session)
        if not price:
            return []
        target = _extract_price_target(question)
        if not target:
            return []
        direction = "below" if any(k in question for k in ["dip", "drop", "fall", "below", "↓"]) else "above"

        # "above $X on [date]" → at-expiry (harga PADA tanggal tertentu)
        # "reach $X in [period]" → barrier (menyentuh kapanpun sebelum expire)
        use_barrier = " on " not in question

        result = await calc.calculate_async(
            symbol, price, target, days_remaining, session,
            direction=direction, use_barrier=use_barrier,
        )
        if result.probability == 0.0:
            return []
        return [builder.from_manual(rate=result.probability, confidence=result.confidence, notes=result.notes)]

    # XRP & DOGE excluded — MAE >4%, edge effective negatif (CLAUDE.md)
    for symbol, keywords in [
        ("BTC",  ["bitcoin", "btc"]),
        ("ETH",  ["ethereum", " eth ", "ether "]),
        ("SOL",  ["solana", " sol "]),
        ("BNB",  [" bnb ", "binance coin"]),
    ]:
        result = await _check_crypto(symbol, keywords)
        if result is not None:
            return result

    # ── Fed / Interest Rate ───────────────────────────────────────
    if any(k in question for k in ["fed", "interest rate", "bps", "federal"]):
        rate = await get_fed_base_rate(question, session)
        if rate is not None:
            return [builder.from_cme_fedwatch(rate)]

    # ── Political / Event fallback: Kalshi + Manifold ─────────────
    from src.logic.political_mispricing import get_political_base_rates, is_political_market
    if not is_political_market(market.get("question", market.get("title", ""))):
        return []
    political_rates = await get_political_base_rates(
        market.get("question", market.get("title", "")),
        session,
        condition_id=market.get("conditionId", market.get("id")),
    )
    if political_rates:
        return political_rates

    return []


async def _analyze_market(
    market, clob, gamma, detector, sizer, manager,
    builder, breaker, capital, session
):
    """Analisis satu market secara async."""
    from src.logic.pricing import ke_decimal

    condition_id = market.get("conditionId", market.get("id", ""))
    question     = market.get("question", market.get("title", ""))
    prices       = gamma.get_token_prices(market)
    yes_price    = prices.get("Yes")

    if not yes_price or yes_price <= 0 or yes_price >= 1:
        return

    yes_base_rates = await _get_base_rates(market, builder, session)
    if not yes_base_rates:
        return

    # Detect political vs crypto/fed market berdasarkan source base rate.
    # Political markets pakai threshold + min_volume yang lebih longgar.
    is_political = any(br.source in ("kalshi", "manifold") for br in yes_base_rates)

    if is_political:
        threshold  = getattr(config, "POLITICAL_THRESHOLD", 0.08)
        min_volume = getattr(config, "POLITICAL_MIN_VOLUME", 5_000.0)
    else:
        threshold  = getattr(config, "MISPRICING_THRESHOLD", 0.15)
        min_volume = getattr(config, "MIN_MARKET_VOLUME", 10_000.0)

    # Volume filter — gamma scan pakai threshold paling rendah, jadi cek lagi di sini
    market_vol = float(market.get("volume") or market.get("volumeNum") or 0)
    if market_vol < min_volume:
        logger.debug(
            f"Skip {question[:40]} | volume ${market_vol:,.0f} < ${min_volume:,.0f} "
            f"({'political' if is_political else 'crypto'})"
        )
        return

    results = detector.analyze_market(
        market=market,
        yes_base_rates=yes_base_rates,
        threshold=threshold,
    )

    for result in results:
        if not result.is_mispriced:
            continue

        if result.outcome != "Yes":
            continue

        if result.direction == MispricingDirection.UNDERPRICED:
            buy_outcome = "Yes"
            buy_price   = float(result.market_price)
            buy_winrate = float(result.base_rate)
        else:
            buy_outcome = "No"
            buy_price   = round(1.0 - float(result.market_price), 4)
            buy_winrate = round(1.0 - float(result.base_rate), 4)

        if buy_price <= 0 or buy_price >= 1:
            continue

        kelly = sizer.calculate(
            winrate      = buy_winrate,
            market_price = buy_price,
            capital      = capital,
        )

        if not kelly.is_positive_ev or float(kelly.bet_usdc) <= 0:
            logger.debug(f"Skip {question[:40]} | EV={float(kelly.expected_value):.3f}")
            continue

        # Skip kalau profit potensial terlalu kecil
        profit_if_win = (1.0 - buy_price) * float(kelly.shares)
        min_profit    = float(kelly.bet_usdc) * getattr(config, "MIN_PROFIT_PCT", 0.15)
        if profit_if_win < min_profit:
            logger.debug(f"Skip {question[:40]} | profit terlalu kecil: ${profit_if_win:.2f} < ${min_profit:.2f}")
            continue

        can_open, reason = manager.can_open(
            condition_id  = condition_id,
            outcome       = buy_outcome,
            bet_usdc      = kelly.bet_usdc,
            total_capital = ke_decimal(capital),
        )

        if not can_open:
            logger.debug(f"Skip {condition_id[:8]} {buy_outcome}: {reason}")
            continue

        if not breaker.check(unrealized_pnl=manager.get_unrealized_pnl()).can_trade:
            logger.warning("[CIRCUIT BREAKER] Skip posisi — circuit breaker triggered")
            return

        log.info(
            f"[bold cyan]SIGNAL[/bold cyan] {question[:45]} | "
            f"BUY {buy_outcome} @ {buy_price:.3f} | "
            f"Gap {result.gap_pct:.1f}% | "
            f"Bet ${float(kelly.bet_usdc):.2f} | "
            f"EV {float(kelly.expected_value):.3f}"
        )

        alert = get_alert()
        if alert:
            await alert.alert_signal(
                question = question,
                outcome  = buy_outcome,
                price    = buy_price,
                bet_usdc = float(kelly.bet_usdc),
                gap_pct  = result.gap_pct,
                ev       = float(kelly.expected_value),
                session  = session,
                dry_run  = config.DRY_RUN,
            )

        tokens   = gamma.extract_token_ids(market)
        token    = next((t for t in tokens if t["outcome"] == buy_outcome), None)
        token_id = str(token["token_id"]) if token and token.get("token_id") else ""

        if config.DRY_RUN:
            log.warning("[yellow]DRY RUN — simulasi posisi dibuka[/yellow]")
            await _dry_run_open(result, kelly, market, manager, buy_outcome, buy_price, token_id)
            continue

        if not token_id:
            logger.warning(f"token_id tidak ditemukan untuk {buy_outcome}")
            continue

        order = clob.pasang_order(
            sisi     = SisiOrder.BELI,
            harga    = ke_decimal(buy_price),
            ukuran   = kelly.shares,
            token_id = token_id,
        )

        if order:
            resolve_date_str = market.get("endDate") or market.get("end_date_iso", "")
            try:
                resolve_date = datetime.fromisoformat(resolve_date_str.replace("Z", "+00:00"))
            except Exception:
                resolve_date = datetime.now(timezone.utc)

            manager.open_position(
                condition_id    = condition_id,
                question        = question,
                outcome         = buy_outcome,
                entry_price     = ke_decimal(buy_price),
                shares          = kelly.shares,
                capital_at_risk = kelly.bet_usdc,
                resolve_date    = resolve_date,
                gap_pct         = result.gap_pct / 100,
                kelly_fraction  = float(kelly.bet_fraction),
                strategy_mode   = "mispricing",
                token_id        = token_id,
            )
            log_prediction({
                "condition_id":   condition_id,
                "question":       question,
                "outcome":        buy_outcome,
                "predicted_prob": str(result.base_rate),
                "market_price":   str(buy_price),
                "gap_pct":        str(round(result.gap_pct, 2)),
                "resolve_date":   resolve_date.isoformat(),
            })


async def _dry_run_open(result, kelly, market, manager, buy_outcome: str, buy_price: float, token_id: str = ""):
    """Simulate buka posisi di DRY RUN mode."""
    from src.logic.pricing import ke_decimal

    condition_id     = market.get("conditionId", market.get("id", ""))
    question         = market.get("question", market.get("title", ""))
    resolve_date_str = market.get("endDate") or market.get("end_date_iso", "")
    try:
        resolve_date = datetime.fromisoformat(resolve_date_str.replace("Z", "+00:00"))
    except Exception:
        resolve_date = datetime.now(timezone.utc)

    manager.open_position(
        condition_id    = condition_id,
        question        = question,
        outcome         = buy_outcome,
        entry_price     = ke_decimal(buy_price),
        shares          = kelly.shares,
        capital_at_risk = kelly.bet_usdc,
        resolve_date    = resolve_date,
        gap_pct         = result.gap_pct / 100,
        kelly_fraction  = float(kelly.bet_fraction),
        strategy_mode   = "mispricing_dry_run",
        token_id        = token_id,
    )
    log_prediction({
        "condition_id":   condition_id,
        "question":       question,
        "outcome":        buy_outcome,
        "predicted_prob": str(result.base_rate),
        "market_price":   str(buy_price),
        "gap_pct":        str(round(result.gap_pct, 2)),
        "resolve_date":   resolve_date.isoformat(),
    })


FORCE_CLOSE_GRACE_HOURS = 24


async def _backfill_token_id(gamma, session, condition_id: str, outcome: str) -> str:
    """Fetch market dari Gamma → ekstrak token_id buat outcome ini. Return '' kalau gagal."""
    try:
        market = await gamma.aget_market(condition_id, session)
        if not market:
            return ""
        tokens = gamma.extract_token_ids(market)
        token  = next((t for t in tokens if t["outcome"] == outcome), None)
        return str(token["token_id"]) if token and token.get("token_id") else ""
    except Exception as e:
        logger.debug(f"Gagal backfill token_id {condition_id[:8]}: {e}")
        return ""


async def _backfill_missing_token_ids(gamma, session) -> None:
    """
    Cari semua posisi open yang token_id-nya kosong → fetch dari Gamma → simpan.
    Jalan tiap cycle, no-op kalau semua posisi udah ada token_id.
    """
    from src.models.database import get_open_positions, update_position_token_id

    missing = [p for p in get_open_positions() if not (p.get("token_id") or "")]
    if not missing:
        return

    for pos in missing:
        cid     = pos["condition_id"]
        outcome = pos["outcome"]
        tid     = await _backfill_token_id(gamma, session, cid, outcome)
        if tid:
            update_position_token_id(cid, outcome, tid)
            log.info(f"[BACKFILL] token_id {cid[:8]} {outcome} ✓")
        else:
            logger.debug(f"[BACKFILL] gagal {cid[:8]} {outcome} — market mungkin sudah closed di Gamma")


async def _get_resolved_price_from_gamma(gamma, session, condition_id: str, outcome: str) -> Optional[float]:
    """
    Untuk market yang sudah resolved di Polymarket, CLOB return 404 (no order book).
    Tapi Gamma masih simpan `outcomePrices` final (e.g. ["1", "0"] kalau YES menang).
    Return harga resolved (0.0 atau 1.0) atau None kalau gagal.
    """
    import json
    try:
        market = await gamma.aget_market(condition_id, session)
        if not market or not market.get("closed"):
            return None
        outcomes       = market.get("outcomes", [])
        outcome_prices = market.get("outcomePrices", [])
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if isinstance(outcome_prices, str):
            outcome_prices = json.loads(outcome_prices)
        if outcome not in outcomes:
            return None
        idx = outcomes.index(outcome)
        if 0 <= idx < len(outcome_prices):
            return float(outcome_prices[idx])
    except Exception as e:
        logger.debug(f"Gagal fetch resolved price {condition_id[:8]} {outcome}: {e}")
    return None


async def _resolve_checker(clob, gamma, manager, breaker, session: aiohttp.ClientSession) -> None:
    """
    Cek posisi open yang resolve_date-nya sudah lewat.
    - Resolved di Polymarket → ambil final outcomePrices dari Gamma
    - Belum resolved tapi expired → fetch best_bid dari CLOB
    - Auto-close kalau price ≥0.98 atau ≤0.02
    - Force-close kalau >{FORCE_CLOSE_GRACE_HOURS}h lewat resolve & harga mid-range

    Asumsi: token_id sudah di-backfill duluan oleh `_backfill_missing_token_ids()`.
    """
    from src.models.database import get_open_positions
    from src.logic.pricing import ke_decimal

    now       = datetime.now(timezone.utc)
    positions = get_open_positions()
    expired   = []

    for pos in positions:
        try:
            resolve = datetime.fromisoformat(pos["resolve_date"])
            if resolve.tzinfo is None:
                resolve = resolve.replace(tzinfo=timezone.utc)
            if resolve < now:
                expired.append((pos, resolve))
        except Exception:
            continue

    if not expired:
        return

    log.info(f"[RESOLVE CHECK] {len(expired)} posisi sudah melewati resolve_date")

    for pos, resolve in expired:
        cid     = pos["condition_id"]
        outcome = pos["outcome"]
        tid     = pos.get("token_id") or ""

        # 1. Cek Gamma dulu — kalau market udah resolved, ambil harga final
        price = await _get_resolved_price_from_gamma(gamma, session, cid, outcome)

        # 2. Fallback ke CLOB book untuk market yang belum resolved
        if price is None and tid:
            try:
                snapshot = clob.ambil_snapshot(token_id=tid)
                if snapshot and snapshot.valid:
                    price = float(snapshot.best_bid)
            except Exception:
                pass

        hours_past = (now - resolve).total_seconds() / 3600

        if price is None:
            log.warning(
                f"[RESOLVE CHECK] {pos['question'][:45]} | {outcome} — "
                f"tidak bisa fetch harga ({hours_past:.1f}h lewat resolve), skip"
            )
            continue

        if price >= 0.98 or price <= 0.02:
            reason = "resolve_expired"
        elif hours_past > FORCE_CLOSE_GRACE_HOURS:
            reason = "resolve_force_close"
            log.warning(
                f"[RESOLVE CHECK] {pos['question'][:45]} | {outcome} @ {price:.3f} — "
                f"sudah {hours_past:.1f}h lewat resolve, force close di bid sekarang"
            )
        else:
            log.warning(
                f"[RESOLVE CHECK] {pos['question'][:45]} | {outcome} @ {price:.3f} — "
                f"harga mid-range, belum settle ({hours_past:.1f}h lewat), skip"
            )
            continue

        entry  = ke_decimal(pos["entry_price"])
        shares = ke_decimal(pos["shares"])
        pnl    = (ke_decimal(str(price)) - entry) * shares
        won    = float(pnl) > 0

        manager._process_exit_manual(cid, outcome, ke_decimal(str(price)), pnl, reason)
        breaker.record_trade(float(pnl))

        log.info(
            f"[RESOLVE CHECK] {'✅ WIN' if won else '❌ LOSE'} — "
            f"{pos['question'][:45]} | {outcome} @ {price:.3f} | PnL: ${float(pnl):+.2f}"
        )

        alert = get_alert()
        if alert:
            await alert.alert_exit(
                question    = pos["question"],
                outcome     = outcome,
                entry_price = float(entry),
                exit_price  = price,
                pnl_usdc    = float(pnl),
                reason      = reason,
                session     = session,
            )


async def _fetch_current_prices(clob, manager) -> dict:
    """Ambil harga terkini dari CLOB untuk semua posisi open."""
    from src.models.database import get_open_positions
    from src.logic.pricing import ke_decimal

    positions = get_open_positions()
    prices    = {}
    for pos in positions:
        cid      = pos["condition_id"]
        tid      = pos.get("token_id") or ""
        outcome  = pos["outcome"]
        if cid in prices and outcome in prices[cid]:
            continue
        if not tid:
            logger.debug(f"Skip price fetch {cid[:8]} — token_id tidak tersimpan")
            continue
        try:
            snapshot = clob.ambil_snapshot(token_id=tid)
            if snapshot and snapshot.valid:
                prices.setdefault(cid, {})[outcome] = ke_decimal(snapshot.best_bid)
        except Exception as e:
            logger.debug(f"Gagal fetch price {tid[:8]}: {e}")
    return prices


async def run_mispricing_mode(clob: ClobClient):
    """Loop utama mispricing strategy — fully async."""
    gamma    = GammaClient(host=getattr(config, "GAMMA_HOST", "https://gamma-api.polymarket.com"))
    detector = MispricingDetector(threshold=getattr(config, "MISPRICING_THRESHOLD", 0.15))
    sizer    = KellySizer(
        kelly_multiplier = getattr(config, "KELLY_MULTIPLIER", 0.5),
        max_fraction     = getattr(config, "MAX_KELLY_FRACTION", 0.30),
        min_bet_usdc     = getattr(config, "MIN_BET_USDC", 5.0),
        min_winrate      = getattr(config, "MIN_WINRATE", 0.52),
    )
    manager  = PositionManager(
        max_open_positions     = getattr(config, "MAX_OPEN_POSITIONS", 5),
        max_capital_per_market = getattr(config, "MAX_CAPITAL_PER_MARKET", 30.0),
        exit_evaluator         = ExitEvaluator(
            trailing_stop_pct       = getattr(config, "TRAILING_STOP_PCT", 0.15),
            profit_threshold        = getattr(config, "PROFIT_THRESHOLD", 0.75),
            tight_trailing_stop_pct = getattr(config, "TIGHT_TRAILING_STOP_PCT", 0.07),
        ),
    )
    builder = BaseRateBuilder()
    breaker = CircuitBreaker(
        starting_capital       = float(config.SALDO_AWAL),
        max_drawdown_pct       = getattr(config, "MAX_DRAWDOWN_PCT", 0.30),
        max_daily_loss_pct     = getattr(config, "MAX_DAILY_LOSS_PCT", 0.10),
        max_consecutive_losses = getattr(config, "MAX_CONSECUTIVE_LOSSES", 3),
    )

    init_telegram(
        token   = getattr(config, "TELEGRAM_BOT_TOKEN", ""),
        chat_id = getattr(config, "TELEGRAM_CHAT_ID", ""),
    )

    log.info(
        f"[bold green]Mispricing mode aktif (async).[/bold green] "
        f"Threshold: {detector.threshold:.0%} | "
        f"Polling: {config.POLLING_INTERVAL}s"
    )

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await _backfill_missing_token_ids(gamma, session)
                await _resolve_checker(clob, gamma, manager, breaker, session)

                balance = clob.get_balance()
                if config.DRY_RUN and balance == 0.0:
                    balance = float(config.SALDO_AWAL)
                capital = balance
                prefix  = "[DRY RUN] " if config.DRY_RUN else ""
                log.info(f"{prefix}Balance: ${capital:.2f} USDC | {manager.get_summary()}")

                cb_status = breaker.check(unrealized_pnl=manager.get_unrealized_pnl())
                if not cb_status.can_trade:
                    log.warning(f"[CIRCUIT BREAKER] {cb_status}")
                    alert = get_alert()
                    if alert:
                        await alert.alert_circuit_breaker(
                            reason       = str(cb_status),
                            drawdown_pct = getattr(cb_status, "drawdown_pct", 0.0),
                            session      = session,
                        )
                    await asyncio.sleep(config.POLLING_INTERVAL)
                    continue

                log.info(breaker.get_summary(unrealized_pnl=manager.get_unrealized_pnl()))
                await _prefetch_prices(session)

                current_prices = await _fetch_current_prices(clob, manager)
                exits = manager.evaluate_exits(current_prices)
                for decision in exits:
                    pos = decision.position

                    # Eksekusi SELL order ke CLOB (kecuali DRY RUN)
                    if not config.DRY_RUN and pos.token_id:
                        clob.pasang_order(
                            sisi     = SisiOrder.JUAL,
                            harga    = pos.current_price,
                            ukuran   = pos.shares,
                            token_id = pos.token_id,
                        )
                    elif config.DRY_RUN:
                        log.warning(
                            f"[yellow][DRY RUN] Simulasi exit {pos.outcome} "
                            f"@ {float(pos.current_price):.3f}[/yellow]"
                        )

                    if decision.estimated_pnl_usdc is not None:
                        breaker.record_trade(float(decision.estimated_pnl_usdc))
                        alert = get_alert()
                        if alert:
                            await alert.alert_exit(
                                question    = pos.question,
                                outcome     = pos.outcome,
                                entry_price = float(pos.entry_price),
                                exit_price  = float(pos.current_price),
                                pnl_usdc    = float(decision.estimated_pnl_usdc),
                                reason      = decision.reason,
                                session     = session,
                            )

                if exits:
                    log.info(f"[yellow]{len(exits)} posisi di-exit cycle ini[/yellow]")

                # Pakai min_volume paling rendah dari semua kategori — filter
                # spesifik per-kategori (crypto vs political) di-apply di _analyze_market
                scan_min_volume = min(
                    getattr(config, "MIN_MARKET_VOLUME", 10_000),
                    getattr(config, "POLITICAL_MIN_VOLUME", 5_000),
                )
                markets = await gamma.ascan_opportunities(
                    session,
                    min_volume          = scan_min_volume,
                    min_liquidity       = getattr(config, "MIN_MARKET_LIQUIDITY", 5_000),
                    max_days_to_resolve = getattr(config, "MAX_DAYS_TO_RESOLVE", 30),
                    min_days_to_resolve = getattr(config, "MIN_DAYS_TO_RESOLVE", 1),
                    limit               = 500,
                )
                log.info(f"Scan: {len(markets)} market lolos filter")

                if breaker.check(unrealized_pnl=manager.get_unrealized_pnl()).can_trade:
                    results = await asyncio.gather(*[
                        _analyze_market(
                            market, clob, gamma, detector, sizer, manager,
                            builder, breaker, capital, session
                        )
                        for market in markets
                    ], return_exceptions=True)
                    for r in results:
                        if isinstance(r, Exception):
                            logger.warning(f"[ANALYZE] Error di market analysis: {str(r).replace('[', '\\[')}")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"Error di cycle utama: {str(e).replace('[', '\\[')}")
                alert = get_alert()
                if alert:
                    await alert.alert_error(str(e), session)

            await asyncio.sleep(config.POLLING_INTERVAL)


def run_market_making_mode(clob: ClobClient):
    import time
    strategy = ScalarStrategy(KonfigurasiStrategy(
        spread_minimum = config.SPREAD_MINIMUM,
        jumlah_tick    = config.JUMLAH_TICK,
        mode_satu_sisi = config.MODE_SATU_SISI,
    ))

    log.info(f"[bold green]Market making mode aktif.[/bold green] Polling: {config.POLLING_INTERVAL}s")

    while True:
        snapshot = clob.ambil_snapshot()
        if not snapshot or not snapshot.valid:
            log.warning("[yellow]Snapshot tidak valid, menunggu...[/yellow]")
            time.sleep(config.POLLING_INTERVAL)
            continue

        sinyal = strategy.generate_sinyal(best_bid=snapshot.best_bid, best_ask=snapshot.best_ask)
        tampilkan_sinyal(sinyal, snapshot)

        if not config.DRY_RUN:
            if sinyal.sinyal in (SinyalTrade.BELI, SinyalTrade.KEDUANYA):
                clob.pasang_order(SisiOrder.BELI, sinyal.bid_diusulkan, config.UKURAN_ORDER)
            if sinyal.sinyal in (SinyalTrade.JUAL, SinyalTrade.KEDUANYA):
                clob.pasang_order(SisiOrder.JUAL, sinyal.ask_diusulkan, config.UKURAN_ORDER)

        time.sleep(config.POLLING_INTERVAL)


def main():
    tampilkan_header()

    if config.DRY_RUN:
        log.warning("[yellow]Mode DRY RUN aktif — tidak ada order nyata.[/yellow]")

    log.info(f"Strategy mode: [bold]{STRATEGY_MODE}[/bold]")

    clob = ClobClient()
    if not clob.hubungkan():
        log.error("[red]Gagal terhubung ke API. Periksa .env[/red]")
        sys.exit(1)

    if STRATEGY_MODE == "mispricing":
        async def _run():
            stop_event = asyncio.Event()

            def shutdown(sig, frame):
                log.info("[yellow]Shutdown... membatalkan order aktif.[/yellow]")
                clob.batalkan_semua_order()
                stop_event.set()

            signal.signal(signal.SIGINT, shutdown)
            signal.signal(signal.SIGTERM, shutdown)

            task = asyncio.create_task(run_mispricing_mode(clob))
            await stop_event.wait()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        try:
            asyncio.run(_run())
        except (KeyboardInterrupt, SystemExit):
            pass

    elif STRATEGY_MODE == "market_making":
        def shutdown(sig, frame):
            log.info("[yellow]Shutdown...[/yellow]")
            clob.batalkan_semua_order()
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)
        run_market_making_mode(clob)

    else:
        log.error(f"[red]STRATEGY_MODE tidak dikenal: {STRATEGY_MODE}[/red]")
        sys.exit(1)


def _extract_price_target(question: str) -> float | None:
    """Extract angka target harga dari teks pertanyaan."""
    patterns = [
        r'\$([0-9]{1,3}(?:,[0-9]{3})+)',    # $77,000
        r'\$([0-9]+(?:\.[0-9]+)?)[kK]',      # $77k
        r'\$([0-9]{4,})',                     # $77000
        r'[↑↓]\s*([0-9]{1,3}(?:,[0-9]{3})+)', # ↓ 77,000
        r'[↑↓]\s*([0-9]+(?:\.[0-9]+)?)[kK]',  # ↑ 77k
        r'[↑↓]\s*([0-9]+(?:\.[0-9]+)?)',       # ↓ 1.40 (XRP)
    ]
    for pattern in patterns:
        match = re.search(pattern, question)
        if match:
            raw = match.group(1).replace(",", "")
            try:
                value = float(raw)
                if 'k' in question[match.start():match.end()].lower():
                    value *= 1000
                return value
            except ValueError:
                continue
    return None


if __name__ == "__main__":
    main()