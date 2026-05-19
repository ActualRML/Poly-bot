from __future__ import annotations

import logging
from datetime import datetime, timezone

import aiohttp

from src.utils.config import config
from src.utils.logger import log
from src.utils.pricing_cache import _open_position_lock
from src.utils.telegram_alert import get_alert
from src.models.types import SisiOrder
from src.models.database import log_prediction
from src.risk.slots import record_slot_entry, HOURLY_MAX_ENTRIES_PER_SLOT
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

    ctx = await ScoutContext.build(
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
    if ctx is None:
        return

    ctx.sizer   = sizer
    ctx.gamma   = gamma
    ctx.clob    = clob
    ctx.manager = manager
    ctx.breaker = breaker

    async with _open_position_lock:
        decision = await evaluate_entry(ctx)
        log.info(f"[SCOUT] {ctx.symbol} {decision.summary()}")
        if not decision.enter:
            return

        symbol         = ctx.symbol
        condition_id   = ctx.condition_id
        question       = ctx.question
        buy_outcome    = ctx.buy_outcome
        buy_price      = ctx.buy_price
        kelly          = ctx.kelly
        token_id       = ctx.token_id
        end_date       = ctx.end_date
        sym_mtf        = ctx.sym_mtf or {}
        sym_momentum   = sym_mtf.get("m_15m", 0.0)
        sym_m5         = sym_mtf.get("m_5m", 0.0)
        sym_m30        = sym_mtf.get("m_30m", 0.0)
        sym_vol_ratio  = sym_mtf.get("vol_ratio", 0.0)
        max_entries    = getattr(config, "UPDOWN_HOURLY_MAX_ENTRIES_PER_SLOT", HOURLY_MAX_ENTRIES_PER_SLOT)
        slot_history   = ctx.slot_history_count
        t_min          = ctx.t_min

        log.info(
            f"[bold cyan][UPDOWN HOURLY][/bold cyan] {symbol} {t_min:.0f}m left | "
            f"BUY {buy_outcome} @ {buy_price:.3f} [momentum] | "
            f"sym 5m/15m/30m {sym_m5:+.2%}/{sym_momentum:+.2%}/{sym_m30:+.2%} "
            f"vol×{sym_vol_ratio:.2f} | "
            f"scalp={btc_scalp.get('action', '-') if btc_scalp else '-'} "
            f"wr={ctx.buy_winrate:.2f} km={ctx.scalp_kelly_mult} | "
            f"slot {slot_history+1}/{max_entries} | Kelly ${float(kelly.bet_usdc):.2f}"
        )

        try:
            resolve_date = datetime.fromisoformat(market.get("endDate", "").replace("Z", "+00:00"))
        except Exception:
            resolve_date = datetime.now(timezone.utc)

        _record_gap  = abs(btc_regime or 0.0)
        _record_prob = str(round(ctx.buy_winrate, 4))

        _scout_sub_score = None
        if "scout_composite" in decision.breakdown:
            _val = decision.breakdown["scout_composite"].value
            if isinstance(_val, dict):
                _scout_sub_score = _val.get("score")

        _diag_kwargs = {
            "sym_m5m":      float(sym_m5) if sym_m5 is not None else None,
            "sym_m15m":     float(sym_momentum) if sym_momentum is not None else None,
            "sym_m30m":     float(sym_m30) if sym_m30 is not None else None,
            "vol_ratio":    float(sym_vol_ratio) if sym_vol_ratio is not None else None,
            "btc_m15m":     float(btc_regime) if btc_regime is not None else None,
            "scout_score": int(_scout_sub_score) if _scout_sub_score is not None else None,
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
