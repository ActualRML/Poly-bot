from __future__ import annotations

import json as _json
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

logger = logging.getLogger(__name__)

_UPDOWN_SERIES = {
    "BTC": "41",
    "ETH": "40",
    "SOL": "10086",
    "XRP": "10100",
}


async def scan_updown_markets(session: aiohttp.ClientSession, gamma) -> list[dict]:
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


async def analyze_updown_market(
    market: dict, clob, gamma, sizer, manager,
    breaker, capital: float, session: aiohttp.ClientSession,
    vol_data: dict | None = None,
    closed_this_cycle: set | None = None,
    profit_locked_markets: set | None = None,
    market_regime: dict | None = None,
):
    from src.execute.updown import calculate_updown_probability
    from src.risk.pricing import ke_decimal

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
    question = market.get("question", market.get("title", f"{symbol} Up or Down Daily"))

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
        regime_dir = market_regime.get("direction")
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

    max_size = calculate_position_size(get_recent_closed_pnls(limit=5), capital=float(capital))
    if float(kelly.bet_usdc) > max_size:
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
