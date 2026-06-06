from datetime import datetime, timedelta, timezone

from src.data.snapshot import MarketSnapshot
from src.data.writer import SnapshotWriter

BASE = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)


def _snap(event_type, *, asset_id=None, symbol=None, source="polymarket", offset_s=0.0):
    return MarketSnapshot(
        ts=BASE + timedelta(seconds=offset_s),
        source=source,
        event_type=event_type,
        symbol=symbol,
        market_id=None,
        asset_id=asset_id,
        price=0.5,
        best_bid=None,
        best_ask=None,
    )


def _writer():
    return SnapshotWriter(db=None)  # add() never touches the db


def test_price_change_same_asset_within_window_drops_second():
    w = _writer()
    w.add(_snap("price_change", asset_id="tok1", offset_s=0))
    w.add(_snap("price_change", asset_id="tok1", offset_s=5))
    assert len(w._buf) == 1


def test_price_change_same_asset_beyond_window_keeps_both():
    w = _writer()
    w.add(_snap("price_change", asset_id="tok1", offset_s=0))
    w.add(_snap("price_change", asset_id="tok1", offset_s=11))
    assert len(w._buf) == 2


def test_price_change_different_assets_within_window_keeps_both():
    w = _writer()
    w.add(_snap("price_change", asset_id="tok1", offset_s=0))
    w.add(_snap("price_change", asset_id="tok2", offset_s=1))
    assert len(w._buf) == 2


def test_book_and_trade_bypass_throttle():
    w = _writer()
    w.add(_snap("book", asset_id="tok1", offset_s=0))
    w.add(_snap("book", asset_id="tok1", offset_s=1))               # back-to-back, still stored
    w.add(_snap("last_trade_price", asset_id="tok1", offset_s=2))
    w.add(_snap("last_trade_price", asset_id="tok1", offset_s=3))
    assert len(w._buf) == 4


def test_binance_ticker_throttled_per_symbol():
    w = _writer()
    w.add(_snap("ticker", symbol="BTC", source="binance", offset_s=0))
    w.add(_snap("ticker", symbol="BTC", source="binance", offset_s=5))   # same symbol, dropped
    w.add(_snap("ticker", symbol="ETH", source="binance", offset_s=6))   # different symbol, kept
    assert len(w._buf) == 2
