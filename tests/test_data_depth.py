"""Order-book depth capture: parser extraction + additive schema migration.

The parser now promotes top-of-book size and total per-side depth from `book`
events (for future order-book-imbalance analysis); the schema gains four nullable
columns, ALTER-added on startup so an existing DB is upgraded without data loss.
"""
from datetime import datetime, timezone

from src.data.db import Database
from src.data.parsers import parse_polymarket
from src.data.schema import _SNAPSHOT_DEPTH_COLS, create_tables
from src.data.snapshot import MarketSnapshot
from src.data.writer import SnapshotWriter

_DEPTH = set(_SNAPSHOT_DEPTH_COLS)

# old snapshots shape (pre-depth) — used to exercise the migration
_OLD_SNAPSHOTS = """
    CREATE TABLE snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, source TEXT, event_type TEXT,
        symbol TEXT, market_id TEXT, asset_id TEXT, price REAL, best_bid REAL,
        best_ask REAL, vol_regime TEXT, price_zone TEXT
    )
"""


# --- parser ---------------------------------------------------------------
def test_book_extracts_top_size_and_total_depth():
    raw = {
        "event_type": "book", "market": "0xM", "asset_id": "tok",
        "bids": [{"price": "0.60", "size": "100"}, {"price": "0.59", "size": "50"}],
        "asks": [{"price": "0.62", "size": "30"}, {"price": "0.63", "size": "20"}],
    }
    s = parse_polymarket(raw, {})
    assert (s.best_bid, s.best_ask) == (0.60, 0.62)
    assert (s.bid_size, s.ask_size) == (100.0, 30.0)        # size at the best price
    assert (s.bid_depth, s.ask_depth) == (150.0, 50.0)      # summed across levels


def test_book_handles_pair_levels():
    raw = {"event_type": "book", "asset_id": "tok",
           "bids": [["0.40", "10"]], "asks": [["0.45", "5"]]}
    s = parse_polymarket(raw, {})
    assert (s.bid_size, s.ask_size, s.bid_depth, s.ask_depth) == (10.0, 5.0, 10.0, 5.0)


def test_book_sums_size_at_shared_best_price():
    raw = {"event_type": "book", "asset_id": "tok",
           "bids": [{"price": "0.50", "size": "7"}, {"price": "0.50", "size": "3"}],
           "asks": [{"price": "0.55", "size": "4"}]}
    s = parse_polymarket(raw, {})
    assert s.bid_size == 10.0 and s.bid_depth == 10.0


def test_book_price_only_yields_no_size():
    raw = {"event_type": "book", "asset_id": "tok",
           "bids": [{"price": "0.6"}], "asks": [{"price": "0.62"}]}
    s = parse_polymarket(raw, {})
    assert s.best_bid == 0.6 and s.bid_size is None and s.bid_depth is None


def test_non_book_events_have_no_depth():
    raw = {"event_type": "price_change", "asset_id": "tok",
           "price_changes": [{"asset_id": "tok", "price": "0.5"}]}
    s = parse_polymarket(raw, {})
    assert (s.bid_size, s.ask_size, s.bid_depth, s.ask_depth) == (None, None, None, None)


# --- writer ---------------------------------------------------------------
def test_writer_row_carries_depth():
    s = MarketSnapshot(
        ts=datetime(2026, 6, 9, tzinfo=timezone.utc), source="polymarket", event_type="book",
        symbol="BTC", market_id="m", asset_id="a", price=0.6, best_bid=0.6, best_ask=0.62,
        bid_size=100.0, ask_size=30.0, bid_depth=150.0, ask_depth=50.0,
    )
    row = SnapshotWriter._to_row(s)
    assert len(row) == 15                          # matches _INSERT_SQL placeholders
    assert {100.0, 30.0, 150.0, 50.0} <= set(row)


# --- schema / migration ---------------------------------------------------
async def test_fresh_schema_has_depth_columns(tmp_path):
    db = Database(tmp_path / "new.db")
    await db.connect()
    await create_tables(db)
    cols = {r["name"] for r in await db.fetchall("PRAGMA table_info(snapshots)")}
    await db.close()
    assert _DEPTH <= cols


async def test_migration_adds_depth_columns_to_old_db(tmp_path):
    db = Database(tmp_path / "old.db")
    await db.connect()
    await db.execute(_OLD_SNAPSHOTS)
    await db.execute(
        "INSERT INTO snapshots (ts, source, event_type, best_bid, best_ask) "
        "VALUES ('2026-06-01T00:00:00+00:00', 'polymarket', 'book', 0.5, 0.51)"
    )
    await create_tables(db)                        # should ALTER-add the depth cols
    cols = {r["name"] for r in await db.fetchall("PRAGMA table_info(snapshots)")}
    assert _DEPTH <= cols
    # pre-existing row preserved, new columns NULL
    row = await db.fetchone("SELECT best_bid, bid_size FROM snapshots LIMIT 1")
    await db.close()
    assert row["best_bid"] == 0.5 and row["bid_size"] is None


async def test_migration_is_idempotent(tmp_path):
    db = Database(tmp_path / "twice.db")
    await db.connect()
    await create_tables(db)
    await create_tables(db)                        # second run must not raise (cols exist)
    cols = {r["name"] for r in await db.fetchall("PRAGMA table_info(snapshots)")}
    await db.close()
    assert _DEPTH <= cols
