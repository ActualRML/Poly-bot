from __future__ import annotations

import logging
from dataclasses import replace as _dc_replace
from datetime import datetime, timezone
from decimal import Decimal

import aiohttp

from src.utils.config import config
from src.utils.logger import log
from src.utils.pricing_cache import _open_position_lock
from src.utils.telegram_alert import get_alert
from src.models.types import SisiOrder
from src.models.database import log_prediction, get_recent_closed_pnls
from src.risk.manager import calculate_position_size
from src.risk.slots import record_slot_entry, HOURLY_MAX_ENTRIES_PER_SLOT
from src.risk.blacklist import check_symbol_blacklist
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

    symbol          = _scout_ctx.symbol
    condition_id    = _scout_ctx.condition_id
    question        = _scout_ctx.question
    market_price_up = _scout_ctx.market_price_up
    end_date        = _scout_ctx.end_date
    end_date_str    = market.get("endDate", "")
    delta_sec       = _scout_ctx.delta_sec

    max_per_slot = config.MAX_POSITIONS_PER_SLOT
    if max_per_slot > 0 and _scout_ctx.slot_open_count >= max_per_slot:
        logger.debug(
            f"[UPDOWN HOURLY] Skip {symbol} — "
            f"{_scout_ctx.slot_open_count}/{max_per_slot} OPEN di slot {end_date.strftime('%H:%M')} UTC"
        )
        return
    _max_entries = getattr(config, "UPDOWN_HOURLY_MAX_ENTRIES_PER_SLOT", HOURLY_MAX_ENTRIES_PER_SLOT)
    slot_history = _scout_ctx.slot_history_count
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

    _scout_decision = evaluate_entry(_scout_ctx)
    log.info(f"[SCOUT] {symbol} {_scout_decision.summary()}")
    if not _scout_decision.enter:
        return

    buy_outcome   = _scout_ctx.buy_outcome
    buy_price     = _scout_ctx.buy_price
    sym_mtf       = _scout_ctx.sym_mtf
    sym_momentum  = sym_mtf["m_15m"]
    sym_m5        = sym_mtf["m_5m"]
    sym_m30       = sym_mtf["m_30m"]
    sym_vol_ratio = sym_mtf["vol_ratio"]
    _t_gate       = _scout_ctx.event_horizon or {}

    _scalp_kelly_mult = 1.0
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
