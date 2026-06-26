"""The per-tick slow-rise exit gate (src.execute.exits.maybe_slow_rise_exit).

Pins the validated rule: sell the held side the FIRST time its value reaches
slowrise_value (0.40) IFF the climb open→0.40 was SLOW (> slowrise_min_sec). A fast
riser is HELD (but still recorded in `seen`, so it can't re-trigger). Plus the
kill-switch (OFF by default), the held YES-vs-NO value, and the `seen` short-circuit.
"""
from datetime import datetime, timedelta, timezone

from src.config import Settings
from src.data.db import Database
from src.data.schema import create_tables
from src.data.snapshot import MarketSnapshot
from src.execute.executor import DryRunExecutor
from src.execute.exits import maybe_slow_rise_exit
from src.execute.fill import YesBook
from src.execute.portfolio import Portfolio

MKT = "0xSR"
T0 = datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc)  # position open time


async def _setup(tmp_path, *, side="YES", entry=0.20, size=20.0, open_ts=T0):
    db = Database(tmp_path / "sr.db")
    await db.connect()
    await create_tables(db)
    await db.execute("UPDATE balance SET balance_usdc = balance_usdc - ? WHERE id = 1", (size,))
    await db.execute(
        """
        INSERT INTO positions
            (ts, market_id, symbol, side, entry_price, size_usdc, status, resolve_time,
             strategy, fill_flag)
        VALUES (?, ?, 'BTC', ?, ?, ?, 'open', NULL, 'contrarian', 'ok')
        """,
        (open_ts.isoformat(), MKT, side, entry, size),
    )
    return db, Portfolio(db), DryRunExecutor(db, dry_run=True)


def _snap(price, *, event_ts):
    return MarketSnapshot(ts=event_ts, source="polymarket", event_type="book",
                          symbol="BTC", market_id=MKT, asset_id=None, price=price,
                          best_bid=None, best_ask=None)


_BOOK = {MKT: YesBook(yes_bid=0.40, yes_ask=0.42)}


async def test_slow_climb_sells(tmp_path):
    db, pf, ex = await _setup(tmp_path)
    seen = set()
    # value 0.42 in [0.40,0.45]; opened 11 min before this event -> slow -> sell.
    await maybe_slow_rise_exit(Settings(slowrise_enabled=True),
                               _snap(0.42, event_ts=T0 + timedelta(minutes=11)),
                               {}, pf, ex, _BOOK, seen)
    assert await pf.get_open(MKT) is None
    row = await db.fetchone("SELECT status, closed_reason FROM positions WHERE market_id=?", (MKT,))
    assert row["status"] == "closed"
    assert row["closed_reason"] == "slow_rise_exit"
    assert MKT in seen
    dec = await db.fetchone("SELECT action, reason FROM decisions WHERE market_id=?", (MKT,))
    assert dec["action"] == "SELL" and dec["reason"] == "slow_rise_exit"
    await db.close()


async def test_fast_climb_holds_but_marks_seen(tmp_path):
    db, pf, ex = await _setup(tmp_path)
    seen = set()
    # reaches 0.40 only 5 min after open -> fast riser -> HOLD, but decision recorded.
    await maybe_slow_rise_exit(Settings(slowrise_enabled=True),
                               _snap(0.42, event_ts=T0 + timedelta(minutes=5)),
                               {}, pf, ex, _BOOK, seen)
    assert await pf.get_open(MKT) is not None
    assert MKT in seen
    await db.close()


async def test_disabled_by_default(tmp_path):
    db, pf, ex = await _setup(tmp_path)
    # Explicit disable — exercise the OFF path of the guard, not the code default:
    # .env.local sets SLOWRISE_ENABLED=true (the live canary), so a bare Settings()
    # here reads True and would silently test the wrong branch.
    await maybe_slow_rise_exit(Settings(slowrise_enabled=False),
                               _snap(0.42, event_ts=T0 + timedelta(minutes=11)),
                               {}, pf, ex, _BOOK, set())
    assert await pf.get_open(MKT) is not None
    await db.close()


async def test_value_below_band_skips(tmp_path):
    db, pf, ex = await _setup(tmp_path)
    await maybe_slow_rise_exit(Settings(slowrise_enabled=True),
                               _snap(0.38, event_ts=T0 + timedelta(minutes=11)),  # value 0.38 < 0.40
                               {}, pf, ex, _BOOK, set())
    assert await pf.get_open(MKT) is not None
    await db.close()


async def test_seen_short_circuits(tmp_path):
    db, pf, ex = await _setup(tmp_path)
    await maybe_slow_rise_exit(Settings(slowrise_enabled=True),
                               _snap(0.42, event_ts=T0 + timedelta(minutes=11)),
                               {}, pf, ex, _BOOK, {MKT})  # already handled
    assert await pf.get_open(MKT) is not None
    await db.close()


async def test_no_side_value_uses_one_minus_price(tmp_path):
    db, pf, ex = await _setup(tmp_path, side="NO", entry=0.20)
    # held NO; YES price 0.58 -> NO value 0.42 in band; slow -> sell (NO sell hits 1-ask).
    book = {MKT: YesBook(yes_bid=0.57, yes_ask=0.59)}
    await maybe_slow_rise_exit(Settings(slowrise_enabled=True),
                               _snap(0.58, event_ts=T0 + timedelta(minutes=11)),
                               {}, pf, ex, book, set())
    assert await pf.get_open(MKT) is None
    await db.close()
