"""Diagnostic segment & concentration analysis (src/backtest/diagnostics.py).

Covers the metric math, the float-trap-prone entry banding, time-to-resolution
bucketing off the recovered resolve_ts, the Pareto concentration verdict
(outlier vs broad), and that the new vol_regime field survives settle().
"""
import math
from datetime import datetime, timedelta, timezone

import pytest

from src.backtest.diagnostics import (
    ConcentrationSide,
    SegmentStat,
    concentration,
    entry_band,
    segment,
    slippage_markdown,
    ttr_keyfunc,
)
from src.backtest.engine import SimPortfolio, SimPosition
from src.backtest.recovery import MarketResolution


def _utc(h, mi, s=0):
    return datetime(2026, 6, 8, h, mi, s, tzinfo=timezone.utc)


def _pos(pnl=None, *, won=None, size=10.0, strategy="momentum", symbol="BTC",
         entry=0.60, zone="high", vol="low_vol", market_id="0xM", ts=None, side="YES"):
    if won is None and pnl is not None:
        won = pnl > 0
    return SimPosition(
        market_id=market_id, symbol=symbol, side=side, entry_price=entry,
        size_usdc=size, strategy=strategy, opened_ts=ts or _utc(20, 0),
        price_zone=zone, vol_regime=vol, won=won, pnl_usdc=pnl,
    )


def _many(pnls, **kw):
    return [_pos(p, market_id=f"0x{i}", **kw) for i, p in enumerate(pnls)]


def _res(market_id, outcome, resolve_ts):
    return MarketResolution(
        market_id=market_id, resolve_ts=resolve_ts, outcome=outcome,
        last_ts=resolve_ts, last_price=1.0 if outcome == "YES" else 0.0, n=1,
    )


def _one_group(positions) -> SegmentStat:
    return segment(positions, lambda p: "g")[0]


# --- metric math -----------------------------------------------------------
def test_segment_metrics_math():
    # two +0.6R winners, two -1.0R losers, all $10 stakes
    stat = _one_group(_many([6.0, 6.0, -10.0, -10.0]))
    assert stat.trades == 4
    assert stat.win_rate == 50.0
    assert stat.total_pnl == pytest.approx(-8.0)
    assert stat.avg_pnl == pytest.approx(-2.0)
    assert stat.expectancy_r == pytest.approx(-0.2)      # mean(0.6, 0.6, -1, -1)
    assert stat.profit_factor == pytest.approx(0.6)      # 12 / 20


def test_profit_factor_inf_when_no_losses():
    assert _one_group(_many([5.0, 5.0])).profit_factor == math.inf


def test_profit_factor_zero_when_no_wins():
    assert _one_group(_many([-5.0, -5.0])).profit_factor == 0.0


def test_expectancy_r_differs_from_avg_pnl_when_stakes_differ():
    # same +$5 pnl, different stakes -> 0.5R vs 0.05R
    stat = _one_group([_pos(5.0, size=10.0, market_id="a"),
                       _pos(5.0, size=100.0, market_id="b")])
    assert stat.avg_pnl == pytest.approx(5.0)
    assert stat.expectancy_r == pytest.approx(0.275)     # mean(0.5, 0.05)
    assert stat.expectancy_r != pytest.approx(stat.avg_pnl)


# --- entry banding (cent-aligned; guards the 0.6/0.1 float-floor trap) ------
@pytest.mark.parametrize("entry,band", [
    (0.50, "0.50-0.60"),
    (0.59, "0.50-0.60"),
    (0.60, "0.60-0.70"),
    (0.65, "0.60-0.70"),
    (0.69, "0.60-0.70"),
    (0.70, "0.70-0.80"),
    (0.05, "0.00-0.10"),
    (0.10, "0.10-0.20"),
])
def test_entry_band_boundaries(entry, band):
    assert entry_band(_pos(entry=entry)) == band


# --- time-to-resolution bucketing ------------------------------------------
def _ttr(secs):
    opened = _utc(20, 0)
    pos = _pos(market_id="0xT", ts=opened)
    res = {"0xT": _res("0xT", "YES", opened + timedelta(seconds=secs))}
    return ttr_keyfunc(res)(pos)


@pytest.mark.parametrize("secs,bucket", [
    (3600, ">=30m"),
    (1800, ">=30m"),      # 30m boundary is inclusive on the high bucket
    (1799, "15-30m"),
    (900, "15-30m"),      # 15m boundary -> 15-30m
    (899, "5-15m"),
    (300, "5-15m"),       # 5m boundary -> 5-15m
    (299, "2-5m"),
    (121, "2-5m"),
])
def test_ttr_buckets(secs, bucket):
    assert _ttr(secs) == bucket


def test_ttr_none_when_market_unresolved():
    assert ttr_keyfunc({})(_pos(market_id="0xT")) is None


# --- concentration (Pareto) ------------------------------------------------
def test_concentration_outlier_driven():
    # one whale + four small winners -> the whale alone clears 80%
    side = concentration(_many([80.0, 5.0, 5.0, 5.0, 5.0])).profit
    assert isinstance(side, ConcentrationSide)
    assert side.n_trades == 5
    assert side.gross_total == pytest.approx(100.0)
    assert side.n_for_80pct == 1
    assert side.concentration_pct == pytest.approx(20.0)
    assert side.verdict == "OUTLIER-driven"
    # rows sorted by magnitude desc, cum_share monotonic up to 1.0
    shares = [r.cum_share for r in side.rows]
    assert shares == sorted(shares)
    assert side.rows[0].pnl_usdc == 80.0
    assert side.rows[-1].cum_share == pytest.approx(1.0)


def test_concentration_broad():
    side = concentration(_many([20.0, 20.0, 20.0, 20.0, 20.0])).profit
    assert side.n_for_80pct == 4               # need 4 of 5 to reach 80%
    assert side.concentration_pct == pytest.approx(80.0)
    assert side.verdict == "broad"


def test_concentration_loss_side_independent():
    settled = _many([10.0, 10.0]) + _many([-50.0, -10.0, -10.0], strategy="x")
    conc = concentration(settled)
    assert conc.profit.n_trades == 2
    assert conc.loss.n_trades == 3
    assert conc.loss.gross_total == pytest.approx(70.0)
    assert conc.loss.rows[0].pnl_usdc == -50.0       # biggest loss ranked first
    assert conc.loss.n_for_80pct == 2                # 50 then 60/70 >= 80%


def test_concentration_empty_side():
    side = concentration(_many([5.0])).loss
    assert side.n_trades == 0
    assert side.verdict == "none"


# --- engine change: vol_regime survives settle() ---------------------------
def test_vol_regime_carried_through_settle():
    sim = SimPortfolio()
    sim.open_positions.append(
        _pos(market_id="0xZ", side="YES", vol="high_vol", won=None)
    )
    resolutions = {"0xZ": _res("0xZ", "YES", _utc(21, 0))}
    settled, _ = sim.settle(resolutions, slippage=0.01)
    assert settled[0].vol_regime == "high_vol"
    assert settled[0].won is True


# --- ordering + smoke ------------------------------------------------------
def test_segment_orders_by_pnl_desc_without_key_order():
    settled = (_many([-5.0], strategy="loser") + _many([20.0], strategy="winner"))
    stats = segment(settled, lambda p: p.strategy)
    assert [s.key for s in stats] == ["winner", "loser"]


def test_segment_respects_key_order():
    settled = _many([1.0, 1.0], vol="high_vol") + _many([1.0], vol="low_vol")
    stats = segment(settled, lambda p: p.vol_regime,
                    key_order=("low_vol", "mid_vol", "high_vol"))
    assert [s.key for s in stats] == ["low_vol", "high_vol"]


def test_slippage_markdown_smoke():
    settled = _many([6.0, -10.0], strategy="momentum")
    md = slippage_markdown(settled, {}, slippage=0.01, strategies=["momentum"])
    assert "## Slippage 0.01" in md
    assert "**By strategy**" in md
    assert "### Scope: momentum" in md
    assert "concentration" in md.lower()
