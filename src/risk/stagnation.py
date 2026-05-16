from __future__ import annotations

_market_price_history: dict[str, list[tuple[float, float]]] = {}


def track_market_price(cid: str, price: float) -> None:
    import time as _t
    now = _t.monotonic()
    hist = _market_price_history.setdefault(cid, [])
    hist.append((price, now))
    cutoff = now - 600
    _market_price_history[cid] = [(p, t) for p, t in hist if t > cutoff]


def is_price_stagnant(cid: str, lookback_s: float = 300, threshold_pct: float = 0.010) -> bool:
    import time as _t
    hist = _market_price_history.get(cid, [])
    if len(hist) < 3:
        return False
    cutoff = _t.monotonic() - lookback_s
    recent = [p for p, t in hist if t > cutoff]
    if len(recent) < 3:
        return False
    p_min, p_max = min(recent), max(recent)
    if p_min <= 0:
        return False
    return (p_max - p_min) / p_min < threshold_pct


def get_price_velocity(cid: str, lookback_s: float = 300.0) -> float | None:
    """% change in market_price_up over last lookback_s seconds. Positive = Up rising."""
    import time as _t
    hist = _market_price_history.get(cid, [])
    if len(hist) < 3:
        return None
    cutoff = _t.monotonic() - lookback_s
    window = [(p, t) for p, t in hist if t >= cutoff]
    if len(window) < 3:
        return None
    oldest_p = window[0][0]
    newest_p = window[-1][0]
    if oldest_p <= 0:
        return None
    return (newest_p - oldest_p) / oldest_p
