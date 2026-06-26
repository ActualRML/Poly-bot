"""Offline resolution recovery for the snapshot-replay backtest.

The ``snapshots`` table stores neither a market's resolve time nor who won; both
are recovered here from the stored stream itself, with ZERO network calls.

``resolve_ts`` — the hour boundary the market settled on. After settlement the WS
keeps streaming the now-dead token (price pinned ~0/1) until the re-discovery
prune drops it ~14 min later, so the LAST snapshot sits a few-to-~19 min past the
real ``:00``. Rounding ``last_ts`` to the NEAREST hour recovers the true
resolve_ts: it floors the usual lingering case (``:14`` -> ``:00``) and ceils a
market that stopped ticking just before settlement (``:57`` -> next ``:00``).
Matches the stored ``resolve_time`` for ~99% of ``positions`` rows.

⚠️ **Known limit — long linger (~0.5% of markets):** occasionally the re-discovery
prune misses a dead token and it keeps printing its pinned price for >30 min (seen
up to ~1h40m) past the real ``:00``. Its last touch then rounds to a LATER hour.
This is irreducible offline: there is no clean settlement anchor — rounding the
*pin onset* (first decisive touch) misfires on early-decisive favourites (a 0.95
favourite at ``:20`` is live, not settled), and ``last_trade_price`` lingers too.
So round-the-last-touch stays; the rare long-linger miss is tolerated + asserted as
such in ``test_backtest_recovery``. Impact is ~0.4% of usable markets, and the
recovered OUTCOME is still correct (the token pins to the true result) — only the
hour label drifts, so at worst a few post-settlement pinned ticks leak past the
look-ahead guard on those markets.

``outcome`` — YES if the final YES-perspective price locked high (``> 0.9``), NO if
it locked low (``< 0.1``). Anything in between never resolved decisively inside our
window and is EXCLUDED: we can't score a trade whose result we don't know.

⚠️ **Touch-only rule (fix 2026-06-11):** ``last_ts`` and the decisive last price
are derived ONLY from touch-bearing rows — ``event_type IN ('book',
'last_trade_price')`` — never ``price_change``. A ``price_change`` row stores the
changed LEVEL's price (audit: ~30% land >10c off-touch), so a deep 0.0x bid /
0.9x ask level at the end of a market's stream forged "decisive" labels under
the old any-event rule (ground truth: ``research/verify_labels.py``). The live
resolver/ledger never used this module and is unaffected. A one-line
``label-rule fix`` log reports how many labels the fix changed vs the legacy
rule on the current DB.

A market is also excluded when its recovered ``resolve_ts`` falls past the last ts
we have data for (``global_max_ts``) — it was still trading when the bot stopped, so
it never settled in-sample.

⚠️ **In-flight guard (fix 2026-06-12):** a market whose stream is still ticking
within ``LIVE_EDGE_GRACE_SEC`` of ``global_max_ts`` is excluded as IN FLIGHT: its
hour hasn't closed, so a pinned mid-hour price there is a premature label, not a
settlement (observed live: a market dipping to 0.02 at :24 was labeled "resolved
NO at :00" while its true settle was the NEXT :00). A settled market's token goes
quiet minutes after the prune, so it passes this check on the next run.

Reads ``mode=ro`` and never writes (mirrors ``research/analyze_trades.py``).
"""
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from src.monitor.logger import get_logger

# A YES-perspective final price above/below these is treated as a decisive
# YES/NO settlement; the band between is "never resolved decisively" -> excluded.
YES_ABOVE = 0.9
NO_BELOW = 0.1

# Touch-bearing event types: rows whose `price` is a real market touch (book ->
# best bid; last_trade_price -> trade print). price_change rows carry the changed
# LEVEL's price and are NEVER allowed to decide a label (fix 2026-06-11).
TOUCH_EVENTS: tuple[str, ...] = ("book", "last_trade_price")

# A market still streaming this close to the data edge is in flight (its hour
# hasn't closed). Live markets tick every few seconds; a settled token goes
# quiet once the re-discovery prune drops it, so 2 min cleanly separates them.
LIVE_EDGE_GRACE_SEC = 120


@dataclass(frozen=True)
class MarketResolution:
    """Recovered ground truth for one usable market."""
    market_id: str
    resolve_ts: datetime   # UTC, tz-aware — the :00 the market settled on
    outcome: str           # "YES" | "NO"
    last_ts: datetime      # last TOUCH-bearing snapshot (≈ resolve_ts + lingering)
    last_price: float      # YES-perspective touch price at last_ts (book/last_trade)
    n: int                 # touch-bearing snapshots for this market


@dataclass(frozen=True)
class RecoveryResult:
    usable: dict[str, MarketResolution]   # market_id -> resolution (scoreable)
    excluded: dict[str, str]              # market_id -> human reason
    global_max_ts: datetime               # last ts anywhere in the stream


def round_to_hour(ts: datetime) -> datetime:
    """Round to the nearest hour boundary (>=30 min ceils, else floors)."""
    floored = ts.replace(minute=0, second=0, microsecond=0)
    return floored + timedelta(hours=1) if ts.minute >= 30 else floored


def recover_outcome(last_price: float | None) -> str | None:
    """Decisive YES/NO from the final YES-perspective price, else None."""
    if last_price is None:
        return None
    if last_price > YES_ABOVE:
        return "YES"
    if last_price < NO_BELOW:
        return "NO"
    return None


def is_in_flight(last_any_ts: datetime, global_max_ts: datetime) -> bool:
    """True when the market was still streaming at the data edge — i.e. its
    hour hadn't closed when our data ends, so no label can be trusted yet."""
    return (global_max_ts - last_any_ts).total_seconds() <= LIVE_EDGE_GRACE_SEC


def classify_market(
    market_id: str,
    last_ts: datetime,
    last_price: float | None,
    n: int,
    global_max_ts: datetime,
) -> tuple[MarketResolution | None, str | None]:
    """Decide one market's usability. Pure (no I/O) so it's unit-testable.

    Returns ``(resolution, None)`` when usable, else ``(None, reason)``.
    """
    resolve_ts = round_to_hour(last_ts)
    # Still trading when the bot stopped: it never settled inside our data.
    if resolve_ts > global_max_ts:
        return None, "truncated (did not settle before shutdown)"
    outcome = recover_outcome(last_price)
    if outcome is None:
        lp = "n/a" if last_price is None else f"{last_price:.3f}"
        return None, f"ambiguous final price {lp}"
    return MarketResolution(market_id, resolve_ts, outcome, last_ts, last_price, n), None


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def recover_resolutions(db_path: Path) -> RecoveryResult:
    """Recover resolve_ts + outcome for every poly market in ``db_path``.

    Read-only. Excluded markets are logged once at INFO with their reason so a
    run is transparent about what it could and couldn't score.
    """
    log = get_logger("backtest.recovery")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        gmax = conn.execute("SELECT MAX(ts) AS m FROM snapshots").fetchone()["m"]
        if gmax is None:
            return RecoveryResult({}, {}, _parse_ts("1970-01-01T00:00:00+00:00"))
        global_max_ts = _parse_ts(gmax)

        rows = conn.execute(
            """
            SELECT market_id, MAX(ts) AS last_ts, COUNT(*) AS n
              FROM snapshots
             WHERE source = 'polymarket'
               AND market_id IS NOT NULL
               AND price IS NOT NULL
               AND event_type IN ('book', 'last_trade_price')
             GROUP BY market_id
            """
        ).fetchall()

        # Liveness map from ANY event type: price_change rows can't decide a
        # label, but they DO prove the stream (and thus the hour) is still open.
        any_last: dict[str, datetime] = {
            r["market_id"]: _parse_ts(r["last_ts"])
            for r in conn.execute(
                """
                SELECT market_id, MAX(ts) AS last_ts
                  FROM snapshots
                 WHERE source = 'polymarket' AND market_id IS NOT NULL
                 GROUP BY market_id
                """
            ).fetchall()
        }

        usable: dict[str, MarketResolution] = {}
        excluded: dict[str, str] = {}
        for r in rows:
            mid = r["market_id"]
            last_ts = _parse_ts(r["last_ts"])
            if is_in_flight(any_last.get(mid, last_ts), global_max_ts):
                excluded[mid] = "in flight at data edge (stream still live)"
                continue
            lp_row = conn.execute(
                """
                SELECT price FROM snapshots
                 WHERE market_id = ? AND price IS NOT NULL
                   AND event_type IN ('book', 'last_trade_price')
                 ORDER BY ts DESC LIMIT 1
                """,
                (mid,),
            ).fetchone()
            last_price = float(lp_row["price"]) if lp_row else None
            res, reason = classify_market(mid, last_ts, last_price, r["n"], global_max_ts)
            if res is not None:
                usable[mid] = res
            else:
                excluded[mid] = reason or "excluded"

        # Legacy-rule diff (pre-2026-06-11: decisive price from ANY event type).
        # Report-only — quantifies what the touch-only fix changed on this DB.
        legacy_changed: list[str] = []
        legacy_dropped = legacy_added = 0
        for r in conn.execute(
            """
            SELECT market_id, MAX(ts) AS last_ts
              FROM snapshots
             WHERE source = 'polymarket'
               AND market_id IS NOT NULL
               AND price IS NOT NULL
             GROUP BY market_id
            """
        ).fetchall():
            mid = r["market_id"]
            if is_in_flight(any_last.get(mid, _parse_ts(r["last_ts"])), global_max_ts):
                continue  # not a label under either rule — keep the diff honest
            lp = conn.execute(
                """
                SELECT price FROM snapshots
                 WHERE market_id = ? AND price IS NOT NULL
                 ORDER BY ts DESC LIMIT 1
                """,
                (mid,),
            ).fetchone()
            legacy_out = None
            if lp is not None and round_to_hour(_parse_ts(r["last_ts"])) <= global_max_ts:
                legacy_out = recover_outcome(float(lp["price"]))
            new_res = usable.get(mid)
            if legacy_out is not None and new_res is None:
                legacy_dropped += 1
            elif legacy_out is None and new_res is not None:
                legacy_added += 1
            elif (legacy_out is not None and new_res is not None
                  and new_res.outcome != legacy_out):
                legacy_changed.append(mid)
    finally:
        conn.close()

    result = RecoveryResult(usable=usable, excluded=excluded, global_max_ts=global_max_ts)
    yes = sum(1 for m in usable.values() if m.outcome == "YES")
    no = len(usable) - yes
    log.info(
        "recovery: %d usable (YES %d / NO %d), %d excluded (of %d markets)",
        len(usable), yes, no, len(excluded), len(usable) + len(excluded),
    )
    log.info(
        "label-rule fix (touch-only decisive price): %d label(s) changed vs the "
        "legacy any-event rule, %d legacy-only dropped, %d newly usable",
        len(legacy_changed), legacy_dropped, legacy_added,
    )
    for mid in legacy_changed:
        log.info("  label changed under touch-only rule: %s", mid[:12] + "..")
    for mid, reason in excluded.items():
        log.info("  excluded %s: %s", mid[:12] + "..", reason)
    return result
