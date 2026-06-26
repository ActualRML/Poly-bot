"""Force-void bookkeeping: refund the stake, mark 'void', and never double-refund.

void_position is the only path that mutates a position outside normal resolution,
so these lock down (a) the balance refund is exactly the stake, (b) the status
flips to 'void' with pnl=0 / exit_price NULL, (c) a second call is a no-op (the
double-refund guard), and (d) a voided row drops out of list_open().
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.data.db import Database
from src.data.schema import create_tables
from src.data.snapshot import MarketSnapshot
from src.execute.decision import Action, Decision
from src.execute.fill import YesBook
from src.execute.portfolio import Portfolio
from src.strategy.params import StrategyParams

TS = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc).isoformat()


def _book(ask: float, bid: float | None = None) -> YesBook:
    """A simple two-sided YES book with no depth data → single-price (model 2)
    fill at the ask, so a YES open fills its full stake at `ask`. Enough to
    exercise open_position's bookkeeping without depth-walk noise."""
    return YesBook(yes_bid=bid if bid is not None else ask - 0.01, yes_ask=ask)


async def _portfolio(tmp_path, strategy_params=None) -> Portfolio:
    db = Database(tmp_path / "void_test.db")
    await db.connect()
    await create_tables(db)  # seeds balance id=1 = 1000.0
    return Portfolio(db, strategy_params=strategy_params)


async def _open_position(pf: Portfolio, *, size_usdc: float, market_id: str = "0xMKT") -> int:
    """Insert an 'open' row and deduct the stake, mirroring open_position's books."""
    await pf.db.execute(
        "UPDATE balance SET balance_usdc = balance_usdc - ? WHERE id = 1", (size_usdc,)
    )
    await pf.db.execute(
        """
        INSERT INTO positions (ts, market_id, symbol, side, entry_price, size_usdc, status, resolve_time, strategy)
        VALUES (?, ?, 'BTC', 'YES', 0.5, ?, 'open', NULL, 'contrarian')
        """,
        (TS, market_id, size_usdc),
    )
    row = await pf.db.fetchone("SELECT id FROM positions WHERE market_id = ?", (market_id,))
    return row["id"]


async def test_void_refunds_full_stake_and_marks_void(tmp_path):
    pf = await _portfolio(tmp_path)
    size = 20.0
    pid = await _open_position(pf, size_usdc=size)

    bal_before = await pf.get_balance()  # 1000 - 20 = 980
    await pf.void_position(pid)
    bal_after = await pf.get_balance()

    assert bal_after - bal_before == pytest.approx(size)  # exactly the stake, no more
    assert bal_after == pytest.approx(1000.0)             # net-zero vs the seeded bankroll

    row = await pf.db.fetchone(
        "SELECT status, pnl_usdc, exit_price FROM positions WHERE id = ?", (pid,)
    )
    assert row["status"] == "void"
    assert row["pnl_usdc"] == 0
    assert row["exit_price"] is None  # left NULL — not 0.0 (which would read as a losing fill)
    await pf.db.close()


async def test_void_twice_is_noop_no_double_refund(tmp_path):
    pf = await _portfolio(tmp_path)
    size = 25.0
    pid = await _open_position(pf, size_usdc=size)

    await pf.void_position(pid)
    bal_after_first = await pf.get_balance()
    await pf.void_position(pid)  # second call must bail at the status='open' guard
    bal_after_second = await pf.get_balance()

    assert bal_after_second == pytest.approx(bal_after_first)  # NOT credited a second time
    row = await pf.db.fetchone("SELECT status, pnl_usdc FROM positions WHERE id = ?", (pid,))
    assert row["status"] == "void"
    assert row["pnl_usdc"] == 0
    await pf.db.close()


async def test_voided_position_excluded_from_list_open(tmp_path):
    pf = await _portfolio(tmp_path)
    pid = await _open_position(pf, size_usdc=10.0)

    assert any(p["id"] == pid for p in await pf.list_open())   # open before void
    await pf.void_position(pid)
    assert all(p["id"] != pid for p in await pf.list_open())   # gone after void
    await pf.db.close()


async def test_open_position_records_strategy(tmp_path):
    """A position opened via open_position carries the originating strategy name,
    so later audits can attribute win/loss per strategy. Pins the INSERT wiring."""
    pf = await _portfolio(tmp_path)  # seeded balance 1000.0, market_meta=None
    decision = Decision(
        action=Action.BUY,
        strategy="contrarian",
        side="YES",
        price=0.5,             # >= the 0.15 default entry floor so the open isn't floored
        market_id="0xMKT",
    )
    snapshot = MarketSnapshot(
        ts=datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc),
        source="polymarket",
        event_type="book",
        symbol="BTC",
        market_id="0xMKT",
        asset_id=None,
        price=0.5,
        best_bid=None,
        best_ask=None,
    )
    opened = await pf.open_position(decision, snapshot, _book(ask=0.5))
    assert opened is True
    row = await pf.db.fetchone(
        "SELECT strategy, side FROM positions WHERE market_id = ?", ("0xMKT",)
    )
    assert row["strategy"] == "contrarian"
    assert row["side"] == "YES"
    await pf.db.close()


def test_contrarian_params_pinned():
    """Contrarian declares its own entry floor + sizing + ceiling (0.15 / 0.02 /
    0.30). Pinning them here guards against a silent revert if the StrategyParams
    defaults or the Plugin's params line ever drift."""
    from src.strategy.contrarian import Plugin

    p = Plugin().params
    assert p.entry_floor == 0.15
    assert p.bet_fraction == 0.02
    assert p.entry_ceiling == 0.30


async def test_open_position_caps_entry_at_strategy_ceiling(tmp_path):
    """The taker walk is limit-priced at the strategy's entry_ceiling: a thin book
    that would otherwise walk an entry well past 0.30 opens instead at avg <= 0.30
    (a partial — only the liquidity under the cap is taken). Guards the wiring of
    entry_ceiling -> simulate_taker_fill(max_price=...)."""
    pf = await _portfolio(
        tmp_path,
        strategy_params={
            "contrarian": StrategyParams(entry_floor=0.15, bet_fraction=0.02, entry_ceiling=0.30)
        },
    )
    # p0=0.16, spread=0.03, tiny size/level but ample depth -> uncapped this walks
    # far past 0.30; the cap must stop it.
    book = YesBook(yes_bid=0.13, yes_ask=0.16,
                   yes_ask_size=5, yes_ask_depth=10000,
                   yes_bid_size=5, yes_bid_depth=10000)
    assert await pf.open_position(_buy(0.16, market_id="0xCAP"), _snap("0xCAP"), book) is True
    row = await pf.db.fetchone("SELECT entry_price, fill_flag FROM positions WHERE market_id = ?", ("0xCAP",))
    assert row["entry_price"] <= 0.30 + 1e-9   # never filled into expensive shares
    assert row["fill_flag"] == "partial"       # cap left the rest of the stake unfilled
    await pf.db.close()


def _buy(price, *, market_id, strategy="contrarian"):
    return Decision(
        action=Action.BUY, strategy=strategy, side="YES", price=price, market_id=market_id
    )


def _snap(market_id):
    # open_position reads ts/symbol off the snapshot and price/side/etc. off the
    # decision; snapshot.price itself is unused here, so None is fine.
    return MarketSnapshot(
        ts=datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc),
        source="polymarket", event_type="book", symbol="BTC",
        market_id=market_id, asset_id=None, price=None, best_bid=None, best_ask=None,
    )


async def test_open_position_applies_strategy_floor(tmp_path):
    """With contrarian registered at floor 0.15 / fraction 0.02, an entry below
    the floor is skipped (no stake taken) and one at/above it opens with
    stake = balance * fraction."""
    pf = await _portfolio(
        tmp_path,
        strategy_params={"contrarian": StrategyParams(entry_floor=0.15, bet_fraction=0.02)},
    )

    # Below the 0.15 floor -> skipped, bankroll untouched.
    assert await pf.open_position(_buy(0.12, market_id="0xLOW"), _snap("0xLOW")) is False
    assert await pf.get_balance() == pytest.approx(1000.0)

    # At/above the floor -> opens; stake = 1000 * 0.02 = 20 (pins `frac`). With a
    # full-fill book (no depth cap) the whole 20 is deployed.
    assert await pf.open_position(_buy(0.16, market_id="0xOK"), _snap("0xOK"), _book(ask=0.16)) is True
    row = await pf.db.fetchone("SELECT size_usdc FROM positions WHERE market_id = ?", ("0xOK",))
    assert row["size_usdc"] == pytest.approx(20.0)
    assert await pf.get_balance() == pytest.approx(980.0)
    await pf.db.close()


async def test_open_position_rejects_stale_book(tmp_path):
    """A book older than MAX_BOOK_AGE_SEC vs the decision is fiction (a
    price_change fired long after the last real book) — skip, don't fill. Mirrors
    the BNB flicker bug: the signal sees an extreme while the only book is minutes
    stale at ~mid, walking to a fake ~0.7 entry."""
    from src.execute.portfolio import MAX_BOOK_AGE_SEC

    pf = await _portfolio(
        tmp_path,
        strategy_params={"contrarian": StrategyParams(entry_floor=0.15, bet_fraction=0.02)},
    )
    snap = _snap("0xSTALE")  # decision snapshot at TS (12:00:00)

    # Book captured 1s past the max age -> stale -> skip, bankroll untouched.
    stale_ts = snap.ts - timedelta(seconds=MAX_BOOK_AGE_SEC + 1)
    assert await pf.open_position(
        _buy(0.16, market_id="0xSTALE"), snap, _book(ask=0.16), stale_ts
    ) is False
    assert await pf.get_balance() == pytest.approx(1000.0)

    # Same book captured within the window -> fresh -> opens.
    fresh_ts = snap.ts - timedelta(seconds=MAX_BOOK_AGE_SEC - 1)
    assert await pf.open_position(
        _buy(0.16, market_id="0xFRESH"), _snap("0xFRESH"), _book(ask=0.16), fresh_ts
    ) is True
    await pf.db.close()


async def test_open_position_floor_is_per_strategy_not_the_default(tmp_path):
    """Proves the floor is sourced from the per-strategy registry, not the 0.15
    module default: contrarian registered ABOVE the default rejects a price the
    default would accept, while an unregistered strategy falls back to the
    default and opens at that same price."""
    pf = await _portfolio(
        tmp_path,
        strategy_params={"contrarian": StrategyParams(entry_floor=0.30, bet_fraction=0.02)},
    )

    # 0.20 clears the 0.15 default but not contrarian's registered 0.30 -> skip.
    assert await pf.open_position(_buy(0.20, market_id="0xC"), _snap("0xC"), _book(ask=0.20)) is False
    # Unregistered "ghost" -> DEFAULT_PARAMS (0.15) -> 0.20 clears it -> opens.
    assert await pf.open_position(
        _buy(0.20, market_id="0xG", strategy="ghost"), _snap("0xG"), _book(ask=0.20)
    ) is True
    await pf.db.close()
