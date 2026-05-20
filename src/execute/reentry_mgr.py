# DISABLED via REENTRY_AFTER_TP_ENABLED=false in .env. See STRATEGY_MISTAKES.md.
from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.utils.parsing import detect_symbol_from_question

log = logging.getLogger(__name__)

reentry_candidates: dict[str, dict] = {}


def register_reentry_candidate(decision, pos_row: dict | None = None) -> None:
    pos = decision.position
    if not (pos.strategy_mode and pos.strategy_mode.startswith("updown_hourly")):
        return
    cid = pos.condition_id
    symbol = detect_symbol_from_question(pos.question)
    reentry_candidates[cid] = {
        "outcome":               pos.outcome,
        "exit_price":            float(pos.current_price),
        "exit_time_iso":         datetime.now(timezone.utc).isoformat(),
        "original_capital_usdc": float(pos.capital_at_risk),
        "token_id":              pos.token_id,
        "resolve_date_iso":      pos.resolve_date.isoformat(),
        "question":              pos.question,
        "symbol":                symbol or "UNKNOWN",
        "slot_key":              pos.resolve_date.replace(second=0, microsecond=0).isoformat(),
    }
    log.info(
        f"[REENTRY WATCH] {symbol} {pos.outcome} @ {float(pos.current_price):.3f} — "
        f"monitoring drop ≥30% with mispricing"
    )


def cleanup_reentry_candidates() -> None:
    now = datetime.now(timezone.utc)
    to_remove = []
    for cid, ctx in reentry_candidates.items():
        try:
            resolve = datetime.fromisoformat(ctx["resolve_date_iso"])
            if resolve <= now or (resolve - now).total_seconds() < 60:
                to_remove.append(cid)
        except Exception:
            to_remove.append(cid)
    for cid in to_remove:
        reentry_candidates.pop(cid, None)

async def scan_reentry_opportunities(
    clob, sizer, manager, breaker, capital: float,
    session,
    btc_scalp: dict | None = None,
    symbol_momentum_map: dict | None = None,
) -> None:
    import aiohttp
    from decimal import Decimal as _D
    from datetime import datetime
    from src.execute.reentry import (
        estimate_fair_value, check_reentry_signal,
        validate_reentry_orderbook, passes_time_gate,
    )
    from src.risk.pricing import ke_decimal as _ked
    from src.models.database import get_recent_closed_hourly
    from src.models.database import count_open_by_resolve_slot
    from src.risk.slots import slot_history_count, record_slot_entry, HOURLY_MAX_ENTRIES_PER_SLOT
    from src.risk.blacklist import check_symbol_blacklist
    from src.utils.parsing import detect_symbol_from_question
    from src.utils.config import config
    from src.utils.logger import log
    from src.models.types import SisiOrder
    from src.utils.pricing_cache import _open_position_lock

    if not reentry_candidates:
        return

    log.info(f"[REENTRY SCAN] {len(reentry_candidates)} kandidat dipantau")
    _recent_closed = get_recent_closed_hourly(limit=20)

    for cid in list(reentry_candidates.keys()):
        ctx    = reentry_candidates[cid]
        symbol = ctx["symbol"]
        outcome = ctx["outcome"]

        if check_symbol_blacklist(symbol):
            log.debug(f"[REENTRY] {symbol} blacklisted — skip reentry candidate")
            continue

        _sym_trades = [
            r for r in _recent_closed
            if detect_symbol_from_question(r.get("question", "")) == symbol.upper()
        ]
        if _sym_trades and _sym_trades[0].get("pnl", 0) < 0:
            log.debug(
                f"[REENTRY] {symbol} — last hourly trade was LOSS "
                f"(${_sym_trades[0]['pnl']:.2f}), skip reentry"
            )
            continue

        exit_price = ctx["exit_price"]
        token_id   = ctx["token_id"]
        try:
            resolve_dt = datetime.fromisoformat(ctx["resolve_date_iso"])
        except Exception:
            reentry_candidates.pop(cid, None)
            continue

        from datetime import timezone
        mins_to_resolve = (resolve_dt - datetime.now(timezone.utc)).total_seconds() / 60.0
        if mins_to_resolve <= 0:
            reentry_candidates.pop(cid, None)
            continue
        if not passes_time_gate(mins_to_resolve, min_minutes=15.0):
            log.debug(f"[REENTRY] {symbol} {cid[:8]} — {mins_to_resolve:.0f}m left < 15m gate, skip")
            continue

        slot_open = count_open_by_resolve_slot(resolve_dt)
        slot_hist = slot_history_count(resolve_dt)
        if config.MAX_POSITIONS_PER_SLOT > 0 and slot_open >= config.MAX_POSITIONS_PER_SLOT:
            log.debug(f"[REENTRY] {symbol} {cid[:8]} — slot OPEN cap reached")
            continue
        if slot_hist >= HOURLY_MAX_ENTRIES_PER_SLOT:
            log.debug(f"[REENTRY] {symbol} {cid[:8]} — slot CUMULATIVE cap reached")
            continue

        try:
            snap = clob.ambil_snapshot(token_id=token_id)
            if not snap or not snap.valid:
                continue
            current_market_price = float(snap.best_ask)
        except Exception as e:
            log.debug(f"[REENTRY] {symbol} {cid[:8]} — snapshot error: {e}")
            continue

        sym_mtf    = (symbol_momentum_map or {}).get(symbol)
        fair_value = estimate_fair_value(outcome, btc_scalp, sym_mtf)
        if fair_value is None:
            log.debug(f"[REENTRY] {symbol} {cid[:8]} — no fair value (missing data)")
            continue

        sig = check_reentry_signal(
            exit_price=exit_price,
            current_market_price=current_market_price,
            fair_value=fair_value,
            drop_threshold=0.30, min_edge=0.05,
        )
        if not sig["should_reenter"]:
            log.debug(f"[REENTRY] {symbol} {cid[:8]} {outcome} — {sig['reason']}")
            continue

        full_ob = clob.get_full_orderbook(token_id)
        bids = full_ob.get("bids", [])
        asks = full_ob.get("asks", [])
        if not asks and snap and snap.valid:
            asks = [(float(snap.best_ask), float(snap.ask_size))]
        capital_required = ctx["original_capital_usdc"] / 2.0
        ob = validate_reentry_orderbook(
            bids=bids, asks=asks,
            capital_required=capital_required, spread_max=0.05,
        )
        if not ob["ok"]:
            log.debug(f"[REENTRY] {symbol} {cid[:8]} — orderbook fail: {ob['reason']}")
            continue

        kelly_capital = capital_required
        kelly_shares  = (_D(str(kelly_capital)) / _D(str(current_market_price))).quantize(_D("0.0001"))

        log.info(
            f"[bold magenta][REENTRY][/bold magenta] {symbol} {outcome} "
            f"@ {current_market_price:.3f} | "
            f"TP exit {exit_price:.3f} → drop {sig['drop_pct']:.0%} | "
            f"fair {fair_value:.2f} (edge {sig['edge']:+.3f}) | "
            f"capital ${kelly_capital:.2f} (half original) | "
            f"{mins_to_resolve:.0f}m left"
        )

        async with _open_position_lock:
            can_open, reason = manager.can_open(
                condition_id=cid, outcome=outcome,
                bet_usdc=_D(str(kelly_capital)),
                total_capital=_ked(capital),
            )
            if not can_open:
                log.debug(f"[REENTRY] {cid[:8]} — manager skip: {reason}")
                continue
            if config.CB_ENABLED and not breaker.check(unrealized_pnl=manager.get_unrealized_pnl()).can_trade:
                continue

            entry_succeeded = False
            if config.DRY_RUN:
                manager.open_position(
                    condition_id    = cid,
                    question        = ctx["question"],
                    outcome         = outcome,
                    entry_price     = _ked(current_market_price),
                    shares          = kelly_shares,
                    capital_at_risk = _D(str(kelly_capital)),
                    resolve_date    = resolve_dt,
                    gap_pct         = sig["edge"],
                    kelly_fraction  = 0.5,
                    strategy_mode   = "updown_hourly_dry_run",
                    token_id        = token_id,
                )
                log.warning(f"[yellow][REENTRY DRY RUN] {symbol} {outcome} simulasi[/yellow]")
                entry_succeeded = True
            else:
                order = clob.pasang_order(
                    sisi=SisiOrder.BELI, harga=_ked(current_market_price),
                    ukuran=kelly_shares, token_id=token_id,
                )
                if order:
                    manager.open_position(
                        condition_id    = cid,
                        question        = ctx["question"],
                        outcome         = outcome,
                        entry_price     = _ked(current_market_price),
                        shares          = kelly_shares,
                        capital_at_risk = _D(str(kelly_capital)),
                        resolve_date    = resolve_dt,
                        gap_pct         = sig["edge"],
                        kelly_fraction  = 0.5,
                        strategy_mode   = "updown_hourly",
                        token_id        = token_id,
                    )
                    entry_succeeded = True
                else:
                    log.warning(f"[REENTRY] {symbol} {cid[:8]} order gagal — keep candidate, retry next cycle")

            if entry_succeeded:
                record_slot_entry(resolve_dt)
                reentry_candidates.pop(cid, None)
