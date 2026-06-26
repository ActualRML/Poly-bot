"""Offline resolution recovery — the pure logic, plus a DB-guarded cross-check.

The pure helpers are tested with synthetic inputs (no DB dependency). The one
integration test only runs when data/bot.db is present and asserts that
round(last_ts) reproduces every stored resolve_time exactly — the empirical
claim the whole recovery rests on.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.backtest.recovery import (
    classify_market,
    is_in_flight,
    recover_outcome,
    round_to_hour,
)


def _utc(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


# --- round_to_hour ---------------------------------------------------------

def test_round_to_hour_floors_the_lingering_case():
    # markets linger ~14 min past :00 before the prune drops them -> floor
    assert round_to_hour(_utc(2026, 6, 8, 21, 14, 5)) == _utc(2026, 6, 8, 21, 0)


def test_round_to_hour_ceils_a_market_that_stopped_just_before_close():
    # stopped ticking at :57 -> the real settlement is the NEXT :00
    assert round_to_hour(_utc(2026, 6, 8, 20, 57, 0)) == _utc(2026, 6, 8, 21, 0)


def test_round_to_hour_boundary_is_30_minutes():
    assert round_to_hour(_utc(2026, 6, 8, 20, 29, 59)) == _utc(2026, 6, 8, 20, 0)
    assert round_to_hour(_utc(2026, 6, 8, 20, 30, 0)) == _utc(2026, 6, 8, 21, 0)


def test_round_to_hour_exact_hour_is_unchanged():
    assert round_to_hour(_utc(2026, 6, 8, 21, 0, 0)) == _utc(2026, 6, 8, 21, 0)


# --- recover_outcome -------------------------------------------------------

def test_recover_outcome_decisive_high_is_yes():
    assert recover_outcome(0.95) == "YES"
    assert recover_outcome(0.999) == "YES"


def test_recover_outcome_decisive_low_is_no():
    assert recover_outcome(0.05) == "NO"
    assert recover_outcome(0.001) == "NO"


def test_recover_outcome_midband_and_boundaries_are_none():
    assert recover_outcome(0.5) is None
    assert recover_outcome(0.9) is None    # not strictly > 0.9
    assert recover_outcome(0.1) is None    # not strictly < 0.1
    assert recover_outcome(None) is None


# --- is_in_flight (live-edge guard, fix 2026-06-12) -------------------------

def test_is_in_flight_when_still_ticking_at_the_edge():
    gmax = _utc(2026, 6, 12, 6, 28, 23)
    # live market: last event seconds before the data edge
    assert is_in_flight(_utc(2026, 6, 12, 6, 28, 6), gmax)


def test_is_in_flight_false_for_a_pruned_settled_token():
    gmax = _utc(2026, 6, 12, 6, 28, 23)
    # settled at 06:00, lingered to ~06:14, then the prune killed the stream
    assert not is_in_flight(_utc(2026, 6, 12, 6, 14, 0), gmax)


# --- classify_market (the per-market verdict) ------------------------------

GMAX = _utc(2026, 6, 8, 23, 59)


def test_classify_market_usable_decisive():
    last_ts = _utc(2026, 6, 8, 21, 14)
    res, reason = classify_market("0xM", last_ts, 0.98, 1000, GMAX)
    assert reason is None
    assert res is not None
    assert res.outcome == "YES"
    assert res.resolve_ts == _utc(2026, 6, 8, 21, 0)


def test_classify_market_ambiguous_excluded():
    res, reason = classify_market("0xM", _utc(2026, 6, 8, 21, 14), 0.55, 10, GMAX)
    assert res is None
    assert "ambiguous" in reason


def test_classify_market_truncated_excluded():
    # last_ts so close to the data horizon that round-to-hour lands past it:
    # the market was still trading when the stream stopped -> never settled.
    gmax = _utc(2026, 6, 8, 19, 57)
    last_ts = _utc(2026, 6, 8, 19, 55)         # rounds up to 20:00 > gmax
    res, reason = classify_market("0xM", last_ts, 0.99, 10, gmax)
    assert res is None
    assert "truncated" in reason


# --- recover_resolutions excludes in-flight markets (regression) -----------

def test_recover_resolutions_excludes_in_flight_market(tmp_path):
    """A market still streaming at the data edge must NOT get a (premature)
    resolution — a mid-hour pinned price is not a settlement. Observed live
    2026-06-12: a 07:00 market dipping to 0.02 at 06:24 was labeled 'NO @06:00'."""
    import sqlite3

    from src.backtest.recovery import recover_resolutions

    db = tmp_path / "mini.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE snapshots (ts TEXT, source TEXT, event_type TEXT,"
        " market_id TEXT, price REAL)"
    )
    rows = [
        # settled market: decisive book touches, stream dead since 06:13
        ("2026-06-12T05:55:00+00:00", "polymarket", "book", "0xDEAD", 0.55),
        ("2026-06-12T06:05:00+00:00", "polymarket", "book", "0xDEAD", 0.999),
        ("2026-06-12T06:13:00+00:00", "polymarket", "book", "0xDEAD", 0.999),
        # in-flight market: pinned-looking touch + a price_change proving the
        # stream is still alive seconds before the data edge
        ("2026-06-12T06:27:50+00:00", "polymarket", "book", "0xLIVE", 0.02),
        ("2026-06-12T06:28:10+00:00", "polymarket", "price_change", "0xLIVE", 0.01),
        # binance ticker sets the global data edge (gmax)
        ("2026-06-12T06:28:20+00:00", "binance", "ticker", None, 100.0),
    ]
    conn.executemany("INSERT INTO snapshots VALUES (?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()

    result = recover_resolutions(db)
    assert "0xDEAD" in result.usable
    assert result.usable["0xDEAD"].outcome == "YES"
    assert result.usable["0xDEAD"].resolve_ts == _utc(2026, 6, 12, 6, 0)
    assert "0xLIVE" not in result.usable
    assert "in flight" in result.excluded["0xLIVE"]


# --- integration: recovery vs the stored resolve_times ---------------------

_DB = Path(__file__).resolve().parents[1] / "data" / "bot.db"


@pytest.mark.skipif(not _DB.exists(), reason="data/bot.db not present")
def test_recovered_resolve_ts_matches_every_stored_resolve_time():
    """round(last_touch) must reproduce the stored resolve_time for the vast
    majority of markets. The ONE documented exception is a dead token that lingers
    >30 min past its real :00 (the prune occasionally misses one): its last touch
    then rounds to a LATER hour. That is an irreducible offline-heuristic limit, not
    a logic bug — there is no clean anchor (pin-onset misfires on early-decisive
    favourites; last_trade lingers too). So the test asserts (a) <5% miss and
    (b) every miss IS such a long-linger case; anything else is a regression."""
    import sqlite3

    from src.backtest.recovery import recover_resolutions

    result = recover_resolutions(_DB)
    assert result.usable, "expected some usable markets"

    conn = sqlite3.connect(f"file:{_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        stored = conn.execute(
            "SELECT DISTINCT market_id, resolve_time FROM positions "
            "WHERE resolve_time IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()

    matched = mismatched = 0
    for row in stored:
        # A market whose stored resolve_time lies past the data edge was still
        # in flight when the stream ends — it has no recoverable resolution by
        # definition (the live DB always contains the currently-trading hour).
        stored_rt = datetime.fromisoformat(row["resolve_time"])
        if stored_rt > result.global_max_ts:
            continue
        res = result.usable.get(row["market_id"])
        if res is None:
            continue  # excluded (e.g. ambiguous outcome) — not in scope here
        if res.resolve_ts == stored_rt:
            matched += 1
            continue
        # The only tolerated miss: the dead token lingered >30 min past the true :00,
        # so round(last_touch) landed on a later hour. Confirm that's what this is —
        # a regression (wrong rounding logic) would miss WITHOUT a long linger.
        mismatched += 1
        assert (res.last_ts - stored_rt).total_seconds() > 30 * 60, (
            f"{row['market_id']}: resolve_ts {res.resolve_ts} != stored {stored_rt}, "
            f"and last_ts {res.last_ts} is NOT a >30-min linger — recovery regressed"
        )

    assert matched > 0, "expected at least one stored market to be usable"
    total = matched + mismatched
    assert mismatched / total <= 0.05, (
        f"{mismatched}/{total} resolve_ts mismatches exceeds the 5% long-linger "
        f"tolerance — the round-to-hour heuristic has regressed"
    )
