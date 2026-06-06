"""momentum: the moderate-favorite, trend-following strategy.

momentum triggers on the MODERATE zones (price_zone "high" -> buy YES, "low" ->
buy NO), both requiring vol_regime "low_vol", and SKIPs the extreme zones. It is
no longer a mirror of contrarian: the two now trade DISJOINT zones - momentum the
moderate band, contrarian the extremes. These tests pin the high/low triggers and
entry-cost math, that momentum no longer mirrors contrarian, that its params match
the house knobs, and that it loads + registers through the existing machinery with
zero plumbing. They touch ONLY momentum; contrarian's own tests in
test_portfolio_void.py are left byte-for-byte unchanged.
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


async def test_momentum_low_zone_buys_no():
    """low zone + low_vol: NO is the favorite (YES 0.20-0.40), so momentum buys
    NO. entry = 1 - yes_price (the true cost), landing in the moderate band."""
    from src.strategy.momentum import Plugin

    d = await Plugin().evaluate(_snap("low", "low_vol", price=0.30))
    assert d.action is Action.BUY
    assert d.side == "NO"                       # NO is the favorite in the low zone
    assert d.price == pytest.approx(0.70)       # NO costs 1 - 0.30


async def test_momentum_high_zone_buys_yes():
    """high zone + low_vol: YES is the favorite (0.60-0.80), so momentum buys
    YES at the yes-price."""
    from src.strategy.momentum import Plugin

    d = await Plugin().evaluate(_snap("high", "low_vol", price=0.70))
    assert d.action is Action.BUY
    assert d.side == "YES"                       # YES is the favorite in the high zone
    assert d.price == pytest.approx(0.70)        # YES costs the yes-price


async def test_momentum_no_longer_mirrors_contrarian():
    """The OLD mirror invariant is gone: momentum and contrarian no longer
    trigger on the SAME zone. Momentum now trades the moderate (high/low) zones
    and SKIPs the extremes; contrarian still trades the extremes and SKIPs the
    moderate zones - disjoint triggers, not mirror images."""
    from src.strategy.contrarian import Plugin as Contrarian
    from src.strategy.momentum import Plugin as Momentum

    # Extreme zones: momentum now SKIPs; contrarian still acts.
    ex_high = _snap("extreme_high", "low_vol", price=0.95)
    assert (await Momentum().evaluate(ex_high)).action is Action.SKIP
    assert (await Contrarian().evaluate(ex_high)).action is Action.BUY

    ex_low = _snap("extreme_low", "low_vol", price=0.05)
    assert (await Momentum().evaluate(ex_low)).action is Action.SKIP
    assert (await Contrarian().evaluate(ex_low)).action is Action.BUY

    # Moderate zones: momentum acts; contrarian SKIPs.
    high = _snap("high", "low_vol", price=0.70)
    m_high = await Momentum().evaluate(high)
    assert m_high.action is Action.BUY and m_high.side == "YES"
    assert (await Contrarian().evaluate(high)).action is Action.SKIP

    low = _snap("low", "low_vol", price=0.30)
    m_low = await Momentum().evaluate(low)
    assert m_low.action is Action.BUY and m_low.side == "NO"
    assert (await Contrarian().evaluate(low)).action is Action.SKIP


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
    """Activation order parses with contrarian FIRST - load-bearing, since the
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
