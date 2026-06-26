"""Replay engine + simulated portfolio.

Covers the three invariants the harness must hold:
  * PnL parity — SimPortfolio's win math is byte-equal to the production
    Portfolio.resolve_position (so a slippage sweep never silently diverges).
  * is_held dedup — at most one position per market.
  * the look-ahead guard — snapshots at/after a market's resolve_ts are never
    dispatched to a strategy (no training on post-settlement 0/1 prices).
"""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.backtest.engine import SimPortfolio, run_backtest, win_pnl
from src.data.snapshot import MarketSnapshot
from src.execute.decision import Action, Decision
from src.execute.fill import YesBook
from src.execute.portfolio import MIN_TIME_TO_RESOLVE_SEC, SLIPPAGE_BUFFER, Portfolio
from src.strategy.params import StrategyParams


def _utc(h, mi, s=0):
    return datetime(2026, 6, 8, h, mi, s, tzinfo=timezone.utc)


def _book(ts, *, bid=0.68, ask=0.72):
    """A (YES-perspective book, capture-ts) tuple for SimPortfolio.open — deep
    enough that a small stake fills 'ok' (no partial)."""
    return (
        YesBook(yes_bid=bid, yes_ask=ask, yes_bid_size=1e6, yes_ask_size=1e6,
                yes_bid_depth=1e6, yes_ask_depth=1e6),
        ts,
    )


def _snap(price, zone, vol="low_vol", *, market_id="0xM", ts=None):
    return MarketSnapshot(
        ts=ts or _utc(20, 30),
        source="polymarket",
        event_type="book",
        symbol="BTC",
        market_id=market_id,
        asset_id="tok",
        price=price,
        best_bid=None,
        best_ask=None,
        outcome="YES",
        vol_regime=vol,
        price_zone=zone,
    )


def _buy(price=0.70, market_id="0xM", strategy="contrarian", side="YES"):
    return Decision(action=Action.BUY, strategy=strategy, side=side,
                    price=price, market_id=market_id)


# --- PnL parity with the production resolver -------------------------------

async def test_win_pnl_matches_resolver(tmp_path):
    """win_pnl(entry, size, SLIPPAGE_BUFFER) must equal what Portfolio
    .resolve_position writes for a winning position — same payout convention.
    SLIPPAGE_BUFFER is now 0.0 (execution cost is modelled at ENTRY via the
    realistic fill, not as a flat buffer here), so this is the entry==fill case."""
    from src.data.db import Database
    from src.data.schema import create_tables

    db = Database(tmp_path / "t.db")
    await db.connect()
    await create_tables(db)
    entry, size = 0.62, 25.0
    await db.execute(
        "INSERT INTO positions (ts, market_id, side, entry_price, size_usdc, status, strategy) "
        "VALUES (?, ?, ?, ?, ?, 'open', ?)",
        (_utc(20, 0).isoformat(), "0xM", "YES", entry, size, "contrarian"),
    )
    portfolio = Portfolio(db)
    await portfolio.resolve_position(1, won=True, exit_price=1.0)
    row = await db.fetchone("SELECT pnl_usdc FROM positions WHERE id = 1")
    await db.close()

    assert row["pnl_usdc"] == win_pnl(entry, size, SLIPPAGE_BUFFER)


def test_win_pnl_realistic_slippage_beats_production():
    """At a 0.70 entry, a 1-cent fill keeps more upside than a 3-cent one."""
    assert win_pnl(0.70, 20.0, 0.01) > win_pnl(0.70, 20.0, 0.03)


# --- SimPortfolio gates ----------------------------------------------------

def test_is_held_blocks_second_open_same_market():
    sim = SimPortfolio({"contrarian": StrategyParams()})
    resolve_ts = _utc(21, 0)
    book = _book(_utc(20, 30))
    assert sim.open(_buy(), _snap(0.70, "high"), resolve_ts, book) is True
    assert sim.open(_buy(), _snap(0.70, "high"), resolve_ts, book) is False
    assert len(sim.open_positions) == 1


def test_open_blocked_too_close_to_resolution():
    sim = SimPortfolio({"contrarian": StrategyParams()})
    snap = _snap(0.70, "high", ts=_utc(20, 59, 30))   # 30s before close
    resolve_ts = _utc(21, 0)
    assert (resolve_ts - snap.ts).total_seconds() < MIN_TIME_TO_RESOLVE_SEC
    assert sim.open(_buy(), snap, resolve_ts) is False
    assert sim.skipped["too_close_to_resolution"] == 1


def test_open_blocked_below_entry_floor():
    sim = SimPortfolio({"contrarian": StrategyParams(entry_floor=0.15)})
    assert sim.open(_buy(price=0.10), _snap(0.10, "extreme_low"), _utc(21, 0)) is False
    assert sim.skipped["below_entry_floor"] == 1


def test_open_blocked_no_book():
    """Realistic fill needs a captured book; with none cached the open is SKIPPED
    (you can't fill against nothing), not opened at the mid like the old engine."""
    sim = SimPortfolio({"contrarian": StrategyParams()})
    assert sim.open(_buy(), _snap(0.70, "high"), _utc(21, 0), None) is False
    assert sim.skipped["no_book"] == 1


def test_open_blocked_stale_book():
    """A cached book older than MAX_BOOK_AGE_SEC vs the decision is rejected — the
    same stale-book guard the live open_position enforces."""
    from src.execute.portfolio import MAX_BOOK_AGE_SEC

    sim = SimPortfolio({"contrarian": StrategyParams()})
    snap = _snap(0.70, "high", ts=_utc(20, 30))
    stale = _book(_utc(20, 30) - timedelta(seconds=MAX_BOOK_AGE_SEC + 1))
    assert sim.open(_buy(), snap, _utc(21, 0), stale) is False
    assert sim.skipped["stale_book"] == 1


def test_open_records_realistic_walked_fill():
    """A YES open fills at the ASK (0.72), not the 0.70 signal/mid, and tags the
    fill flag — the realistic-fill contract."""
    sim = SimPortfolio({"contrarian": StrategyParams()})
    assert sim.open(_buy(price=0.70, side="YES"), _snap(0.70, "high"),
                    _utc(21, 0), _book(_utc(20, 30), ask=0.72)) is True
    pos = sim.open_positions[0]
    assert pos.entry_price == 0.72          # lifted the ask, not the 0.70 signal
    assert pos.fill_flag in ("ok", "walk", "partial")
    assert pos.shares > 0


def test_settle_pays_winner_and_zeroes_loser():
    sim = SimPortfolio({"contrarian": StrategyParams()})
    sim.open(_buy(side="YES"), _snap(0.70, "high"), _utc(21, 0), _book(_utc(20, 30)))
    from src.backtest.recovery import MarketResolution
    res = {"0xM": MarketResolution("0xM", _utc(21, 0), "YES", _utc(21, 14), 0.99, 1)}
    settled, final = sim.settle(res, slippage=0.01)
    assert settled[0].won is True
    assert final > sim.balance            # payout added back on the win
    # flip the outcome: same trade now loses its whole stake
    res["0xM"] = MarketResolution("0xM", _utc(21, 0), "NO", _utc(21, 14), 0.01, 1)
    settled, _ = sim.settle(res, slippage=0.01)
    assert settled[0].won is False
    assert settled[0].pnl_usdc == -settled[0].size_usdc


# --- look-ahead guard, end to end on a synthetic DB ------------------------

_SNAP_DDL = """
CREATE TABLE snapshots (
    ts TEXT, source TEXT, event_type TEXT, symbol TEXT, market_id TEXT,
    asset_id TEXT, price REAL, best_bid REAL, best_ask REAL,
    bid_size REAL, ask_size REAL, bid_depth REAL, ask_depth REAL,
    vol_regime TEXT, price_zone TEXT
)
"""


def _make_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute(_SNAP_DDL)
    # Each row is a book event carrying a (deep) quote so the realistic fill can
    # actually open. best_bid == the YES price so _build_outcome_map infers a YES
    # token; ask a tick above so a YES buy lifts a fillable ask.
    conn.executemany(
        "INSERT INTO snapshots (ts, source, event_type, symbol, market_id, asset_id, "
        "price, best_bid, best_ask, bid_size, ask_size, bid_depth, ask_depth, "
        "vol_regime, price_zone) "
        "VALUES (:ts, 'polymarket', 'book', 'BTC', :mid, 'tok', :price, :bid, :ask, "
        "1e6, 1e6, 1e6, 1e6, 'low_vol', :zone)",
        rows,
    )
    conn.commit()
    conn.close()


def _row(mid, ts, price, zone):
    return {"mid": mid, "ts": ts.isoformat(), "price": price, "zone": zone,
            "bid": price, "ask": min(price + 0.02, 0.99)}


def test_lookahead_guard_ignores_post_resolution_ticks(tmp_path):
    """Market B has a genuine pre-resolution contrarian signal -> opens.
    Market A's ONLY contrarian-qualifying tick is AFTER resolve_ts (a lingering
    post-settlement print) -> the guard must drop it, opening nothing."""
    db = tmp_path / "synthetic.db"
    rows = [
        # B: qualifying tick at 20:30 (pre-resolve) -> contrarian fades extreme_low, buys YES @0.20
        _row("0xB", _utc(20, 30), 0.20, "extreme_low"),
        _row("0xB", _utc(21, 10), 0.99, "extreme_high"),   # last price -> outcome YES
        # A: qualifying extreme_low tick only at 21:05 (POST-resolve) -> must be ignored
        _row("0xA", _utc(21, 5), 0.20, "extreme_low"),
        _row("0xA", _utc(21, 12), 0.99, "extreme_high"),   # last price -> outcome YES
    ]
    _make_db(db, rows)
    # Heartbeat past the last poly event (any real DB has the spot stream still
    # ticking) — without it both markets sit at the data edge and recovery's
    # in-flight guard would defer them as "maybe still trading" instead of
    # exercising the look-ahead guard this test is about.
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO snapshots (ts, source, event_type, symbol, price) "
        "VALUES (?, 'binance', 'ticker', 'BTC', 100.0)",
        (_utc(21, 40).isoformat(),),
    )
    conn.commit()
    conn.close()

    results = run_backtest(db, ["contrarian"], (0.01,))
    settled = results[0].settled

    assert len(settled) == 1
    pos = settled[0]
    assert pos.market_id == "0xB"
    assert pos.side == "YES"
    assert pos.opened_ts == _utc(20, 30)
    assert pos.won is True            # B resolved YES, contrarian was long YES
