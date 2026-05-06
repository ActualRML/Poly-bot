import sys
import pytest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from src.api.clob_client import ClobClient
from src.models.types import SisiOrder, StatusOrder


@pytest.fixture
def fake_clob_types():
    """Inject a fake py_clob_client.clob_types into sys.modules so tests run
    without py-clob-client installed."""
    mod = MagicMock()
    mod.OrderArgs = MagicMock(return_value=MagicMock())
    mod.BalanceAllowanceParams = MagicMock(return_value=MagicMock())
    mod.AssetType = MagicMock()

    old = sys.modules.get("py_clob_client.clob_types")
    sys.modules["py_clob_client.clob_types"] = mod
    yield mod
    if old is None:
        sys.modules.pop("py_clob_client.clob_types", None)
    else:
        sys.modules["py_clob_client.clob_types"] = old


# ==============================================================================
# pasang_order — DRY_RUN=True
# ==============================================================================

def test_pasang_order_dry_run_returns_order():
    client = ClobClient()
    with patch("src.api.clob_client.config") as cfg:
        cfg.DRY_RUN = True
        order = client.pasang_order(SisiOrder.BELI, Decimal("0.65"), Decimal("10.0"), "tok_001")
    assert order is not None
    assert order.sisi == SisiOrder.BELI
    assert order.harga == Decimal("0.65")
    assert order.ukuran == Decimal("10.0")
    assert order.market_id == "tok_001"
    assert order.status == StatusOrder.MENUNGGU
    assert order.order_id.startswith("dryrun-")

def test_pasang_order_dry_run_no_clob_calls():
    client = ClobClient()
    client._terhubung = True
    client._client = MagicMock()
    with patch("src.api.clob_client.config") as cfg:
        cfg.DRY_RUN = True
        client.pasang_order(SisiOrder.BELI, Decimal("0.65"), Decimal("10.0"), "tok_001")
    client._client.create_and_post_order.assert_not_called()

def test_pasang_order_dry_run_no_token_id_returns_order():
    client = ClobClient()
    with patch("src.api.clob_client.config") as cfg:
        cfg.DRY_RUN = True
        order = client.pasang_order(SisiOrder.JUAL, Decimal("0.80"), Decimal("5.0"), None)
    assert order is not None
    assert order.market_id == ""


# ==============================================================================
# pasang_order — DRY_RUN=False
# ==============================================================================

def test_pasang_order_live_not_connected_returns_none():
    client = ClobClient()
    client._terhubung = False
    with patch("src.api.clob_client.config") as cfg:
        cfg.DRY_RUN = False
        result = client.pasang_order(SisiOrder.BELI, Decimal("0.65"), Decimal("10.0"), "tok_001")
    assert result is None

def test_pasang_order_live_no_token_id_returns_none():
    client = ClobClient()
    client._terhubung = True
    client._client = MagicMock()
    with patch("src.api.clob_client.config") as cfg:
        cfg.DRY_RUN = False
        result = client.pasang_order(SisiOrder.BELI, Decimal("0.65"), Decimal("10.0"), None)
    assert result is None

def test_pasang_order_live_calls_clob_returns_order(fake_clob_types):
    client = ClobClient()
    client._terhubung = True
    mock_clob = MagicMock()
    mock_clob.create_and_post_order.return_value = {"orderID": "live-abc123"}
    client._client = mock_clob

    with patch("src.api.clob_client.config") as cfg:
        cfg.DRY_RUN = False
        order = client.pasang_order(SisiOrder.BELI, Decimal("0.65"), Decimal("10.0"), "tok_001")

    assert order is not None
    assert order.order_id == "live-abc123"
    assert order.market_id == "tok_001"
    mock_clob.create_and_post_order.assert_called_once()

def test_pasang_order_live_exception_returns_none(fake_clob_types):
    client = ClobClient()
    client._terhubung = True
    mock_clob = MagicMock()
    mock_clob.create_and_post_order.side_effect = Exception("network error")
    client._client = mock_clob

    with patch("src.api.clob_client.config") as cfg:
        cfg.DRY_RUN = False
        result = client.pasang_order(SisiOrder.BELI, Decimal("0.65"), Decimal("10.0"), "tok_001")
    assert result is None


# ==============================================================================
# get_balance
# ==============================================================================

def test_get_balance_not_connected_returns_zero():
    client = ClobClient()
    client._terhubung = False
    assert client.get_balance() == 0.0

def test_get_balance_no_client_returns_zero():
    client = ClobClient()
    client._terhubung = True
    client._client = None
    assert client.get_balance() == 0.0

def test_get_balance_exception_returns_zero(fake_clob_types):
    client = ClobClient()
    client._terhubung = True
    mock_clob = MagicMock()
    mock_clob.get_balance_allowance.side_effect = Exception("timeout")
    client._client = mock_clob
    assert client.get_balance() == 0.0

def test_get_balance_returns_float_from_dict(fake_clob_types):
    client = ClobClient()
    client._terhubung = True
    mock_clob = MagicMock()
    mock_clob.get_balance_allowance.return_value = {"balance": "120.50"}
    client._client = mock_clob
    assert client.get_balance() == pytest.approx(120.50)

def test_get_balance_returns_float_direct(fake_clob_types):
    client = ClobClient()
    client._terhubung = True
    mock_clob = MagicMock()
    mock_clob.get_balance_allowance.return_value = 75.0
    client._client = mock_clob
    assert client.get_balance() == pytest.approx(75.0)


# ==============================================================================
# ambil_snapshot
# ==============================================================================

def test_ambil_snapshot_none_token_id_returns_none():
    assert ClobClient().ambil_snapshot(None) is None

def test_ambil_snapshot_empty_string_token_id_returns_none():
    assert ClobClient().ambil_snapshot("") is None

def test_ambil_snapshot_not_connected_returns_none():
    client = ClobClient()
    client._terhubung = False
    assert client.ambil_snapshot("tok_001") is None

def test_ambil_snapshot_empty_bids_returns_none():
    client = ClobClient()
    client._terhubung = True
    mock_clob = MagicMock()
    book = MagicMock()
    book.bids = []
    book.asks = [MagicMock()]
    mock_clob.get_order_book.return_value = book
    client._client = mock_clob
    assert client.ambil_snapshot("tok_001") is None

def test_ambil_snapshot_empty_asks_returns_none():
    client = ClobClient()
    client._terhubung = True
    mock_clob = MagicMock()
    book = MagicMock()
    book.bids = [MagicMock()]
    book.asks = []
    mock_clob.get_order_book.return_value = book
    client._client = mock_clob
    assert client.ambil_snapshot("tok_001") is None

def test_ambil_snapshot_returns_correct_prices():
    client = ClobClient()
    client._terhubung = True
    mock_clob = MagicMock()

    bid = MagicMock()
    bid.price = "0.64"
    bid.size = "500"

    ask = MagicMock()
    ask.price = "0.66"
    ask.size = "300"

    book = MagicMock()
    book.bids = [bid]
    book.asks = [ask]
    mock_clob.get_order_book.return_value = book
    client._client = mock_clob

    snapshot = client.ambil_snapshot("tok_001")
    assert snapshot is not None
    assert snapshot.best_bid == Decimal("0.64")
    assert snapshot.best_ask == Decimal("0.66")
    assert snapshot.market_id == "tok_001"

def test_ambil_snapshot_exception_returns_none():
    client = ClobClient()
    client._terhubung = True
    mock_clob = MagicMock()
    mock_clob.get_order_book.side_effect = Exception("API error")
    client._client = mock_clob
    assert client.ambil_snapshot("tok_001") is None
