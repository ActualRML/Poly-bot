import logging
from datetime import datetime, timezone

from src.data.snapshot import MarketSnapshot

log = logging.getLogger("parse")

# Binance combined-stream symbols are all quoted in USDT (e.g. "BTCUSDT").
_QUOTE = "USDT"


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _get(level, key):
    # Book levels are usually dicts ({"price":..,"size":..}); tolerate the
    # occasional [price, size] pair some feeds use.
    if isinstance(level, dict):
        return level.get(key)
    if isinstance(level, (list, tuple)) and level:
        if key == "price":
            return level[0]
        if key == "size" and len(level) > 1:
            return level[1]
    return None


def _book_side(levels, *, is_bid: bool) -> tuple[float | None, float | None, float | None]:
    """(best_price, size_at_best, total_size) for one book side.

    Best = HIGHEST bid / LOWEST ask (Polymarket sends levels unordered). Size at
    best sums any levels sharing that price; total_size sums all levels. Size is
    optional per level — missing sizes are skipped (None at touch / for totals if
    none present), so a price-only book still yields best_price."""
    parsed: list[tuple[float, float | None]] = []
    for lvl in levels or []:
        price = _to_float(_get(lvl, "price"))
        if price is not None:
            parsed.append((price, _to_float(_get(lvl, "size"))))
    if not parsed:
        return None, None, None
    best_price = max(p for p, _ in parsed) if is_bid else min(p for p, _ in parsed)
    size_at_best = sum(s for p, s in parsed if p == best_price and s is not None)
    total = sum(s for _, s in parsed if s is not None)
    return best_price, (size_at_best or None), (total or None)


# price_change payloads vary by feed version: a nested array of update dicts
# under "price_changes" (current) or "changes", or an older flat form with
# top-level "price"/"asset_id". Normalize all three to a list of entries.
def _price_change_entries(raw: dict) -> list[dict]:
    for key in ("price_changes", "changes"):
        arr = raw.get(key)
        if isinstance(arr, list) and arr:
            return [c for c in arr if isinstance(c, dict)]
    if raw.get("price") is not None:
        return [{"asset_id": raw.get("asset_id"), "price": raw.get("price")}]
    return []


# Dump the first few real price_change frames so the actual field layout is
# visible in ws.log for tuning; capped so it never floods.
_pc_debug_count = 0
_PC_DEBUG_LIMIT = 3


def parse_polymarket(raw: dict, symbol_lookup: dict[str, str]) -> MarketSnapshot | None:
    """
    Normalize a single Polymarket market-channel event into a MarketSnapshot.

    WS events never carry the human symbol, so `symbol_lookup` (built at
    discovery, keyed by both asset_id and market_id) supplies it. An
    unmappable event still yields a snapshot with symbol=None rather than
    being dropped — better to log it downstream than to silently lose data.

    Returns None only on a structurally bad message (never raises).
    """
    try:
        event_type = raw.get("event_type") or raw.get("type") or "unknown"
        market_id = raw.get("market") or None
        asset_id = raw.get("asset_id") or None

        best_bid = best_ask = None
        bid_size = ask_size = bid_depth = ask_depth = None
        price: float | None = None

        if event_type == "book":
            best_bid, bid_size, bid_depth = _book_side(raw.get("bids"), is_bid=True)
            best_ask, ask_size, ask_depth = _book_side(raw.get("asks"), is_bid=False)
            if best_bid is not None:
                price = best_bid
            elif best_ask is not None:
                price = best_ask
        elif event_type == "price_change":
            global _pc_debug_count
            if _pc_debug_count < _PC_DEBUG_LIMIT:
                _pc_debug_count += 1
                log.debug(
                    "price_change raw layout",
                    extra={"keys": list(raw.keys()), "raw": str(raw)[:500]},
                )
            entries = _price_change_entries(raw)
            if entries:
                # The price lives in the nested update, not top-level. If the
                # frame bundles several (e.g. Up + Down token), use the first;
                # asset_id from the entry feeds symbol lookup when absent.
                chosen = entries[0]
                price = _to_float(chosen.get("price"))
                asset_id = asset_id or (chosen.get("asset_id") or None)
        elif event_type == "last_trade_price":
            price = _to_float(raw.get("price"))
        # other event types (e.g. tick_size_change) carry no price: pass
        # through with price=None so the snapshot still flows.

        symbol = symbol_lookup.get(asset_id or "") or symbol_lookup.get(market_id or "")

        return MarketSnapshot(
            ts=datetime.now(timezone.utc),
            source="polymarket",
            event_type=event_type,
            symbol=symbol,
            market_id=market_id,
            asset_id=asset_id,
            price=price,
            best_bid=best_bid,
            best_ask=best_ask,
            bid_size=bid_size,
            ask_size=ask_size,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
            raw=raw,
        )
    except Exception as e:  # malformed message must not crash the bot
        log.debug("polymarket parse failed", extra={"error": str(e), "raw": str(raw)[:200]})
        return None


def parse_binance(raw: dict) -> MarketSnapshot | None:
    """
    Normalize a Binance miniTicker into a MarketSnapshot.

    Accepts either the full frame ({"stream":..,"data":{..}}) or just the
    inner `data` dict — the WS client already unwraps to `data`, but being
    tolerant keeps the parser reusable and testable in isolation.

    Returns None on a structurally bad message (never raises).
    """
    try:
        data = raw.get("data") if "data" in raw else raw
        sym_raw = (data.get("s") or "").upper()
        if not sym_raw:
            return None
        symbol = sym_raw[: -len(_QUOTE)] if sym_raw.endswith(_QUOTE) else sym_raw

        return MarketSnapshot(
            ts=datetime.now(timezone.utc),
            source="binance",
            event_type="ticker",
            symbol=symbol or None,
            market_id=None,
            asset_id=None,
            price=_to_float(data.get("c")),
            best_bid=None,
            best_ask=None,
            raw=data,
        )
    except Exception as e:
        log.debug("binance parse failed", extra={"error": str(e), "raw": str(raw)[:200]})
        return None
