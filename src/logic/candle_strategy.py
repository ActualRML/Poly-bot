from __future__ import annotations

import asyncio
import json as _json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from src.logic.slot_manager import slot_history_count, record_slot_entry
from src.logic.symbol_blacklist import check_symbol_blacklist

import aiohttp

from src.utils.config import config
from src.utils.logger import log

logger = logging.getLogger(__name__)

# Candle strategy: bitcoin-up-or-down-may-10-2026-12pm-et style
# Resolves based on whether the named 1h Binance candle is green/red.
CANDLE_UPDOWN_SLUG_PREFIXES: dict[str, str] = {
    "BTC":  "bitcoin-up-or-down-",
    "ETH":  "ethereum-up-or-down-",
    "SOL":  "solana-up-or-down-",
    "XRP":  "xrp-up-or-down-",
    "DOGE": "dogecoin-up-or-down-",
    "BNB":  "bnb-up-or-down-",
}

_DEFAULT_MAX_ENTRIES_PER_SLOT = 5


async def scan_candle_markets(
    session: aiohttp.ClientSession,
    gamma,
) -> list[dict]:
    """
    Fetch bitcoin-up-or-down-* style markets (1h candle green/red).
    Strike = Binance 1h candle OPEN at endDate - 1h.
    Only returns markets where the candle has already started.
    """
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
                "limit":        500,
                "order":        "endDate",
                "ascending":    "true",
                "end_date_min": end_min,
                "end_date_max": end_max,
            },
        )
    except Exception as e:
        logger.debug(f"[CANDLE UPDOWN] Gagal fetch events: {e}")
        return results

    if not isinstance(batch, list):
        return results

    for event in batch:
        slug = (event.get("slug") or "").lower()

        symbol = None
        for sym, prefix in CANDLE_UPDOWN_SLUG_PREFIXES.items():
            if slug.startswith(prefix):
                symbol = sym
                break
        if not symbol:
            continue

        end_date_str = event.get("endDate") or ""
        try:
            end_date     = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            candle_start = end_date - timedelta(hours=1)
        except Exception:
            continue

        if candle_start > now:
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
        mkt["_start_date"] = candle_start.isoformat()
        mkt["endDate"]     = end_date_str
        results.append(mkt)

    return results


async def analyze_candle_market(
    market: dict,
    clob,
    gamma,
    sizer,
    manager,
    breaker,
    capital: float,
    session: aiohttp.ClientSession,
    *,
    open_position_lock: asyncio.Lock,
    closed_this_cycle: set | None = None,
    candle_sl_markets: dict | None = None,
    vol_data: dict | None = None,
) -> None:
    """
    Momentum-based candle strategy entry. Entry in first 5–15m of candle
    using 15m BTC momentum. Asymmetric exit: SL at 50%, profit lock T1/T2.
    Reverse re-entry after SL if momentum flips.
    """
    from src.logic.pricing import ke_decimal
    from src.api.binance_client import fetch_klines
    from src.models.database import log_prediction, get_recent_closed_pnls
    from src.models.types import SisiOrder
    from src.logic.risk_manager import calculate_position_size
    from src.logic.reentry import check_candle_reverse_reentry
    from src.utils.telegram_alert import get_alert

    symbol = market.get("_symbol", "")
    if not symbol:
        return

    condition_id = market.get("conditionId", market.get("id", ""))
    if closed_this_cycle and condition_id in closed_this_cycle:
        return

    if check_symbol_blacklist(symbol):
        logger.debug(f"[CANDLE UPDOWN] {symbol} blacklisted, skip")
        return

    end_date_str = market.get("endDate", "")
    try:
        end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
    except Exception:
        return

    max_entries = getattr(config, "UPDOWN_HOURLY_MAX_ENTRIES_PER_SLOT", _DEFAULT_MAX_ENTRIES_PER_SLOT)
    if slot_history_count(end_date) >= max_entries:
        logger.debug(f"[CANDLE UPDOWN] {symbol} slot penuh ({max_entries}), skip")
        return

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
    if not (0.0 < market_price_up < 1.0):
        return

    start_date_str = market.get("_start_date", "")
    try:
        start_date = datetime.fromisoformat(start_date_str.replace("Z", "+00:00"))
    except Exception:
        return

    now           = datetime.now(timezone.utc)
    t_remaining_s = (end_date - now).total_seconds()
    if t_remaining_s <= 0:
        return

    t_min = t_remaining_s / 60.0
    if t_min < getattr(config, "HOURLY_MIN_MINUTES_TO_RESOLVE", 5):
        return

    candle_running_min = (now - start_date).total_seconds() / 60.0

    sl_outcome         = (candle_sl_markets or {}).get(condition_id)
    is_reverse_reentry = sl_outcome is not None

    if not is_reverse_reentry:
        candle_open_min = getattr(config, "UPDOWN_HOURLY_CANDLE_OPEN_MIN", 5)
        if candle_running_min < candle_open_min:
            logger.debug(
                f"[CANDLE UPDOWN] {symbol} candle baru {candle_running_min:.0f}m jalan, "
                f"tunggu {candle_open_min}m"
            )
            return
        if candle_running_min > 15:
            logger.debug(
                f"[CANDLE UPDOWN] {symbol} candle sudah {candle_running_min:.0f}m, "
                f"lewat window entry"
            )
            return

    klines_1m = await fetch_klines(symbol, session, interval="1m", limit=16)
    if len(klines_1m) < 15:
        logger.debug(f"[CANDLE UPDOWN] {symbol} 1m klines tidak cukup ({len(klines_1m)})")
        return

    closes_1m = [float(k[4]) for k in klines_1m[-15:]]
    if closes_1m[0] <= 0:
        return
    momentum_15m = (closes_1m[-1] - closes_1m[0]) / closes_1m[0]

    _base_threshold = getattr(config, "CANDLE_UPDOWN_MOM_THRESHOLD", 0.0015)
    _vol_annual = (vol_data or {}).get(symbol.upper()) or (vol_data or {}).get("DEFAULT") or 0.40
    _vol_floor = getattr(config, "UPDOWN_VOL_FLOOR", 0.0)
    if _vol_floor > 0:
        _vol_annual = max(_vol_annual, _vol_floor)
    _mom_vol_factor = getattr(config, "CANDLE_UPDOWN_MOM_VOL_FACTOR", 0.003)
    mom_threshold = max(_base_threshold, _vol_annual * _mom_vol_factor)
    if mom_threshold > _base_threshold:
        logger.debug(
            f"[CANDLE UPDOWN] {symbol} vol-adj mom_threshold: "
            f"{_base_threshold:.4f} → {mom_threshold:.4f} (vol={_vol_annual:.0%})"
        )
    max_buy_price = getattr(config, "CANDLE_UPDOWN_MAX_BUY_PRICE", 0.60)

    if is_reverse_reentry:
        market_price_down = round(1.0 - market_price_up, 4)
        opp_price = market_price_down if sl_outcome == "Up" else market_price_up
        rev = check_candle_reverse_reentry(
            sl_outcome            = sl_outcome,
            momentum_15m          = momentum_15m,
            minutes_to_resolve    = t_min,
            market_price_opposite = opp_price,
            momentum_threshold    = mom_threshold,
            min_minutes           = getattr(config, "CANDLE_UPDOWN_REVERSE_MIN_MINUTES", 20.0),
            max_buy_price         = max_buy_price,
        )
        if not rev["should_reenter"]:
            logger.debug(
                f"[CANDLE UPDOWN REVERSE] {symbol} skip — {rev['reason']} "
                f"mom={momentum_15m:+.5f}"
            )
            return
        buy_outcome  = rev["outcome"]
        buy_price    = opp_price
        is_half_size = True
        entry_reason = f"REVERSE_{rev['reason']}"
    else:
        if momentum_15m > mom_threshold:
            buy_outcome  = "Up"
            buy_price    = market_price_up
        elif momentum_15m < -mom_threshold:
            buy_outcome  = "Down"
            buy_price    = round(1.0 - market_price_up, 4)
        else:
            logger.debug(
                f"[CANDLE UPDOWN] {symbol} momentum flat {momentum_15m:+.5f} "
                f"(threshold +/-{mom_threshold:.4f}), skip"
            )
            return
        is_half_size = False
        entry_reason = f"MOM_{momentum_15m:+.5f}"

    if buy_price > max_buy_price:
        logger.debug(
            f"[CANDLE UPDOWN] {symbol} {buy_outcome} price {buy_price:.3f} > "
            f"max {max_buy_price:.2f}, skip"
        )
        return

    if config.CB_ENABLED and not breaker.check(unrealized_pnl=manager.get_unrealized_pnl()).can_trade:
        return

    buy_winrate = min(max(0.50 + abs(momentum_15m) * 30.0, 0.50), 0.75)
    kelly = sizer.calculate(
        winrate      = buy_winrate,
        market_price = buy_price,
        capital      = capital,
    )
    if not kelly.is_positive_ev or float(kelly.bet_usdc) <= 0:
        return

    max_size = calculate_position_size(get_recent_closed_pnls(limit=5))
    effective_max = max_size * 0.5 if is_half_size else max_size
    if float(kelly.bet_usdc) > effective_max:
        from dataclasses import replace as _dc_replace
        capped_usdc   = Decimal(str(effective_max))
        capped_shares = (capped_usdc / Decimal(str(buy_price))).quantize(Decimal("0.0001"))
        kelly = _dc_replace(kelly, bet_usdc=capped_usdc, shares=capped_shares)

    question = market.get("question", market.get("title", f"{symbol} Up or Down"))

    logger.debug(
        f"[CANDLE UPDOWN] {symbol} {t_min:.0f}m left "
        f"{'REVERSE ' if is_reverse_reentry else ''}BUY {buy_outcome} @ {buy_price:.3f} "
        f"mom={momentum_15m:+.5f} kelly=${float(kelly.bet_usdc):.2f}"
    )

    tokens    = gamma.extract_token_ids(market)
    _bo_lower = buy_outcome.lower()
    token     = next((t for t in tokens if str(t.get("outcome", "")).lower() == _bo_lower), None)
    token_id  = str(token["token_id"]) if token and token.get("token_id") else ""

    async with open_position_lock:
        can_open, reason = manager.can_open(
            condition_id  = condition_id,
            outcome       = buy_outcome,
            bet_usdc      = kelly.bet_usdc,
            total_capital = ke_decimal(capital),
        )
        if not can_open:
            logger.debug(f"[CANDLE UPDOWN] Skip {condition_id[:8]} {buy_outcome}: {reason}")
            return

        try:
            resolve_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        except Exception:
            resolve_date = datetime.now(timezone.utc)

        _record_gap  = float(round(momentum_15m, 5))
        _record_prob = str(round(buy_winrate, 4))

        if config.DRY_RUN:
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
                strategy_mode   = "updown_candle_dry_run",
                token_id        = "",
            )
            log_prediction({
                "condition_id":   condition_id,
                "question":       question,
                "outcome":        buy_outcome,
                "predicted_prob": _record_prob,
                "market_price":   str(buy_price),
                "gap_pct":        str(round(_record_gap * 100, 4)),
                "resolve_date":   resolve_date.isoformat(),
            })
            record_slot_entry(end_date)
            if is_reverse_reentry and candle_sl_markets is not None:
                candle_sl_markets.pop(condition_id, None)
        else:
            if not token_id:
                logger.warning(f"[CANDLE UPDOWN] token_id tidak ditemukan untuk {buy_outcome}")
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
                    strategy_mode   = "updown_candle",
                    token_id        = token_id,
                )
                log_prediction({
                    "condition_id":   condition_id,
                    "question":       question,
                    "outcome":        buy_outcome,
                    "predicted_prob": _record_prob,
                    "market_price":   str(buy_price),
                    "gap_pct":        str(round(_record_gap * 100, 4)),
                    "resolve_date":   resolve_date.isoformat(),
                })
                record_slot_entry(end_date)
                if is_reverse_reentry and candle_sl_markets is not None:
                    candle_sl_markets.pop(condition_id, None)
            else:
                logger.debug(f"[CANDLE UPDOWN] {symbol} order gagal")
