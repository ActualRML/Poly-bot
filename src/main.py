"""
src/main.py
===========
Entry point — orchestrator utama bot (async version).
Strategi: Crypto Hourly Trading only.
"""

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
from datetime import datetime, timezone
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
from src.models.database import log_prediction, get_recent_closed_pnls
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
_open_position_lock = asyncio.Lock()  # serialize can_open→open_position untuk hindari race di asyncio.gather


async def _fetch_crypto_price(symbol: str, session: aiohttp.ClientSession) -> float | None:
    """Fetch harga crypto. Binance primary (30s cache), CoinGecko fallback (5m cache)."""
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
    """Fetch semua harga crypto sekaligus secara paralel."""
    await asyncio.gather(
        _fetch_crypto_price("BTC", session),
        _fetch_crypto_price("ETH", session),
        _fetch_crypto_price("SOL", session),
        _fetch_crypto_price("BNB", session),
        return_exceptions=True,
    )


async def _build_vol_data(session: aiohttp.ClientSession) -> dict:
    """Fetch realized vol semua asset aktif. Return {asset: annualized_vol, "DEFAULT": 0.40}."""
    from src.api.binance_client import fetch_realized_vol
    vol_hours = getattr(config, "HOURLY_VOL_HOURS", 4)

    results = await asyncio.gather(
        fetch_realized_vol("BTC", session, hours=vol_hours),
        fetch_realized_vol("ETH", session, hours=vol_hours),
        fetch_realized_vol("SOL", session, hours=vol_hours),
        fetch_realized_vol("BNB", session, hours=vol_hours),
        return_exceptions=True,
    )
    vol_data: dict = {"DEFAULT": 0.40}
    for symbol, result in zip(["BTC", "ETH", "SOL", "BNB"], results):
        if isinstance(result, float) and result > 0:
            vol_data[symbol] = result
    return vol_data


async def _force_exit_check(
    clob,
    manager,
    breaker,
    current_prices: dict,
    session: aiohttp.ClientSession,
) -> None:
    """Jual posisi yang < 10 menit sebelum expiry. Jalan sebelum evaluate_exits()."""
    from src.models.database import get_open_positions
    from src.logic.pricing import ke_decimal

    for pos in get_open_positions():
        try:
            expiry = datetime.fromisoformat(pos["resolve_date"])
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        if not should_force_exit(expiry):
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


async def _get_base_rates(market: dict, builder, session: aiohttp.ClientSession) -> list:
    """Auto-generate base rates untuk crypto market (async)."""
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

        from src.api.binance_client import fetch_realized_vol
        vol_hours  = getattr(config, "HOURLY_VOL_HOURS", 4)
        volatility = await fetch_realized_vol(symbol, session, hours=vol_hours)

        result = await calc.calculate_async(
            symbol, price, target, days_remaining, session,
            direction=direction, use_barrier=use_barrier,
            volatility=volatility, drift=None,
        )
        if result.probability == 0.0:
            return []
        return [builder.from_manual(rate=result.probability, confidence=result.confidence, notes=result.notes)]

    # XRP & DOGE excluded — MAE >4%, edge effective negatif
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
    builder, breaker, capital, session, vol_data: dict | None = None
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

    # Per-asset dynamic threshold berdasarkan realized vol
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

        min_wr = getattr(config, "HOURLY_MIN_WINRATE_STRICT", 0.75)
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

        # Adaptive position size cap — streak-based, cap terhadap Kelly
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

        # Serialize gating + open. Tanpa lock, asyncio.gather bisa loloskan
        # multiple market saat capacity tersisa hanya untuk 1 — exceed MAX_OPEN_POSITIONS.
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
                        strategy_mode   = "hourly",
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

        # Telegram alert di luar lock — tidak perlu blocking entry decision lain
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
        strategy_mode   = "hourly_dry_run",
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


async def reconcile_positions(clob, gamma, manager, breaker, session: aiohttp.ClientSession) -> None:
    """
    Startup reconciliation — jalankan SEKALI sebelum main loop.

    Berbeda dari _resolve_checker yang hanya cek resolve_date < now,
    fungsi ini cek SEMUA posisi open ke Gamma API untuk menangkap:
    - Market yang resolved lebih awal dari resolve_date
    - Posisi yang stuck 'open' karena bot crash saat settle

    Tidak memodifikasi posisi yang marketnya belum closed di Gamma.
    """
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
            # Cek Gamma: apakah market sudah closed & ada outcomePrices?
            price = await _get_resolved_price_from_gamma(gamma, session, cid, outcome)

            # Fallback: cek CLOB order book — tutup kalau near-resolved (≥0.98 atau ≤0.02)
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


async def _resolve_checker(clob, gamma, manager, breaker, session: aiohttp.ClientSession) -> None:
    """
    Cek posisi open yang resolve_date-nya sudah lewat.
    - Resolved di Polymarket → ambil final outcomePrices dari Gamma
    - Belum resolved tapi expired → fetch best_bid dari CLOB
    - Auto-close kalau price ≥0.98 atau ≤0.02
    - Force-close kalau >{FORCE_CLOSE_GRACE_HOURS}h lewat resolve & harga mid-range
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
    """Loop utama hourly crypto strategy — fully async."""
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
        f"[bold green]Hourly crypto strategy aktif (async).[/bold green] "
        f"Threshold: dynamic [6-25%] | "
        f"Min winrate: {getattr(config, 'HOURLY_MIN_WINRATE_STRICT', 0.75):.0%} | "
        f"Polling: {config.POLLING_INTERVAL}s"
    )

    async with aiohttp.ClientSession() as session:
        await _backfill_missing_token_ids(gamma, session)
        await reconcile_positions(clob, gamma, manager, breaker, session)

        while True:
            try:
                await _backfill_missing_token_ids(gamma, session)
                await _resolve_checker(clob, gamma, manager, breaker, session)

                if config.DRY_RUN:
                    from src.models.database import get_open_positions as _get_open
                    locked = sum(float(p["capital_at_risk"]) for p in _get_open())
                    balance = max(0.0, float(config.SALDO_AWAL) - locked)
                else:
                    balance = clob.get_balance()
                capital = balance
                prefix  = "[DRY RUN] " if config.DRY_RUN else ""
                log.info(f"{prefix}Balance: ${capital:.2f} USDC | {manager.get_summary()}")

                # Exit block — SELALU jalan, tidak diblokir circuit breaker
                vol_data = await _build_vol_data(session)
                btc_vol  = vol_data.get("BTC") or vol_data.get("DEFAULT", 0.40)
                log.info(
                    f"[VOL] BTC {btc_vol:.0%} | "
                    f"ETH {vol_data.get('ETH', 0.40):.0%} | "
                    f"SOL {vol_data.get('SOL', 0.40):.0%} | "
                    f"BNB {vol_data.get('BNB', 0.40):.0%} (annualized)"
                )

                current_prices = await _fetch_current_prices(clob, manager)

                # Dynamic trailing stop — adapt ke harga posisi terkini
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

                await _force_exit_check(clob, manager, breaker, current_prices, session)
                exits = manager.evaluate_exits(current_prices)
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

                # Circuit breaker check — HANYA blokir entry baru
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

                markets = await gamma.ascan_hourly_opportunities(
                    session,
                    min_volume             = getattr(config, "HOURLY_MIN_MARKET_VOLUME", 500),
                    min_liquidity          = getattr(config, "HOURLY_MIN_LIQUIDITY", 200),
                    max_minutes_to_resolve = getattr(config, "HOURLY_MAX_MINUTES_TO_RESOLVE", 90),
                    min_minutes_to_resolve = getattr(config, "HOURLY_MIN_MINUTES_TO_RESOLVE", 5),
                    limit                  = 500,
                )
                log.info(f"Hourly scan: {len(markets)} market lolos filter")

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
                    results = await asyncio.gather(*[
                        _analyze_market(
                            market, clob, gamma, detector, sizer, manager,
                            builder, breaker, capital, session, vol_data=vol_data
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
    """Extract angka target harga dari teks pertanyaan."""
    patterns = [
        r'\$([0-9]{1,3}(?:,[0-9]{3})+)',       # $77,000
        r'\$([0-9]+(?:\.[0-9]+)?)[kK]',         # $77k
        r'\$([0-9]{4,})',                        # $77000
        r'[↑↓]\s*([0-9]{1,3}(?:,[0-9]{3})+)',  # ↓ 77,000
        r'[↑↓]\s*([0-9]+(?:\.[0-9]+)?)[kK]',   # ↑ 77k
        r'[↑↓]\s*([0-9]+(?:\.[0-9]+)?)',        # ↓ 1.40 (XRP)
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
