from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.utils.parsing import detect_symbol_from_question

_symbol_blacklist_until: dict[str, datetime] = {}
_SYMBOL_BLACKLIST_HOURS = 4
_SYMBOL_LOSS_STREAK_THRESHOLD = 3


def check_symbol_blacklist(symbol: str) -> bool:
    until = _symbol_blacklist_until.get(symbol.upper())
    if until and datetime.now(timezone.utc) < until:
        return True
    return False


def maybe_blacklist_symbol(symbol: str) -> None:
    try:
        from src.models.database import get_recent_closed_hourly
        import logging
        log = logging.getLogger(__name__)
        sym_upper = symbol.upper()
        rows = get_recent_closed_hourly(limit=20)
        sym_rows = [
            r for r in rows
            if detect_symbol_from_question(r.get("question", "")) == sym_upper
        ]
        if len(sym_rows) < _SYMBOL_LOSS_STREAK_THRESHOLD:
            return
        recent = sym_rows[:_SYMBOL_LOSS_STREAK_THRESHOLD]
        if all(r["pnl"] < 0 for r in recent):
            _symbol_blacklist_until[sym_upper] = (
                datetime.now(timezone.utc) + timedelta(hours=_SYMBOL_BLACKLIST_HOURS)
            )
            log.warning(
                f"[BLACKLIST] {symbol} di-blacklist {_SYMBOL_BLACKLIST_HOURS}h "
                f"setelah {_SYMBOL_LOSS_STREAK_THRESHOLD} loss berturut-turut"
            )
    except Exception as _e:
        import logging
        logging.getLogger(__name__).debug(f"[BLACKLIST] Check error for {symbol}: {_e}")
