import asyncio
import signal
import sys
import logging
import re
from decimal import Decimal

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
from src.models.database import log_prediction, get_recent_closed_pnls, count_open_by_resolve_slot
from src.utils.config import config
from src.utils.logger import log, tampilkan_header
from src.utils.telegram_alert import init_telegram, get_alert
from src.logic.risk_manager import get_dynamic_stop_loss, calculate_position_size
from src.logic.strategy import get_dynamic_threshold, should_force_exit

logger = logging.getLogger(__name__)

_price_cache: dict = {}
_cache_time: dict  = {}
_CACHE_TTL         = 300
_price_lock        = asyncio.Lock()
_open_position_lock = asyncio.Lock()

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

async def _build_vol_data(session: aiohttp.ClientSession) -> dict:
    from src.api.binance_client import fetch_realized_vol
    vol_hours = getattr(config, "HOURLY_VOL_HOURS", 4)

    results = await asyncio.gather(
        fetch_realized_vol("BTC", session, hours=vol_hours),
        fetch_realized_vol("ETH", session, hours=vol_hours),
        fetch_realized_vol("SOL", session, hours=vol_hours),
        fetch_realized_vol("BNB", session, hours=vol_hours),
        fetch_realized_vol("XRP", session, hours=vol_hours),
        return_exceptions=True,
    )
    vol_data: dict = {"DEFAULT": 0.40}
    for symbol, result in zip(["BTC", "ETH", "SOL", "BNB", "XRP"], results):
        if isinstance(result, float) and result > 0:
            vol_data[symbol] = result
    return vol_data

async def _force_exit_check(
    clob,
    manager,
    breaker,
    current_prices: dict,
    session: aiohttp.ClientSession,
) -> set[str]:
    from src.models.database import get_open_positions
    from src.logic.pricing import ke_decimal

    closed: set[str] = set()

    for pos in get_open_positions():
        try:
            expiry = datetime.fromisoformat(pos["resolve_date"])
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        if not should_force_exit(expiry):
            continue

        if pos.get("strategy_mode", "") in ("updown_hourly", "updown_hourly_dry_run"):
            continue

        cid     = pos["condition_id"]
        outcome = pos["outcome"]
        tid     = pos.get("token_id") or ""

        price_decimal = (current_prices.get(cid) or {}).get(outcome)
        if price_decimal is None:
            logger.warning(
                f"[FORCE EXIT] Tidak bisa exit {pos['question'][:40]} — "
                f"harga tidak tersedia di cache"
            )
            continue

        price  = float(price_decimal)
        entry  = ke_decimal(pos["entry_price"])
        shares = ke_decimal(pos["shares"])
        pnl    = (ke_decimal(str(price)) - entry) * shares

        log.warning(
            f"[FORCE EXIT] {pos['question'][:40]} | {outcome} @ {price:.3f} | "
            f"PnL ${float(pnl):+.2f} — mendekati expiry"
        )

        if not config.DRY_RUN and tid:
            clob.pasang_order(
                sisi     = SisiOrder.JUAL,
                harga    = ke_decimal(str(price)),
                ukuran   = shares,
                token_id = tid,
            )
        elif config.DRY_RUN:
            log.warning(
                f"[yellow][DRY RUN] Simulasi force exit {outcome} @ {price:.3f}[/yellow]"
            )

        manager._process_exit_manual(cid, outcome, ke_decimal(str(price)), pnl, "force_exit_expiry")
        breaker.record_trade(float(pnl))
        closed.add(cid)

        alert = get_alert()
        if alert:
            await alert.alert_exit(
                question    = pos["question"],
                outcome     = outcome,
                entry_price = float(entry),
                exit_price  = price,
                pnl_usdc    = float(pnl),
                reason      = "force_exit_expiry",
                session     = session,
            )

    return closed

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
        target = _extract_price_target(question)
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

            if not breaker.check(unrealized_pnl=manager.get_unrealized_pnl()).can_trade:
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
        if not all(p in (0.0, 1.0) for p in prices_float):
            return None
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

        except asyncio.TimeoutError:
            log.warning(f"[RECONCILE] Timeout cek {cid[:8]} {outcome} — skip, akan dicek ulang di loop")
        except Exception as e:
            log.warning(f"[RECONCILE] Gagal cek {cid[:8]} {outcome}: {e} — skip")

    if closed_count:
        log.info(f"[RECONCILE] Selesai — {closed_count}/{len(positions)} posisi di-close (resolved saat bot mati)")
    else:
        log.info(f"[RECONCILE] Selesai — semua {len(positions)} posisi masih aktif")

async def _resolve_checker(clob, gamma, manager, breaker, session: aiohttp.ClientSession) -> set[str]:
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

_UPDOWN_HOURLY_SLUG_PREFIXES = {
    "BTC":  "bitcoin-up-or-down-",
    "ETH":  "ethereum-up-or-down-",
    "SOL":  "solana-up-or-down-",
    "XRP":  "xrp-up-or-down-",
    "DOGE": "dogecoin-up-or-down-",
    "BNB":  "bnb-up-or-down-",
}
_UPDOWN_HOURLY_SKIP_MARKERS = ("-5m-", "-15m-", "-4h-", "updown-5m", "updown-15m", "updown-4h")

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
    token    = next((t for t in tokens if t["outcome"] == buy_outcome), None)
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

        if not breaker.check(unrealized_pnl=manager.get_unrealized_pnl()).can_trade:
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

async def _scan_updown_hourly_markets(session: aiohttp.ClientSession, gamma: GammaClient) -> list[dict]:
    import json as _json

    results = []
    now     = datetime.now(timezone.utc)

    min_min = getattr(config, "HOURLY_MIN_MINUTES_TO_RESOLVE", 5)
    max_min = getattr(config, "UPDOWN_HOURLY_MAX_MINUTES", 90)
    end_min = (now + timedelta(minutes=min_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_max = (now + timedelta(minutes=max_min)).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        batch = await gamma._aget(
            "/events",
            session,
            params={
                "closed":       "false",
                "limit":        200,
                "order":        "endDate",
                "ascending":    "true",
                "end_date_min": end_min,
                "end_date_max": end_max,
            },
        )
    except Exception as e:
        logger.debug(f"[UPDOWN HOURLY] Gagal fetch events: {e}")
        return results

    if not isinstance(batch, list):
        return results

    for event in batch:
        slug = (event.get("slug") or "").lower()

        symbol = None
        for sym, prefix in _UPDOWN_HOURLY_SLUG_PREFIXES.items():
            if slug.startswith(prefix):
                symbol = sym
                break
        if not symbol:
            continue

        if any(m in slug for m in _UPDOWN_HOURLY_SKIP_MARKERS):
            continue

        end_date_str   = event.get("endDate") or ""
        start_date_str = event.get("startDate") or ""
        try:
            end_date   = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            start_date = datetime.fromisoformat(start_date_str.replace("Z", "+00:00"))
        except Exception:
            continue

        minutes_left = (end_date - now).total_seconds() / 60
        if minutes_left < min_min or minutes_left > max_min:
            continue

        mkts = event.get("markets", [])
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

        mkt["_symbol"]     = symbol
        mkt["_start_date"] = start_date.isoformat()
        mkt["endDate"]     = end_date_str
        results.append(mkt)

    return results

async def _analyze_updown_hourly_market(
    market: dict, clob, gamma, sizer, manager,
    breaker, capital: float, session: aiohttp.ClientSession,
    vol_data: dict | None = None,
    closed_this_cycle: set | None = None,
    profit_locked_markets: set | None = None,
):
    import json as _json
    from src.logic.updown_strategy import calculate_updown_probability_hourly, calculate_recent_momentum
    from src.logic.pricing import ke_decimal

    symbol = market.get("_symbol", "")
    if not symbol:
        return

    condition_id = market.get("conditionId", market.get("id", ""))

    if closed_this_cycle and condition_id in closed_this_cycle:
        logger.debug(f"[UPDOWN HOURLY] Skip {condition_id[:8]} — closed this cycle, no re-entry")
        return
    if profit_locked_markets and condition_id in profit_locked_markets:
        logger.debug(f"[UPDOWN HOURLY] Skip {condition_id[:8]} — profit locked this session, no re-entry")
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

    max_per_slot = config.MAX_POSITIONS_PER_SLOT
    if max_per_slot > 0:
        slot_count = count_open_by_resolve_slot(end_date)
        if slot_count >= max_per_slot:
            logger.debug(
                f"[UPDOWN HOURLY] Skip {symbol} — "
                f"{slot_count}/{max_per_slot} posisi sudah di slot {end_date.strftime('%H:%M')} UTC"
            )
            return

    prob_up = await calculate_updown_probability_hourly(
        symbol, session, vol_data or {}, end_date, start_date
    )
    if prob_up is None:
        return

    edge      = prob_up - market_price_up
    threshold = config.UPDOWN_HOURLY_THRESHOLD

    if abs(edge) < threshold:
        logger.debug(f"[UPDOWN HOURLY] {symbol} edge={edge:+.3f} < {threshold:.2f} — skip")
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

    max_entry = config.UPDOWN_HOURLY_MAX_ENTRY_PRICE
    if max_entry > 0 and buy_price > max_entry:
        logger.debug(
            f"[UPDOWN HOURLY] Skip {symbol} {buy_outcome} — "
            f"buy_price {buy_price:.3f} > max {max_entry:.3f} (upside terlalu tipis)"
        )
        return

    min_wr = getattr(config, "UPDOWN_HOURLY_MIN_WINRATE", 0.55)
    if buy_winrate < min_wr:
        logger.debug(f"[UPDOWN HOURLY] {symbol} winrate {buy_winrate:.2f} < {min_wr:.2f} — skip")
        return

    momentum_min = config.UPDOWN_HOURLY_MOMENTUM_MINUTES
    momentum_thr = config.UPDOWN_HOURLY_MOMENTUM_THRESHOLD
    if momentum_thr > 0:
        momentum = await calculate_recent_momentum(symbol, session, minutes=momentum_min)
        if momentum is not None:
            if buy_outcome == "Up" and momentum < -momentum_thr:
                logger.info(
                    f"[UPDOWN HOURLY] Skip {symbol} Up — momentum {momentum:+.2%} "
                    f"in {momentum_min}m < -{momentum_thr:.1%}"
                )
                return
            if buy_outcome == "Down" and momentum > momentum_thr:
                logger.info(
                    f"[UPDOWN HOURLY] Skip {symbol} Down — momentum {momentum:+.2%} "
                    f"in {momentum_min}m > +{momentum_thr:.1%}"
                )
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

    t_min = delta_sec / 60.0
    log.info(
        f"[bold cyan][UPDOWN HOURLY][/bold cyan] {symbol} {t_min:.0f}m left | "
        f"BUY {buy_outcome} @ {buy_price:.3f} | "
        f"P(Up)={prob_up:.3f} Mkt={market_price_up:.3f} Edge={edge:+.3f} | "
        f"Kelly ${float(kelly.bet_usdc):.2f}"
    )

    tokens   = gamma.extract_token_ids(market)
    token    = next((t for t in tokens if t["outcome"] == buy_outcome), None)
    token_id = str(token["token_id"]) if token and token.get("token_id") else ""

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

        if not breaker.check(unrealized_pnl=manager.get_unrealized_pnl()).can_trade:
            return

        try:
            resolve_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        except Exception:
            resolve_date = datetime.now(timezone.utc)

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
                gap_pct         = abs(edge),
                kelly_fraction  = float(kelly.bet_fraction),
                strategy_mode   = "updown_hourly_dry_run",
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
                    gap_pct         = abs(edge),
                    kelly_fraction  = float(kelly.bet_fraction),
                    strategy_mode   = "updown_hourly",
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
                strategy = "Up/Down Hourly",
            )

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
        _profit_locked_markets: set[str] = set()
        while True:
            try:
                await _backfill_missing_token_ids(gamma, session)
                closed_this_cycle: set[str] = set()
                closed_this_cycle |= await _resolve_checker(clob, gamma, manager, breaker, session)

                if config.DRY_RUN:
                    from src.models.database import get_open_positions as _get_open, get_stats as _get_stats
                    locked        = sum(float(p["capital_at_risk"]) for p in _get_open())
                    realized_pnl  = float(_get_stats().get("total_pnl") or 0)
                    balance       = max(0.0, float(config.SALDO_AWAL) + realized_pnl - locked)
                    total_capital = float(config.SALDO_AWAL) + realized_pnl
                    breaker.starting_capital       = total_capital
                    breaker.state.starting_capital = total_capital
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
                    f"XRP {vol_data.get('XRP', 0.40):.0%} (annualized)"
                )

                current_prices = await _fetch_current_prices(clob, manager)

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

                closed_this_cycle |= await _force_exit_check(clob, manager, breaker, current_prices, session)
                exits = manager.evaluate_exits(current_prices)
                closed_this_cycle |= {d.position.condition_id for d in exits}
                for _d in exits:
                    if _d.signal.value == "exit_lock_profit":
                        _profit_locked_markets.add(_d.position.condition_id)
                for decision in exits:
                    pos = decision.position

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

                log.info(breaker.get_summary(unrealized_pnl=manager.get_unrealized_pnl()))
                await _prefetch_prices(session)

                markets = await gamma.ascan_hourly_opportunities(
                    session,
                    min_volume             = getattr(config, "HOURLY_MIN_MARKET_VOLUME", 500),
                    min_liquidity          = getattr(config, "HOURLY_MIN_LIQUIDITY", 200),
                    max_minutes_to_resolve = getattr(config, "HOURLY_MAX_MINUTES_TO_RESOLVE", 90),
                    min_minutes_to_resolve = getattr(config, "HOURLY_MIN_MINUTES_TO_RESOLVE", 5),
                    limit                  = 500,
                )

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

                cb_ok = breaker.check(unrealized_pnl=manager.get_unrealized_pnl()).can_trade
                if cb_ok and not safety.halt_new_entries:
                    log.info("[bold]── DAILY CRYPTO ─────────────────────────────────[/bold]")
                    log.info(f"Scan: {len(markets)} market lolos filter")
                    results = await asyncio.gather(*[
                        _analyze_market(
                            market, clob, gamma, detector, sizer, manager,
                            builder, breaker, capital, session, vol_data=vol_data,
                            closed_this_cycle=closed_this_cycle,
                            profit_locked_markets=_profit_locked_markets,
                        )
                        for market in markets
                    ], return_exceptions=True)
                    for r in results:
                        if isinstance(r, Exception):
                            logger.warning(f"[ANALYZE] Error di market analysis: {str(r).replace('[', '\\[')}")

                    log.info("[bold]── UP/DOWN DAILY ────────────────────────────────[/bold]")
                    updown_markets = await _scan_updown_markets(session, gamma)
                    log.info(f"[UPDOWN DAILY] {len(updown_markets)} active market")
                    for ud_mkt in updown_markets:
                        try:
                            await _analyze_updown_market(
                                ud_mkt, clob, gamma, sizer, manager,
                                breaker, capital, session, vol_data=vol_data,
                                closed_this_cycle=closed_this_cycle,
                                profit_locked_markets=_profit_locked_markets,
                            )
                        except Exception as e:
                            logger.warning(f"[UPDOWN DAILY] Error analyze {ud_mkt.get('_symbol', '?')}: {e}")

                    log.info("[bold]── UP/DOWN HOURLY ───────────────────────────────[/bold]")
                    hourly_markets = await _scan_updown_hourly_markets(session, gamma)
                    log.info(f"[UPDOWN HOURLY] {len(hourly_markets)} active market")
                    for hm in hourly_markets:
                        try:
                            await _analyze_updown_hourly_market(
                                hm, clob, gamma, sizer, manager,
                                breaker, capital, session, vol_data=vol_data,
                                closed_this_cycle=closed_this_cycle,
                                profit_locked_markets=_profit_locked_markets,
                            )
                        except Exception as e:
                            logger.warning(f"[UPDOWN HOURLY] Error analyze {hm.get('_symbol', '?')}: {e}")

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

def _extract_price_target(question: str) -> float | None:
    patterns = [
        r'\$([0-9]{1,3}(?:,[0-9]{3})+)',
        r'\$([0-9]+(?:\.[0-9]+)?)[kK]',
        r'\$([0-9]{4,})',
        r'[↑↓]\s*([0-9]{1,3}(?:,[0-9]{3})+)',
        r'[↑↓]\s*([0-9]+(?:\.[0-9]+)?)[kK]',
        r'[↑↓]\s*([0-9]+(?:\.[0-9]+)?)',
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
