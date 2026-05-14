from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.utils.parsing import detect_symbol_from_question

log = logging.getLogger(__name__)

reentry_candidates: dict[str, dict] = {}


def register_reentry_candidate(decision, pos_row: dict | None = None) -> None:
    pos = decision.position
    if pos.strategy_mode not in ("updown_hourly", "updown_hourly_dry_run"):
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
