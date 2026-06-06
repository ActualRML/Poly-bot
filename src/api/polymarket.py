import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

_UPDOWN_HINTS = ("up-or-down", "updown", "up or down")
# Sub-hourly resolution markets (5m/15m/30m) — we only want 1h Up/Down.
_SUBHOURLY_HINTS = ("-5m-", "-15m-", "-30m-", "-1m-")
# Authoritative 1h gate: endDate - eventStartTime span (minutes). 4h=240 and
# 30m=30 sit far outside this band, so the exact ±10 window is safe.
_HOURLY_SPAN_LO = 50
_HOURLY_SPAN_HI = 70
SYMBOL_HINTS = {
    "BTC":  ("bitcoin", "btc"),
    "ETH":  ("ethereum", "eth"),
    "SOL":  ("solana", "sol"),
    "XRP":  ("xrp", "ripple"),
    "DOGE": ("dogecoin", "doge"),
    "BNB":  ("bnb", "binance coin"),
}


def _is_updown(*texts: str) -> bool:
    blob = " ".join(t or "" for t in texts).lower()
    return any(h in blob for h in _UPDOWN_HINTS)


def _is_subhourly(*texts: str) -> bool:
    blob = " ".join(t or "" for t in texts).lower()
    return any(h in blob for h in _SUBHOURLY_HINTS)


def _detect_symbol(*texts: str) -> str | None:
    blob = " ".join(t or "" for t in texts).lower()
    for sym, hints in SYMBOL_HINTS.items():
        if any(h in blob for h in hints):
            return sym
    return None


def _parse_token_ids(raw: Any) -> list[str]:
    # Gamma returns clobTokenIds as a JSON-string most of the time, but
    # the CLOB endpoints sometimes hand back an actual list — accept both.
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x) for x in parsed if x]
        except json.JSONDecodeError:
            pass
    return []


def _normalize_outcome(label: str) -> str | None:
    s = (label or "").strip().lower()
    if s in ("yes", "up"):
        return "YES"
    if s in ("no", "down"):
        return "NO"
    return None


def _build_token_outcomes(
    token_ids: list[str], raw_outcomes: Any, log: logging.Logger, slug: str
) -> dict[str, str]:
    # outcomes is parallel to clobTokenIds; same JSON-string-or-list shape.
    outcomes = _parse_token_ids(raw_outcomes)
    norm = [_normalize_outcome(o) for o in outcomes]
    if len(token_ids) == 2 and len(norm) == 2 and None not in norm:
        return {tid: side for tid, side in zip(token_ids, norm)}
    # Fallback: convention is clobTokenIds[0]=YES, [1]=NO.
    log.warning("outcomes missing/unparseable for %s; assuming index 0=YES,1=NO", slug)
    return {tid: ("YES" if i == 0 else "NO") for i, tid in enumerate(token_ids)}


def _parse_end_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


class PolymarketREST:
    """
    Thin async wrappers around Gamma (discovery) and CLOB (trading) HTTP
    endpoints. Skeletal on purpose — methods get added when a strategy
    actually needs them, not preemptively.

    Use as an async context manager so the aiohttp session has a
    deterministic lifecycle:

        async with PolymarketREST(gamma, clob) as api:
            events = await api.get_events(active=True)
    """

    def __init__(self, gamma_url: str, clob_url: str):
        self.gamma_url = gamma_url.rstrip("/")
        self.clob_url = clob_url.rstrip("/")
        self.log = logging.getLogger("api.polymarket")
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "PolymarketREST":
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def get_events(self, **params) -> list[dict]:
        return await self._get(f"{self.gamma_url}/events", params=params)

    async def get_markets(self, **params) -> list[dict]:
        return await self._get(f"{self.gamma_url}/markets", params=params)

    async def _get(self, url: str, params: dict | None = None):
        assert self._session is not None, "use 'async with PolymarketREST(...)'"
        async with self._session.get(url, params=params) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def discover_updown_markets(
        self,
        *,
        max_hours: float = 3.0,
        per_page: int = 100,
        max_pages: int = 10,
    ) -> tuple[list[dict], str]:
        """
        Find active crypto hourly Up/Down markets and their CLOB token IDs.

        Returns (markets, "hourly"). Only markets resolving within
        `max_hours` are returned; if none are live the list is empty and
        the bot runs Binance-only (graceful).

        Each market dict: condition_id, question, symbol, token_ids, end_date.

        Gamma caps /events at ~100 rows per request, and ordered by endDate
        ascending the soonest slots are dominated by 5m/15m markets — so the
        hourly markets for thinner coins (XRP/DOGE/BNB) sit PAST the
        first page and a single fetch silently misses them. We page through
        with `offset`, processing each page as it arrives, and stop early
        once every tracked coin has an hourly market or the data runs out.
        """
        # end_date_min anchors the ascending sort at NOW; without it the
        # oldest, long-resolved markets sort first and every candidate gets
        # dropped by the time window.
        now = datetime.now(timezone.utc)
        url = f"{self.gamma_url}/events"
        base_params = {
            "closed": "false",
            "end_date_min": now.isoformat(),
            "order": "endDate",
            "ascending": "true",
            "limit": str(per_page),
        }

        hourly_cutoff = now + timedelta(hours=max_hours)

        hourly: list[dict] = []
        per_coin: dict[str, int] = {}   # symbol -> count, kept only for early-stop
        expected = set(SYMBOL_HINTS)   # the 6 coins we track

        n_slug_match = 0   # markets under an up/down event with a known symbol
        total_events = 0
        pages = 0

        for page in range(max_pages):
            params = {**base_params, "offset": str(page * per_page)}
            self.log.debug("discover_updown GET %s params=%s", url, params)
            events = await self._get(url, params=params)
            if not isinstance(events, list) or not events:
                break
            pages += 1
            total_events += len(events)
            if page == 0:
                sample = [e.get("slug", "") for e in events[:3] if isinstance(e, dict)]
                self.log.debug("discover_updown first 3 slugs: %s", sample)

            for event in events:
                if not isinstance(event, dict):
                    continue
                ev_slug = event.get("slug", "")
                ev_title = event.get("title", "")
                if not _is_updown(ev_slug, ev_title):
                    continue
                # Drop sub-hourly (5m/15m/30m) resolution markets — we want 1h.
                if _is_subhourly(ev_slug, ev_title):
                    self.log.debug("updown DROP %s (sub-hourly resolution)", ev_slug)
                    continue

                for m in event.get("markets") or []:
                    if not isinstance(m, dict):
                        continue
                    question = m.get("question", "")
                    symbol = _detect_symbol(question, ev_slug, ev_title)
                    if symbol is None:
                        continue
                    token_ids = _parse_token_ids(m.get("clobTokenIds"))
                    if not token_ids:
                        continue
                    n_slug_match += 1

                    end = _parse_end_ts(
                        m.get("endDate") or m.get("end_date_iso") or m.get("endDateIso")
                    )
                    delta_s = (end - now).total_seconds() if end else None
                    # Trace the first 5 matched markets: delta_s must be POSITIVE
                    # (resolving in the future) for a market to be kept.
                    if n_slug_match <= 5:
                        self.log.debug(
                            "updown candidate #%d %s end=%s delta_s=%s",
                            n_slug_match, ev_slug,
                            end.isoformat() if end else None, delta_s,
                        )
                    # accept currently-live markets: end must be in the (near) future
                    if end is None or end <= now:
                        self.log.debug(
                            "updown DROP %s end=%s delta_s=%s (closed/past)",
                            ev_slug, end.isoformat() if end else None, delta_s,
                        )
                        continue
                    # Authoritative duration gate: only true 1h candles pass.
                    start = _parse_end_ts(m.get("eventStartTime"))
                    if start is None:
                        self.log.debug(
                            "updown DROP %s (no eventStartTime; can't verify 1h)", ev_slug
                        )
                        continue
                    span_min = (end - start).total_seconds() / 60
                    if not (_HOURLY_SPAN_LO <= span_min <= _HOURLY_SPAN_HI):
                        self.log.debug(
                            "updown DROP %s span=%.1fmin (not ~hourly)", ev_slug, span_min
                        )
                        continue
                    if end > hourly_cutoff:
                        self.log.debug(
                            "updown DROP %s end=%s delta_s=%s (beyond %sh window)",
                            ev_slug, end.isoformat(), delta_s, max_hours,
                        )
                        continue

                    entry = {
                        "condition_id": m.get("conditionId") or m.get("condition_id"),
                        "question": question,
                        "symbol": symbol,
                        "token_ids": token_ids,
                        "token_outcomes": _build_token_outcomes(
                            token_ids, m.get("outcomes"), self.log, ev_slug
                        ),
                        "end_date": end.isoformat(),
                    }
                    self.log.debug(
                        "updown PASS %s sym=%s end=%s delta_s=%s",
                        ev_slug, symbol, end.isoformat(), delta_s,
                    )
                    hourly.append(entry)
                    per_coin[symbol] = per_coin.get(symbol, 0) + 1

            # Stop paging when the data runs out or every coin has an hourly market.
            if len(events) < per_page:
                break
            if expected <= per_coin.keys():
                self.log.debug("discover_updown: all coins found after page %d", page)
                break

        self.log.debug(
            "discover_updown: %d page(s), %d events, %d hourly markets",
            pages, total_events, len(hourly),
        )
        if not hourly:
            self.log.warning("no active hourly Up/Down markets")
        return hourly, "hourly"

    async def get_market_resolution(self, market_id: str) -> dict | None:
        """
        Query CLOB GET /markets/{condition_id} (0x-prefixed, no stripping).
        Returns {resolved: True, winning_outcome: "YES"/"NO"} once the market
        is closed and a winning token is flagged, else None.
        """
        try:
            m = await self._get(f"{self.clob_url}/markets/{market_id}")
        except Exception as e:
            self.log.warning("resolution query failed for %s: %s", market_id, e)
            return None

        if not isinstance(m, dict) or not m.get("closed"):
            return None

        winner = next(
            (t for t in m.get("tokens") or [] if isinstance(t, dict) and t.get("winner")),
            None,
        )
        if winner is None:  # closed but winner not flagged yet
            return None

        outcome = _normalize_outcome(winner.get("outcome", ""))
        if outcome is None:
            return None
        return {"resolved": True, "winning_outcome": outcome}
