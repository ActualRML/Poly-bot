import asyncio
from datetime import datetime, timedelta, timezone

from src.execute.labels import format_market
from src.monitor.logger import get_logger

STUCK_WARN_AGE = timedelta(hours=2)  # past-close age that flags a position as abnormally stuck (tunable)
VOID_AGE = timedelta(hours=6)        # past-close age at which a never-resolving market is force-voided (DRY-RUN)


async def resolve_loop(portfolio, polymarket_rest, market_meta=None, interval_sec: int = 300, dry_run: bool = False) -> None:
    """Periodically settle open simulated positions against Polymarket outcomes."""
    log = get_logger("resolver")
    warned_stuck: set[int] = set()  # ids already warned this run; resets on restart
    while True:
        try:
            open_positions = await portfolio.list_open()
        except Exception as e:
            log.exception("list_open failed", extra={"error": str(e)})
            open_positions = []  # nothing to check; fall through to the sleep below

        for pos in open_positions:
            try:
                now = datetime.now(timezone.utc)
                rt_raw = pos.get("resolve_time")
                rt = datetime.fromisoformat(rt_raw) if rt_raw else None
                if rt is not None and rt.tzinfo is None:
                    rt = rt.replace(tzinfo=timezone.utc)
                # Not due yet (resolve_time in the future). NULL rt is fail-safe: still check.
                if rt is not None and rt > now:
                    log.debug("resolver skip (not due): %s", format_market(pos["market_id"], market_meta))
                    continue

                res = await polymarket_rest.get_market_resolution(pos["market_id"])
                if not res or not res.get("resolved"):
                    # Market not resolved yet. Escalating response for an overdue position:
                    #   >6h  -> force-void (DRY-RUN only): market will likely never finalize,
                    #           so cancel the bet, refund the stake, stop re-checking it.
                    #   2-6h -> alarm once (existing); no mutation.
                    age = (now - rt) if rt is not None else None
                    if dry_run and age is not None and age > VOID_AGE:
                        await portfolio.void_position(pos["id"])
                        log.warning(
                            "position id=%s %s force-voided after %.1fh stuck (market never "
                            "resolved), stake $%.2f refunded, pnl=0",
                            pos["id"], pos.get("symbol"),
                            age.total_seconds() / 3600, float(pos["size_usdc"]),
                        )
                        continue
                    # Still open: alarm once if abnormally overdue. Alarm only - no auto-resolve.
                    if (rt is not None and now - rt > STUCK_WARN_AGE
                            and pos["id"] not in warned_stuck):
                        warned_stuck.add(pos["id"])
                        log.warning(
                            "position id=%s %s stuck: market closed %.1fh ago, still "
                            "unresolved - check Polymarket market_id=%s",
                            pos["id"], pos.get("symbol"),
                            (now - rt).total_seconds() / 3600, pos["market_id"],
                        )
                    continue
                won = pos["side"] == res["winning_outcome"]
                exit_price = 1.0 if won else 0.0
                await portfolio.resolve_position(pos["id"], won, exit_price)
            except Exception as e:
                log.exception(
                    "position resolve failed",
                    extra={"position_id": pos.get("id"), "error": str(e)},
                )

        await asyncio.sleep(interval_sec)
