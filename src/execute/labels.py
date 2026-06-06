from datetime import datetime, timezone


def format_market(market_id, meta: dict | None) -> str:
    """'BTC 05:00Z (resolves in 12m)' from market_meta, or the raw id if unknown."""
    if not meta or market_id not in meta:
        return market_id or "?"
    m = meta[market_id]
    label = m.get("label") or market_id
    rt = m.get("resolve_time")
    if rt is None:
        return label
    # rt is tz-aware UTC; compare against tz-aware now
    delta = rt - datetime.now(timezone.utc)
    mins = int(delta.total_seconds() // 60)
    if mins < 0:
        return f"{label} (resolved)"
    return f"{label} (resolves in {mins}m)"
