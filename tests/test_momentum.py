"""momentum: the plug-and-play proof strategy.

momentum is a MIRROR of contrarian with the OPPOSITE side selection — same
triggers (extreme zone + low_vol), same guards, same debounce, same entry-cost
math — but it FOLLOWS the market (buys the expensive favorite) instead of fading
it (buying the cheap underdog). These tests pin (a) the mirror is correct, (b)
its params match contrarian's, and (c) it loads + registers through the existing
machinery with zero plumbing. They touch ONLY momentum; contrarian's own tests
in test_portfolio_void.py are left byte-for-byte unchanged.
"""
from datetime import datetime, timezone

import pytest

from src.execute.decision import Action
from src.strategy.params import StrategyParams


def _snap(price_zone, vol_regime, *, price, market_id="0xMKT"):
    """A polymarket snapshot pre-tagged with zone/vol (as the orchestrator would
    tag it before dispatch), with every field momentum.evaluate() guards on set."""
    from src.data.snapshot import MarketSnapshot

    return MarketSnapshot(
        ts=datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc),
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
    )


async def test_momentum_extreme_low_buys_no():
    """extreme_low + low_vol: contrarian buys YES (cheap); momentum buys the
    opposite, NO — the expensive favorite. entry = 1 - yes_price (the true cost)."""
    from src.strategy.momentum import Plugin

    d = await Plugin().evaluate(_snap("extreme_low", "low_vol", price=0.05))
    assert d.action is Action.BUY
    assert d.side == "NO"                       # opposite of contrarian's "YES"
    assert d.price == pytest.approx(0.95)       # NO costs 1 - 0.05 -> expensive side


async def test_momentum_extreme_high_buys_yes():
    """extreme_high + low_vol: contrarian buys NO (cheap); momentum buys YES."""
    from src.strategy.momentum import Plugin

    d = await Plugin().evaluate(_snap("extreme_high", "low_vol", price=0.95))
    assert d.action is Action.BUY
    assert d.side == "YES"                       # opposite of contrarian's "NO"
    assert d.price == pytest.approx(0.95)        # YES costs the yes-price -> expensive


async def test_momentum_is_exact_opposite_of_contrarian():
    """The defining property of the mirror: same snapshot, opposite side."""
    from src.strategy.contrarian import Plugin as Contrarian
    from src.strategy.momentum import Plugin as Momentum

    snap = _snap("extreme_low", "low_vol", price=0.05)
    c = await Contrarian().evaluate(snap)   # separate instances -> independent debounce
    m = await Momentum().evaluate(snap)
    assert c.side == "YES" and m.side == "NO"


async def test_momentum_skips_non_polymarket():
    """Guard parity with contrarian: a non-polymarket snapshot is skipped."""
    from src.strategy.momentum import Plugin

    snap = _snap("extreme_low", "low_vol", price=0.05)
    snap.source = "binance"
    assert (await Plugin().evaluate(snap)).action is Action.SKIP


async def test_momentum_skips_outside_signal_zone():
    """Guard parity: an extreme zone under high_vol is NOT a signal -> skip."""
    from src.strategy.momentum import Plugin

    assert (await Plugin().evaluate(_snap("extreme_low", "high_vol", price=0.05))).action is Action.SKIP


def test_momentum_params_pinned():
    """momentum declares the SAME knobs as contrarian (0.15 / 0.02) for parity.
    It buys the expensive side, so the floor never binds in practice; it's pinned
    here to match contrarian and to trip if the Plugin's params line drifts."""
    from src.strategy.momentum import Plugin

    p = Plugin().params
    assert isinstance(p, StrategyParams)
    assert p.entry_floor == 0.15
    assert p.bet_fraction == 0.02


def test_active_strategies_order_contrarian_first(monkeypatch, tmp_path):
    """Activation order parses with contrarian FIRST — load-bearing, since the
    first-dispatched strategy wins is_held collisions on shared markets."""
    monkeypatch.chdir(tmp_path)  # clean cwd so the real .env.local can't leak in
    for k in ("PK_PRIVATE_KEY", "CLOB_API_KEY", "CLOB_SECRET", "CLOB_PASS"):
        monkeypatch.setenv(k, "t")
    monkeypatch.setenv("ACTIVE_STRATEGIES", "contrarian,momentum")

    from src.config import Settings

    assert Settings().active_strategies == ["contrarian", "momentum"]


def test_registry_resolves_momentum_params_via_existing_loader():
    """The plug-and-play claim, exercised end-to-end: _load_strategies imports +
    instantiates momentum, and the registry main.py builds ({s.name: s.params})
    resolves momentum's params with zero extra plumbing."""
    from src.main import _load_strategies

    strategies = _load_strategies(["contrarian", "momentum"])
    assert [s.name for s in strategies] == ["contrarian", "momentum"]  # order preserved

    registry = {s.name: s.params for s in strategies}  # mirrors src/main.py:325
    assert registry["momentum"].entry_floor == 0.15
    assert registry["momentum"].bet_fraction == 0.02
