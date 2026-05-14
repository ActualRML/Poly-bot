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
from src.scout.mispricing import MispricingDirection
from src.execute.strategy import get_dynamic_threshold
from src.risk.manager import calculate_position_size
from src.scout.base_rates import get_base_rates

logger = logging.getLogger(__name__)


async def analyze_market(
    market, clob, gamma, detector, sizer, manager,
    builder, breaker, capital, session: aiohttp.ClientSession,
    vol_data: dict | None = None,
    closed_this_cycle: set | None = None,
    profit_locked_markets: set | None = None,
):
    from src.risk.pricing import ke_decimal

    condition_id = market.get("conditionId", market.get("id", ""))
    question     = market.get("question", market.get("title", ""))

    if closed_this_cycle and condition_id in closed_this_cycle:
        logger.debug(f"Skip {condition_id[:8]} — closed this cycle, no re-entry")
        return
    if profit_locked_markets and condition_id in profit_locked_markets:
        logger.debug(f"Skip {condition_id[:8]} — profit locked this session, no re-entry")
        return
    prices    = gamma.get_token_prices(market)
    yes_price = prices.get("Yes")

    if not yes_price or yes_price <= 0 or yes_price >= 1:
        return

    yes_base_rates = await get_base_rates(market, builder, session, vol_data=vol_data)
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

        max_size = calculate_position_size(get_recent_closed_pnls(limit=5), capital=float(capital))
        if float(kelly.bet_usdc) > max_size:
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
                await dry_run_open(result, kelly, market, manager, buy_outcome, buy_price, token_id)
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


async def dry_run_open(result, kelly, market, manager, buy_outcome: str, buy_price: float, token_id: str = ""):
    from src.risk.pricing import ke_decimal
    from datetime import datetime, timezone

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
