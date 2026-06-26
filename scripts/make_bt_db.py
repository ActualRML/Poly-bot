"""Build an INDEXED backtest copy of the live ``data/bot.db``.

WHY. Replaying the live DB directly is what hung for >10 min: ``snapshots`` has no
``market_id`` index, so recovery full-scans 3 M rows per market. The fix is the
INDEX, not dropping data — this copy adds ``idx_snap_mkt`` (10 min -> ~45 s) and
gives the backtest a stable read-only snapshot that never contends with the
running bot.

WHAT IT KEEPS. Every PRICED poly event (``book`` + ``price_change`` +
``last_trade_price``) and the ``binance`` heartbeat. We tried slimming to just
book+last_trade and it BROKE the replay: the live strategy triggers on whichever
event first carries an extreme zone, and that's a ``price_change`` 84% of the
time, so dropping it caught a later/opposite extreme — 29/115 markets wrong side,
11% vs 31% live WR (2026-06-16). So price_change is load-bearing for selection
fidelity and stays. Only ``tick_size_change`` (a few k no-price rows that neither
trigger, fill, nor label) is dropped.

  * ``book``            — decisions + the captured quote/depth for realistic fills
  * ``price_change``    — the extreme-zone TRIGGER the strategy most often fires on
  * ``last_trade_price`` — touch labels (recovery) + a trigger
  * binance ``ticker``  — the data-edge heartbeat recovery uses for global_max_ts
  * ``positions`` / ``balance`` — the live ledger, copied whole for validation

Reads the live DB ``mode=ro`` so it never blocks/corrupts the running bot.
Re-run any time to refresh:  ``.venv/Scripts/python.exe scripts/make_bt_db.py``
"""
import os
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "data" / "bot.db"
DST = REPO / "data" / "bot_bt.db"

# Every priced poly event (book/price_change/last_trade_price are all load-bearing —
# see module docstring) + the binance heartbeat. Only tick_size_change is dropped.
_KEEP_SNAPSHOT_WHERE = (
    "(source = 'polymarket' AND event_type IN ('book', 'price_change', 'last_trade_price')) "
    "OR (source = 'binance' AND event_type = 'ticker')"
)


def build(src: Path = SRC, dst: Path = DST) -> None:
    if not src.exists():
        sys.exit(f"source DB not found: {src}")
    # Remove a stale dest (+ its WAL/SHM siblings) so we rebuild clean.
    for p in (dst, dst.with_suffix(dst.suffix + "-wal"), dst.with_suffix(dst.suffix + "-shm")):
        if p.exists():
            p.unlink()

    t0 = time.time()
    db = sqlite3.connect(str(dst), uri=True)
    db.execute("PRAGMA journal_mode = OFF")      # throwaway DB — no durability needed
    db.execute("PRAGMA synchronous = OFF")
    db.execute("PRAGMA busy_timeout = 60000")    # ride out the live writer's commits
    db.execute(f"ATTACH DATABASE 'file:{src.as_posix()}?mode=ro' AS src")

    # Recreate table shapes EXACTLY from the source (picks up every ALTER-added
    # column — bid_size.., closed_reason.. — so INSERT .. SELECT * lines up).
    ddl = db.execute(
        "SELECT sql FROM src.sqlite_master WHERE type='table' "
        "AND name IN ('snapshots','positions','balance') AND sql IS NOT NULL"
    ).fetchall()
    for (sql,) in ddl:
        db.execute(sql)

    db.execute(f"INSERT INTO snapshots SELECT * FROM src.snapshots WHERE {_KEEP_SNAPSHOT_WHERE}")
    kept = db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    db.execute("INSERT INTO positions SELECT * FROM src.positions")
    db.execute("INSERT INTO balance SELECT * FROM src.balance")
    npos = db.execute("SELECT COUNT(*) FROM positions").fetchone()[0]

    # The index that turns the 10-min hang into seconds: recovery + replay both
    # group/scan per market_id, and the per-market last-price probe sorts by ts.
    db.execute("CREATE INDEX idx_snap_mkt ON snapshots(market_id)")
    db.execute("CREATE INDEX idx_snap_mkt_ts ON snapshots(market_id, ts)")
    db.execute("CREATE INDEX idx_snapshots_ts ON snapshots(ts)")
    db.commit()
    # ANALYZE all attached DBs would try to write stats into the read-only src;
    # detach first and analyze only our new DB.
    db.execute("DETACH DATABASE src")
    db.execute("ANALYZE main")
    db.commit()
    db.close()

    total_src = sqlite3.connect(f"file:{src}?mode=ro", uri=True).execute(
        "SELECT COUNT(*) FROM snapshots").fetchone()[0]
    size_mb = dst.stat().st_size / 1e6
    print(f"built {dst.name}: {kept:,} snapshots (of {total_src:,} = "
          f"{kept/total_src:.0%}) + {npos} positions, {size_mb:,.0f} MB, "
          f"{time.time()-t0:.0f}s")


if __name__ == "__main__":
    build()
