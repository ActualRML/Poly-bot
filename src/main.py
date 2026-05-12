import asyncio
import signal
import sys
import logging
import re
from decimal import Decimal
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import aiohttp
from datetime import datetime, timezone, timedelta
from typing import Optional

from src.api.clob_client import ClobClient
from src.api.gamma_client import GammaClient
from src.logic.mispricing import MispricingDetector, BaseRateBuilder, MispricingDirection
from src.logic.kelly import KellySizer
from src.logic.exit_strategy import ExitEvaluator
from src.logic.manager import PositionManager
from src.logic.probability import CryptoProbabilityCalculator
from src.logic.circuit_breaker import CircuitBreaker
from src.models.types import SisiOrder
from src.models.database import log_prediction, get_recent_closed_pnls, count_open_by_resolve_slot, get_recent_closed_hourly
from src.utils.config import config
from src.utils.logger import log, tampilkan_header
from src.utils.telegram_alert import init_telegram, get_alert
from src.logic.risk_manager import get_dynamic_stop_loss, calculate_position_size
from src.logic.strategy import get_dynamic_threshold, should_force_exit
from src.logic.candle_strategy import scan_candle_markets, analyze_candle_market
from src.logic.slot_manager import (
    slot_history_count, record_slot_entry, cleanup_old_slots,
    HOURLY_MAX_ENTRIES_PER_SLOT,
)
from src.logic.symbol_blacklist import check_symbol_blacklist, maybe_blacklist_symbol
from src.logic.price_stagnation import track_market_price, is_price_stagnant
from src.logic.hourly_scanner import scan_updown_hourly_markets
from src.logic.reentry_manager import (
    reentry_candidates, register_reentry_candidate, cleanup_reentry_candidates,
)
from src.utils.parsing import detect_symbol_from_question, extract_price_target

logger = logging.getLogger(__name__)

_price_cache: dict = {}
_cache_time: dict  = {}
_CACHE_TTL         = 300
_price_lock        = asyncio.Lock()
_open_position_lock = asyncio.Lock()
_cg_ban_until: float = 0.0
_CG_BAN_COOLDOWN    = 120

async def _fetch_crypto_price(symbol: str, session: aiohttp.ClientSession) -> float | None:
    from src.api.binance_client import fetch_price as _binance_price
    symbol = symbol.upper()

    price = await _binance_price(symbol, session)
    if price:
        return price

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

    async with _price_lock:
        global _cg_ban_until
        now = datetime.now(timezone.utc).timestamp()
        if symbol in _price_cache and now - _cache_time.get(symbol, 0) < _CACHE_TTL:
            return _price_cache[symbol]

        if now < _cg_ban_until:
            return _price_cache.get(symbol)

        try:
            async with session.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": coin_id, "vs_currencies": "usd"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 429:
                    _cg_ban_until = now + _CG_BAN_COOLDOWN
                    logger.warning(f"[COINGECKO] 429 — pause {_CG_BAN_COOLDOWN}s, pakai stale cache")
                    return _price_cache.get(symbol)
                resp.raise_for_status()
                data  = await resp.json()
                price = data.get(coin_id, {}).get("usd")

                if price:
                    _price_cache[symbol] = float(price)
                    _cache_time[symbol]  = now
                    log.info(f"[PRICE] {symbol} CoinGecko = ${float(price):,.4f}")
                    return float(price)

        except Exception as e:
            logger.warning(f"Gagal fetch harga {symbol}: {e}")

        return None

async def _prefetch_prices(session: aiohttp.ClientSession) -> None:
    await asyncio.gather(
        _fetch_crypto_price("BTC", session),
        _fetch_crypto_price("ETH", session),
        _fetch_crypto_price("SOL", session),
        _fetch_crypto_price("BNB", session),
        return_exceptions=True,
    )

async def _build_vol_data(session: aiohttp.ClientSession, hours: int | None = None) -> dict:
    from src.api.binance_client import fetch_realized_vol
    if hours is None:
        hours = getattr(config, "HOURLY_VOL_HOURS", 4)

    symbols = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE"]
    results = await asyncio.gather(
        *(fetch_realized_vol(s, session, hours=hours) for s in symbols),
        return_exceptions=True,
    )
    vol_data: dict = {"DEFAULT": 0.40}
    for symbol, result in zip(symbols, results):
        if isinstance(result, float) and result > 0:
            vol_data[symbol] = result
    return vol_data

async def _get_base_rates(
    market: dict, builder, session: aiohttp.ClientSession,
    vol_data: dict | None = None,
) -> list:
    question     = (market.get("question") or "").lower()
    calc         = CryptoProbabilityCalculator()
    end_date_str = market.get("endDate") or market.get("end_date_iso", "")

    days_remaining: float = 30.0
    try:
        end_date  = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        delta_sec = (end_date - datetime.now(timezone.utc)).total_seconds()
        days_remaining = max(0.001, delta_sec / 86400.0)
    except Exception:
        pass

    if any(k in question for k in ["between", "range", "dip to"]):
        return []

    if "up or down" in question:
        return []

    async def _check_crypto(symbol: str, keywords: list[str]) -> list | None:
        if not any(k in question for k in keywords):
            return None
        price = await _fetch_crypto_price(symbol, session)
        if not price:
            return []
        target = extract_price_target(question)
        if not target:
            return []
        direction = "below" if any(k in question for k in ["dip", "drop", "fall", "below", "↓"]) else "above"
        use_barrier = " on " not in question

        if vol_data and symbol in vol_data:
            volatility = vol_data[symbol]
        else:
            from src.api.binance_client import fetch_realized_vol
            volatility = await fetch_realized_vol(symbol, session, hours=getattr(config, "HOURLY_VOL_HOURS", 4))

        result = await calc.calculate_async(
            symbol, price, target, days_remaining, session,
            direction=direction, use_barrier=use_barrier,
            volatility=volatility, drift=None,
        )
        if result.probability == 0.0:
            return []
        return [builder.from_manual(rate=result.probability, confidence=result.confidence, notes=result.notes)]

    for symbol, keywords in [
        ("BTC",  ["bitcoin", "btc"]),
        ("ETH",  ["ethereum", " eth ", "ether "]),
        ("SOL",  ["solana", " sol "]),
        ("BNB",  [" bnb ", "binance coin"]),
    ]:
        result = await _check_crypto(symbol, keywords)
        if result is not None:
            return result

    return []

async def _analyze_market(
    market, clob, gamma, detector, sizer, manager,
    builder, breaker, capital, session, vol_data: dict | None = None,
    closed_this_cycle: set | None = None,
    profit_locked_markets: set | None = None,
):
    from src.logic.pricing import ke_decimal

    condition_id = market.get("conditionId", market.get("id", ""))
    question     = market.get("question", market.get("title", ""))

    if closed_this_cycle and condition_id in closed_this_cycle:
        logger.debug(f"Skip {condition_id[:8]} — closed this cycle, no re-entry")
        return
    if profit_locked_markets and condition_id in profit_locked_markets:
        logger.debug(f"Skip {condition_id[:8]} — profit locked this session, no re-entry")
        return
    prices       = gamma.get_token_prices(market)
    yes_price    = prices.get("Yes")

    if not yes_price or yes_price <= 0 or yes_price >= 1:
        return

    yes_base_rates = await _get_base_rates(market, builder, session, vol_data=vol_data)
    if not yes_base_rates:
        return

    question_lower = question.lower()
    asset = "DEFAULT"
    for sym, kws in [
        ("BTC", ["bitcoin", "btc"]),
        ("ETH", ["ethereum", " eth ", "ether "]),
        ("SOL", ["solana", " sol "]),
        ("BNB", [" bnb ", "binance coin"]),
    ]:
        if any(k in question_lower for k in kws):
            asset = sym
            break
    threshold  = get_dynamic_threshold(asset, vol_data or {})
    min_volume = getattr(config, "HOURLY_MIN_MARKET_VOLUME", 500.0)

    market_vol = float(market.get("volume") or market.get("volumeNum") or 0)
    if market_vol < min_volume:
        logger.debug(f"Skip {question[:40]} | volume ${market_vol:,.0f} < ${min_volume:,.0f}")
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

        min_wr = config.HOURLY_MIN_WINRATE_STRICT
        if buy_winrate < min_wr:
            logger.debug(f"Skip {question[:40]} | winrate {buy_winrate:.2f} < {min_wr:.2f}")
            continue

        kelly = sizer.calculate(
            winrate      = buy_winrate,
            market_price = buy_price,
            capital      = capital,
        )

        if not kelly.is_positive_ev or float(kelly.bet_usdc) <= 0:
            logger.debug(f"Skip {question[:40]} | EV={float(kelly.expected_value):.3f}")
            continue

        max_size = calculate_position_size(get_recent_closed_pnls(limit=5))
        if float(kelly.bet_usdc) > max_size:
            from dataclasses import replace as _dc_replace
            capped_usdc   = Decimal(str(max_size))
            capped_shares = (capped_usdc / Decimal(str(buy_price))).quantize(Decimal("0.0001"))
            kelly = _dc_replace(kelly, bet_usdc=capped_usdc, shares=capped_shares)

        profit_if_win = (1.0 - buy_price) * float(kelly.shares)
        min_profit    = float(kelly.bet_usdc) * getattr(config, "MIN_PROFIT_PCT", 0.15)
        if profit_if_win < min_profit:
            logger.debug(f"Skip {question[:40]} | profit terlalu kecil: ${profit_if_win:.2f} < ${min_profit:.2f}")
            continue

        async with _open_position_lock:
            can_open, reason = manager.can_open(
                condition_id  = condition_id,
                outcome       = buy_outcome,
                bet_usdc      = kelly.bet_usdc,
                total_capital = ke_decimal(capital),
            )

            if not can_open:
                logger.debug(f"Skip {condition_id[:8]} {buy_outcome}: {reason}")
                continue

            if config.CB_ENABLED and not breaker.check(unrealized_pnl=manager.get_unrealized_pnl()).can_trade:
                logger.warning("[CIRCUIT BREAKER] Skip posisi — circuit breaker triggered")
                return

            log.info(
                f"[bold cyan]SIGNAL[/bold cyan] {question[:40]} | "
                f"BUY {buy_outcome} @ {buy_price:.3f} | "
                f"Gap {result.gap_pct:.1f}% | Winrate {buy_winrate:.0%} | "
                f"Kelly ${float(kelly.bet_usdc):.2f} ({float(kelly.bet_fraction):.1%}) | "
                f"EV {float(kelly.expected_value):.3f}"
            )

            tokens   = gamma.extract_token_ids(market)
            token    = next((t for t in tokens if t["outcome"] == buy_outcome), None)
            token_id = str(token["token_id"]) if token and token.get("token_id") else ""

            if config.DRY_RUN:
                log.warning("[yellow]DRY RUN — simulasi posisi dibuka[/yellow]")
                await _dry_run_open(result, kelly, market, manager, buy_outcome, buy_price, token_id)
            else:
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
                        strategy_mode   = "daily",
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
                strategy = "Daily Crypto",
            )

async def _dry_run_open(result, kelly, market, manager, buy_outcome: str, buy_price: float, token_id: str = ""):
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
        strategy_mode   = "daily_dry_run",
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
    import json
    try:
        market = await gamma.aget_market(condition_id, session)
        if not market:
            return None
        outcomes       = market.get("outcomes", [])
        outcome_prices = market.get("outcomePrices", [])
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if isinstance(outcome_prices, str):
            outcome_prices = json.loads(outcome_prices)
        if not outcome_prices:
            return None
        prices_float = [float(p) for p in outcome_prices]
        if outcome not in outcomes:
            return None
        idx = outcomes.index(outcome)
        if 0 <= idx < len(prices_float):
            return prices_float[idx]
    except Exception as e:
        logger.debug(f"Gagal fetch resolved price {condition_id[:8]} {outcome}: {e}")
    return None

async def reconcile_positions(clob, gamma, manager, breaker, session: aiohttp.ClientSession) -> None:
    from src.models.database import get_open_positions
    from src.logic.pricing import ke_decimal

    positions = get_open_positions()
    if not positions:
        return

    log.info(f"[RECONCILE] Memeriksa {len(positions)} posisi open saat startup...")
    closed_count = 0

    for pos in positions:
        cid     = pos["condition_id"]
        outcome = pos["outcome"]
        tid     = pos.get("token_id") or ""

        try:
            price = await _get_resolved_price_from_gamma(gamma, session, cid, outcome)

            if price is not None and not (price >= 0.98 or price <= 0.02):
                price = None

            if price is None and tid:
                try:
                    snapshot = clob.ambil_snapshot(token_id=tid)
                    if snapshot and snapshot.valid:
                        clob_price = float(snapshot.best_bid)
                        if clob_price >= 0.98 or clob_price <= 0.02:
                            price = clob_price
                except Exception:
                    pass

            if price is None:
                logger.debug(f"[RECONCILE] {cid[:8]} {outcome} — market masih aktif, skip")
                continue

            entry  = ke_decimal(pos["entry_price"])
            shares = ke_decimal(pos["shares"])
            pnl    = (ke_decimal(str(price)) - entry) * shares
            won    = float(pnl) > 0

            manager._process_exit_manual(cid, outcome, ke_decimal(str(price)), pnl, "reconcile_startup")
            breaker.record_trade(float(pnl))
            closed_count += 1

            log.info(
                f"[RECONCILE] {'✅ WIN' if won else '❌ LOSE'} — "
                f"{pos['question'][:45]} | {outcome} @ {price:.3f} | PnL: ${float(pnl):+.2f}"
            )

            alert = get_alert()
            if alert:
                await alert.alert_exit(
                    question    = pos["question"],
                    outcome     = outcome,
                    entry_price = float(ke_decimal(pos["entry_price"])),
                    exit_price  = price,
                    pnl_usdc    = float(pnl),
                    reason      = "reconcile_startup",
                    session     = session,
                )

        except asyncio.TimeoutError:
            log.warning(f"[RECONCILE] Timeout cek {cid[:8]} {outcome} — skip, akan dicek ulang di loop")
        except Exception as e:
            log.warning(f"[RECONCILE] Gagal cek {cid[:8]} {outcome}: {e} — skip")

    if closed_count:
        log.info(f"[RECONCILE] Selesai — {closed_count}/{len(positions)} posisi di-close (resolved saat bot mati)")
    else:
        log.info(f"[RECONCILE] Selesai — semua {len(positions)} posisi masih aktif")

async def _resolve_checker(clob, gamma, manager, breaker, session: aiohttp.ClientSession, current_prices: dict | None = None) -> set[str]:
    from src.models.database import get_open_positions
    from src.logic.pricing import ke_decimal

    now       = datetime.now(timezone.utc)
    positions = get_open_positions()
    expired   = []
    closed: set[str] = set()

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
        return closed

    log.info(f"[RESOLVE CHECK] {len(expired)} posisi sudah melewati resolve_date")

    for pos, resolve in expired:
        cid     = pos["condition_id"]
        outcome = pos["outcome"]
        tid     = pos.get("token_id") or ""

        price = await _get_resolved_price_from_gamma(gamma, session, cid, outcome)

        if price is None and tid:
            try:
                snapshot = clob.ambil_snapshot(token_id=tid)
                if snapshot and snapshot.valid:
                    price = float(snapshot.best_bid)
            except Exception:
                pass

        hours_past = (now - resolve).total_seconds() / 3600

        if price is None and current_prices:
            cached = (current_prices.get(cid) or {}).get(outcome)
            if cached is not None:
                price = float(cached)

        if price is None:
            if hours_past > 2:
                entry_price = float(ke_decimal(pos["entry_price"]))
                price = entry_price
                log.warning(
                    f"[RESOLVE CHECK] {pos['question'][:45]} | {outcome} — "
                    f"tidak bisa fetch harga setelah {hours_past:.1f}h, force close @ entry"
                )
            else:
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
            _log_fn = log.debug if hours_past < 1.0 else log.warning
            _log_fn(
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
        if (
            pos.get("strategy_mode") in ("updown_hourly", "updown_hourly_dry_run")
            and float(pnl) < 0
        ):
            _sym = detect_symbol_from_question(pos.get("question", ""))
            if _sym != "UNKNOWN":
                maybe_blacklist_symbol(_sym)
        closed.add(cid)

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

    return closed

async def _fetch_current_prices(clob, manager) -> dict:
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

_UPDOWN_SERIES = {
    "BTC": "41",
    "ETH": "40",
    "SOL": "10086",
    "XRP": "10100",
}

async def _scan_updown_markets(session: aiohttp.ClientSession, gamma: GammaClient) -> list[dict]:
    import json as _json
    results = []
    for symbol, series_id in _UPDOWN_SERIES.items():
        try:
            batch = await gamma._aget(
                "/events",
                session,
                params={
                    "series_id": series_id,
                    "closed":    "false",
                    "limit":     1,
                    "order":     "startDate",
                    "ascending": "false",
                },
            )
        except Exception as e:
            logger.debug(f"[UPDOWN] Gagal fetch events {symbol}: {e}")
            continue

        if not isinstance(batch, list) or not batch:
            continue

        e    = batch[0]
        mkts = e.get("markets", [])
        if not mkts:
            continue
        mkt = mkts[0]

        outcomes = mkt.get("outcomes", [])
        if isinstance(outcomes, str):
            try: outcomes = _json.loads(outcomes)
            except: outcomes = []
        op = mkt.get("outcomePrices", [])
        if isinstance(op, str):
            try: op = _json.loads(op)
            except: op = []

        outcomes_lower = [str(o).lower() for o in outcomes]
        if "up" not in outcomes_lower or not op:
            continue

        mkt["_symbol"] = symbol
        mkt["endDate"] = e.get("endDate", mkt.get("endDate", ""))
        results.append(mkt)

    return results

async def _analyze_updown_market(
    market: dict, clob, gamma, sizer, manager,
    breaker, capital: float, session: aiohttp.ClientSession,
    vol_data: dict | None = None,
    closed_this_cycle: set | None = None,
    profit_locked_markets: set | None = None,
    market_regime: dict | None = None,
):
    import json as _json
    from src.logic.updown_strategy import calculate_updown_probability
    from src.logic.pricing import ke_decimal

    symbol = market.get("_symbol", "")
    if not symbol:
        return

    condition_id = market.get("conditionId", market.get("id", ""))

    if closed_this_cycle and condition_id in closed_this_cycle:
        logger.debug(f"[UPDOWN] Skip {condition_id[:8]} — closed this cycle, no re-entry")
        return
    if profit_locked_markets and condition_id in profit_locked_markets:
        logger.debug(f"[UPDOWN] Skip {condition_id[:8]} — profit locked this session, no re-entry")
        return
    question     = market.get("question", market.get("title", f"{symbol} Up or Down Daily"))

    outcomes = market.get("outcomes", [])
    op       = market.get("outcomePrices", [])
    if isinstance(outcomes, str):
        try: outcomes = _json.loads(outcomes)
        except: outcomes = []
    if isinstance(op, str):
        try: op = _json.loads(op)
        except: op = []

    outcomes_lower = [str(o).lower() for o in outcomes]
    if "up" not in outcomes_lower or not op:
        return
    try:
        up_idx          = outcomes_lower.index("up")
        market_price_up = float(op[up_idx])
    except (ValueError, IndexError):
        return

    if market_price_up <= 0 or market_price_up >= 1:
        return

    end_date_str = market.get("endDate", "")
    try:
        end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
    except Exception:
        return

    if (end_date - datetime.now(timezone.utc)).total_seconds() <= 0:
        return

    delta_sec = (end_date - datetime.now(timezone.utc)).total_seconds()
    max_hours = getattr(config, "UPDOWN_MAX_HOURS", 8.0)
    if delta_sec > max_hours * 3600:
        logger.debug(f"[UPDOWN] {symbol} {delta_sec/3600:.1f}h left > {max_hours}h max — terlalu awal, skip")
        return

    min_hours = getattr(config, "UPDOWN_DAILY_MIN_HOURS_TO_RESOLVE", 2.0)
    if delta_sec < min_hours * 3600:
        logger.debug(
            f"[UPDOWN] {symbol} {delta_sec/3600:.2f}h left < {min_hours}h floor "
            f"— vol regime risk too high, skip"
        )
        return

    _vol_floor = getattr(config, "UPDOWN_VOL_FLOOR", 0.0)
    if _vol_floor > 0 and vol_data:
        vol_data = {
            k: max(float(v), _vol_floor) if isinstance(v, (int, float)) else v
            for k, v in vol_data.items()
        }

    prob_up = await calculate_updown_probability(symbol, session, vol_data or {}, end_date)
    if prob_up is None:
        return

    edge      = prob_up - market_price_up
    threshold = config.UPDOWN_THRESHOLD

    if abs(edge) < threshold:
        logger.debug(f"[UPDOWN] {symbol} edge={edge:+.3f} < {threshold:.2f} — skip")
        return

    if edge > 0:
        buy_outcome = "Up"
        buy_price   = market_price_up
        buy_winrate = prob_up
    else:
        buy_outcome = "Down"
        buy_price   = round(1.0 - market_price_up, 4)
        buy_winrate = round(1.0 - prob_up, 4)

    if buy_price <= 0 or buy_price >= 1:
        return

    if market_regime and market_regime.get("skip_contrarian"):
        regime_dir = market_regime.get("direction")  # "up" / "down" / None
        if regime_dir == "up" and buy_outcome == "Down":
            logger.debug(
                f"[UPDOWN] {symbol} skip — model picks Down but cross-asset trending UP "
                f"(score={market_regime.get('trend_score')})"
            )
            return
        if regime_dir == "down" and buy_outcome == "Up":
            logger.debug(
                f"[UPDOWN] {symbol} skip — model picks Up but cross-asset trending DOWN "
                f"(score={market_regime.get('trend_score')})"
            )
            return

    min_wr = getattr(config, "UPDOWN_MIN_WINRATE", 0.55)
    if buy_winrate < min_wr:
        logger.debug(f"[UPDOWN] {symbol} winrate {buy_winrate:.2f} < {min_wr:.2f} — skip")
        return

    kelly = sizer.calculate(
        winrate      = buy_winrate,
        market_price = buy_price,
        capital      = capital,
    )

    if not kelly.is_positive_ev or float(kelly.bet_usdc) <= 0:
        return

    max_size = calculate_position_size(get_recent_closed_pnls(limit=5))
    if float(kelly.bet_usdc) > max_size:
        from dataclasses import replace as _dc_replace
        capped_usdc   = Decimal(str(max_size))
        capped_shares = (capped_usdc / Decimal(str(buy_price))).quantize(Decimal("0.0001"))
        kelly = _dc_replace(kelly, bet_usdc=capped_usdc, shares=capped_shares)

    t_hours = delta_sec / 3600.0
    log.info(
        f"[bold cyan][UPDOWN][/bold cyan] {symbol} {t_hours:.1f}h left | "
        f"BUY {buy_outcome} @ {buy_price:.3f} | "
        f"P(Up)={prob_up:.3f} Mkt={market_price_up:.3f} Edge={edge:+.3f} | "
        f"Kelly ${float(kelly.bet_usdc):.2f}"
    )

    tokens   = gamma.extract_token_ids(market)
    _bo_lower = buy_outcome.lower()
    token    = next((t for t in tokens if str(t.get("outcome", "")).lower() == _bo_lower), None)
    token_id = str(token["token_id"]) if token and token.get("token_id") else ""

    async with _open_position_lock:
        can_open, reason = manager.can_open(
            condition_id  = condition_id,
            outcome       = buy_outcome,
            bet_usdc      = kelly.bet_usdc,
            total_capital = ke_decimal(capital),
        )
        if not can_open:
            logger.debug(f"[UPDOWN] Skip {condition_id[:8]} {buy_outcome}: {reason}")
            return

        if config.CB_ENABLED and not breaker.check(unrealized_pnl=manager.get_unrealized_pnl()).can_trade:
            return

        try:
            resolve_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        except Exception:
            resolve_date = datetime.now(timezone.utc)

        if config.DRY_RUN:
            log.warning("[yellow][UPDOWN] DRY RUN — simulasi posisi dibuka[/yellow]")
            manager.open_position(
                condition_id    = condition_id,
                question        = question,
                outcome         = buy_outcome,
                entry_price     = ke_decimal(buy_price),
                shares          = kelly.shares,
                capital_at_risk = kelly.bet_usdc,
                resolve_date    = resolve_date,
                gap_pct         = abs(edge),
                kelly_fraction  = float(kelly.bet_fraction),
                strategy_mode   = "updown_dry_run",
                token_id        = token_id,
            )
            log_prediction({
                "condition_id":   condition_id,
                "question":       question,
                "outcome":        buy_outcome,
                "predicted_prob": str(round(buy_winrate, 4)),
                "market_price":   str(buy_price),
                "gap_pct":        str(round(abs(edge) * 100, 2)),
                "resolve_date":   resolve_date.isoformat(),
            })
        else:
            if not token_id:
                logger.warning(f"[UPDOWN] token_id tidak ditemukan untuk {buy_outcome}")
                return

            order = clob.pasang_order(
                sisi     = SisiOrder.BELI,
                harga    = ke_decimal(buy_price),
                ukuran   = kelly.shares,
                token_id = token_id,
            )

            if order:
                manager.open_position(
                    condition_id    = condition_id,
                    question        = question,
                    outcome         = buy_outcome,
                    entry_price     = ke_decimal(buy_price),
                    shares          = kelly.shares,
                    capital_at_risk = kelly.bet_usdc,
                    resolve_date    = resolve_date,
                    gap_pct         = abs(edge),
                    kelly_fraction  = float(kelly.bet_fraction),
                    strategy_mode   = "updown",
                    token_id        = token_id,
                )
                log_prediction({
                    "condition_id":   condition_id,
                    "question":       question,
                    "outcome":        buy_outcome,
                    "predicted_prob": str(round(buy_winrate, 4)),
                    "market_price":   str(buy_price),
                    "gap_pct":        str(round(abs(edge) * 100, 2)),
                    "resolve_date":   resolve_date.isoformat(),
                })

        alert = get_alert()
        if alert:
            await alert.alert_signal(
                question = question,
                outcome  = buy_outcome,
                price    = buy_price,
                bet_usdc = float(kelly.bet_usdc),
                gap_pct  = abs(edge) * 100,
                ev       = float(kelly.expected_value),
                session  = session,
                dry_run  = config.DRY_RUN,
                strategy = "Up/Down Daily",
            )


async def _scan_reentry_opportunities(
    clob, sizer, manager, breaker, capital: float,
    session: aiohttp.ClientSession,
    btc_scalp: dict | None = None,
    symbol_momentum_map: dict | None = None,
) -> None:
    from decimal import Decimal as _D
    from src.logic.reentry import (
        estimate_fair_value, check_reentry_signal,
        validate_reentry_orderbook, passes_time_gate,
    )
    from src.logic.pricing import ke_decimal as _ked

    if not reentry_candidates:
        return

    log.info(f"[REENTRY SCAN] {len(reentry_candidates)} kandidat dipantau")
    _recent_closed = get_recent_closed_hourly(limit=20)

    for cid in list(reentry_candidates.keys()):
        ctx = reentry_candidates[cid]
        symbol     = ctx["symbol"]
        outcome    = ctx["outcome"]

        if check_symbol_blacklist(symbol):
            logger.debug(f"[REENTRY] {symbol} blacklisted — skip reentry candidate")
            continue

        _sym_trades = [
            r for r in _recent_closed
            if detect_symbol_from_question(r.get("question", "")) == symbol.upper()
        ]
        if _sym_trades and _sym_trades[0].get("pnl", 0) < 0:
            logger.debug(
                f"[REENTRY] {symbol} — last hourly trade was LOSS "
                f"(${_sym_trades[0]['pnl']:.2f}), skip reentry"
            )
            continue
        exit_price = ctx["exit_price"]
        token_id   = ctx["token_id"]
        try:
            resolve_dt = datetime.fromisoformat(ctx["resolve_date_iso"])
        except Exception:
            reentry_candidates.pop(cid, None)
            continue

        mins_to_resolve = (resolve_dt - datetime.now(timezone.utc)).total_seconds() / 60.0
        if mins_to_resolve <= 0:
            reentry_candidates.pop(cid, None)
            continue
        if not passes_time_gate(mins_to_resolve, min_minutes=15.0):
            logger.debug(f"[REENTRY] {symbol} {cid[:8]} — {mins_to_resolve:.0f}m left < 15m gate, skip")
            continue

        slot_open = count_open_by_resolve_slot(resolve_dt)
        slot_hist = slot_history_count(resolve_dt)
        if config.MAX_POSITIONS_PER_SLOT > 0 and slot_open >= config.MAX_POSITIONS_PER_SLOT:
            logger.debug(f"[REENTRY] {symbol} {cid[:8]} — slot OPEN cap reached")
            continue
        if slot_hist >= HOURLY_MAX_ENTRIES_PER_SLOT:
            logger.debug(f"[REENTRY] {symbol} {cid[:8]} — slot CUMULATIVE cap reached")
            continue

        try:
            snap = clob.ambil_snapshot(token_id=token_id)
            if not snap or not snap.valid:
                continue
            current_market_price = float(snap.best_ask)
        except Exception as e:
            logger.debug(f"[REENTRY] {symbol} {cid[:8]} — snapshot error: {e}")
            continue

        sym_mtf = (symbol_momentum_map or {}).get(symbol)
        fair_value = estimate_fair_value(outcome, btc_scalp, sym_mtf)
        if fair_value is None:
            logger.debug(f"[REENTRY] {symbol} {cid[:8]} — no fair value (missing data)")
            continue

        sig = check_reentry_signal(
            exit_price=exit_price,
            current_market_price=current_market_price,
            fair_value=fair_value,
            drop_threshold=0.30, min_edge=0.05,
        )
        if not sig["should_reenter"]:
            logger.debug(
                f"[REENTRY] {symbol} {cid[:8]} {outcome} — {sig['reason']}"
            )
            continue

        full_ob = clob.get_full_orderbook(token_id)
        bids = full_ob.get("bids", [])
        asks = full_ob.get("asks", [])
        if not asks and snap and snap.valid:
            asks = [(float(snap.best_ask), float(snap.ask_size))]
        capital_required = ctx["original_capital_usdc"] / 2.0  # half size
        ob = validate_reentry_orderbook(
            bids=bids, asks=asks,
            capital_required=capital_required, spread_max=0.05,
        )
        if not ob["ok"]:
            logger.debug(f"[REENTRY] {symbol} {cid[:8]} — orderbook fail: {ob['reason']}")
            continue

        kelly_capital = capital_required
        kelly_shares  = (_D(str(kelly_capital)) / _D(str(current_market_price))).quantize(_D("0.0001"))

        log.info(
            f"[bold magenta][REENTRY][/bold magenta] {symbol} {outcome} "
            f"@ {current_market_price:.3f} | "
            f"TP exit {exit_price:.3f} → drop {sig['drop_pct']:.0%} | "
            f"fair {fair_value:.2f} (edge {sig['edge']:+.3f}) | "
            f"capital ${kelly_capital:.2f} (½ original) | "
            f"{mins_to_resolve:.0f}m left"
        )

        async with _open_position_lock:
            can_open, reason = manager.can_open(
                condition_id=cid, outcome=outcome,
                bet_usdc=_D(str(kelly_capital)),
                total_capital=_ked(capital),
            )
            if not can_open:
                logger.debug(f"[REENTRY] {cid[:8]} — manager skip: {reason}")
                continue
            if config.CB_ENABLED and not breaker.check(unrealized_pnl=manager.get_unrealized_pnl()).can_trade:
                continue

            entry_succeeded = False
            if config.DRY_RUN:
                manager.open_position(
                    condition_id    = cid,
                    question        = ctx["question"],
                    outcome         = outcome,
                    entry_price     = _ked(current_market_price),
                    shares          = kelly_shares,
                    capital_at_risk = _D(str(kelly_capital)),
                    resolve_date    = resolve_dt,
                    gap_pct         = sig["edge"],
                    kelly_fraction  = 0.5,
                    strategy_mode   = "updown_hourly_dry_run",
                    token_id        = token_id,
                )
                log.warning(f"[yellow][REENTRY DRY RUN] {symbol} {outcome} simulasi[/yellow]")
                entry_succeeded = True
            else:
                order = clob.pasang_order(
                    sisi=SisiOrder.BELI, harga=_ked(current_market_price),
                    ukuran=kelly_shares, token_id=token_id,
                )
                if order:
                    manager.open_position(
                        condition_id    = cid,
                        question        = ctx["question"],
                        outcome         = outcome,
                        entry_price     = _ked(current_market_price),
                        shares          = kelly_shares,
                        capital_at_risk = _D(str(kelly_capital)),
                        resolve_date    = resolve_dt,
                        gap_pct         = sig["edge"],
                        kelly_fraction  = 0.5,
                        strategy_mode   = "updown_hourly",
                        token_id        = token_id,
                    )
                    entry_succeeded = True
                else:
                    logger.warning(f"[REENTRY] {symbol} {cid[:8]} order gagal — keep candidate, retry next cycle")

            if entry_succeeded:
                record_slot_entry(resolve_dt)
                reentry_candidates.pop(cid, None)


async def _analyze_updown_hourly_market(
    market: dict, clob, gamma, sizer, manager,
    breaker, capital: float, session: aiohttp.ClientSession,
    vol_data: dict | None = None,
    closed_this_cycle: set | None = None,
    profit_locked_markets: dict | None = None,
    btc_regime: float | None = None,
    btc_scalp: dict | None = None,
    symbol_momentum_map: dict | None = None,
    market_session: str = "US_MAIN",
):
    import json as _json
    from src.logic.pricing import ke_decimal

    symbol = market.get("_symbol", "")
    if not symbol:
        return

    condition_id = market.get("conditionId", market.get("id", ""))

    if closed_this_cycle and condition_id in closed_this_cycle:
        logger.debug(f"[UPDOWN HOURLY] Skip {condition_id[:8]} — closed this cycle, no re-entry")
        return

    locked_outcome: str | None = None
    if profit_locked_markets and condition_id in profit_locked_markets:
        if not getattr(config, "UPDOWN_HOURLY_OPPOSITE_REENTRY", False):
            logger.debug(
                f"[UPDOWN HOURLY] Skip {condition_id[:8]} — profit locked this session, no re-entry"
            )
            return
        locked_outcome = profit_locked_markets.get(condition_id) if isinstance(profit_locked_markets, dict) else None
        if not locked_outcome:
            logger.debug(
                f"[UPDOWN HOURLY] Skip {condition_id[:8]} — profit locked (no outcome recorded)"
            )
            return
    question     = market.get("question", market.get("title", f"{symbol} Up or Down Hourly"))

    outcomes = market.get("outcomes", [])
    op       = market.get("outcomePrices", [])
    if isinstance(outcomes, str):
        try: outcomes = _json.loads(outcomes)
        except: outcomes = []
    if isinstance(op, str):
        try: op = _json.loads(op)
        except: op = []

    outcomes_lower = [str(o).lower() for o in outcomes]
    if "up" not in outcomes_lower or not op:
        return
    try:
        up_idx          = outcomes_lower.index("up")
        market_price_up = float(op[up_idx])
    except (ValueError, IndexError):
        return

    if market_price_up <= 0 or market_price_up >= 1:
        return

    end_date_str   = market.get("endDate", "")
    start_date_str = market.get("_start_date", "")
    try:
        end_date   = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        start_date = datetime.fromisoformat(start_date_str.replace("Z", "+00:00"))
    except Exception:
        return

    now = datetime.now(timezone.utc)
    delta_sec = (end_date - now).total_seconds()
    if delta_sec <= 0:
        return

    candle_open_min = getattr(config, "UPDOWN_HOURLY_CANDLE_OPEN_MIN", 10)
    if now < start_date + timedelta(minutes=candle_open_min):
        logger.debug(
            f"[UPDOWN HOURLY] {symbol} candle baru buka "
            f"{(now - start_date).total_seconds() / 60:.1f}m — "
            f"tunggu {candle_open_min}m setelah open"
        )
        return

    _min_t_min = getattr(config, "UPDOWN_HOURLY_MIN_T_MINUTES", 20)
    if delta_sec < _min_t_min * 60:
        logger.debug(
            f"[UPDOWN HOURLY] {symbol} {delta_sec/60:.1f}m tersisa < {_min_t_min}m floor — skip"
        )
        return

    track_market_price(condition_id, market_price_up)

    slot_open_count = count_open_by_resolve_slot(end_date)
    slot_history    = slot_history_count(end_date)
    max_per_slot    = config.MAX_POSITIONS_PER_SLOT
    if max_per_slot > 0 and slot_open_count >= max_per_slot:
        logger.debug(
            f"[UPDOWN HOURLY] Skip {symbol} — "
            f"{slot_open_count}/{max_per_slot} OPEN di slot {end_date.strftime('%H:%M')} UTC"
        )
        return
    _max_entries = getattr(config, "UPDOWN_HOURLY_MAX_ENTRIES_PER_SLOT", HOURLY_MAX_ENTRIES_PER_SLOT)
    if slot_history >= _max_entries:
        logger.debug(
            f"[UPDOWN HOURLY] Skip {symbol} — "
            f"{slot_history}/{_max_entries} CUMULATIVE entries "
            f"di slot {end_date.strftime('%H:%M')} UTC (slot exhausted)"
        )
        return

    if check_symbol_blacklist(symbol):
        logger.debug(f"[UPDOWN HOURLY] {symbol} blacklisted — skip")
        return

    sym_mtf = (symbol_momentum_map or {}).get(symbol.upper())
    if sym_mtf is None:
        logger.warning(f"[UPDOWN HOURLY] {symbol} — no momentum data (Binance fetch failed?), skip")
        return

    sym_momentum   = sym_mtf["m_15m"]
    sym_m5         = sym_mtf["m_5m"]
    sym_m30        = sym_mtf["m_30m"]
    sym_vol_ratio  = sym_mtf["vol_ratio"]

    use_gbm = getattr(config, "UPDOWN_HOURLY_USE_GBM", True)

    if not use_gbm:
        regime_thr = config.UPDOWN_HOURLY_MOMENTUM_THRESHOLD
        regime_max = getattr(config, "UPDOWN_HOURLY_MOMENTUM_MAX", 0.008)
        if regime_thr <= 0 or abs(sym_momentum) < regime_thr:
            logger.debug(
                f"[UPDOWN HOURLY] {symbol} — momentum 15m {sym_momentum:+.2%} "
                f"< threshold {regime_thr:.1%}, skip"
            )
            return
        if regime_max > 0 and abs(sym_momentum) > regime_max:
            logger.debug(
                f"[UPDOWN HOURLY] {symbol} — momentum 15m {sym_momentum:+.2%} "
                f"> max {regime_max:.1%}, trend terlalu kuat"
            )
            return
        if abs(sym_m30) > regime_max * 1.5:
            logger.debug(
                f"[UPDOWN HOURLY] {symbol} — 30m momentum {sym_m30:+.2%} terlalu kuat "
                f"untuk contrarian, skip"
            )
            return

    min_vol_ratio = config.UPDOWN_HOURLY_MIN_VOL_RATIO
    if sym_vol_ratio < min_vol_ratio:
        logger.debug(
            f"[UPDOWN HOURLY] {symbol} — volume ratio {sym_vol_ratio:.2f} "
            f"< {min_vol_ratio} (low conviction), skip"
        )
        return

    if is_price_stagnant(condition_id):
        logger.debug(
            f"[UPDOWN HOURLY] {symbol} — Polymarket price stagnan "
            f"(<0.5% range dalam 5m), skip"
        )
        return

    _gbm_decision: dict | None = None
    if use_gbm:
        from src.logic.gbm_hourly import evaluate_hourly_entry
        vol_annual = (vol_data or {}).get(symbol.upper()) or (vol_data or {}).get("DEFAULT") or 0.40
        _vol_floor = getattr(config, "UPDOWN_VOL_FLOOR", 0.0)
        if _vol_floor > 0:
            vol_annual = max(vol_annual, _vol_floor)
        _vol_edge_factor = config.UPDOWN_GBM_VOL_EDGE_FACTOR
        _vol_edge_cap    = config.UPDOWN_GBM_VOL_EDGE_CAP
        _adj_min_edge = config.UPDOWN_HOURLY_GBM_MIN_EDGE + min(
            vol_annual * _vol_edge_factor, _vol_edge_cap
        )
        logger.debug(
            f"[UPDOWN HOURLY] {symbol} vol-adj min_edge: "
            f"{config.UPDOWN_HOURLY_GBM_MIN_EDGE:.0%} + {min(vol_annual * _vol_edge_factor, _vol_edge_cap):.1%} "
            f"= {_adj_min_edge:.1%} (vol={vol_annual:.0%})"
        )
        if config.UPDOWN_GBM_REGIME_SURCHARGE_ENABLED:
            from src.logic.regime_filter import detect_market_regime as _det_regime
            _regime = await _det_regime(session)
            _trend_score  = _regime.get("trend_score", 0)
            _cross        = _regime.get("cross_asset", {})
            _cross_n      = _cross.get("aligned_count", 0)
            _cross_tot    = _cross.get("total_count", 1)
            _full_consensus = _cross_tot > 0 and _cross_n == _cross_tot
            if _trend_score >= 4 or (_trend_score >= 3 and _full_consensus):
                _adj_min_edge += config.UPDOWN_GBM_REGIME_SURCHARGE
                logger.debug(
                    f"[UPDOWN HOURLY] {symbol} GBM regime surcharge +{config.UPDOWN_GBM_REGIME_SURCHARGE:.0%} "
                    f"(score={_trend_score} consensus={_cross_n}/{_cross_tot}) → adj_min_edge={_adj_min_edge:.1%}"
                )
        try:
            _gbm_decision = await evaluate_hourly_entry(
                symbol          = symbol,
                start_date      = start_date,
                end_date        = end_date,
                market_price_up = market_price_up,
                vol_annual      = vol_annual,
                session         = session,
                fee             = config.UPDOWN_HOURLY_FEE,
                min_edge        = _adj_min_edge,
                max_edge        = getattr(config, "UPDOWN_HOURLY_GBM_MAX_EDGE", 0.25),
                use_trend_bias  = getattr(config, "UPDOWN_HOURLY_USE_TREND_BIAS", True),
            )
        except Exception as _e:
            logger.warning(f"[UPDOWN HOURLY] {symbol} GBM eval error: {_e}")
            return

        if _gbm_decision is None:
            logger.debug(f"[UPDOWN HOURLY] {symbol} — strike/current price unavailable, skip")
            return
        if _gbm_decision["action"] != "BUY":
            _stk  = _gbm_decision.get("strike")
            _cur  = _gbm_decision.get("current")
            _stk_str = f" strike={_stk:.2f} cur={_cur:.2f}" if _stk and _cur else ""
            log.info(
                f"[UPDOWN HOURLY] {symbol} GBM skip — "
                f"P(Up)={_gbm_decision['prob_up']:.3f} mkt={market_price_up:.3f} "
                f"edge_up={_gbm_decision['edge_up']:+.3f} edge_down={_gbm_decision['edge_down']:+.3f}"
                f"{_stk_str} min={_adj_min_edge:.1%} ({_gbm_decision['reason']})"
            )
            return
        buy_outcome  = _gbm_decision["outcome"]
        buy_price    = _gbm_decision["buy_price"]

        if sym_mtf is not None:
            _mtf_dir = sym_mtf.get("direction")
            if _mtf_dir is not None:
                _gbm_opposed = (
                    (buy_outcome == "Up" and _mtf_dir == "down") or
                    (buy_outcome == "Down" and _mtf_dir == "up")
                )
                if _gbm_opposed:
                    _flip_edge    = _gbm_decision["edge_down"] if buy_outcome == "Up" else _gbm_decision["edge_up"]
                    _flip_outcome = "Down" if buy_outcome == "Up" else "Up"
                    if _flip_edge >= _adj_min_edge:
                        log.info(
                            f"[UPDOWN HOURLY] {symbol} GBM→MTM flip {buy_outcome}→{_flip_outcome} "
                            f"(flip_edge={_flip_edge:+.3f} mom={_mtf_dir})"
                        )
                        buy_outcome = _flip_outcome
                        buy_price   = round(1.0 - buy_price, 4)
                    else:
                        log.info(
                            f"[UPDOWN HOURLY] {symbol} GBM skip — mom={_mtf_dir} opposed, "
                            f"flip_edge={_flip_edge:+.3f} < min={_adj_min_edge:.1%}"
                        )
                        return

        _strike_val  = _gbm_decision.get("strike")
        _current_val = _gbm_decision.get("current")
        if _strike_val and _current_val:
            _drift     = abs(_current_val - _strike_val) / _strike_val
            _max_drift = config.UPDOWN_GBM_MAX_STRIKE_DRIFT
            if _max_drift > 0 and _drift > _max_drift:
                logger.debug(
                    f"[UPDOWN HOURLY] {symbol} GBM skip — "
                    f"price drift {_drift:.1%} > max {_max_drift:.1%} "
                    f"(current={_current_val:.2f} strike={_strike_val:.2f})"
                )
                return
    else:
        if sym_momentum > 0:
            buy_outcome = "Down"
            buy_price   = round(1.0 - market_price_up, 4)
        else:
            buy_outcome = "Up"
            buy_price   = market_price_up

    _consensus_thr = getattr(config, "UPDOWN_HOURLY_CONSENSUS_FLOOR", 0.90)
    if buy_outcome == "Down" and market_price_up > _consensus_thr:
        logger.debug(
            f"[UPDOWN HOURLY] Skip {symbol} Down — market {market_price_up:.3f} "
            f"consensus Up (>{_consensus_thr:.0%})"
        )
        return
    if buy_outcome == "Up" and market_price_up < (1.0 - _consensus_thr):
        logger.debug(
            f"[UPDOWN HOURLY] Skip {symbol} Up — market {market_price_up:.3f} "
            f"consensus Down (<{1.0-_consensus_thr:.0%})"
        )
        return

    if use_gbm and getattr(config, "UPDOWN_HOURLY_USE_TECHNICAL", True):
        from src.api.binance_client import fetch_technical_signals as _fetch_tech
        _tech_rsi_p = getattr(config, "UPDOWN_HOURLY_RSI_PERIOD", 14)
        _tech_z_w   = getattr(config, "UPDOWN_HOURLY_ZSCORE_WINDOW", 20)
        _tech_sp_m  = getattr(config, "UPDOWN_HOURLY_VOL_SPIKE_MULT", 3.0)
        _tech_tr_h  = getattr(config, "UPDOWN_HOURLY_MACRO_TREND_HOURS", 4)
        _tech = await _fetch_tech(
            symbol, session,
            rsi_period     = _tech_rsi_p,
            zscore_window  = _tech_z_w,
            vol_spike_mult = _tech_sp_m,
            trend_hours    = _tech_tr_h,
        )
        if _tech:  # empty dict = fetch failed → skip filter, don't block
            _rsi   = _tech.get("rsi")
            _z     = _tech.get("zscore")
            _spike = _tech.get("vol_spike", False)
            _rsi_ob = getattr(config, "UPDOWN_HOURLY_RSI_OVERBOUGHT", 70.0)
            _rsi_os = getattr(config, "UPDOWN_HOURLY_RSI_OVERSOLD", 30.0)
            _z_thr  = getattr(config, "UPDOWN_HOURLY_ZSCORE_THR", 2.5)

            if _spike:
                logger.debug(f"[UPDOWN HOURLY] {symbol} — Binance vol spike, GBM unreliable, skip")
                return

            if _rsi is not None:
                if buy_outcome == "Up" and _rsi >= _rsi_ob:
                    logger.debug(
                        f"[UPDOWN HOURLY] {symbol} — RSI {_rsi:.1f} overbought, skip Up (reversal risk)"
                    )
                    return
                if buy_outcome == "Down" and _rsi <= _rsi_os:
                    logger.debug(
                        f"[UPDOWN HOURLY] {symbol} — RSI {_rsi:.1f} oversold, skip Down (bounce risk)"
                    )
                    return

            if _z is not None and _z_thr > 0:
                if buy_outcome == "Up" and _z >= _z_thr:
                    logger.debug(
                        f"[UPDOWN HOURLY] {symbol} — Z={_z:.2f} far above 24h mean, skip Up"
                    )
                    return
                if buy_outcome == "Down" and _z <= -_z_thr:
                    logger.debug(
                        f"[UPDOWN HOURLY] {symbol} — Z={_z:.2f} far below 24h mean, skip Down"
                    )
                    return

            _rsi_mom_ob = getattr(config, "UPDOWN_HOURLY_RSI_MOM_OB", 65.0)
            _rsi_mom_os = getattr(config, "UPDOWN_HOURLY_RSI_MOM_OS", 35.0)
            if _rsi is not None:
                if buy_outcome == "Down" and _rsi >= _rsi_mom_ob:
                    logger.debug(
                        f"[UPDOWN HOURLY] {symbol} — RSI {_rsi:.1f} uptrend, skip Down (momentum alignment)"
                    )
                    return
                if buy_outcome == "Up" and _rsi <= _rsi_mom_os:
                    logger.debug(
                        f"[UPDOWN HOURLY] {symbol} — RSI {_rsi:.1f} downtrend, skip Up (momentum alignment)"
                    )
                    return

            _z_mom_thr = getattr(config, "UPDOWN_HOURLY_ZSCORE_MOM_THR", 1.5)
            if _z is not None and _z_mom_thr > 0:
                if buy_outcome == "Down" and _z >= _z_mom_thr:
                    logger.debug(
                        f"[UPDOWN HOURLY] {symbol} — Z={_z:.2f} uptrend vs 24h mean, skip Down"
                    )
                    return
                if buy_outcome == "Up" and _z <= -_z_mom_thr:
                    logger.debug(
                        f"[UPDOWN HOURLY] {symbol} — Z={_z:.2f} downtrend vs 24h mean, skip Up"
                    )
                    return

            if getattr(config, "UPDOWN_HOURLY_MACRO_TREND_GATE", True):
                _macro_thr = getattr(config, "UPDOWN_HOURLY_MACRO_TREND_THR", 0.02)
                if _macro_thr > 0:
                    if symbol != "BTC":
                        _btc_tech = await _fetch_tech(
                            "BTC", session,
                            rsi_period     = _tech_rsi_p,
                            zscore_window  = _tech_z_w,
                            vol_spike_mult = _tech_sp_m,
                            trend_hours    = _tech_tr_h,
                        )
                    else:
                        _btc_tech = _tech
                    _btc_trend = (_btc_tech or {}).get("trend_4h") or 0.0
                    if _btc_trend > _macro_thr and buy_outcome == "Down":
                        logger.debug(
                            f"[UPDOWN HOURLY] {symbol} — BTC 4h trend {_btc_trend:+.2%} UP, "
                            f"skip Down (macro trend gate)"
                        )
                        return
                    if _btc_trend < -_macro_thr and buy_outcome == "Up":
                        logger.debug(
                            f"[UPDOWN HOURLY] {symbol} — BTC 4h trend {_btc_trend:+.2%} DOWN, "
                            f"skip Up (macro trend gate)"
                        )
                        return

            logger.debug(
                f"[UPDOWN HOURLY] {symbol} tech OK — RSI={_rsi} Z={_z} "
                f"trend={_tech.get('trend_4h', 0):.2%} spike={_spike}"
                if _tech.get("trend_4h") is not None else
                f"[UPDOWN HOURLY] {symbol} tech OK — RSI={_rsi} Z={_z} spike={_spike}"
            )

    _btc_corr_thr = getattr(config, "UPDOWN_HOURLY_BTC_CORR_THR", 0.005)
    if use_gbm and symbol != "BTC" and _btc_corr_thr > 0 and symbol_momentum_map:
        _btc_mtf = symbol_momentum_map.get("BTC")
        if _btc_mtf is not None:
            _btc_15m = _btc_mtf.get("m_15m", 0.0) or 0.0
            if abs(_btc_15m) >= _btc_corr_thr:
                _btc_dir = "Up" if _btc_15m > 0 else "Down"
                if _btc_dir != buy_outcome:
                    logger.debug(
                        f"[UPDOWN HOURLY] {symbol} — BTC 15m {_btc_15m:+.3%} → {_btc_dir}, "
                        f"opposes {buy_outcome}, skip (BTC lead)"
                    )
                    return

    if locked_outcome is not None:
        from src.logic.gbm_hourly import passes_opposite_reentry_gate
        opp_min_min = getattr(config, "UPDOWN_HOURLY_OPPOSITE_MIN_MINUTES", 10)
        allowed, reason = passes_opposite_reentry_gate(
            locked_outcome   = locked_outcome,
            proposed_outcome = buy_outcome,
            time_remaining_s = delta_sec,
            min_minutes      = opp_min_min,
        )
        if not allowed:
            logger.debug(
                f"[UPDOWN HOURLY] Skip {symbol} opposite re-entry — "
                f"{reason} (locked={locked_outcome}, picked={buy_outcome}, "
                f"{delta_sec/60:.1f}m left)"
            )
            return
        logger.info(
            f"[UPDOWN HOURLY] {symbol} opposite re-entry — "
            f"locked={locked_outcome}, picking {buy_outcome} ({delta_sec/60:.1f}m left)"
        )

    _scalp_kelly_mult = 1.0

    if buy_price <= 0 or buy_price >= 1:
        return

    max_entry = config.UPDOWN_HOURLY_MAX_ENTRY_PRICE
    min_entry = getattr(config, "UPDOWN_HOURLY_MIN_ENTRY_PRICE", 0.20)
    if max_entry > 0 and buy_price > max_entry:
        logger.debug(
            f"[UPDOWN HOURLY] Skip {symbol} {buy_outcome} — "
            f"buy_price {buy_price:.3f} > max {max_entry:.3f} (odds terlalu tipis)"
        )
        return
    if min_entry > 0 and buy_price < min_entry:
        logger.debug(
            f"[UPDOWN HOURLY] Skip {symbol} {buy_outcome} — "
            f"buy_price {buy_price:.3f} < min {min_entry:.3f} (high variance pick)"
        )
        return

    # GBM prob_up = P(candle closes above strike). Map ke sisi yang dibeli.
    if _gbm_decision is not None:
        _raw_prob = (
            _gbm_decision["prob_up"]
            if buy_outcome == "Up"
            else 1.0 - _gbm_decision["prob_up"]
        )
        buy_winrate = max(0.50, min(0.80, _raw_prob))
    else:
        buy_winrate = 0.55

    if btc_scalp is not None:
        # BTC scalp tetap drive kelly_multiplier (ATR-based), bukan winrate
        _scalp_kelly_mult = max(0.5, btc_scalp.get("kelly_multiplier", 1.0))

    # Momentum alignment: same direction as GBM → bet more, opposite → bet less
    _mom_aligned = (
        (buy_outcome == "Up" and sym_momentum > 0.0005) or
        (buy_outcome == "Down" and sym_momentum < -0.0005)
    )
    _mom_opposed = (
        (buy_outcome == "Up" and sym_momentum < -0.0005) or
        (buy_outcome == "Down" and sym_momentum > 0.0005)
    )
    if _mom_aligned:
        _scalp_kelly_mult = min(_scalp_kelly_mult * 1.2, 1.5)
    elif _mom_opposed:
        _scalp_kelly_mult = _scalp_kelly_mult * 0.75

    if market_session == "ASIA":
        _scalp_kelly_mult = min(_scalp_kelly_mult, 0.7)

    kelly = sizer.calculate(
        winrate      = buy_winrate,
        market_price = buy_price,
        capital      = capital,
    )

    if not kelly.is_positive_ev or float(kelly.bet_usdc) <= 0:
        return

    max_size = calculate_position_size(get_recent_closed_pnls(limit=5))
    if float(kelly.bet_usdc) > max_size:
        from dataclasses import replace as _dc_replace
        capped_usdc   = Decimal(str(max_size))
        capped_shares = (capped_usdc / Decimal(str(buy_price))).quantize(Decimal("0.0001"))
        kelly = _dc_replace(kelly, bet_usdc=capped_usdc, shares=capped_shares)

    if _scalp_kelly_mult < 1.0:
        from dataclasses import replace as _dc_replace
        scaled_usdc   = Decimal(str(round(float(kelly.bet_usdc) * _scalp_kelly_mult, 2)))
        scaled_shares = (scaled_usdc / Decimal(str(buy_price))).quantize(Decimal("0.0001"))
        kelly = _dc_replace(kelly, bet_usdc=scaled_usdc, shares=scaled_shares)
        if float(kelly.bet_usdc) <= 0:
            return

    t_min = delta_sec / 60.0

    tokens   = gamma.extract_token_ids(market)
    _bo_lower = buy_outcome.lower()
    token    = next((t for t in tokens if str(t.get("outcome", "")).lower() == _bo_lower), None)
    token_id = str(token["token_id"]) if token and token.get("token_id") else ""

    if token_id and not config.DRY_RUN:
        try:
            from src.logic.scalping_exit import liquidity_check
            depth = clob.get_orderbook_depth(token_id)
            if depth:
                liq = liquidity_check(
                    bids=depth, size_shares=float(kelly.shares),
                    entry_price=buy_price, capital_usdc=float(kelly.bet_usdc),
                    slippage_warn_threshold=0.05,
                )
                if not liq["ok"]:
                    logger.warning(
                        f"[UPDOWN HOURLY] {symbol} likuiditas tidak cukup: "
                        f"{liq['warning']} — skip"
                    )
                    return
        except Exception as _e:
            logger.debug(f"[UPDOWN HOURLY] {symbol} liquidity check error: {_e}")

    if _gbm_decision is not None:
        _mode_label = (
            f"[GBM] P(Up)={_gbm_decision['prob_up']:.3f} mkt={market_price_up:.3f} "
            f"strike={_gbm_decision['strike']:.2f} cur={_gbm_decision['current']:.2f} "
            f"edge={_gbm_decision['edge']:+.3f} min={_adj_min_edge:.1%}"
        )
    else:
        _mode_label = "[contrarian]"
    log.info(
        f"[bold cyan][UPDOWN HOURLY][/bold cyan] {symbol} {t_min:.0f}m left | "
        f"BUY {buy_outcome} @ {buy_price:.3f} {_mode_label} | "
        f"sym 5m/15m/30m {sym_m5:+.2%}/{sym_momentum:+.2%}/{sym_m30:+.2%} "
        f"vol×{sym_vol_ratio:.2f} | "
        f"scalp={btc_scalp.get('action', '-') if btc_scalp else '-'} wr={buy_winrate:.2f} km={_scalp_kelly_mult} | "
        f"slot {slot_history+1}/{_max_entries} | Kelly ${float(kelly.bet_usdc):.2f}"
    )

    async with _open_position_lock:
        can_open, reason = manager.can_open(
            condition_id  = condition_id,
            outcome       = buy_outcome,
            bet_usdc      = kelly.bet_usdc,
            total_capital = ke_decimal(capital),
        )
        if not can_open:
            logger.debug(f"[UPDOWN HOURLY] Skip {condition_id[:8]} {buy_outcome}: {reason}")
            return

        if config.CB_ENABLED and not breaker.check(unrealized_pnl=manager.get_unrealized_pnl()).can_trade:
            return

        try:
            resolve_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        except Exception:
            resolve_date = datetime.now(timezone.utc)

        _record_gap = (
            float(_gbm_decision["edge"])
            if _gbm_decision is not None
            else abs(btc_regime or 0.0)
        )
        _record_prob = (
            str(round(_gbm_decision["prob_up"], 4))
            if _gbm_decision is not None
            else str(round(buy_winrate, 4))
        )

        if config.DRY_RUN:
            log.warning("[yellow][UPDOWN HOURLY] DRY RUN — simulasi posisi dibuka[/yellow]")
            manager.open_position(
                condition_id    = condition_id,
                question        = question,
                outcome         = buy_outcome,
                entry_price     = ke_decimal(buy_price),
                shares          = kelly.shares,
                capital_at_risk = kelly.bet_usdc,
                resolve_date    = resolve_date,
                gap_pct         = _record_gap,
                kelly_fraction  = float(kelly.bet_fraction),
                strategy_mode   = "updown_hourly_dry_run",
                token_id        = token_id,
            )
            log_prediction({
                "condition_id":   condition_id,
                "question":       question,
                "outcome":        buy_outcome,
                "predicted_prob": _record_prob,
                "market_price":   str(buy_price),
                "gap_pct":        str(round(_record_gap * 100, 2)),
                "resolve_date":   resolve_date.isoformat(),
            })
            record_slot_entry(end_date)
        else:
            if not token_id:
                logger.warning(f"[UPDOWN HOURLY] token_id tidak ditemukan untuk {buy_outcome}")
                return

            order = clob.pasang_order(
                sisi     = SisiOrder.BELI,
                harga    = ke_decimal(buy_price),
                ukuran   = kelly.shares,
                token_id = token_id,
            )

            if order:
                manager.open_position(
                    condition_id    = condition_id,
                    question        = question,
                    outcome         = buy_outcome,
                    entry_price     = ke_decimal(buy_price),
                    shares          = kelly.shares,
                    capital_at_risk = kelly.bet_usdc,
                    resolve_date    = resolve_date,
                    gap_pct         = _record_gap,
                    kelly_fraction  = float(kelly.bet_fraction),
                    strategy_mode   = "updown_hourly",
                    token_id        = token_id,
                )
                log_prediction({
                    "condition_id":   condition_id,
                    "question":       question,
                    "outcome":        buy_outcome,
                    "predicted_prob": _record_prob,
                    "market_price":   str(buy_price),
                    "gap_pct":        str(round(_record_gap * 100, 2)),
                    "resolve_date":   resolve_date.isoformat(),
                })
                record_slot_entry(end_date)
            else:
                logger.warning(f"[UPDOWN HOURLY] {symbol} order gagal — slot tidak di-record")
                return

        alert = get_alert()
        if not alert:
            logger.warning("[UPDOWN HOURLY] alert=None — Telegram tidak terkonfigurasi")
        else:
            try:
                ok = await alert.alert_signal(
                    question = question,
                    outcome  = buy_outcome,
                    price    = buy_price,
                    bet_usdc = float(kelly.bet_usdc),
                    gap_pct  = _record_gap * 100,
                    ev       = float(kelly.expected_value),
                    session  = session,
                    dry_run  = config.DRY_RUN,
                    strategy = "Up/Down Hourly",
                )
                if ok is False:
                    logger.warning("[UPDOWN HOURLY] Entry alert gagal dikirim ke Telegram")
            except Exception as _ae:
                logger.warning(f"[UPDOWN HOURLY] Entry alert exception: {_ae}")

_TARIK_FLAG = Path(__file__).resolve().parent.parent / "data" / "tarik.flag"


async def _execute_tarik(
    manager,
    clob,
    session: aiohttp.ClientSession,
    condition_ids: list[str] | None = None,
) -> str:
    from src.models.database import get_open_positions
    from src.logic.pricing import ke_decimal

    all_positions = get_open_positions()
    if not all_positions:
        return "📭 Tidak ada posisi open untuk ditarik."

    if condition_ids:
        cid_set = set(condition_ids)
        targets = [p for p in all_positions if p["condition_id"] in cid_set]
    else:
        targets = all_positions

    if not targets:
        return "📭 Posisi yang dipilih tidak ditemukan."

    skipped = len(all_positions) - len(targets)

    results = []
    for pos in targets:
        cid      = pos["condition_id"]
        outcome  = pos["outcome"]
        current  = float(pos["current_price"])
        entry    = float(pos["entry_price"])
        shares   = float(pos["shares"])
        pnl      = (current - entry) * shares
        question = pos.get("question", "")[:40]

        if not config.DRY_RUN and pos.get("token_id"):
            try:
                clob.pasang_order(
                    sisi     = SisiOrder.JUAL,
                    harga    = ke_decimal(str(current)),
                    ukuran   = ke_decimal(str(shares)),
                    token_id = str(pos["token_id"]),
                )
            except Exception as _e:
                logger.warning(f"[TARIK] Sell order error {cid[:8]}: {_e}")

        manager._process_exit_manual(
            condition_id = cid,
            outcome      = outcome,
            exit_price   = ke_decimal(str(current)),
            pnl          = ke_decimal(str(round(pnl, 4))),
            reason       = "MANUAL_TARIK",
        )
        results.append(
            f"  {'📈' if pnl >= 0 else '📉'} {outcome} @ {current:.3f} | PnL <b>${pnl:+.2f}</b>\n"
            f"     <i>{question}</i>"
        )
        logger.info(f"[TARIK] Closed {cid[:8]} {outcome} @ {current:.3f} PnL=${pnl:+.2f}")

    total_pnl = sum(
        (float(p["current_price"]) - float(p["entry_price"])) * float(p["shares"])
        for p in targets
    )
    total_emoji = "📈" if total_pnl >= 0 else "📉"
    mode   = " [DRY RUN]" if config.DRY_RUN else ""
    header = f"🏁 <b>TARIK {len(targets)} posisi</b>{mode}\n\n"
    footer = f"\n{total_emoji} Total PnL: <b>${total_pnl:+.2f}</b>"
    if skipped:
        footer += f"\n⏭ {skipped} posisi lain dibiarkan jalan"
    return header + "\n".join(results) + footer


async def run_mispricing_mode(clob: ClobClient):
    gamma   = GammaClient(host=getattr(config, "GAMMA_HOST", "https://gamma-api.polymarket.com"))
    detector = MispricingDetector(threshold=getattr(config, "HOURLY_MISPRICING_THRESHOLD", 0.12))
    sizer   = KellySizer(
        kelly_multiplier = getattr(config, "KELLY_MULTIPLIER", 0.5),
        max_fraction     = getattr(config, "MAX_KELLY_FRACTION", 0.30),
        min_bet_usdc     = getattr(config, "MIN_BET_USDC", 5.0),
        min_winrate      = getattr(config, "MIN_WINRATE", 0.52),
    )
    manager = PositionManager(
        max_open_positions     = getattr(config, "MAX_OPEN_POSITIONS", 5),
        max_capital_per_market = getattr(config, "MAX_CAPITAL_PER_MARKET", 30.0),
        max_same_direction     = getattr(config, "MAX_SAME_DIRECTION", 2),
        exit_evaluator         = ExitEvaluator(
            trailing_stop_pct       = getattr(config, "TRAILING_STOP_PCT", 0.15),
            profit_threshold        = getattr(config, "PROFIT_THRESHOLD", 0.75),
            tight_trailing_stop_pct = getattr(config, "TIGHT_TRAILING_STOP_PCT", 0.07),
            profit_lock_pct              = getattr(config, "PROFIT_LOCK_PCT", 20.0),
            profit_lock_high_pct         = getattr(config, "PROFIT_LOCK_HIGH_PCT", 35.0),
            updown_profit_lock_pct       = getattr(config, "UPDOWN_PROFIT_LOCK_PCT", 40.0),
            updown_profit_lock_high_pct  = getattr(config, "UPDOWN_PROFIT_LOCK_HIGH_PCT", 60.0),
            hourly_profit_lock_pct       = getattr(config, "HOURLY_PROFIT_LOCK_PCT", 60.0),
            hourly_profit_lock_high_pct  = getattr(config, "HOURLY_PROFIT_LOCK_HIGH_PCT", 60.0),
            hourly_trailing_activate_pct = getattr(config, "HOURLY_TRAILING_ACTIVATE_PCT", 15.0),
            hourly_trailing_retrace_pct  = getattr(config, "HOURLY_TRAILING_RETRACE_PCT", 0.30),
            hourly_lock_t1_pct           = getattr(config, "HOURLY_LOCK_T1_PCT", 150.0),
            hourly_lock_t2_pct           = getattr(config, "HOURLY_LOCK_T2_PCT", 100.0),
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
        f"[bold green]Daily Crypto + Up/Down Daily + Up/Down Hourly aktif.[/bold green] "
        f"Threshold: dynamic [6-25%] | "
        f"Min winrate: {getattr(config, 'HOURLY_MIN_WINRATE_STRICT', 0.75):.0%} | "
        f"Polling: {config.POLLING_INTERVAL}s"
    )

    async with aiohttp.ClientSession() as session:
        await _backfill_missing_token_ids(gamma, session)
        await reconcile_positions(clob, gamma, manager, breaker, session)

        _cb_alerted = False
        _profit_locked_markets: dict[str, str] = {}
        _candle_sl_markets: dict[str, str] = {}
        _hourly_flip_queue: dict[str, dict] = {}
        _hourly_flip_last_exec: dict[str, datetime] = {}
        while True:
            try:
                await _backfill_missing_token_ids(gamma, session)
                closed_this_cycle: set[str] = set()
                current_prices = await _fetch_current_prices(clob, manager)
                closed_this_cycle |= await _resolve_checker(clob, gamma, manager, breaker, session, current_prices)

                if config.DRY_RUN:
                    from src.models.database import get_open_positions as _get_open, get_stats as _get_stats
                    locked        = sum(float(p["capital_at_risk"]) for p in _get_open())
                    realized_pnl  = float(_get_stats().get("total_pnl") or 0)
                    balance       = max(0.0, float(config.SALDO_AWAL) + realized_pnl - locked)
                else:
                    balance = clob.get_balance()
                capital = balance
                prefix  = "[DRY RUN] " if config.DRY_RUN else ""
                log.info(f"{prefix}Balance: ${capital:.2f} USDC | {manager.get_summary()}")

                vol_data = await _build_vol_data(session)
                btc_vol  = vol_data.get("BTC") or vol_data.get("DEFAULT", 0.40)
                log.info(
                    f"[VOL] BTC {btc_vol:.0%} | "
                    f"ETH {vol_data.get('ETH', 0.40):.0%} | "
                    f"SOL {vol_data.get('SOL', 0.40):.0%} | "
                    f"BNB {vol_data.get('BNB', 0.40):.0%} | "
                    f"XRP {vol_data.get('XRP', 0.40):.0%} | "
                    f"DOGE {vol_data.get('DOGE', 0.40):.0%} (annualized)"
                )

                open_prices = [
                    float(p)
                    for outcome_map in current_prices.values()
                    for p in outcome_map.values()
                ]
                if open_prices:
                    avg_P    = sum(open_prices) / len(open_prices)
                    new_stop = get_dynamic_stop_loss(avg_P, btc_vol)
                    manager.exit_evaluator.trailing_stop_pct = Decimal(str(new_stop))
                    logger.debug(f"[STOP] trailing_stop={new_stop:.1%} avg_P={avg_P:.2f}")

                from src.models.database import get_open_positions as _gop, update_position_price as _upp
                for _pos in _gop():
                    _cid, _out = _pos["condition_id"], _pos["outcome"]
                    _p = (current_prices.get(_cid) or {}).get(_out)
                    if _p:
                        _upp(_cid, _out, _p)

                if _TARIK_FLAG.exists():
                    try:
                        _tarik_raw = _TARIK_FLAG.read_text().strip()
                        _TARIK_FLAG.unlink()
                        _tarik_cids = [c for c in _tarik_raw.split(",") if c] or None
                        log.warning(
                            f"[yellow][TARIK] Flag detected — menutup "
                            f"{len(_tarik_cids) if _tarik_cids else 'semua'} posisi...[/yellow]"
                        )
                        _tarik_summary = await _execute_tarik(manager, clob, session, condition_ids=_tarik_cids)
                        log.info(f"[TARIK] Done:\n{_tarik_summary}")
                        _alert = get_alert()
                        if _alert:
                            await _alert.send(_tarik_summary, session)
                    except Exception as _te:
                        logger.warning(f"[TARIK] Error: {_te}")

                try:
                    from src.logic.exit_strategy import ExitSignal as _XS
                    exit_decisions = manager.evaluate_exits(current_prices)
                    for _d in exit_decisions:
                        if not _d.should_exit:
                            continue

                        _exit_pnl = float(_d.estimated_pnl_usdc or 0)
                        log.info(
                            f"[EXIT] {'✅ WIN' if _exit_pnl >= 0 else '❌ LOSE'} "
                            f"{_d.signal.value.upper()} — "
                            f"{_d.position.question[:40]} | {_d.position.outcome} "
                            f"@ {float(_d.position.entry_price):.3f}"
                            f"→{float(_d.position.current_price):.3f} | "
                            f"PnL ${_exit_pnl:+.2f} | {_d.reason}"
                        )

                        try:
                            _pnl = float(_d.estimated_pnl_usdc or 0)
                            breaker.record_trade(_pnl)
                        except Exception as _e:
                            logger.warning(f"[BREAKER] record_trade error: {_e}")

                        if (
                            _d.position.strategy_mode in ("updown_hourly", "updown_hourly_dry_run")
                            and float(_d.estimated_pnl_usdc or 0) < 0
                        ):
                            _sym = detect_symbol_from_question(_d.position.question)
                            if _sym != "UNKNOWN":
                                maybe_blacklist_symbol(_sym)

                        if (_d.signal == _XS.EXIT_CATASTROPHIC
                                and _d.position.strategy_mode in (
                                    "updown_candle", "updown_candle_dry_run"
                                )
                                and float(_d.estimated_pnl_usdc or 0) < 0):
                            _candle_sl_markets[_d.position.condition_id] = _d.position.outcome
                            logger.debug(
                                f"[CANDLE SL] {_d.position.condition_id[:8]} "
                                f"{_d.position.outcome} SL'd — queued for reverse re-entry"
                            )

                        if (_d.signal == _XS.EXIT_CATASTROPHIC
                                and _d.position.strategy_mode in (
                                    "updown_hourly", "updown_hourly_dry_run"
                                )
                                and float(_d.estimated_pnl_usdc or 0) < 0):
                            _fq_pnl = (
                                (float(_d.position.current_price) - float(_d.position.entry_price))
                                / float(_d.position.entry_price) * 100
                            )
                            _fq_mins = (
                                (_d.position.resolve_date - datetime.now(timezone.utc))
                                .total_seconds() / 60
                            )
                            _fq_opp  = round(1.0 - float(_d.position.current_price), 4)
                            if (
                                _fq_pnl  <= config.HOURLY_FLIP_TRIGGER_PCT
                                and _fq_mins >= config.HOURLY_FLIP_MIN_MINUTES
                                and _fq_opp  <= config.HOURLY_FLIP_MAX_ENTRY
                            ):
                                _fq_dir = "Down" if _d.position.outcome == "Up" else "Up"
                                _hourly_flip_queue[_d.position.condition_id] = {
                                    "flip_to":          _fq_dir,
                                    "flip_price":       _fq_opp,
                                    "original_capital": float(_d.position.capital_at_risk),
                                    "resolve_date":     _d.position.resolve_date,
                                    "question":         _d.position.question,
                                    "symbol":           detect_symbol_from_question(_d.position.question),
                                }
                                log.info(
                                    f"[FLIP] {_d.position.condition_id[:8]} queued: "
                                    f"{_d.position.outcome}→{_fq_dir} @ {_fq_opp:.3f} "
                                    f"(pnl {_fq_pnl:.0f}%, {_fq_mins:.0f}m left)"
                                )

                        if _d.signal == _XS.EXIT_LOCK_PROFIT and _d.position.strategy_mode in (
                            "updown_hourly", "updown_hourly_dry_run"
                        ):
                            register_reentry_candidate(_d)
                            _profit_locked_markets[_d.position.condition_id] = _d.position.outcome

                        if not config.DRY_RUN and _d.position.token_id:
                            try:
                                from src.logic.pricing import ke_decimal as _ked
                                clob.pasang_order(
                                    sisi     = SisiOrder.JUAL,
                                    harga    = _ked(str(_d.position.current_price)),
                                    ukuran   = _d.position.shares,
                                    token_id = _d.position.token_id,
                                )
                            except Exception as _e:
                                logger.warning(f"[EXIT] Sell order error {_d.position.condition_id[:8]}: {_e}")

                        _exit_alert = get_alert()
                        if _exit_alert:
                            await _exit_alert.alert_exit(
                                question    = _d.position.question,
                                outcome     = _d.position.outcome,
                                entry_price = float(_d.position.entry_price),
                                exit_price  = float(_d.position.current_price),
                                pnl_usdc    = float(_d.estimated_pnl_usdc or 0),
                                reason      = _d.reason,
                                session     = session,
                            )
                except Exception as _e:
                    logger.warning(f"[EXIT EVAL] Error: {_e}")

                if config.CB_ENABLED:
                    cb_status = breaker.check(unrealized_pnl=manager.get_unrealized_pnl())
                    if not cb_status.can_trade:
                        log.warning(f"[CIRCUIT BREAKER] {cb_status}")
                        if not _cb_alerted:
                            alert = get_alert()
                            if alert:
                                await alert.alert_circuit_breaker(
                                    reason       = str(cb_status),
                                    drawdown_pct = getattr(cb_status, "drawdown_pct", 0.0),
                                    session      = session,
                                )
                            _cb_alerted = True
                        await asyncio.sleep(config.POLLING_INTERVAL)
                        continue
                    _cb_alerted = False

                if config.CB_ENABLED:
                    log.info(breaker.get_summary(unrealized_pnl=manager.get_unrealized_pnl()))
                await _prefetch_prices(session)

                try:
                    markets = await gamma.ascan_hourly_opportunities(
                        session,
                        min_volume             = getattr(config, "HOURLY_MIN_MARKET_VOLUME", 500),
                        min_liquidity          = getattr(config, "HOURLY_MIN_LIQUIDITY", 200),
                        max_minutes_to_resolve = getattr(config, "HOURLY_MAX_MINUTES_TO_RESOLVE", 90),
                        min_minutes_to_resolve = getattr(config, "HOURLY_MIN_MINUTES_TO_RESOLVE", 5),
                        limit                  = 500,
                    )
                except Exception as _e:
                    logger.warning(f"[HOURLY SCAN] Gamma API gagal ({_e}), skip cycle")
                    markets = []

                if config.CB_ENABLED:
                    daily_drawdown = breaker.state.daily_loss / breaker.starting_capital
                    safety         = breaker.check_safety_thresholds(btc_vol, daily_drawdown)
                    if safety.halt_new_entries:
                        log.warning(f"[SAFETY HALT] {safety}")
                        alert = get_alert()
                        if alert:
                            await alert.alert_circuit_breaker(
                                reason       = safety.reason,
                                drawdown_pct = abs(daily_drawdown),
                                session      = session,
                            )
                    can_enter = breaker.check(unrealized_pnl=manager.get_unrealized_pnl()).can_trade and not safety.halt_new_entries
                else:
                    can_enter = True

                if can_enter:
                    log.info("[bold]── UP/DOWN HOURLY ───────────────────────────────[/bold]")
                    cleanup_old_slots()
                    cleanup_reentry_candidates()

                    from src.logic.updown_strategy import calculate_multi_tf_momentum as _mtf_mom
                    from src.logic.regime_filter import CRYPTO_BASKET
                    _mtf_tasks = [_mtf_mom(s, session) for s in CRYPTO_BASKET]
                    _mtf_results = await asyncio.gather(*_mtf_tasks, return_exceptions=True)
                    symbol_momentum_map: dict[str, dict] = {}
                    for sym, r in zip(CRYPTO_BASKET, _mtf_results):
                        if isinstance(r, Exception) or r is None:
                            continue
                        symbol_momentum_map[sym] = r

                    btc_mtf = symbol_momentum_map.get("BTC", {})
                    btc_regime = btc_mtf.get("m_15m") if btc_mtf else None
                    regime_thr = config.UPDOWN_HOURLY_MOMENTUM_THRESHOLD
                    if btc_regime is not None and regime_thr > 0:
                        if btc_regime > regime_thr:
                            log.info(f"[REGIME] BTC momentum {btc_regime:+.2%} → BULLISH (contrarian: beli Down)")
                        elif btc_regime < -regime_thr:
                            log.info(f"[REGIME] BTC momentum {btc_regime:+.2%} → BEARISH (contrarian: beli Up)")
                        else:
                            log.info(f"[REGIME] BTC momentum {btc_regime:+.2%} → NEUTRAL")

                    _btc_scalp = None
                    try:
                        from src.logic.updown_strategy import calculate_scalping_signals as _csc
                        _btc_scalp = await _csc("BTC", session)
                        if _btc_scalp:
                            log.info(
                                f"[SCALP] BTC: action={_btc_scalp['action']} "
                                f"conf={_btc_scalp['confidence']:.2f} "
                                f"km={_btc_scalp['kelly_multiplier']} "
                                f"score={_btc_scalp['momentum_score']:.2f} "
                                f"noise={_btc_scalp['noise_level']}"
                            )
                    except Exception as _e:
                        logger.debug(f"[SCALP] BTC signal error: {_e}")

                    _market_regime = None
                    try:
                        from src.logic.regime_filter import detect_market_regime
                        _market_regime = await detect_market_regime(session)
                        ca = _market_regime["cross_asset"]
                        htf = _market_regime["higher_tf"]
                        sess_info = _market_regime["session"]
                        log.info(
                            f"[MARKET REGIME] {_market_regime['regime']} "
                            f"score={_market_regime['trend_score']} | "
                            f"cross={ca['aligned_count']}/{ca['total_count']} {ca.get('direction') or 'mixed'} "
                            f"avg={ca['avg_move_pct']:+.2%} | "
                            f"htf_1h={htf['tf_1h']} htf_4h={htf['tf_4h']} | "
                            f"session={sess_info['session']}"
                        )
                    except Exception as _e:
                        logger.warning(f"[MARKET REGIME] Error: {_e}")

                    hourly_markets, candle_markets = await asyncio.gather(
                        scan_updown_hourly_markets(session, gamma),
                        scan_candle_markets(session, gamma),
                    )
                    log.info(
                        f"[UPDOWN] hourly={len(hourly_markets)} active"
                    )

                    # --- Hourly flip queue processor ---
                    for _fcid in list(_hourly_flip_queue.keys()):
                        _fq = _hourly_flip_queue[_fcid]
                        _f_mins = (
                            (_fq["resolve_date"] - datetime.now(timezone.utc))
                            .total_seconds() / 60
                        )
                        if _f_mins < config.HOURLY_FLIP_MIN_MINUTES:
                            log.info(f"[FLIP] {_fcid[:8]} expired ({_f_mins:.0f}m left)")
                            _hourly_flip_queue.pop(_fcid)
                            continue

                        _f_market = next(
                            (m for m in hourly_markets
                             if m.get("conditionId", m.get("id", "")) == _fcid),
                            None,
                        )
                        if _f_market is None:
                            _hourly_flip_queue.pop(_fcid)
                            continue

                        # Cooldown guard
                        _f_last_exec = _hourly_flip_last_exec.get(_fcid)
                        if (_f_last_exec and
                                (datetime.now(timezone.utc) - _f_last_exec).total_seconds() / 60
                                < config.HOURLY_FLIP_COOLDOWN_MINUTES):
                            continue

                        # Momentum filter (m_5m): flip→Down needs bearish, flip→Up needs bullish
                        _f_sym_key  = (_fq.get("symbol") or "").upper()
                        _f_mom_data = (symbol_momentum_map or {}).get(_f_sym_key, {})
                        _f_m5       = _f_mom_data.get("m_5m", None)
                        if _f_m5 is None:
                            log.info(f"[FLIP] {_fcid[:8]} wait: no m_5m for {_f_sym_key}")
                            continue
                        _f_want_bearish = _fq["flip_to"] == "Down"
                        if _f_want_bearish and _f_m5 >= 0:
                            log.info(
                                f"[FLIP] {_fcid[:8]} skip: →Down but m_5m={_f_m5:+.4f} (bullish)"
                            )
                            continue
                        if not _f_want_bearish and _f_m5 <= 0:
                            log.info(
                                f"[FLIP] {_fcid[:8]} skip: →Up but m_5m={_f_m5:+.4f} (bearish)"
                            )
                            continue

                        # Live price check + price buffer guard
                        _f_tokens  = _f_market.get("tokens", [])
                        _f_tok_obj = next(
                            (t for t in _f_tokens if t.get("outcome") == _fq["flip_to"]), None
                        )
                        if not _f_tok_obj:
                            _hourly_flip_queue.pop(_fcid)
                            continue
                        _f_token_id = (
                            _f_tok_obj.get("token_id")
                            or _f_tok_obj.get("tokenId")
                            or _f_tok_obj.get("id", "")
                        )
                        _f_live_price = float(
                            _f_tok_obj.get("price", _fq["flip_price"])
                        )
                        if _f_live_price > config.HOURLY_FLIP_MAX_ENTRY:
                            log.info(
                                f"[FLIP] {_fcid[:8]} pop: {_fq['flip_to']} @ {_f_live_price:.3f}"
                                f" > max {config.HOURLY_FLIP_MAX_ENTRY}"
                            )
                            _hourly_flip_queue.pop(_fcid)
                            continue
                        if _f_live_price > _fq["flip_price"] * (1 + config.HOURLY_FLIP_PRICE_BUFFER_PCT):
                            log.info(
                                f"[FLIP] {_fcid[:8]} pop: slippage {_f_live_price:.3f}"
                                f" vs queued {_fq['flip_price']:.3f}"
                            )
                            _hourly_flip_queue.pop(_fcid)
                            continue

                        # Spread guard (optional — only if clob exposes get_spread)
                        try:
                            if hasattr(clob, "get_spread"):
                                _f_spread = clob.get_spread(_f_token_id)
                                if _f_spread is not None and _f_spread > config.HOURLY_FLIP_MAX_SPREAD:
                                    log.info(
                                        f"[FLIP] {_fcid[:8]} skip: spread {_f_spread:.3f}"
                                        f" > {config.HOURLY_FLIP_MAX_SPREAD}"
                                    )
                                    continue
                        except Exception:
                            pass

                        # Entry
                        _f_capital = _fq["original_capital"] * 0.5
                        _f_shares  = Decimal(str(round(_f_capital / _f_live_price, 4)))
                        _f_cap_dec = Decimal(str(round(_f_capital, 4)))
                        _f_mode    = "updown_hourly_dry_run" if config.DRY_RUN else "updown_hourly"
                        _f_sym     = _fq.get("symbol") or "?"

                        async with _open_position_lock:
                            if not manager.can_open(capital=_f_capital):
                                continue

                            if config.DRY_RUN:
                                manager.open_position(
                                    condition_id    = _fcid,
                                    question        = _fq["question"],
                                    outcome         = _fq["flip_to"],
                                    entry_price     = Decimal(str(_f_live_price)),
                                    shares          = _f_shares,
                                    capital_at_risk = _f_cap_dec,
                                    resolve_date    = _fq["resolve_date"],
                                    gap_pct         = 0.0,
                                    kelly_fraction  = 0.5,
                                    strategy_mode   = _f_mode,
                                    token_id        = _f_token_id,
                                )
                                record_slot_entry(_fq["resolve_date"])
                                log.info(
                                    f"[FLIP DRY] {_f_sym} {_fq['flip_to']} @ {_f_live_price:.3f}"
                                    f" cap ${_f_capital:.2f} ({_f_mins:.0f}m left)"
                                )
                            else:
                                from src.logic.pricing import ke_decimal as _fked
                                _f_order = clob.pasang_order(
                                    sisi     = SisiOrder.BELI,
                                    harga    = _fked(str(_f_live_price)),
                                    ukuran   = _f_shares,
                                    token_id = _f_token_id,
                                )
                                if _f_order:
                                    manager.open_position(
                                        condition_id    = _fcid,
                                        question        = _fq["question"],
                                        outcome         = _fq["flip_to"],
                                        entry_price     = Decimal(str(_f_live_price)),
                                        shares          = _f_shares,
                                        capital_at_risk = _f_cap_dec,
                                        resolve_date    = _fq["resolve_date"],
                                        gap_pct         = 0.0,
                                        kelly_fraction  = 0.5,
                                        strategy_mode   = _f_mode,
                                        token_id        = _f_token_id,
                                    )
                                    record_slot_entry(_fq["resolve_date"])
                                    log.info(
                                        f"[FLIP] ✅ {_f_sym} {_fq['flip_to']} @ {_f_live_price:.3f}"
                                        f" cap ${_f_capital:.2f} ({_f_mins:.0f}m left)"
                                    )

                            _hourly_flip_last_exec[_fcid] = datetime.now(timezone.utc)
                            _hourly_flip_queue.pop(_fcid, None)

                    _market_session_label = (
                        _market_regime["session"]["session"] if _market_regime else "US_MAIN"
                    )

                    _gbm_active = getattr(config, "UPDOWN_HOURLY_USE_GBM", True)
                    if (
                        _market_regime and _market_regime.get("skip_contrarian")
                        and not _gbm_active
                    ):
                        log.warning(
                            f"[MARKET REGIME] {_market_regime['regime']} terdeteksi — "
                            f"skip {len(hourly_markets)} hourly contrarian entry "
                            f"(score {_market_regime['trend_score']} ≥ threshold)"
                        )
                    else:
                        for hm in hourly_markets:
                            try:
                                await _analyze_updown_hourly_market(
                                    hm, clob, gamma, sizer, manager,
                                    breaker, capital, session, vol_data=vol_data,
                                    closed_this_cycle=closed_this_cycle,
                                    profit_locked_markets=_profit_locked_markets,
                                    btc_regime=btc_regime,
                                    btc_scalp=_btc_scalp,
                                    symbol_momentum_map=symbol_momentum_map,
                                    market_session=_market_session_label,
                                )
                            except Exception as e:
                                logger.warning(f"[UPDOWN HOURLY] Error analyze {hm.get('_symbol', '?')}: {e}")

                        for cm in candle_markets:
                            try:
                                await analyze_candle_market(
                                    cm, clob, gamma, sizer, manager,
                                    breaker, capital, session,
                                    open_position_lock=_open_position_lock,
                                    closed_this_cycle=closed_this_cycle,
                                    candle_sl_markets=_candle_sl_markets,
                                    vol_data=vol_data,
                                )
                            except Exception as _ce:
                                logger.warning(f"[CANDLE UPDOWN] Error analyze {cm.get('_symbol','?')}: {_ce}")

                        if reentry_candidates:
                            await _scan_reentry_opportunities(
                                clob, sizer, manager, breaker, capital, session,
                                btc_scalp=_btc_scalp,
                                symbol_momentum_map=symbol_momentum_map,
                            )

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"Error di cycle utama: {str(e).replace('[', '\\[')}")
                alert = get_alert()
                if alert:
                    await alert.alert_error(str(e), session)

            await asyncio.sleep(config.POLLING_INTERVAL)

def main():
    tampilkan_header()

    if config.DRY_RUN:
        log.warning("[yellow]Mode DRY RUN aktif — tidak ada order nyata.[/yellow]")

    clob = ClobClient()
    if not clob.hubungkan():
        log.error("[red]Gagal terhubung ke API. Periksa .env[/red]")
        sys.exit(1)

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

if __name__ == "__main__":
    main()
