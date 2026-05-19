from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal

import aiohttp

from src.api.gamma_client import GammaClient
from src.risk.kelly import KellySizer
from src.execute.exit import ExitEvaluator
from src.execute.position import PositionManager
# ARCHIVED: CB_ENABLED=false — revive by moving src/_archive/circuit.py back to src/risk/
from src._archive.circuit import CircuitBreaker
from src.models.types import SisiOrder
from src.models.database import log_prediction, get_recent_closed_pnls
from src.utils.config import config
from src.utils.logger import log
from src.utils.telegram_alert import init_telegram, get_alert
from src.risk.manager import get_dynamic_stop_loss, calculate_position_size
from src.execute.candle import scan_candle_markets, analyze_candle_market
from src.risk.slots import (
    slot_history_count, record_slot_entry, cleanup_old_slots,
    HOURLY_MAX_ENTRIES_PER_SLOT,
)
from src.risk.blacklist import check_symbol_blacklist, maybe_blacklist_symbol
from src.scout.scanner import scan_updown_hourly_markets
from src.execute.reentry_mgr import (
    reentry_candidates, register_reentry_candidate, cleanup_reentry_candidates,
    scan_reentry_opportunities,
)
from src.utils.parsing import detect_symbol_from_question
from src.utils.pricing_cache import build_vol_data, prefetch_prices, _open_position_lock
from src.models.token_backfill import backfill_missing_token_ids
from src.execute.reconciliation import reconcile_positions, resolve_checker, fetch_current_prices
from src.execute.tarik import execute_tarik, TARIK_FLAG
from src.scout.updown_hourly import analyze_updown_hourly_market
from src.api.clob_client import ClobClient

logger = logging.getLogger(__name__)


async def run_hourly_updown_mode(clob: ClobClient):
    gamma   = GammaClient(host=getattr(config, "GAMMA_HOST", "https://gamma-api.polymarket.com"))
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
            hourly_late_sl_t4_pct           = getattr(config, "HOURLY_LATE_SL_T4_PCT", -45.0),
            hourly_late_sl_t4_max_remaining = getattr(config, "HOURLY_LATE_SL_T4_MAX_REMAINING", 40.0),
            hourly_lock_t1_pct           = getattr(config, "HOURLY_LOCK_T1_PCT", 80.0),
            hourly_lock_t1_min_remaining = getattr(config, "HOURLY_LOCK_T1_MIN_REMAINING", 20.0),
            hourly_lock_t2_pct           = getattr(config, "HOURLY_LOCK_T2_PCT", 50.0),
            hourly_lock_t2_min_remaining = getattr(config, "HOURLY_LOCK_T2_MIN_REMAINING", 35.0),
        ),
    )
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
        await backfill_missing_token_ids(gamma, session)

        # Warmup Binance cache to avoid startup burst → FULL_PAUSE loop
        log.info("[STARTUP] Warming up Binance cache (6 symbols)...")
        from src.api.binance_client import fetch_klines, fetch_price
        from src.scout.regime import CRYPTO_BASKET
        for _sym in CRYPTO_BASKET:
            try:
                await fetch_price(_sym, session)
                await fetch_klines(_sym, session, interval="1m", limit=30)
                await fetch_klines(_sym, session, interval="5m", limit=30)
                await asyncio.sleep(0.8)
            except Exception as _e:
                logger.debug(f"[STARTUP] {_sym} warmup error: {_e}")
        log.info("[STARTUP] Cache warmup done.")

        await reconcile_positions(clob, gamma, manager, breaker, session)

        _cb_alerted = False
        _profit_locked_markets: dict[str, str] = {}
        _candle_sl_markets: dict[str, str] = {}
        _hourly_flip_queue: dict[str, dict] = {}
        _hourly_flip_last_exec: dict[str, datetime] = {}
        while True:
            try:
                await backfill_missing_token_ids(gamma, session)
                closed_this_cycle: set[str] = set()
                current_prices = await fetch_current_prices(clob, manager)
                closed_this_cycle |= await resolve_checker(clob, gamma, manager, breaker, session, current_prices)

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

                vol_data = await build_vol_data(session)
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

                if TARIK_FLAG.exists():
                    try:
                        _tarik_raw  = TARIK_FLAG.read_text().strip()
                        TARIK_FLAG.unlink()
                        _tarik_cids = [c for c in _tarik_raw.split(",") if c] or None
                        log.warning(
                            f"[yellow][TARIK] Flag detected — menutup "
                            f"{len(_tarik_cids) if _tarik_cids else 'semua'} posisi...[/yellow]"
                        )
                        _tarik_summary = await execute_tarik(manager, clob, session, condition_ids=_tarik_cids)
                        log.info(f"[TARIK] Done:\n{_tarik_summary}")
                        _alert = get_alert()
                        if _alert:
                            await _alert.send(_tarik_summary, session)
                    except Exception as _te:
                        logger.warning(f"[TARIK] Error: {_te}")

                try:
                    from src.execute.exit import ExitSignal as _XS
                    exit_decisions = manager.evaluate_exits(current_prices)
                    for _d in exit_decisions:
                        if not _d.should_exit:
                            continue

                        closed_this_cycle.add(_d.position.condition_id)
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

                        if _d.signal == _XS.EXIT_LOCK_PROFIT and _d.position.strategy_mode in (
                            "updown_hourly", "updown_hourly_dry_run"
                        ):
                            if config.REENTRY_AFTER_TP_ENABLED:
                                register_reentry_candidate(_d)
                            _profit_locked_markets[_d.position.condition_id] = _d.position.outcome

                        if not config.DRY_RUN and _d.position.token_id:
                            try:
                                from src.risk.pricing import ke_decimal as _ked
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

                # --- Hourly flip early-exit scan ---
                if config.HOURLY_FLIP_ENABLED:
                    try:
                        from src.models.database import (
                            close_position as _fp_close,
                            log_trade as _fp_log,
                            resolve_prediction as _fp_resolve,
                            get_open_positions as _fp_get_open,
                        )
                        _fp_trigger  = config.HOURLY_FLIP_TRIGGER_PCT
                        _fp_min_mins = config.HOURLY_FLIP_MIN_MINUTES
                        _fp_max_ent  = config.HOURLY_FLIP_MAX_ENTRY

                        for _fp in _fp_get_open():
                            if _fp.get("strategy_mode") not in (
                                "updown_hourly", "updown_hourly_dry_run"
                            ):
                                continue
                            _fp_cid = _fp["condition_id"]
                            if _fp_cid in _hourly_flip_queue:
                                continue
                            _fp_outcome = _fp.get("outcome", "")
                            _fp_ent     = float(_fp.get("entry_price") or 0)
                            _fp_cur     = float(
                                current_prices.get(_fp_cid, {}).get(_fp_outcome, Decimal("0"))
                            )
                            if _fp_ent <= 0 or _fp_cur <= 0:
                                continue
                            _fp_pnl_pct = (_fp_cur - _fp_ent) / _fp_ent * 100
                            try:
                                _fp_resolve_dt = datetime.fromisoformat(
                                    str(_fp.get("resolve_date", "")).replace("Z", "+00:00")
                                )
                                if _fp_resolve_dt.tzinfo is None:
                                    _fp_resolve_dt = _fp_resolve_dt.replace(tzinfo=timezone.utc)
                            except Exception:
                                continue
                            _fp_mins_left = (
                                _fp_resolve_dt - datetime.now(timezone.utc)
                            ).total_seconds() / 60
                            _fp_opp_price = round(1.0 - _fp_cur, 4)

                            if not (
                                _fp_pnl_pct  <= _fp_trigger
                                and _fp_mins_left >= _fp_min_mins
                                and _fp_opp_price <= _fp_max_ent
                            ):
                                continue

                            _fp_shares_val = float(_fp.get("shares") or 0)
                            _fp_cap        = float(_fp.get("capital_at_risk") or 0)
                            _fp_pnl_usdc   = Decimal(str(round(
                                (_fp_cur - _fp_ent) * _fp_shares_val, 4
                            )))
                            _fp_question   = _fp.get("question", "")
                            _fp_sym        = detect_symbol_from_question(_fp_question)

                            _fp_close(
                                condition_id = _fp_cid,
                                outcome      = _fp_outcome,
                                exit_price   = Decimal(str(_fp_cur)),
                                exit_reason  = "flip_early_exit",
                                pnl_usdc     = _fp_pnl_usdc,
                            )
                            _fp_log({
                                "condition_id": _fp_cid,
                                "question":     _fp_question,
                                "outcome":      _fp_outcome,
                                "action":       "exit",
                                "price":        _fp_cur,
                                "shares":       _fp_shares_val,
                                "usdc_amount":  float(_fp_pnl_usdc),
                                "notes": (
                                    f"flip_early_exit pnl={_fp_pnl_pct:.1f}% "
                                    f"{_fp_mins_left:.0f}m left"
                                ),
                            })
                            _fp_resolve(
                                condition_id  = _fp_cid,
                                outcome       = _fp_outcome,
                                won           = False,
                                resolve_price = _fp_cur,
                            )
                            try:
                                breaker.record_trade(float(_fp_pnl_usdc))
                            except Exception:
                                pass
                            if _fp_sym != "UNKNOWN":
                                maybe_blacklist_symbol(_fp_sym)

                            log.info(
                                f"[EXIT] ❌ FLIP_EARLY_EXIT — {_fp_question[:40]} | "
                                f"{_fp_outcome} @ {_fp_ent:.3f}→{_fp_cur:.3f} | "
                                f"PnL ${float(_fp_pnl_usdc):+.2f} | "
                                f"{_fp_pnl_pct:.0f}% {_fp_mins_left:.0f}m left"
                            )

                            _fp_direction = "Down" if _fp_outcome == "Up" else "Up"
                            _hourly_flip_queue[_fp_cid] = {
                                "flip_to":          _fp_direction,
                                "flip_price":       _fp_opp_price,
                                "original_capital": _fp_cap,
                                "resolve_date":     _fp_resolve_dt,
                                "question":         _fp_question,
                                "symbol":           _fp_sym,
                            }
                            log.info(
                                f"[FLIP] {_fp_cid[:8]} queued: "
                                f"{_fp_outcome}→{_fp_direction} @ ~{_fp_opp_price:.3f} "
                                f"({_fp_mins_left:.0f}m left)"
                            )

                            _exit_alert = get_alert()
                            if _exit_alert:
                                await _exit_alert.alert_exit(
                                    question    = _fp_question,
                                    outcome     = _fp_outcome,
                                    entry_price = _fp_ent,
                                    exit_price  = _fp_cur,
                                    pnl_usdc    = float(_fp_pnl_usdc),
                                    reason      = (
                                        f"flip early exit "
                                        f"({_fp_pnl_pct:.0f}%, {_fp_mins_left:.0f}m left)"
                                    ),
                                    session     = session,
                                )

                            if not config.DRY_RUN and _fp.get("token_id"):
                                try:
                                    from src.risk.pricing import ke_decimal as _fped
                                    clob.pasang_order(
                                        sisi     = SisiOrder.JUAL,
                                        harga    = _fped(str(_fp_cur)),
                                        ukuran   = Decimal(str(_fp_shares_val)),
                                        token_id = _fp["token_id"],
                                    )
                                except Exception as _fse:
                                    logger.warning(
                                        f"[FLIP] Sell order error {_fp_cid[:8]}: {_fse}"
                                    )

                    except Exception as _fe:
                        logger.warning(f"[FLIP SCAN] Error: {_fe}")

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
                await prefetch_prices(session)

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

                    from src.execute.updown import calculate_multi_tf_momentum as _mtf_mom
                    from src.scout.regime import CRYPTO_BASKET
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
                            log.info(f"[REGIME] BTC momentum {btc_regime:+.2%} → BULLISH (momentum: beli Up)")
                        elif btc_regime < -regime_thr:
                            log.info(f"[REGIME] BTC momentum {btc_regime:+.2%} → BEARISH (momentum: beli Down)")
                        else:
                            log.info(f"[REGIME] BTC momentum {btc_regime:+.2%} → NEUTRAL")

                    _btc_scalp = None
                    try:
                        from src.execute.updown import calculate_scalping_signals as _csc
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
                        from src.scout.regime import detect_market_regime
                        _market_regime = await detect_market_regime(session)
                        ca = _market_regime["cross_asset"]
                        htf = _market_regime["higher_tf"]
                        sess_info = _market_regime["session"]
                        log.info(
                            f"[MARKET REGIME] {_market_regime['regime']} "
                            f"score={_market_regime['trend_score']} "
                            f"vol={_market_regime.get('vol_state','?')} "
                            f"({_market_regime.get('vol_annual', 0) or 0:.0%} ann) | "
                            f"cross={ca['aligned_count']}/{ca['total_count']} {ca.get('direction') or 'mixed'} "
                            f"avg={ca['avg_move_pct']:+.2%} | "
                            f"htf_1h={htf['tf_1h']} htf_4h={htf['tf_4h']} | "
                            f"session={sess_info['session']}"
                        )
                    except Exception as _e:
                        logger.warning(f"[MARKET REGIME] Error: {_e}")

                    if config.CANDLE_ENABLED:
                        hourly_markets, candle_markets = await asyncio.gather(
                            scan_updown_hourly_markets(session, gamma),
                            scan_candle_markets(session, gamma),
                        )
                    else:
                        hourly_markets = await scan_updown_hourly_markets(session, gamma)
                        candle_markets = []
                    log.info(
                        f"[UPDOWN] hourly={len(hourly_markets)} active"
                    )

                    # --- Hourly flip queue processor ---
                    if config.HOURLY_FLIP_ENABLED and _hourly_flip_queue:
                        import json as _fjson
                        for _fcid in list(_hourly_flip_queue.keys()):
                            try:
                                _fq = _hourly_flip_queue[_fcid]
                                if not isinstance(_fq, dict):
                                    log.warning(f"[FLIP PROC] {_fcid[:8]} invalid entry type {type(_fq).__name__}, purging")
                                    _hourly_flip_queue.pop(_fcid)
                                    continue
                                _f_mins = (
                                    _fq["resolve_date"] - datetime.now(timezone.utc)
                                ).total_seconds() / 60

                                if _f_mins < 5:
                                    log.info(f"[FLIP] {_fcid[:8]} expired")
                                    _hourly_flip_queue.pop(_fcid)
                                    continue

                                if check_symbol_blacklist(_fq.get("symbol", "")):
                                    log.info(f"[FLIP] {_fcid[:8]} skip — blacklisted")
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

                                _f_outcomes = _f_market.get("outcomes", [])
                                _f_op       = _f_market.get("outcomePrices", [])
                                if isinstance(_f_outcomes, str):
                                    try: _f_outcomes = _fjson.loads(_f_outcomes)
                                    except: _f_outcomes = []
                                if isinstance(_f_op, str):
                                    try: _f_op = _fjson.loads(_f_op)
                                    except: _f_op = []
                                _f_out_lower  = [str(o).lower() for o in _f_outcomes]
                                _f_flip_lower = _fq["flip_to"].lower()
                                if _f_flip_lower in _f_out_lower:
                                    _f_idx   = _f_out_lower.index(_f_flip_lower)
                                    _f_price = (
                                        float(_f_op[_f_idx])
                                        if _f_idx < len(_f_op)
                                        else _fq["flip_price"]
                                    )
                                else:
                                    _f_price = _fq["flip_price"]

                                if _f_price > config.HOURLY_FLIP_MAX_ENTRY:
                                    log.info(
                                        f"[FLIP] {_fcid[:8]} pop: price {_f_price:.3f}"
                                        f" > max {config.HOURLY_FLIP_MAX_ENTRY}"
                                    )
                                    _hourly_flip_queue.pop(_fcid)
                                    continue

                                _f_sym_str = _fq.get("symbol", "")
                                _f_last_exec = _hourly_flip_last_exec.get(_f_sym_str)
                                if _f_last_exec and (
                                    datetime.now(timezone.utc) - _f_last_exec
                                ).total_seconds() / 60 < config.HOURLY_FLIP_COOLDOWN_MINUTES:
                                    log.info(f"[FLIP] {_fcid[:8]} skip: cooldown")
                                    continue

                                _f_queued_price = _fq["flip_price"]
                                if _f_price > _f_queued_price * (1 + config.HOURLY_FLIP_PRICE_BUFFER_PCT):
                                    log.info(
                                        f"[FLIP] {_fcid[:8]} pop: slippage "
                                        f"{_f_price:.3f} > {_f_queued_price:.3f}×(1+{config.HOURLY_FLIP_PRICE_BUFFER_PCT:.0%})"
                                    )
                                    _hourly_flip_queue.pop(_fcid)
                                    continue

                                _f_spread = 1.0 - sum(
                                    float(p) for p in (_f_op if isinstance(_f_op, list) else [])
                                )
                                if _f_spread > config.HOURLY_FLIP_MAX_SPREAD:
                                    log.info(f"[FLIP] {_fcid[:8]} skip: spread {_f_spread:.3f}")
                                    continue

                                _f_mom_data = symbol_momentum_map.get(_f_sym_str.upper(), {})
                                _f_m5 = float(_f_mom_data.get("m_5m") or 0.0)
                                if _fq["flip_to"] == "Down" and _f_m5 > 0:
                                    log.info(
                                        f"[FLIP] {_fcid[:8]} skip: bullish m5={_f_m5:.4f} vs flip Down"
                                    )
                                    continue
                                if _fq["flip_to"] == "Up" and _f_m5 < 0:
                                    log.info(
                                        f"[FLIP] {_fcid[:8]} skip: bearish m5={_f_m5:.4f} vs flip Up"
                                    )
                                    continue

                                _f_token_ids = gamma.extract_token_ids(_f_market)
                                _f_token_id  = _f_token_ids.get(_fq["flip_to"], "")

                                _f_capital = _fq["original_capital"] * 0.5
                                _f_cap_dec = Decimal(str(round(_f_capital, 4)))
                                _f_shares  = (_f_cap_dec / Decimal(str(_f_price))).quantize(
                                    Decimal("0.0001")
                                )
                                _f_mode = (
                                    "updown_hourly_dry_run" if config.DRY_RUN else "updown_hourly"
                                )
                                _f_sym = _fq.get("symbol", "?")

                                async with _open_position_lock:
                                    _can, _reason = manager.can_open(
                                        condition_id  = _fcid,
                                        outcome       = _fq["flip_to"],
                                        bet_usdc      = _f_cap_dec,
                                        total_capital = Decimal(str(capital)),
                                    )
                                    if not _can:
                                        log.info(f"[FLIP] {_fcid[:8]} wait: {_reason}")
                                        continue

                                    if config.DRY_RUN:
                                        manager.open_position(
                                            condition_id    = _fcid,
                                            question        = _fq["question"],
                                            outcome         = _fq["flip_to"],
                                            entry_price     = Decimal(str(_f_price)),
                                            shares          = _f_shares,
                                            capital_at_risk = _f_cap_dec,
                                            resolve_date    = _fq["resolve_date"],
                                            gap_pct         = 0.0,
                                            kelly_fraction  = 0.5,
                                            strategy_mode   = _f_mode,
                                            token_id        = _f_token_id,
                                        )
                                        record_slot_entry(_fq["resolve_date"])
                                        _hourly_flip_last_exec[_f_sym_str] = datetime.now(timezone.utc)
                                        log.info(
                                            f"[FLIP DRY] {_f_sym} {_fq['flip_to']}"
                                            f" @ {_f_price:.3f} cap ${_f_capital:.2f}"
                                            f" ({_f_mins:.0f}m left)"
                                        )
                                    else:
                                        if not _f_token_id:
                                            logger.warning(
                                                f"[FLIP] token_id missing {_fcid[:8]}"
                                            )
                                            _hourly_flip_queue.pop(_fcid, None)
                                            continue
                                        from src.risk.pricing import ke_decimal as _fked2
                                        _f_order = clob.pasang_order(
                                            sisi     = SisiOrder.BELI,
                                            harga    = _fked2(str(_f_price)),
                                            ukuran   = _f_shares,
                                            token_id = _f_token_id,
                                        )
                                        if _f_order:
                                            manager.open_position(
                                                condition_id    = _fcid,
                                                question        = _fq["question"],
                                                outcome         = _fq["flip_to"],
                                                entry_price     = Decimal(str(_f_price)),
                                                shares          = _f_shares,
                                                capital_at_risk = _f_cap_dec,
                                                resolve_date    = _fq["resolve_date"],
                                                gap_pct         = 0.0,
                                                kelly_fraction  = 0.5,
                                                strategy_mode   = _f_mode,
                                                token_id        = _f_token_id,
                                            )
                                            record_slot_entry(_fq["resolve_date"])
                                            _hourly_flip_last_exec[_f_sym_str] = datetime.now(timezone.utc)
                                            log.info(
                                                f"[FLIP] ✅ {_f_sym} {_fq['flip_to']}"
                                                f" @ {_f_price:.3f} cap ${_f_capital:.2f}"
                                                f" ({_f_mins:.0f}m left)"
                                            )
                                        else:
                                            logger.warning(
                                                f"[FLIP] Order failed {_fcid[:8]}"
                                            )

                                if config.DRY_RUN or _f_order:
                                    _hourly_flip_queue.pop(_fcid, None)

                            except Exception as _fpe:
                                logger.warning(f"[FLIP PROC] {_fcid[:8]}: {_fpe}")

                    _market_session_label = (
                        _market_regime["session"]["session"] if _market_regime else "US_MAIN"
                    )

                    from src.scout.cycle import ScoutCycleGate
                    _cycle = ScoutCycleGate.evaluate(
                        market_regime = _market_regime,
                        breaker       = breaker,
                        manager       = manager,
                        btc_vol       = btc_vol,
                    )
                    _flash_crash = (
                        (_market_regime or {}).get("vol_state") == "EXTREME_HIGH"
                        and getattr(config, "FLASH_CRASH_HARD_SKIP", True)
                    )
                    if not _cycle.enter_allowed:
                        log.warning(
                            f"[CYCLE GATE] skip {len(hourly_markets)} hourly + "
                            f"{len(candle_markets)} candle — {_cycle.reason}"
                        )
                    else:
                        for hm in hourly_markets:
                            try:
                                await analyze_updown_hourly_market(
                                    hm, clob, gamma, sizer, manager,
                                    breaker, capital, session, vol_data=vol_data,
                                    closed_this_cycle=closed_this_cycle,
                                    profit_locked_markets=_profit_locked_markets,
                                    btc_regime=btc_regime,
                                    btc_scalp=_btc_scalp,
                                    symbol_momentum_map=symbol_momentum_map,
                                    market_session=_market_session_label,
                                    market_regime=_market_regime,
                                )
                            except Exception as e:
                                logger.warning(f"[UPDOWN HOURLY] Error analyze {hm.get('_symbol', '?')}: {e}")

                    if _flash_crash or not _cycle.enter_allowed:
                        candle_markets = []
                    if config.CANDLE_ENABLED:
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

                    if config.REENTRY_AFTER_TP_ENABLED and reentry_candidates:
                        await scan_reentry_opportunities(
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
