"""Per-strategy position dedup.

is_held used to be per-MARKET (one open position per market, full stop), which made
the momentum canary FIGHT contrarian over the same extreme markets — whichever
dispatched first opened, the other was blocked. It is now per-(market, strategy),
so contrarian (longshot side) and momentum (favorite side) can BOTH hold the same
extreme market at once. These tests pin that, and that get_open(strategy=...)
disambiguates the two so an exit overlay only ever closes its own strategy's side.
"""
from datetime import datetime, timezone

from src.data.db import Database
from src.data.schema import create_tables
from src.execute.portfolio import Portfolio

MKT = "0xMS"
TS = datetime(2026, 6, 18, 12, 0, 0, tzinfo=timezone.utc)


async def _pf(tmp_path):
    db = Database(tmp_path / "ms.db")
    await db.connect()
    await create_tables(db)
    return db, Portfolio(db)


async def _open(pf, *, strategy, side, entry, size=20.0):
    """Insert an 'open' row for `strategy`, mirroring open_position's books."""
    await pf.db.execute(
        "UPDATE balance SET balance_usdc = balance_usdc - ? WHERE id = 1", (size,)
    )
    await pf.db.execute(
        """INSERT INTO positions
             (ts, market_id, symbol, side, entry_price, size_usdc, status,
              resolve_time, strategy, fill_flag)
           VALUES (?, ?, 'BTC', ?, ?, ?, 'open', NULL, ?, 'ok')""",
        (TS.isoformat(), MKT, side, entry, size, strategy),
    )


async def test_is_held_is_per_strategy(tmp_path):
    db, pf = await _pf(tmp_path)
    await _open(pf, strategy="contrarian", side="YES", entry=0.20)
    assert await pf.is_held(MKT, "contrarian") is True
    # Same market, DIFFERENT strategy: NOT blocked — the momentum canary may enter.
    assert await pf.is_held(MKT, "momentum") is False
    await db.close()


async def test_two_strategies_coexist_on_one_market(tmp_path):
    db, pf = await _pf(tmp_path)
    await _open(pf, strategy="contrarian", side="YES", entry=0.20)   # longshot
    await _open(pf, strategy="momentum", side="NO", entry=0.80)      # favorite
    assert len(await pf.list_open()) == 2  # both held on the same market
    # get_open scopes by strategy so an exit closes only its own side.
    assert (await pf.get_open(MKT, strategy="contrarian"))["side"] == "YES"
    assert (await pf.get_open(MKT, strategy="momentum"))["side"] == "NO"
    await db.close()


async def test_get_open_without_strategy_returns_some_open_row(tmp_path):
    """Legacy callers (strategy=None) still get a position back when one is open."""
    db, pf = await _pf(tmp_path)
    await _open(pf, strategy="contrarian", side="YES", entry=0.20)
    assert (await pf.get_open(MKT)) is not None
    await db.close()
