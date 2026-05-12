"""Tests for src/utils/telegram_alert.py — alert method return values."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from src.utils.telegram_alert import TelegramAlert

_async = pytest.mark.asyncio(loop_scope="function")


def _make_session(status=200):
    resp = AsyncMock()
    resp.status = status
    resp.text = AsyncMock(return_value="")
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.post = MagicMock(return_value=ctx)
    return session


@_async
async def test_alert_exit_returns_true_on_success():
    result = await TelegramAlert("tok", "123").alert_exit(
        "Q?", "Up", 0.5, 0.8, 1.5, "TP", _make_session(200)
    )
    assert result is True


@_async
async def test_alert_exit_returns_false_on_http_error():
    result = await TelegramAlert("tok", "123").alert_exit(
        "Q?", "Up", 0.5, 0.8, 1.5, "TP", _make_session(400)
    )
    assert result is False


@_async
async def test_alert_circuit_breaker_returns_true_on_success():
    result = await TelegramAlert("tok", "123").alert_circuit_breaker(
        "drawdown", 25.0, _make_session(200)
    )
    assert result is True


@_async
async def test_alert_daily_summary_returns_true_on_success():
    result = await TelegramAlert("tok", "123").alert_daily_summary(
        120.0, 2, 5, 10.0, 60.0, _make_session(200)
    )
    assert result is True


@_async
async def test_alert_error_returns_true_on_success():
    result = await TelegramAlert("tok", "123").alert_error(
        "something failed", _make_session(200)
    )
    assert result is True


@_async
async def test_all_alerts_return_false_when_disabled():
    a = TelegramAlert("", "")
    s = _make_session(200)
    assert await a.alert_exit("Q?", "Up", 0.5, 0.8, 1.5, "TP", s) is False
    assert await a.alert_circuit_breaker("r", 10.0, s) is False
    assert await a.alert_daily_summary(100.0, 0, 0, 0.0, 0.0, s) is False
    assert await a.alert_error("e", s) is False
