from src.data.db import Database

# Schema is intentionally tiny — only what the foundation needs.
# Future tables (positions, fills, pnl) get added here, with a migration
# step if the column shape changes. For now CREATE IF NOT EXISTS is enough.

_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS decisions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          TEXT    NOT NULL,
        strategy    TEXT    NOT NULL,
        market_id   TEXT,
        action      TEXT    NOT NULL,
        side        TEXT,
        size_usdc   REAL,
        price       REAL,
        reason      TEXT,
        dry_run     INTEGER NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts)
    """,
    # Structured snapshot fields only — never the raw dict (thousands/min).
    """
    CREATE TABLE IF NOT EXISTS snapshots (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          TEXT    NOT NULL,
        source      TEXT    NOT NULL,
        event_type  TEXT    NOT NULL,
        symbol      TEXT,
        market_id   TEXT,
        asset_id    TEXT,
        price       REAL,
        best_bid    REAL,
        best_ask    REAL,
        bid_size    REAL,
        ask_size    REAL,
        bid_depth   REAL,
        ask_depth   REAL,
        vol_regime  TEXT,
        price_zone  TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots(ts)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_snapshots_symbol ON snapshots(symbol)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_snapshots_source_symbol ON snapshots(source, symbol)
    """,
    # Per-market lookups (backtest recovery + replay, check_state) scan/group by
    # market_id; without this they full-scan the whole snapshots table (the >10-min
    # backtest hang). Composite (market_id, ts) also serves the "latest touch per
    # market" ORDER BY ts probes. One-time build cost on first startup after upgrade.
    """
    CREATE INDEX IF NOT EXISTS idx_snapshots_market ON snapshots(market_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_snapshots_market_ts ON snapshots(market_id, ts)
    """,
    # Fase 5: simulated (dry-run) position ledger.
    """
    CREATE TABLE IF NOT EXISTS positions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          TEXT    NOT NULL,
        market_id   TEXT    NOT NULL,
        symbol      TEXT,
        side        TEXT    NOT NULL,
        entry_price REAL    NOT NULL,
        size_usdc   REAL    NOT NULL,
        status      TEXT    NOT NULL,
        exit_price  REAL,
        pnl_usdc    REAL,
        resolved_ts TEXT,
        resolve_time TEXT,
        strategy    TEXT    NOT NULL,
        fill_flag         TEXT,
        intended_size_usdc REAL,
        closed_reason     TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status)
    """,
    # Single-row simulated cash ledger.
    """
    CREATE TABLE IF NOT EXISTS balance (
        id          INTEGER PRIMARY KEY CHECK (id = 1),
        balance_usdc REAL   NOT NULL
    )
    """,
)


# Columns added after the snapshots table first shipped. CREATE TABLE IF NOT
# EXISTS won't alter an existing table, so they're ALTER-added on startup.
_SNAPSHOT_DEPTH_COLS: tuple[str, ...] = ("bid_size", "ask_size", "bid_depth", "ask_depth")

# Columns added to positions after it first shipped (realistic-fill bookkeeping):
# how the order filled (ok/walk/partial) and the stake we WANTED before depth
# capped it; plus why a position was CLOSED early (status='closed') instead of
# resolved — e.g. 'time_gated_sl' for the stop-loss canary. (name, sqlite type).
_POSITION_FILL_COLS: tuple[tuple[str, str], ...] = (
    ("fill_flag", "TEXT"),
    ("intended_size_usdc", "REAL"),
    ("closed_reason", "TEXT"),
)


async def _migrate_snapshot_columns(db: Database) -> None:
    """Additively add depth columns to a snapshots table created before they
    existed. New columns default NULL — so pre-existing rows simply have no depth,
    which is correct: we didn't capture it then. Safe to run every startup."""
    info = await db.fetchall("PRAGMA table_info(snapshots)")
    existing = {row["name"] for row in info}
    for col in _SNAPSHOT_DEPTH_COLS:
        if col not in existing:
            await db.execute(f"ALTER TABLE snapshots ADD COLUMN {col} REAL")


async def _migrate_position_columns(db: Database) -> None:
    """Additively add fill-bookkeeping columns to a positions table created
    before they existed. NULL on old rows is correct — those were opened under
    the old mid-fill model. Safe to run every startup."""
    info = await db.fetchall("PRAGMA table_info(positions)")
    existing = {row["name"] for row in info}
    for col, col_type in _POSITION_FILL_COLS:
        if col not in existing:
            await db.execute(f"ALTER TABLE positions ADD COLUMN {col} {col_type}")


async def create_tables(db: Database) -> None:
    for stmt in _SCHEMA:
        await db.execute(stmt)
    await _migrate_snapshot_columns(db)
    await _migrate_position_columns(db)
    # Seed the starting bankroll once; no-op on subsequent runs.
    await db.execute(
        "INSERT OR IGNORE INTO balance (id, balance_usdc) VALUES (1, 1000.0)"
    )
