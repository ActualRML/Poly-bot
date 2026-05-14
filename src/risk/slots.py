from __future__ import annotations

from datetime import datetime, timedelta, timezone

_hourly_slot_history: dict[str, int] = {}
HOURLY_MAX_ENTRIES_PER_SLOT = 8


def slot_key(end_date: datetime) -> str:
    return end_date.replace(second=0, microsecond=0).isoformat()


def record_slot_entry(end_date: datetime) -> None:
    k = slot_key(end_date)
    _hourly_slot_history[k] = _hourly_slot_history.get(k, 0) + 1


def slot_history_count(end_date: datetime) -> int:
    return _hourly_slot_history.get(slot_key(end_date), 0)


def cleanup_old_slots() -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(second=0, microsecond=0).isoformat()
    for k in list(_hourly_slot_history.keys()):
        if k < cutoff:
            del _hourly_slot_history[k]
