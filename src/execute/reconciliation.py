from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal

import aiohttp

from src.utils.logger import log
from src.utils.parsing import detect_symbol_from_question
from src.models.token_backfill import get_resolved_price_from_gamma
from src.risk.blacklist import maybe_blacklist_symbol

logger = logging.getLogger(__name__)

FORCE_CLOSE_GRACE_HOURS = 24


async def reconcile_positions(clob, gamma, manager, breaker, session: aiohttp.ClientSession) -> None:
    from src.models.database import get_open_positions
    from src.risk.pricing import ke_decimal
    from src.utils.telegram_alert import get_alert

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
            price = await get_resolved_price_from_gamma(gamma, session, cid, outcome)

            if price is not None and not (price >= 0.98 or price <= 0.02):
                price = None

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

            alert = get_alert()
            if alert:
                await alert.alert_exit(
                    question    = pos["question"],
                    outcome     = outcome,
                    entry_price = float(ke_decimal(pos["entry_price"])),
                    exit_price  = price,
                    pnl_usdc    = float(pnl),
                    reason      = "reconcile_startup",
                    session     = session,
                )

        except asyncio.TimeoutError:
            log.warning(f"[RECONCILE] Timeout cek {cid[:8]} {outcome} — skip, akan dicek ulang di loop")
        except Exception as e:
            log.warning(f"[RECONCILE] Gagal cek {cid[:8]} {outcome}: {e} — skip")

    if closed_count:
        log.info(f"[RECONCILE] Selesai — {closed_count}/{len(positions)} posisi di-close (resolved saat bot mati)")
    else:
        log.info(f"[RECONCILE] Selesai — semua {len(positions)} posisi masih aktif")


async def resolve_checker(
    clob, gamma, manager, breaker, session: aiohttp.ClientSession,
    current_prices: dict | None = None,
) -> set[str]:
    from src.models.database import get_open_positions
    from src.risk.pricing import ke_decimal
    from src.utils.telegram_alert import get_alert

    now       = datetime.now(timezone.utc)
    positions = get_open_positions()
    expired   = []
    closed: set[str] = set()

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
        return closed

    log.info(f"[RESOLVE CHECK] {len(expired)} posisi sudah melewati resolve_date")

    for pos, resolve in expired:
        cid     = pos["condition_id"]
        outcome = pos["outcome"]
        tid     = pos.get("token_id") or ""

        price = await get_resolved_price_from_gamma(gamma, session, cid, outcome)

        if price is None and tid:
            try:
                snapshot = clob.ambil_snapshot(token_id=tid)
                if snapshot and snapshot.valid:
                    price = float(snapshot.best_bid)
            except Exception:
                pass

        hours_past = (now - resolve).total_seconds() / 3600

        if price is None and current_prices:
            cached = (current_prices.get(cid) or {}).get(outcome)
            if cached is not None:
                price = float(cached)

        if price is None:
            if hours_past > 2:
                entry_price = float(ke_decimal(pos["entry_price"]))
                price = entry_price
                log.warning(
                    f"[RESOLVE CHECK] {pos['question'][:45]} | {outcome} — "
                    f"tidak bisa fetch harga setelah {hours_past:.1f}h, force close @ entry"
                )
            else:
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
            _log_fn = log.debug if hours_past < 1.0 else log.warning
            _log_fn(
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
        if (
            pos.get("strategy_mode") in ("updown_hourly", "updown_hourly_dry_run")
            and float(pnl) < 0
        ):
            _sym = detect_symbol_from_question(pos.get("question", ""))
            if _sym != "UNKNOWN":
                maybe_blacklist_symbol(_sym)
        closed.add(cid)

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

    return closed


async def fetch_current_prices(clob, manager) -> dict:
    from src.models.database import get_open_positions
    from src.risk.pricing import ke_decimal

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
