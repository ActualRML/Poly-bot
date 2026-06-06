import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from src.api.polymarket_ws import PolymarketWSClient


class FakeWS:
    """Records frames the client sends; mimics the websockets send/close API."""

    def __init__(self):
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def close(self):
        self.closed = True


class FakePolyWS:
    """Stand-in for PolymarketWSClient that records resubscribe/add_assets."""

    def __init__(self, assets):
        self._assets = set(assets)
        self.resubscribed = None

    @property
    def assets(self):
        return set(self._assets)

    async def add_assets(self, token_ids):
        self._assets.update(token_ids)

    async def resubscribe(self, token_ids):
        self.resubscribed = set(token_ids)
        self._assets = set(token_ids)


# --- WS client: frame shapes -------------------------------------------------

async def test_on_connected_sends_one_full_set_frame():
    client = PolymarketWSClient("wss://example")
    client.set_assets(["a", "b", "c"])
    client._ws = FakeWS()
    await client._on_connected()
    sent = client._ws.sent
    assert len(sent) == 1
    assert sent[0]["type"] == "market"
    assert "operation" not in sent[0]
    assert set(sent[0]["assets_ids"]) == {"a", "b", "c"}


async def test_on_connected_empty_set_sends_nothing():
    client = PolymarketWSClient("wss://example")
    client._ws = FakeWS()
    await client._on_connected()
    assert client._ws.sent == []


async def test_add_assets_sends_operation_frame_for_new_only():
    client = PolymarketWSClient("wss://example")
    client.set_assets(["a", "b"])
    client._ws = FakeWS()
    await client.add_assets(["b", "c", "d"])  # "b" already subscribed
    sent = client._ws.sent
    assert len(sent) == 1
    assert sent[0]["operation"] == "subscribe"
    assert "type" not in sent[0]
    assert set(sent[0]["assets_ids"]) == {"c", "d"}
    assert client.assets == {"a", "b", "c", "d"}


async def test_add_assets_records_when_socket_down():
    client = PolymarketWSClient("wss://example")
    client.set_assets(["a"])
    await client.add_assets(["b"])  # _ws is None: record for next reconnect
    assert client.assets == {"a", "b"}


async def test_resubscribe_replaces_assets_and_closes_socket():
    client = PolymarketWSClient("wss://example")
    client.set_assets(["a", "b", "c"])
    client._ws = FakeWS()
    await client.resubscribe(["b", "c"])
    assert client.assets == {"b", "c"}
    assert client._ws.closed is True


# --- rediscovery_loop: prune trigger -----------------------------------------

def _stop_after_one_tick(monkeypatch, main):
    """Let the loop's first sleep pass, then break out on the second via
    CancelledError (a BaseException, so the loop's `except Exception` won't
    swallow it and re-loop)."""
    calls = {"n": 0}

    async def fake_sleep(_):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise asyncio.CancelledError()

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)


async def test_rediscovery_prune_resubscribes_only_live_tokens(monkeypatch):
    import src.main as main

    now = datetime.now(timezone.utc)
    market_meta = {
        "live": {"label": "BTC", "resolve_time": now + timedelta(hours=1),
                 "token_ids": ["live1", "live2"]},
        "dead": {"label": "ETH", "resolve_time": now - timedelta(hours=1),
                 "token_ids": ["dead1", "dead2"]},
    }
    poly_ws = FakePolyWS(["live1", "live2", "dead1", "dead2"])

    async def fake_discover(_settings):
        return []  # nothing new: exercise the prune branch only

    monkeypatch.setattr(main, "_discover_polymarket", fake_discover)
    monkeypatch.setattr(main, "PRUNE_THRESHOLD", 1)  # force "due" by size
    _stop_after_one_tick(monkeypatch, main)

    with pytest.raises(asyncio.CancelledError):
        await main.rediscovery_loop(None, poly_ws, {}, {}, market_meta)

    assert poly_ws.resubscribed == {"live1", "live2"}


async def test_rediscovery_no_prune_when_nothing_resolved(monkeypatch):
    import src.main as main

    now = datetime.now(timezone.utc)
    market_meta = {
        "live": {"label": "BTC", "resolve_time": now + timedelta(hours=1),
                 "token_ids": ["live1", "live2"]},
    }
    poly_ws = FakePolyWS(["live1", "live2"])

    async def fake_discover(_settings):
        return []

    monkeypatch.setattr(main, "_discover_polymarket", fake_discover)
    monkeypatch.setattr(main, "PRUNE_THRESHOLD", 1)  # "due", but nothing is dead
    _stop_after_one_tick(monkeypatch, main)

    with pytest.raises(asyncio.CancelledError):
        await main.rediscovery_loop(None, poly_ws, {}, {}, market_meta)

    assert poly_ws.resubscribed is None
