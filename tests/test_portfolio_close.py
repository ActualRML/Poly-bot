"""Early-close bookkeeping for the time-gated stop-loss canary.

close_position is the second path (after void_position) that mutates a position
outside normal resolution, so these lock down: (a) a FULL sell credits exactly the
proceeds and flips status to 'closed' with the realized pnl + closed_reason, (b) a
no_exit/partial book leaves the position OPEN (conservative — hold to resolution),
(c) a second call can't double-credit (the still-'open' guard), and (d) get_open
returns the held side the trigger needs.
"""
from datetime import datetime, timezone

import pytest

from src.data.db import Database
from src.data.schema import create_tables
from src.execute.fill import YesBook
from src.execute.portfolio import Portfolio

TS = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc).isoformat()


async def _portfolio(tmp_path) -> Portfolio:
    db = Database(tmp_path / "close_test.db")
    await db.connect()
    await create_tables(db)  # seeds balance id=1 = 1000.0
    return Portfolio(db)


async def _open(pf: Portfolio, *, market_id="0xSL", side="YES", entry=0.20, size=20.0) -> int:
    """Insert an 'open' row and deduct the stake, mirroring open_position's books.
    entry 0.20 / size 20 → shares = 100, so sell proceeds are easy to reason about."""
    await pf.db.execute(
        "UPDATE balance SET balance_usdc = balance_usdc - ? WHERE id = 1", (size,)
    )
    await pf.db.execute(
        """
        INSERT INTO positions
            (ts, market_id, symbol, side, entry_price, size_usdc, status, resolve_time,
             strategy, fill_flag)
        VALUES (?, ?, 'BTC', ?, ?, ?, 'open', NULL, 'contrarian', 'ok')
        """,
        (TS, market_id, side, entry, size),
    )
    row = await pf.db.fetchone("SELECT id FROM positions WHERE market_id = ?", (market_id,))
    return row["id"]


async def test_close_full_fill_credits_proceeds_and_marks_closed(tmp_path):
    pf = await _portfolio(tmp_path)
    pid = await _open(pf)                       # shares 100, balance 1000-20=980
    bal_before = await pf.get_balance()
    assert bal_before == pytest.approx(980.0)

    # No depth → single-price sell of all 100 shares at the 0.10 bid → proceeds 10.
    closed = await pf.close_position(pid, YesBook(yes_bid=0.10, yes_ask=0.12))
    assert closed is True

    assert await pf.get_balance() == pytest.approx(990.0)   # 980 + 10 proceeds
    row = await pf.db.fetchone(
        "SELECT status, exit_price, pnl_usdc, closed_reason FROM positions WHERE id = ?",
        (pid,),
    )
    assert row["status"] == "closed"
    assert row["exit_price"] == pytest.approx(0.10)
    assert row["pnl_usdc"] == pytest.approx(-10.0)          # 10 proceeds - 20 stake
    assert row["closed_reason"] == "time_gated_sl"
    await pf.db.close()


async def test_close_no_exit_leaves_position_open(tmp_path):
    pf = await _portfolio(tmp_path)
    pid = await _open(pf)
    # No bid on the held side → cannot sell.
    closed = await pf.close_position(pid, YesBook(yes_bid=None, yes_ask=0.12))
    assert closed is False
    assert await pf.get_balance() == pytest.approx(980.0)   # untouched
    assert any(p["id"] == pid for p in await pf.list_open())  # still open
    await pf.db.close()


async def test_close_partial_leaves_position_open(tmp_path):
    pf = await _portfolio(tmp_path)
    pid = await _open(pf)                       # 100 shares to sell
    # Bid depth of only 20 shares can't absorb the 100-share lot → partial → hold.
    book = YesBook(yes_bid=0.10, yes_ask=0.12, yes_bid_size=10, yes_bid_depth=20,
                   yes_ask_size=10, yes_ask_depth=20)
    closed = await pf.close_position(pid, book)
    assert closed is False
    assert await pf.get_balance() == pytest.approx(980.0)
    assert any(p["id"] == pid for p in await pf.list_open())
    await pf.db.close()


async def test_close_twice_is_noop_no_double_credit(tmp_path):
    pf = await _portfolio(tmp_path)
    pid = await _open(pf)
    assert await pf.close_position(pid, YesBook(yes_bid=0.10, yes_ask=0.12)) is True
    bal_after_first = await pf.get_balance()
    # Second call must bail at the status='open' guard — no second credit.
    assert await pf.close_position(pid, YesBook(yes_bid=0.10, yes_ask=0.12)) is False
    assert await pf.get_balance() == pytest.approx(bal_after_first)
    await pf.db.close()


async def test_closed_position_excluded_from_list_open(tmp_path):
    pf = await _portfolio(tmp_path)
    pid = await _open(pf)
    assert any(p["id"] == pid for p in await pf.list_open())
    await pf.close_position(pid, YesBook(yes_bid=0.10, yes_ask=0.12))
    assert all(p["id"] != pid for p in await pf.list_open())
    await pf.db.close()


async def test_get_open_returns_held_side(tmp_path):
    pf = await _portfolio(tmp_path)
    pid = await _open(pf, side="NO", entry=0.15)
    pos = await pf.get_open("0xSL")
    assert pos is not None
    assert pos["id"] == pid
    assert pos["side"] == "NO"
    # After close, get_open finds nothing.
    await pf.close_position(pid, YesBook(yes_bid=0.84, yes_ask=0.86))  # NO sell hits 1-ask
    assert await pf.get_open("0xSL") is None
    await pf.db.close()
