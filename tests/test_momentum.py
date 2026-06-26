"""momentum: the follow-the-EXTREME-favorite canary (mirror of contrarian).

momentum triggers on the EXTREME zones in low_vol and buys the FAVORITE (the
expensive side): extreme_high -> buy YES, extreme_low -> buy NO. It is the exact
opposite bet of contrarian on the SAME trigger (contrarian buys the longshot).
These tests pin the extreme-favorite triggers + entry-cost math, that non-extreme
and non-low_vol zones SKIP, that it has NO contrarian-style runway gate (follow
wins late), that its params raise entry_ceiling so a ~0.80 favorite can fill, and
that it exports the Plugin the loader looks up. They touch ONLY momentum;
contrarian's tests are left unchanged.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.execute.decision import Action
from src.strategy.params import StrategyParams


def _snap(price_zone, vol_regime, *, price, market_id="0xMKT", ts=None, resolve_time=None):
    """A polymarket snapshot pre-tagged with zone/vol (as the orchestrator would
    tag it before dispatch), with every field momentum.evaluate() guards on set."""
    from src.data.snapshot import MarketSnapshot

    return MarketSnapshot(
        ts=ts or datetime(2026, 6, 18, 12, 0, 0, tzinfo=timezone.utc),
        source="polymarket",
        event_type="book",
        symbol="BTC",
        market_id=market_id,
        asset_id="tok-1",
        price=price,
        best_bid=None,
        best_ask=None,
        outcome="YES",
        vol_regime=vol_regime,
        price_zone=price_zone,
        resolve_time=resolve_time,
    )


async def test_extreme_high_low_vol_buys_yes_favorite():
    from src.strategy.momentum import Plugin

    d = await Plugin().evaluate(_snap("extreme_high", "low_vol", price=0.82))
    assert d.action is Action.BUY
    assert d.side == "YES"
    assert d.price == pytest.approx(0.82)  # favorite cost = the YES price itself


async def test_extreme_low_low_vol_buys_no_favorite():
    from src.strategy.momentum import Plugin

    d = await Plugin().evaluate(_snap("extreme_low", "low_vol", price=0.18))
    assert d.action is Action.BUY
    assert d.side == "NO"
    assert d.price == pytest.approx(0.82)  # NO favorite cost = 1 - 0.18


async def test_is_mirror_of_contrarian_side():
    """Same trigger, OPPOSITE side: contrarian buys the longshot, momentum the
    favorite. This is the whole point of the canary, so pin it explicitly."""
    from src.strategy.contrarian import Plugin as Contra
    from src.strategy.momentum import Plugin as Mom

    snap = _snap("extreme_low", "low_vol", price=0.18,
                 resolve_time=datetime(2026, 6, 18, 13, 0, 0, tzinfo=timezone.utc))
    cd = await Contra().evaluate(snap)
    md = await Mom().evaluate(snap)
    assert cd.side == "YES" and md.side == "NO"  # opposite sides, same market


async def test_moderate_and_uncertain_zones_skip():
    # momentum trades EXTREMES only; the moderate band is the dead -$187 design.
    from src.strategy.momentum import Plugin

    for z in ("high", "low", "uncertain"):
        d = await Plugin().evaluate(_snap(z, "low_vol", price=0.70))
        assert d.action is Action.SKIP, z


async def test_non_low_vol_skips():
    from src.strategy.momentum import Plugin

    for v in ("mid_vol", "high_vol", "unknown"):
        d = await Plugin().evaluate(_snap("extreme_high", v, price=0.85))
        assert d.action is Action.SKIP, v


async def test_no_runway_gate_follow_wins_late():
    """Unlike contrarian, momentum must STILL fire with little time left (a
    follow-the-favorite bet wins late). Same near-lock snapshot: contrarian SKIPs
    on its runway gate, momentum BUYs."""
    from src.strategy.contrarian import Plugin as Contra
    from src.strategy.momentum import Plugin as Mom

    ts = datetime(2026, 6, 18, 12, 0, 0, tzinfo=timezone.utc)
    near_lock = _snap("extreme_high", "low_vol", price=0.85, ts=ts,
                      resolve_time=ts + timedelta(seconds=120))  # 2 min left
    assert (await Contra().evaluate(near_lock)).action is Action.SKIP   # runway gate
    assert (await Mom().evaluate(near_lock)).action is Action.BUY       # no gate


async def test_debounce_same_market_same_ts():
    from src.strategy.momentum import Plugin

    p = Plugin()
    s = _snap("extreme_high", "low_vol", price=0.85)
    assert (await p.evaluate(s)).action is Action.BUY
    assert (await p.evaluate(s)).action is Action.SKIP  # within debounce window


def test_params_raise_ceiling_for_favorites():
    from src.strategy.momentum import Plugin

    assert Plugin.params == StrategyParams(
        entry_floor=0.50, bet_fraction=0.02, entry_ceiling=0.85
    )
    # ceiling MUST clear a ~0.80 favorite or every favorite fill is rejected.
    assert Plugin.params.entry_ceiling >= 0.80


def test_exports_plugin_for_loader():
    """_load_strategies(['momentum']) does importlib + getattr(module,'Plugin')."""
    import importlib

    mod = importlib.import_module("src.strategy.momentum")
    assert mod.Plugin().name == "momentum"
