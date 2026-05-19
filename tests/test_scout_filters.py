from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.scout.context import ScoutContext
from src.scout.result import FilterResult, ScoutDecision
from src.scout.filters.precheck import (
    AlreadyClosedFilter, ProfitLockedFilter, CandleOpenDelayFilter,
    MinTimeFloorFilter, EventHorizonTierFilter, MomentumDataAvailableFilter,
)
from src.scout.filters.signal import (
    MinMomentumFilter, MaxMomentumCapFilter, VolumeRatioFilter,
    PriceStagnationFilter, DirectionalDecisionFilter, ConsensusFloorFilter,
    BtcCorrelationFilter, PriceBandFilter,
)
from src.scout.filters.risk import (
    SlotOpenCapFilter, SlotCumulativeCapFilter, SymbolBlacklistFilter,
)
from src.scout.cycle import ScoutCycleGate


def _make_ctx(**overrides):
    now = datetime.now(timezone.utc)
    end = now + timedelta(minutes=30)
    base = dict(
        market                = {"_symbol": "BTC", "conditionId": "0xabc", "volume": 5000.0},
        symbol                = "BTC",
        condition_id          = "0xabc",
        question              = "Will BTC be Up or Down?",
        market_price_up       = 0.50,
        start_date            = now - timedelta(minutes=15),
        end_date              = end,
        delta_sec             = 30 * 60.0,
        sym_mtf               = {"m_5m": 0.002, "m_15m": 0.003, "m_30m": 0.001,
                                 "vol_ratio": 1.0, "direction": "up", "all_tf_aligned": True},
        vol_annual            = 0.40,
        vol_data              = {"BTC": 0.40, "DEFAULT": 0.40},
        market_regime         = {"vol_state": "NORMAL", "direction": "up"},
        btc_scalp             = None,
        market_session        = "US_MAIN",
        btc_mtf               = {"m_15m": 0.003},
        closed_this_cycle     = set(),
        profit_locked_markets = {},
        slot_open_count       = 0,
        slot_history_count    = 0,
        session               = None,
        capital               = 120.0,
    )
    base.update(overrides)
    return ScoutContext(**base)


def test_filter_result_helpers():
    p = FilterResult.pass_("ok", value=42)
    f = FilterResult.fail("nope")
    assert p.passed and p.value == 42 and p.reason == "ok"
    assert not f.passed and f.reason == "nope"


def test_scout_decision_add_tracks_score():
    d = ScoutDecision()
    d.add("a", FilterResult.pass_("ok"))
    d.add("b", FilterResult.fail("bad"))
    assert d.score == 1 and d.max_score == 2
    assert d.reasons_passed == ["a"]
    assert any("b: bad" in r for r in d.reasons_failed)


def test_summary_uses_parens_not_brackets():
    d = ScoutDecision()
    d.add("x", FilterResult.fail("some reason"))
    s = d.summary()
    assert "failed=(x: some reason)" in s
    assert "[" not in s


def test_summary_all_pass_shows_dash():
    d = ScoutDecision()
    d.add("x", FilterResult.pass_("ok"))
    d.enter = True
    s = d.summary()
    assert "failed=(-)" in s
    assert "enter=True" in s


def test_already_closed_pass_and_fail():
    ctx = _make_ctx()
    assert AlreadyClosedFilter().evaluate(ctx).passed

    ctx2 = _make_ctx(closed_this_cycle={"0xabc"})
    r = AlreadyClosedFilter().evaluate(ctx2)
    assert not r.passed and "closed this cycle" in r.reason


def test_profit_locked_blocks_reentry():
    ctx = _make_ctx(profit_locked_markets={"0xabc": "Up"})
    r = ProfitLockedFilter().evaluate(ctx)
    assert not r.passed and "profit locked" in r.reason


def test_candle_open_delay():
    now = datetime.now(timezone.utc)
    ctx_too_early = _make_ctx(start_date=now - timedelta(minutes=1))
    r = CandleOpenDelayFilter().evaluate(ctx_too_early)
    assert not r.passed

    ctx_ok = _make_ctx(start_date=now - timedelta(minutes=20))
    assert CandleOpenDelayFilter().evaluate(ctx_ok).passed


def test_min_time_floor():
    ctx_close = _make_ctx(delta_sec=5 * 60)
    r = MinTimeFloorFilter().evaluate(ctx_close)
    assert not r.passed and "floor" in r.reason

    ctx_ok = _make_ctx(delta_sec=30 * 60)
    assert MinTimeFloorFilter().evaluate(ctx_ok).passed


def test_event_horizon_tier_stores_on_ctx():
    ctx = _make_ctx(delta_sec=40 * 60)
    r = EventHorizonTierFilter().evaluate(ctx)
    assert r.passed
    assert ctx.event_horizon is not None
    assert ctx.event_horizon["tier"] == "WIDE"


def test_momentum_data_available_missing():
    ctx = _make_ctx(sym_mtf=None, binance_full_pause=False)
    r = MomentumDataAvailableFilter().evaluate(ctx)
    assert not r.passed and "no momentum data" in r.reason

    ctx_pause = _make_ctx(sym_mtf=None, binance_full_pause=True)
    r2 = MomentumDataAvailableFilter().evaluate(ctx_pause)
    assert not r2.passed and "FULL_PAUSE" in r2.reason

    ctx_ok = _make_ctx()
    assert MomentumDataAvailableFilter().evaluate(ctx_ok).passed


def test_min_momentum_below_threshold():
    ctx = _make_ctx(sym_mtf={"m_5m": 0.0001, "m_15m": 0.0001, "m_30m": 0.0001, "vol_ratio": 1.0})
    r = MinMomentumFilter().evaluate(ctx)
    assert not r.passed

    ctx_ok = _make_ctx(sym_mtf={"m_5m": 0.005, "m_15m": 0.005, "m_30m": 0.005, "vol_ratio": 1.0})
    assert MinMomentumFilter().evaluate(ctx_ok).passed


def test_max_momentum_cap_filter_disabled_passes():
    ctx = _make_ctx()
    r = MaxMomentumCapFilter().evaluate(ctx)
    assert r.passed and "filter_disabled" in r.reason


def test_volume_ratio_filter():
    ctx_low = _make_ctx(sym_mtf={"m_5m": 0.0, "m_15m": 0.0, "m_30m": 0.0, "vol_ratio": 0.1})
    r = VolumeRatioFilter().evaluate(ctx_low)
    assert not r.passed and "low conviction" in r.reason

    ctx_ok = _make_ctx()
    assert VolumeRatioFilter().evaluate(ctx_ok).passed


def test_price_stagnation_no_history():
    ctx = _make_ctx(condition_id="0xnew_no_history")
    r = PriceStagnationFilter().evaluate(ctx)
    assert r.passed


def test_directional_decision_sets_outcome():
    ctx = _make_ctx(market_price_up=0.4, sym_mtf={"m_5m": 0.0, "m_15m": 0.005, "m_30m": 0.0, "vol_ratio": 1.0})
    r = DirectionalDecisionFilter().evaluate(ctx)
    assert r.passed
    assert ctx.buy_outcome == "Up" and ctx.buy_price == 0.4

    ctx2 = _make_ctx(market_price_up=0.4, sym_mtf={"m_5m": 0.0, "m_15m": -0.005, "m_30m": 0.0, "vol_ratio": 1.0})
    DirectionalDecisionFilter().evaluate(ctx2)
    assert ctx2.buy_outcome == "Down" and abs(ctx2.buy_price - 0.6) < 1e-9


def test_directional_missing_data():
    ctx = _make_ctx(sym_mtf=None)
    r = DirectionalDecisionFilter().evaluate(ctx)
    assert not r.passed


def test_consensus_floor_blocks_down_at_high_market():
    ctx = _make_ctx(buy_outcome="Down", market_price_up=0.95)
    r = ConsensusFloorFilter().evaluate(ctx)
    assert not r.passed and "consensus Up" in r.reason

    ctx2 = _make_ctx(buy_outcome="Up", market_price_up=0.05)
    r2 = ConsensusFloorFilter().evaluate(ctx2)
    assert not r2.passed and "consensus Down" in r2.reason

    ctx3 = _make_ctx(buy_outcome="Up", market_price_up=0.55)
    assert ConsensusFloorFilter().evaluate(ctx3).passed


def test_btc_correlation_skips_for_btc():
    ctx = _make_ctx(symbol="BTC")
    r = BtcCorrelationFilter().evaluate(ctx)
    assert r.passed

    ctx_opp = _make_ctx(symbol="ETH", buy_outcome="Up", btc_mtf={"m_15m": -0.01})
    r2 = BtcCorrelationFilter().evaluate(ctx_opp)
    assert not r2.passed and "opposes" in r2.reason

    ctx_no_btc = _make_ctx(symbol="ETH", buy_outcome="Up", btc_mtf=None)
    assert BtcCorrelationFilter().evaluate(ctx_no_btc).passed


def test_price_band_filter():
    ctx_too_high = _make_ctx(buy_price=0.99)
    r = PriceBandFilter().evaluate(ctx_too_high)
    assert not r.passed

    ctx_too_low = _make_ctx(buy_price=0.01)
    r2 = PriceBandFilter().evaluate(ctx_too_low)
    assert not r2.passed

    ctx_ok = _make_ctx(buy_price=0.40)
    assert PriceBandFilter().evaluate(ctx_ok).passed


def test_slot_open_cap():
    from src.utils.config import config
    cap = config.MAX_POSITIONS_PER_SLOT
    ctx_full = _make_ctx(slot_open_count=cap)
    r = SlotOpenCapFilter().evaluate(ctx_full)
    assert not r.passed
    ctx_ok = _make_ctx(slot_open_count=0)
    assert SlotOpenCapFilter().evaluate(ctx_ok).passed


def test_slot_cumulative_cap():
    ctx_full = _make_ctx(slot_history_count=999)
    r = SlotCumulativeCapFilter().evaluate(ctx_full)
    assert not r.passed
    ctx_ok = _make_ctx(slot_history_count=0)
    assert SlotCumulativeCapFilter().evaluate(ctx_ok).passed


def test_symbol_blacklist():
    with patch("src.risk.blacklist.check_symbol_blacklist", return_value=True):
        ctx = _make_ctx(symbol="DOGE")
        r = SymbolBlacklistFilter().evaluate(ctx)
        assert not r.passed and "blacklisted" in r.reason


def test_cycle_gate_flash_crash():
    decision = ScoutCycleGate.evaluate(
        market_regime={"vol_state": "EXTREME_HIGH", "vol_annual": 1.5},
        breaker=None, manager=None, btc_vol=1.5,
    )
    assert not decision.enter_allowed
    assert "FLASH_CRASH" in decision.reason


def test_cycle_gate_normal_allows():
    decision = ScoutCycleGate.evaluate(
        market_regime={"vol_state": "NORMAL", "skip_contrarian": False, "direction": "up"},
        breaker=None, manager=None, btc_vol=0.40,
    )
    assert decision.enter_allowed


def test_cycle_gate_macro_skip_when_gate_on(monkeypatch):
    monkeypatch.setattr("src.utils.config.config.UPDOWN_HOURLY_MACRO_TREND_GATE", True)
    decision = ScoutCycleGate.evaluate(
        market_regime={"vol_state": "NORMAL", "skip_contrarian": True,
                       "regime": "TRENDING_LOW_VOL", "trend_score": 4},
        breaker=None, manager=None, btc_vol=0.40,
    )
    assert not decision.enter_allowed
    assert "macro regime" in decision.reason


@pytest.mark.asyncio
async def test_scout_context_build_handles_missing_symbol():
    market = {"conditionId": "0xabc"}  # no _symbol
    ctx = await ScoutContext.build(
        market=market, session=None, capital=100.0,
        vol_data={}, symbol_momentum_map={}, market_regime=None,
        btc_scalp=None, market_session="US_MAIN",
        closed_this_cycle=set(), profit_locked_markets={},
    )
    assert ctx is None


@pytest.mark.asyncio
async def test_scout_context_build_handles_bad_dates():
    market = {
        "_symbol": "BTC", "conditionId": "0xabc",
        "outcomes": '["Up", "Down"]', "outcomePrices": '["0.5", "0.5"]',
        "endDate": "not-a-date", "_start_date": "not-a-date",
    }
    ctx = await ScoutContext.build(
        market=market, session=None, capital=100.0,
        vol_data={}, symbol_momentum_map={}, market_regime=None,
        btc_scalp=None, market_session="US_MAIN",
        closed_this_cycle=set(), profit_locked_markets={},
    )
    assert ctx is None
