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
        strategy    TEXT    NOT NULL
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


async def create_tables(db: Database) -> None:
    for stmt in _SCHEMA:
        await db.execute(stmt)
    # Seed the starting bankroll once; no-op on subsequent runs.
    await db.execute(
        "INSERT OR IGNORE INTO balance (id, balance_usdc) VALUES (1, 1000.0)"
    )
