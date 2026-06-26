"""The per-tick stop-loss trigger gate (src.execute.exits.maybe_stop_loss).

Pins the four scalar gates that must ALL pass before the canary touches a
position — symbol ∈ sl_symbols, inside the final sl_window_sec, held-side value ≤
sl_threshold, and the kill-switch on — plus the held YES-vs-NO value computation
and the audit SELL decision it writes on a successful close.
"""
from datetime import datetime, timedelta, timezone

from src.config import Settings
from src.data.db import Database
from src.data.schema import create_tables
from src.data.snapshot import MarketSnapshot
from src.execute.executor import DryRunExecutor
from src.execute.exits import maybe_stop_loss
from src.execute.fill import YesBook
from src.execute.portfolio import Portfolio

MKT = "0xSL"


async def _setup(tmp_path, *, side="YES", entry=0.20, size=20.0):
    db = Database(tmp_path / "trig.db")
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
        (datetime(2026, 6, 4, 12, tzinfo=timezone.utc).isoformat(), MKT, side, entry, size),
    )
    return db, Portfolio(db), DryRunExecutor(db, dry_run=True)


def _snap(price, *, symbol="BTC", event_type="book"):
    return MarketSnapshot(
        ts=datetime.now(timezone.utc), source="polymarket", event_type=event_type,
        symbol=symbol, market_id=MKT, asset_id=None, price=price,
        best_bid=None, best_ask=None,
    )


def _meta(secs_left):
    return {MKT: {"resolve_time": datetime.now(timezone.utc) + timedelta(seconds=secs_left)}}


_BOOK = {MKT: YesBook(yes_bid=0.08, yes_ask=0.10)}


async def test_trigger_closes_held_position_in_window(tmp_path):
    db, pf, ex = await _setup(tmp_path)
    s = Settings(sl_canary_enabled=True)
    # YES held, value 0.08 ≤ 0.10, 60s left ≤ 120s window, BTC ∈ {BTC,ETH}.
    await maybe_stop_loss(s, _snap(0.08), _meta(60), pf, ex, _BOOK)

    assert await pf.get_open(MKT) is None                 # position closed
    row = await db.fetchone("SELECT status, closed_reason FROM positions WHERE market_id = ?", (MKT,))
    assert row["status"] == "closed"
    assert row["closed_reason"] == "time_gated_sl"
    dec = await db.fetchone("SELECT action, side, reason FROM decisions WHERE market_id = ?", (MKT,))
    assert dec["action"] == "SELL"
    assert dec["side"] == "YES"
    assert dec["reason"] == "time_gated_sl"
    await db.close()


async def test_trigger_skips_symbol_not_in_set(tmp_path):
    db, pf, ex = await _setup(tmp_path)
    s = Settings(sl_canary_enabled=True, sl_symbols=["ETH"])   # BTC excluded
    await maybe_stop_loss(s, _snap(0.08), _meta(60), pf, ex, _BOOK)
    assert await pf.get_open(MKT) is not None                  # untouched
    await db.close()


async def test_trigger_skips_outside_window(tmp_path):
    db, pf, ex = await _setup(tmp_path)
    s = Settings(sl_canary_enabled=True)
    await maybe_stop_loss(s, _snap(0.08), _meta(600), pf, ex, _BOOK)  # 10 min left > 2 min
    assert await pf.get_open(MKT) is not None
    await db.close()


async def test_trigger_skips_value_above_threshold(tmp_path):
    db, pf, ex = await _setup(tmp_path)
    s = Settings(sl_canary_enabled=True)
    await maybe_stop_loss(s, _snap(0.50), _meta(60), pf, ex, _BOOK)   # value 0.50 > 0.10
    assert await pf.get_open(MKT) is not None
    await db.close()


async def test_trigger_skips_when_disabled(tmp_path):
    db, pf, ex = await _setup(tmp_path)
    s = Settings(sl_canary_enabled=False)
    await maybe_stop_loss(s, _snap(0.08), _meta(60), pf, ex, _BOOK)
    assert await pf.get_open(MKT) is not None
    await db.close()


async def test_trigger_no_side_value_uses_one_minus_price(tmp_path):
    """A held NO position's value is 1 − YES price: at YES 0.95 the NO side is worth
    0.05 ≤ 0.10, so the stop fires."""
    db, pf, ex = await _setup(tmp_path, side="NO", entry=0.15)
    s = Settings(sl_canary_enabled=True)
    book = {MKT: YesBook(yes_bid=0.90, yes_ask=0.92)}     # NO sell hits 1-ask = 0.08
    await maybe_stop_loss(s, _snap(0.95), _meta(60), pf, ex, book)
    assert await pf.get_open(MKT) is None                 # closed
    await db.close()
