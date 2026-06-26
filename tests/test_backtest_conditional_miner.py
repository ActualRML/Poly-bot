"""Conditional-edge miner (src/backtest/conditional_miner.py).

Covers the stability metrics (median / pstdev / max_drawdown / dominance), the
ROBUST/WEAK/NO classification gates, graceful skipping of single-value axes,
shallow (depth 1-2) enumeration + the min_trades VALID flag, and a markdown smoke.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.backtest.conditional_miner import (
    MAX_DEPTH,
    NO_EDGE,
    ROBUST_EDGE,
    WEAK_SIGNAL,
    _active_axes,
    _classify,
    _max_drawdown,
    _stat_fields,
    mine,
    to_csv_rows,
    to_markdown,
    CSV_HEADER,
)
from src.backtest.engine import SimPosition
from src.backtest.recovery import MarketResolution


def _utc(h, mi, s=0):
    return datetime(2026, 6, 8, h, mi, s, tzinfo=timezone.utc)


def _pos(pnl, *, won=None, size=10.0, strategy="momentum", symbol="BTC",
         entry=0.65, zone="high", vol="low_vol", market_id="0xM", ts=None, side="YES"):
    if won is None:
        won = pnl > 0
    return SimPosition(
        market_id=market_id, symbol=symbol, side=side, entry_price=entry,
        size_usdc=size, strategy=strategy, opened_ts=ts or _utc(20, 0),
        price_zone=zone, vol_regime=vol, won=won, pnl_usdc=pnl,
    )


def _res(market_id, outcome, resolve_ts):
    return MarketResolution(
        market_id=market_id, resolve_ts=resolve_ts, outcome=outcome,
        last_ts=resolve_ts, last_price=1.0 if outcome == "YES" else 0.0, n=1,
    )


# --- stability metrics -----------------------------------------------------
def test_max_drawdown_peak_to_trough():
    ps = [_pos(10, ts=_utc(20, 0)), _pos(-30, ts=_utc(20, 1)), _pos(5, ts=_utc(20, 2))]
    assert _max_drawdown(ps) == 30.0           # cum 10 -> -20 (peak 10) -> -15


def test_max_drawdown_zero_when_monotonic():
    ps = [_pos(5, ts=_utc(20, 0)), _pos(5, ts=_utc(20, 1))]
    assert _max_drawdown(ps) == 0.0


def test_stat_fields_math():
    f = _stat_fields([_pos(6.0), _pos(6.0), _pos(-10.0), _pos(-10.0)])
    assert f["trades"] == 4
    assert f["win_rate"] == 50.0
    assert f["total_pnl"] == pytest.approx(-8.0)
    assert f["avg_pnl"] == pytest.approx(-2.0)
    assert f["median_pnl"] == pytest.approx(-2.0)   # median(-10,-10,6,6)
    assert f["profit_factor"] == pytest.approx(0.6)
    assert f["pnl_std"] == pytest.approx(8.0)        # pstdev about mean -2
    assert f["dominance_share"] is None              # not a positive group


def test_dominance_share_for_positive_group():
    assert _stat_fields([_pos(60.0)] + [_pos(10.0)] * 4)["dominance_share"] == pytest.approx(0.6)
    assert _stat_fields([_pos(20.0)] * 5)["dominance_share"] == pytest.approx(0.2)


# --- classification gates --------------------------------------------------
def _f(**kw):
    base = dict(trades=12, win_rate=60.0, total_pnl=100.0, avg_pnl=8.0, median_pnl=5.0,
                profit_factor=2.0, pnl_std=3.0, max_drawdown=2.0, max_trade_pnl=10.0,
                dominance_share=0.1)
    base.update(kw)
    return base


def test_classify_robust_edge():
    assert _classify(_f(), min_trades=10) == (True, ROBUST_EDGE)


@pytest.mark.parametrize("override", [
    {"dominance_share": 0.6},   # single-trade dominated
    {"median_pnl": -0.5},       # positive mean but negative median
    {"profit_factor": 1.0},     # not profitable enough
])
def test_classify_weak_when_a_gate_fails(override):
    valid, label = _classify(_f(**override), min_trades=10)
    assert valid is True and label == WEAK_SIGNAL


def test_classify_weak_when_low_sample():
    valid, label = _classify(_f(trades=5), min_trades=10)
    assert valid is False and label == WEAK_SIGNAL


@pytest.mark.parametrize("avg", [0.0, -1.0])
def test_classify_no_edge_when_not_positive(avg):
    assert _classify(_f(avg_pnl=avg), min_trades=10)[1] == NO_EDGE


# --- enumeration: graceful skip, depth, valid flag -------------------------
def _balanced_set():
    """20 trades, 2 strategies x 2 symbols; entry/ttr/vol all single-valued so only
    strategy + symbol are active axes."""
    settled, resolutions = [], {}
    opened = _utc(20, 0)
    for i in range(20):
        strat = "momentum" if i % 2 == 0 else "contrarian"
        sym = "BTC" if i % 4 < 2 else "ETH"
        mid = f"0x{i}"
        settled.append(_pos(1.0 if i % 3 else -1.0, strategy=strat, symbol=sym,
                            market_id=mid, ts=opened, vol="low_vol"))
        resolutions[mid] = _res(mid, "YES", opened + timedelta(hours=1))
    return settled, resolutions


def test_active_axes_skips_single_value_axes():
    settled, resolutions = _balanced_set()
    assert set(_active_axes(settled, resolutions)) == {"strategy", "symbol"}


def test_mine_depth_and_no_skipped_axis_in_combos():
    settled, resolutions = _balanced_set()
    groups = mine(settled, resolutions, min_trades=10, max_depth=MAX_DEPTH)
    assert max(g.depth for g in groups) == 2
    used_axes = {a for g in groups for a, _ in g.combo}
    assert used_axes == {"strategy", "symbol"}     # vol_regime/entry_band/ttr skipped


def test_mine_valid_flag_respects_min_trades():
    settled, resolutions = _balanced_set()
    groups = mine(settled, resolutions, min_trades=10)
    g_m = next(g for g in groups if g.combo == (("strategy", "momentum"),))
    assert g_m.trades == 10 and g_m.valid is True
    g_mb = next(g for g in groups
                if set(g.combo) == {("strategy", "momentum"), ("symbol", "BTC")})
    assert g_mb.trades == 5 and g_mb.valid is False   # 2-axis slice too thin


# --- renderers -------------------------------------------------------------
def test_to_csv_rows_shape():
    settled, resolutions = _balanced_set()
    rows = to_csv_rows(mine(settled, resolutions), slippage=0.01)
    assert rows and all(len(r) == len(CSV_HEADER) for r in rows)


def test_markdown_has_required_sections():
    settled, resolutions = _balanced_set()
    groups = mine(settled, resolutions, min_trades=10)
    md = to_markdown(groups, settled, resolutions, slippage=0.01, min_trades=10)
    for heading in [
        "## Conditional edge",
        "### Top ROBUST_EDGE groups",
        "### Is there any repeatable edge?",
        "### Is performance driven by outliers?",
        "### Contribution",
        "### FINAL: Does ANY conditional slice",
    ]:
        assert heading in md
    assert "vol_regime" in md            # named among the auto-skipped axes
    assert ("**YES.**" in md) or ("**NO.**" in md)
