"""contrarian: the reversion-RUNWAY entry gate.

A fade bet (contrarian) only pays if the extreme price has TIME to revert before the
market locks. Entering in the final minutes fades a price that is extreme precisely
because the outcome is nearly settled — empirically a 0-win tail (sub-10-min entries
were 0/6, every one a full-stake loss). So contrarian now requires MORE than
MIN_RUNWAY_SECONDS of life left to ENTER, measured event-time (resolve_time -
snapshot.ts) so it holds in live AND replay.

These tests pin: a normal early signal still fires; a final-minutes signal is SKIPPED
(with a 'runway' reason); the gate is INERT when resolve_time is unknown (so feeds /
backtests that don't wire it are unaffected); and the boundary is exactly
MIN_RUNWAY_SECONDS (the `<=` is exclusive of entry). They touch ONLY the new gate —
contrarian's zone/param tests in test_portfolio_void.py are unchanged.
"""
from datetime import datetime, timedelta, timezone

from src.execute.decision import Action
from src.strategy.contrarian import MIN_RUNWAY_SECONDS, Plugin


def _snap(*, secs_left, price=0.05, zone="extreme_low", vol="low_vol"):
    """An extreme_low + low_vol contrarian BUY signal whose market resolves
    `secs_left` seconds after the snapshot's event time (None => resolve_time
    unknown, i.e. a feed that hasn't wired it)."""
    from src.data.snapshot import MarketSnapshot

    ts = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)
    return MarketSnapshot(
        ts=ts,
        source="polymarket",
        event_type="book",
        symbol="BTC",
        market_id="0xMKT",
        asset_id="tok-1",
        price=price,
        best_bid=None,
        best_ask=None,
        outcome="YES",
        vol_regime=vol,
        price_zone=zone,
        resolve_time=(ts + timedelta(seconds=secs_left)) if secs_left is not None else None,
    )


async def test_contrarian_enters_with_ample_runway():
    """A signal 45 min from resolution fires normally — well above the floor."""
    d = await Plugin().evaluate(_snap(secs_left=45 * 60))
    assert d.action is Action.BUY
    assert d.side == "YES"


async def test_contrarian_skips_final_minutes():
    """The SAME signal with only ~4 min left is SKIPPED — no runway to revert."""
    d = await Plugin().evaluate(_snap(secs_left=4 * 60))
    assert d.action is Action.SKIP
    assert "runway" in d.reason


async def test_contrarian_gate_inert_when_resolve_time_unknown():
    """resolve_time None (a feed / backtest that hasn't wired it) leaves the gate
    inert — the signal still fires, mirroring the portfolio's MIN_TIME gate, so the
    gate can never silently mute a strategy on data that lacks the field."""
    d = await Plugin().evaluate(_snap(secs_left=None))
    assert d.action is Action.BUY


async def test_contrarian_runway_boundary_is_exclusive():
    """Exactly MIN_RUNWAY_SECONDS left is NOT enough (the gate is `<=`); one second
    more passes. Pins the boundary so a `<` vs `<=` drift trips, and re-derives the
    threshold from the module constant rather than hard-coding 600."""
    assert (await Plugin().evaluate(_snap(secs_left=MIN_RUNWAY_SECONDS))).action is Action.SKIP
    assert (await Plugin().evaluate(_snap(secs_left=MIN_RUNWAY_SECONDS + 1))).action is Action.BUY
