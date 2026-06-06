from pathlib import Path

import aiosqlite


class Database:
    """
    Tiny async wrapper around a single SQLite connection.

    Why one connection: SQLite serializes writes anyway, and our access
    pattern is "one bot process, sub-Hz write rate." Pool complexity
    would buy us nothing. WAL mode keeps reads non-blocking against the
    single writer.
    """

    def __init__(self, path: Path):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        # WAL: concurrent readers don't block the writer; durability still solid.
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def execute(self, sql: str, params: tuple = ()) -> None:
        assert self._conn is not None, "Database not connected — call connect() first"
        await self._conn.execute(sql, params)
        await self._conn.commit()

    async def executemany(self, sql: str, rows: list[tuple]) -> None:
        # One commit per batch — the throughput path (see SnapshotWriter).
        assert self._conn is not None, "Database not connected"
        await self._conn.executemany(sql, rows)
        await self._conn.commit()

    async def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        assert self._conn is not None, "Database not connected"
        cur = await self._conn.execute(sql, params)
        try:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            await cur.close()

    async def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        assert self._conn is not None, "Database not connected"
        cur = await self._conn.execute(sql, params)
        try:
            row = await cur.fetchone()
            return dict(row) if row else None
        finally:
            await cur.close()
