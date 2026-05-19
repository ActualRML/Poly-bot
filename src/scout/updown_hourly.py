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
from src.scout.context import ScoutContext
from src.scout.scout import evaluate_entry

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

    if profit_locked_markets and condition_id in profit_locked_markets:
        logger.debug(
            f"[UPDOWN HOURLY] Skip {condition_id[:8]} — profit locked this session, no re-entry"
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
    _t_gate = _classify_t(
        t_min              = _t_min,
        strategy           = "momentum",
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

    _vol_annual = (vol_data or {}).get(symbol.upper()) or (vol_data or {}).get("DEFAULT") or 0.40
    _vol_15m    = _vol_annual / (252 * 96) ** 0.5
    _mom_min    = getattr(config, "UPDOWN_HOURLY_MOMENTUM_MIN",        0.0015)
    _mom_factor = getattr(config, "UPDOWN_HOURLY_MOMENTUM_VOL_FACTOR", 0.75)
    regime_thr  = max(_mom_min, _vol_15m * _mom_factor)
    regime_max  = getattr(config, "UPDOWN_HOURLY_MOMENTUM_MAX", 0.012)
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

    if sym_momentum > 0:
        buy_outcome = "Up"
        buy_price   = market_price_up
    else:
        buy_outcome = "Down"
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

    _scout_ctx = await ScoutContext.build(
        market                = market,
        session               = session,
        capital               = capital,
        vol_data              = vol_data or {},
        symbol_momentum_map   = symbol_momentum_map or {},
        market_regime         = market_regime,
        btc_scalp             = btc_scalp,
        market_session        = market_session,
        closed_this_cycle     = closed_this_cycle or set(),
        profit_locked_markets = profit_locked_markets or {},
    )
    if _scout_ctx is None:
        return
    _scout_ctx.buy_outcome = buy_outcome
    _scout_ctx.buy_price   = buy_price
    _scout_decision = evaluate_entry(_scout_ctx)
    log.info(f"[SCOUT] {symbol} {buy_outcome} {_scout_decision.summary()}")
    if not _scout_decision.enter:
        return

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

    log.info(
        f"[bold cyan][UPDOWN HOURLY][/bold cyan] {symbol} {t_min:.0f}m left | "
        f"BUY {buy_outcome} @ {buy_price:.3f} [momentum] | "
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

        _record_gap  = abs(btc_regime or 0.0)
        _record_prob = str(round(buy_winrate, 4))

        _scout_sub_score = None
        if _scout_decision is not None and "scout_composite" in _scout_decision.breakdown:
            _val = _scout_decision.breakdown["scout_composite"].value
            if isinstance(_val, dict):
                _scout_sub_score = _val.get("score")

        _diag_kwargs = {
            "sym_m5m":      float(sym_m5) if sym_m5 is not None else None,
            "sym_m15m":     float(sym_momentum) if sym_momentum is not None else None,
            "sym_m30m":     float(sym_m30) if sym_m30 is not None else None,
            "vol_ratio":    float(sym_vol_ratio) if sym_vol_ratio is not None else None,
            "btc_m15m":     float(btc_regime) if btc_regime is not None else None,
            "regime_score": int(_scout_sub_score) if _scout_sub_score is not None else None,
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
                strategy_mode   = "updown_hourly_momentum_dry_run",
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
                    strategy_mode   = "updown_hourly_momentum",
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
