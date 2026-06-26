"""contrarian_mv: the MID-VOL fade canary (BTC/ETH), a one-variable mirror of
contrarian (mid_vol instead of low_vol). These tests pin the mid_vol extreme
triggers + entry-cost math, that it FADES (same side as contrarian, the OPPOSITE
of momentum) and KEEPS the reversion-runway gate, that non-mid_vol / non-extreme
zones SKIP, that the BTC/ETH coin scope filters thin coins (and that SYMBOLS=None
opens it up for research), the params, and that it exports the Plugin the loader
looks up. They touch ONLY contrarian_mv.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.execute.decision import Action
from src.strategy.params import StrategyParams


def _snap(price_zone, vol_regime, *, price, symbol="BTC", market_id="0xMKT",
          ts=None, resolve_time=None):
    """A polymarket snapshot pre-tagged with zone/vol (as the orchestrator would
    tag it before dispatch), with every field contrarian_mv.evaluate() guards on."""
    from src.data.snapshot import MarketSnapshot

    return MarketSnapshot(
        ts=ts or datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc),
        source="polymarket",
        event_type="book",
        symbol=symbol,
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


async def test_extreme_low_mid_vol_buys_yes_longshot():
    from src.strategy.contrarian_mv import Plugin

    d = await Plugin().evaluate(_snap("extreme_low", "mid_vol", price=0.18))
    assert d.action is Action.BUY
    assert d.side == "YES"
    assert d.price == pytest.approx(0.18)  # longshot cost = the YES price itself


async def test_extreme_high_mid_vol_buys_no_longshot():
    from src.strategy.contrarian_mv import Plugin

    d = await Plugin().evaluate(_snap("extreme_high", "mid_vol", price=0.82))
    assert d.action is Action.BUY
    assert d.side == "NO"
    assert d.price == pytest.approx(0.18)  # NO longshot cost = 1 - 0.82


async def test_fades_same_side_as_contrarian():
    """MV is the mid_vol twin of contrarian: SAME side on the same extreme (both
    fade / buy the longshot) — the OPPOSITE of the momentum favorite canary."""
    from src.strategy.contrarian import Plugin as Contra
    from src.strategy.contrarian_mv import Plugin as MV

    rt = datetime(2026, 6, 14, 13, 0, 0, tzinfo=timezone.utc)  # 1h runway
    cd = await Contra().evaluate(_snap("extreme_low", "low_vol", price=0.18, resolve_time=rt))
    md = await MV().evaluate(_snap("extreme_low", "mid_vol", price=0.18, resolve_time=rt))
    assert cd.side == md.side == "YES"


async def test_non_mid_vol_skips():
    # the WHOLE point: MV is the mid_vol twin — low_vol is contrarian's, not MV's.
    from src.strategy.contrarian_mv import Plugin

    for v in ("low_vol", "high_vol", "unknown"):
        d = await Plugin().evaluate(_snap("extreme_low", v, price=0.18))
        assert d.action is Action.SKIP, v


async def test_non_extreme_zones_skip():
    from src.strategy.contrarian_mv import Plugin

    for z in ("low", "uncertain", "high"):
        d = await Plugin().evaluate(_snap(z, "mid_vol", price=0.50))
        assert d.action is Action.SKIP, z


async def test_coin_scope_btc_eth_only():
    """LIVE canary scope: only BTC/ETH trade; thin coins SKIP (mechanically dead)."""
    from src.strategy.contrarian_mv import Plugin

    for sym in ("BTC", "ETH"):
        d = await Plugin().evaluate(_snap("extreme_low", "mid_vol", price=0.18, symbol=sym))
        assert d.action is Action.BUY, sym
    for sym in ("SOL", "XRP", "BNB", "DOGE"):
        d = await Plugin().evaluate(_snap("extreme_low", "mid_vol", price=0.18, symbol=sym))
        assert d.action is Action.SKIP, sym


async def test_symbols_none_opens_all_coins_for_research(monkeypatch):
    """Research override: SYMBOLS=None disables the coin filter (offline probes)."""
    import src.strategy.contrarian_mv as mv

    monkeypatch.setattr(mv, "SYMBOLS", None)
    d = await mv.Plugin().evaluate(_snap("extreme_low", "mid_vol", price=0.18, symbol="XRP"))
    assert d.action is Action.BUY


async def test_runway_gate_skips_near_lock():
    """Like contrarian (a fade needs runway), MV SKIPs with ≤10min left — UNLIKE
    momentum, which fires late."""
    from src.strategy.contrarian_mv import Plugin

    ts = datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc)
    near = _snap("extreme_low", "mid_vol", price=0.18, ts=ts,
                 resolve_time=ts + timedelta(seconds=120))  # 2 min left
    assert (await Plugin().evaluate(near)).action is Action.SKIP


async def test_runway_ok_with_enough_time():
    from src.strategy.contrarian_mv import Plugin

    ts = datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc)
    early = _snap("extreme_low", "mid_vol", price=0.18, ts=ts,
                  resolve_time=ts + timedelta(seconds=1800))  # 30 min left
    assert (await Plugin().evaluate(early)).action is Action.BUY


async def test_debounce_same_market_same_ts():
    from src.strategy.contrarian_mv import Plugin

    p = Plugin()
    s = _snap("extreme_low", "mid_vol", price=0.18)
    assert (await p.evaluate(s)).action is Action.BUY
    assert (await p.evaluate(s)).action is Action.SKIP  # within debounce window


def test_params_match_contrarian():
    """One-variable mirror: identical risk knobs to contrarian (only vol gate differs)."""
    from src.strategy.contrarian import Plugin as Contra
    from src.strategy.contrarian_mv import Plugin as MV

    assert MV.params == StrategyParams(entry_floor=0.15, bet_fraction=0.02, entry_ceiling=0.30)
    assert MV.params == Contra.params


def test_exports_plugin_for_loader():
    """_load_strategies(['contrarian_mv']) does importlib + getattr(module,'Plugin')."""
    import importlib

    mod = importlib.import_module("src.strategy.contrarian_mv")
    assert mod.Plugin().name == "contrarian_mv"
