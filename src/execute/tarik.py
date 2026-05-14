from __future__ import annotations

import logging
from pathlib import Path

import aiohttp

from src.models.types import SisiOrder
from src.utils.config import config
from src.utils.logger import log

logger = logging.getLogger(__name__)

TARIK_FLAG = Path(__file__).resolve().parent.parent.parent / "data" / "tarik.flag"


async def execute_tarik(
    manager,
    clob,
    session: aiohttp.ClientSession,
    condition_ids: list[str] | None = None,
) -> str:
    from src.models.database import get_open_positions
    from src.risk.pricing import ke_decimal

    all_positions = get_open_positions()
    if not all_positions:
        return "📭 Tidak ada posisi open untuk ditarik."

    if condition_ids:
        cid_set = set(condition_ids)
        targets = [p for p in all_positions if p["condition_id"] in cid_set]
    else:
        targets = all_positions

    if not targets:
        return "📭 Posisi yang dipilih tidak ditemukan."

    skipped = len(all_positions) - len(targets)

    results = []
    for pos in targets:
        cid      = pos["condition_id"]
        outcome  = pos["outcome"]
        current  = float(pos["current_price"])
        entry    = float(pos["entry_price"])
        shares   = float(pos["shares"])
        pnl      = (current - entry) * shares
        question = pos.get("question", "")[:40]

        if not config.DRY_RUN and pos.get("token_id"):
            try:
                clob.pasang_order(
                    sisi     = SisiOrder.JUAL,
                    harga    = ke_decimal(str(current)),
                    ukuran   = ke_decimal(str(shares)),
                    token_id = str(pos["token_id"]),
                )
            except Exception as _e:
                logger.warning(f"[TARIK] Sell order error {cid[:8]}: {_e}")

        manager._process_exit_manual(
            condition_id = cid,
            outcome      = outcome,
            exit_price   = ke_decimal(str(current)),
            pnl          = ke_decimal(str(round(pnl, 4))),
            reason       = "MANUAL_TARIK",
        )
        results.append(
            f"  {'📈' if pnl >= 0 else '📉'} {outcome} @ {current:.3f} | PnL <b>${pnl:+.2f}</b>\n"
            f"     <i>{question}</i>"
        )
        logger.info(f"[TARIK] Closed {cid[:8]} {outcome} @ {current:.3f} PnL=${pnl:+.2f}")

    total_pnl = sum(
        (float(p["current_price"]) - float(p["entry_price"])) * float(p["shares"])
        for p in targets
    )
    total_emoji = "📈" if total_pnl >= 0 else "📉"
    mode   = " [DRY RUN]" if config.DRY_RUN else ""
    header = f"🏁 <b>TARIK {len(targets)} posisi</b>{mode}\n\n"
    footer = f"\n{total_emoji} Total PnL: <b>${total_pnl:+.2f}</b>"
    if skipped:
        footer += f"\n⏭ {skipped} posisi lain dibiarkan jalan"
    return header + "\n".join(results) + footer
