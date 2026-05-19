from __future__ import annotations

import json as _json
import logging
from dataclasses import replace as _dc_replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import aiohttp

from src.utils.config import config
from src.utils.logger import log
from src.utils.pricing_cache import _open_position_lock
from src.utils.telegram_alert import get_alert
from src.models.types import SisiOrder
from src.models.database import log_prediction, get_recent_closed_pnls, count_open_by_resolve_slot
from src.risk.manager import calculate_position_size
from src.risk.slots import (
    slot_history_count, record_slot_entry,
    HOURLY_MAX_ENTRIES_PER_SLOT,
)
from src.risk.blacklist import check_symbol_blacklist
from src.risk.stagnation import track_market_price, is_price_stagnant
from src.scout.updown_scout import evaluate_updown_scout

logger = logging.getLogger(__name__)


async def analyze_updown_hourly_market(
    market: dict, clob, gamma, sizer, manager,
    breaker, capital: float, session: aiohttp.ClientSession,
    vol_data: dict | None = None,
    closed_this_cycle: set | None = None,
    profit_locked_markets: dict | None = None,
    btc_regime: float | None = None,
    btc_scalp: dict | None = None,
    symbol_momentum_map: dict | None = None,
    market_session: str = "US_MAIN",
    market_regime: dict | None = None,
):
    from src.risk.pricing import ke_decimal

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
    question = market.get("question", market.get("title", f"{symbol} Up or Down Hourly"))

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

    # Event horizon tier gate
    from src.scout.regime import classify_event_horizon as _classify_t
    _t_min = delta_sec / 60.0
    _use_gbm_now = getattr(config, "UPDOWN_HOURLY_USE_GBM", True)
    _t_gate = _classify_t(
        t_min              = _t_min,
        strategy           = "gbm",  # Phase 4.5: momentum-following post-Phase 4; 'gbm' = non-contrarian behavior. Rename deferred Phase 6.
        floor_min          = float(_min_t_min),
        contrarian_min     = getattr(config, "UPDOWN_HOURLY_CONTRARIAN_MIN_T", 20.0),
        tight_max          = getattr(config, "UPDOWN_HOURLY_T_TIER_TIGHT_MAX", 35.0),
        critical_max       = getattr(config, "UPDOWN_HOURLY_T_TIER_CRITICAL_MAX", 25.0),
        edge_mult_tight    = getattr(config, "UPDOWN_HOURLY_T_EDGE_MULT_TIGHT", 1.5),
        edge_mult_critical = getattr(config, "UPDOWN_HOURLY_T_EDGE_MULT_CRITICAL", 2.0),
    )
    if not _t_gate["allowed"]:
        log.info(
            f"[UPDOWN HOURLY] {symbol} skip — event horizon {_t_gate['tier']}: "
            f"{_t_gate['reason']}"
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
        from src.api.binance_client import get_rate_limit_status
        if get_rate_limit_status().get("status") == "FULL_PAUSE":
            logger.debug(f"[UPDOWN HOURLY] {symbol} skip — Binance pause aktif")
        else:
            logger.warning(f"[UPDOWN HOURLY] {symbol} — no momentum data (Binance fetch failed?), skip")
        return

    sym_momentum   = sym_mtf["m_15m"]
    sym_m5         = sym_mtf["m_5m"]
    sym_m30        = sym_mtf["m_30m"]
    sym_vol_ratio  = sym_mtf["vol_ratio"]

    use_gbm = getattr(config, "UPDOWN_HOURLY_USE_GBM", False)

    if not use_gbm:
        _vol_annual = (vol_data or {}).get(symbol.upper()) or (vol_data or {}).get("DEFAULT") or 0.40
        _vol_15m    = _vol_annual / (252 * 96) ** 0.5
        _mom_min    = getattr(config, "UPDOWN_HOURLY_MOMENTUM_MIN",        0.0015)
        _mom_factor = getattr(config, "UPDOWN_HOURLY_MOMENTUM_VOL_FACTOR", 0.75)
        regime_thr  = max(_mom_min, _vol_15m * _mom_factor)
        regime_max = getattr(config, "UPDOWN_HOURLY_MOMENTUM_MAX", 0.012)
        if abs(sym_momentum) < regime_thr:
            log.info(
                f"[UPDOWN HOURLY] {symbol} skip — "
                f"mom {sym_momentum:+.2%} < thr {regime_thr:.2%}"
            )
            return
        if getattr(config, "FILTER_MOMENTUM_CAP_ENABLED", False):
            if regime_max > 0 and abs(sym_momentum) > regime_max:
                log.info(
                    f"[UPDOWN HOURLY] {symbol} skip — "
                    f"mom {sym_momentum:+.2%} > max {regime_max:.1%} (trend too strong)"
                )
                return
            if abs(sym_m30) > regime_max * 1.5:
                log.info(
                    f"[UPDOWN HOURLY] {symbol} skip — "
                    f"30m mom {sym_m30:+.2%} too strong for contrarian"
                )
                return

    min_vol_ratio = config.UPDOWN_HOURLY_MIN_VOL_RATIO
    if sym_vol_ratio < min_vol_ratio:
        logger.debug(
            f"[UPDOWN HOURLY] {symbol} — volume ratio {sym_vol_ratio:.2f} "
            f"< {min_vol_ratio} (low conviction), skip"
        )
        return

    if is_price_stagnant(
        condition_id,
        threshold_pct=getattr(config, "UPDOWN_HOURLY_STAGNATION_THRESHOLD", 0.010),
    ):
        logger.debug(
            f"[UPDOWN HOURLY] {symbol} — Polymarket price stagnan "
            f"(<0.5% range dalam 5m), skip"
        )
        return

    _gbm_decision: dict | None = None
    if use_gbm:
        # ARCHIVED: UPDOWN_HOURLY_USE_GBM=false — revive by moving src/_archive/gbm.py back to src/scout/
        from src._archive.gbm import evaluate_hourly_entry
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
            from src.scout.regime import detect_market_regime as _det_regime
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

        # Event horizon edge multiplier (TIGHT/CRITICAL tiers)
        if _t_gate["edge_mult"] > 1.0:
            _adj_min_edge = _adj_min_edge * _t_gate["edge_mult"]
            logger.debug(
                f"[UPDOWN HOURLY] {symbol} T={_t_min:.0f}m tier={_t_gate['tier']} "
                f"→ edge mult ×{_t_gate['edge_mult']:.1f} → adj_min_edge={_adj_min_edge:.1%}"
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
            buy_outcome = "Up"     # momentum-following
            buy_price   = market_price_up
        else:
            buy_outcome = "Down"   # momentum-following
            buy_price   = round(1.0 - market_price_up, 4)

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

    # ── Per-market state gate: ILLIQUID / ONE_SIDED ──────────────────────────────
    from src.scout.regime import classify_market_state as _classify_mkt
    from src.risk.stagnation import get_price_velocity as _get_vel
    _mkt_state = _classify_mkt(
        market_price_up  = market_price_up,
        volume_24h       = float(market.get("volume", 0) or 0),
        price_velocity   = _get_vel(condition_id),
        intended_outcome = buy_outcome,
        vol_min_usd      = getattr(config, "UPDOWN_HOURLY_MIN_VOLUME_USD", 500.0),
        one_sided_high   = getattr(config, "UPDOWN_HOURLY_ONE_SIDED_HIGH", 0.82),
        one_sided_low    = getattr(config, "UPDOWN_HOURLY_ONE_SIDED_LOW", 0.18),
        velocity_threshold = getattr(config, "UPDOWN_HOURLY_VELOCITY_THR", 0.05),
    )
    if _mkt_state["state"] != "NORMAL":
        log.info(
            f"[UPDOWN HOURLY] {symbol} skip — "
            f"market {_mkt_state['state']}: {', '.join(_mkt_state['reasons'])}"
        )
        return
    # ─────────────────────────────────────────────────────────────────────────────

    if getattr(config, "UPDOWN_SCOUT_ENABLED", False):
        _scout_edge = (_gbm_decision["edge_up"] if buy_outcome == "Up" else _gbm_decision["edge_down"]) if _gbm_decision else 0.0
        _scout = await evaluate_updown_scout(
            symbol       = symbol,
            buy_outcome  = buy_outcome,
            gbm_edge     = _scout_edge,
            adj_min_edge = _adj_min_edge if use_gbm else config.UPDOWN_HOURLY_GBM_MIN_EDGE,
            minutes_left = delta_sec / 60,
            session      = session,
        )
        log.info(
            f"[SCOUT] {symbol} {buy_outcome} score={_scout.score}/{_scout.max_score} "
            f"edge={_scout.breakdown.get('edge')} mom={_scout.breakdown.get('momentum')} "
            f"macro={_scout.breakdown.get('macro')} vol={_scout.breakdown.get('vol_regime')} "
            f"time={_scout.breakdown.get('time')}"
        )
        if not _scout.passes:
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
        if _tech:
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
    if symbol != "BTC" and _btc_corr_thr > 0 and symbol_momentum_map:
        _btc_mtf = symbol_momentum_map.get("BTC")
        if _btc_mtf is not None:
            _btc_15m = _btc_mtf.get("m_15m", 0.0) or 0.0
            if abs(_btc_15m) >= _btc_corr_thr:
                _btc_dir = "Up" if _btc_15m > 0 else "Down"
                if _btc_dir != buy_outcome:
                    log.info(
                        f"[UPDOWN HOURLY] {symbol} skip — BTC 15m {_btc_15m:+.3%} → {_btc_dir} "
                        f"opposes {buy_outcome} (BTC lead, thr={_btc_corr_thr:.2%})"
                    )
                    return

    if locked_outcome is not None:
        # ARCHIVED: UPDOWN_HOURLY_USE_GBM=false — revive by moving src/_archive/gbm.py back to src/scout/
        from src._archive.gbm import passes_opposite_reentry_gate
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
    min_entry = config.UPDOWN_HOURLY_MIN_ENTRY_PRICE
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
        buy_winrate = 0.50

    if btc_scalp is not None:
        _scalp_kelly_mult = max(0.5, btc_scalp.get("kelly_multiplier", 1.0))

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

    _session_cap = {
        "ASIA":    getattr(config, "UPDOWN_HOURLY_ASIA_KELLY_CAP",    1.0),
        "US_MAIN": getattr(config, "UPDOWN_HOURLY_US_MAIN_KELLY_CAP", 0.7),
        "US_OPEN": 1.0,
        "EU":      1.0,
    }.get(market_session, 1.0)
    if _session_cap < 1.0:
        _scalp_kelly_mult = min(_scalp_kelly_mult, _session_cap)

    # Vol-adjusted sizing: high vol → size down
    if market_regime is not None:
        _vol_s = market_regime.get("vol_state", "NORMAL")
        _vol_km = {"EXTREME_HIGH": 0.50, "HIGH": 0.75}.get(_vol_s, 1.0)
        if _vol_km < 1.0:
            _scalp_kelly_mult = min(_scalp_kelly_mult, _vol_km)

    # Conviction bonus: WIDE event horizon + non-volatile vol → reward sniper entry
    _conv_bonus = getattr(config, "UPDOWN_HOURLY_CONVICTION_BONUS", 1.5)
    if (
        _t_gate.get("tier") == "WIDE"
        and (market_regime or {}).get("vol_state") in ("NORMAL", "LOW", "EXTREME_LOW")
        and _conv_bonus > 1.0
    ):
        _scalp_kelly_mult = _scalp_kelly_mult * _conv_bonus
        logger.debug(
            f"[UPDOWN HOURLY] {symbol} conviction bonus ×{_conv_bonus:.1f} "
            f"(WIDE T + {_vol_s} vol) → km={_scalp_kelly_mult:.2f}"
        )

    kelly = sizer.calculate(
        winrate      = buy_winrate,
        market_price = buy_price,
        capital      = capital,
    )

    if not kelly.is_positive_ev or float(kelly.bet_usdc) <= 0:
        return

    max_size = calculate_position_size(get_recent_closed_pnls(limit=5), capital=float(capital))
    if float(kelly.bet_usdc) > max_size:
        capped_usdc   = Decimal(str(max_size))
        capped_shares = (capped_usdc / Decimal(str(buy_price))).quantize(Decimal("0.0001"))
        kelly = _dc_replace(kelly, bet_usdc=capped_usdc, shares=capped_shares)

    if _scalp_kelly_mult < 1.0:
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
            from src.execute.scalping import liquidity_check
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
        _mode_label = "[momentum]"
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

        _diag_kwargs = {
            "sym_m5m":      float(sym_m5) if sym_m5 is not None else None,
            "sym_m15m":     float(sym_momentum) if sym_momentum is not None else None,
            "sym_m30m":     float(sym_m30) if sym_m30 is not None else None,
            "vol_ratio":    float(sym_vol_ratio) if sym_vol_ratio is not None else None,
            "btc_m15m":     float(btc_regime) if btc_regime is not None else None,
            "regime_score": int(_scout.score) if "_scout" in locals() and _scout is not None else None,
            "mtf_aligned":  int(bool(sym_mtf.get("all_tf_aligned"))) if sym_mtf else None,
        }

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
                strategy_mode   = f"updown_hourly_{'gbm' if use_gbm else 'momentum'}_dry_run",
                token_id        = token_id,
                **_diag_kwargs,
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
                    strategy_mode   = f"updown_hourly_{'gbm' if use_gbm else 'momentum'}",
                    token_id        = token_id,
                    **_diag_kwargs,
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
