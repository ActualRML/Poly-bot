from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.scout.context import ScoutContext
from src.scout.filters.signal import DogeMultiTfConfirmFilter


def _make_ctx(symbol="DOGE", buy_outcome="Up", sym_mtf=None, **overrides):
    now = datetime.now(timezone.utc)
    base = dict(
        market                = {"_symbol": symbol},
        symbol                = symbol,
        condition_id          = "0xdoge",
        question              = f"{symbol} Up or Down?",
        market_price_up       = 0.50,
        start_date            = now - timedelta(minutes=15),
        end_date              = now + timedelta(minutes=30),
        delta_sec             = 30 * 60.0,
        sym_mtf               = sym_mtf,
        vol_annual            = 1.00,
        vol_data              = {"DOGE": 1.00, "DEFAULT": 0.40},
        market_regime         = None,
        btc_scalp             = None,
        market_session        = "US_MAIN",
        btc_mtf               = None,
        closed_this_cycle     = set(),
        profit_locked_markets = {},
        slot_open_count       = 0,
        slot_history_count    = 0,
        session               = None,
        capital               = 120.0,
    )
    base.update(overrides)
    ctx = ScoutContext(**base)
    ctx.buy_outcome = buy_outcome
    return ctx


_FILTER = DogeMultiTfConfirmFilter()


def _mtf(m5, m15, m30):
    return {"m_5m": m5, "m_15m": m15, "m_30m": m30, "vol_ratio": 1.0}


def test_doge_up_all_aligned_strong_passes():
    ctx = _make_ctx("DOGE", "Up", _mtf(0.0023, 0.0040, 0.0025))
    r = _FILTER.evaluate(ctx)
    assert r.passed
    assert "confirmed" in r.reason


def test_doge_up_m5m_too_weak_fails():
    # id5-like: m5m +0.04% < 0.08% floor
    ctx = _make_ctx("DOGE", "Up", _mtf(0.00038, 0.00345, 0.00316))
    r = _FILTER.evaluate(ctx)
    assert not r.passed
    assert "m5m" in r.reason


def test_doge_up_m30m_opposed_fails():
    # id17-like: m30m -0.28% opposes an Up bet
    ctx = _make_ctx("DOGE", "Up", _mtf(0.00174, 0.00145, -0.0028))
    r = _FILTER.evaluate(ctx)
    assert not r.passed
    assert "m30m" in r.reason


def test_doge_up_m5m_and_m30m_weak_fails():
    # id27-like: m5m +0.06% and m30m +0.08%-ish, both below floor
    ctx = _make_ctx("DOGE", "Up", _mtf(0.00057, 0.00323, 0.00076))
    r = _FILTER.evaluate(ctx)
    assert not r.passed
    assert "m5m" in r.reason  # m5m checked first


def test_doge_down_all_aligned_strong_passes():
    ctx = _make_ctx("DOGE", "Down", _mtf(-0.0018, -0.0024, -0.0020))
    r = _FILTER.evaluate(ctx)
    assert r.passed


def test_btc_lone_spike_passes_not_doge():
    # BTC with a lone 15m spike — filter must skip non-DOGE symbols
    ctx = _make_ctx("BTC", "Up", _mtf(0.00001, 0.0030, 0.00001))
    r = _FILTER.evaluate(ctx)
    assert r.passed
    assert r.reason == "not_doge_skip"


def test_doge_no_mtf_data_fails():
    ctx = _make_ctx("DOGE", "Up", sym_mtf=None)
    r = _FILTER.evaluate(ctx)
    assert not r.passed
    assert r.reason == "no_mtf_data"


def test_doge_threshold_boundary_exactly_at_floor_passes():
    # Exactly 0.0008 on both — >= floor, should pass
    ctx = _make_ctx("DOGE", "Up", _mtf(0.0008, 0.0030, 0.0008))
    r = _FILTER.evaluate(ctx)
    assert r.passed
