"""Loss-structure probe: the Monte-Carlo Bernoulli null + helpers.

The script lives in research/ (not a package), so it's imported by path. The MC
null is the load-bearing statistic, so it gets the most coverage: a rigged segment
must be flagged, a calibrated one must not.
"""
import sys
from pathlib import Path

import pytest

RESEARCH = Path(__file__).resolve().parents[1] / "research"
sys.path.insert(0, str(RESEARCH))

import probe_loss_structure as pls  # noqa: E402


def _rec(entry, pnl, **kw):
    return pls.LossRecord(
        sample="t", strategy=kw.get("strategy", "momentum"), symbol=kw.get("symbol", "BTC"),
        entry_price=entry, size_usdc=kw.get("size", 10.0), pnl_usdc=pnl,
        ttr_secs=kw.get("ttr"), price_zone=kw.get("zone"), vol_regime=kw.get("vol"),
    )


# --- record semantics -----------------------------------------------------
def test_is_loss_uses_pnl_le_zero():
    assert _rec(0.6, -10).is_loss is True
    assert _rec(0.6, 5).is_loss is False
    assert _rec(0.6, 0).is_loss is True            # pnl<=0 counts as loss (per spec)


def test_expected_loss_is_one_minus_entry():
    assert _rec(0.7, 1).expected_loss == pytest.approx(0.3)
    assert _rec(0.18, -5).expected_loss == pytest.approx(0.82)


# --- bucketing ------------------------------------------------------------
@pytest.mark.parametrize("price,band", [
    (0.50, "0.50-0.60"), (0.59, "0.50-0.60"), (0.60, "0.60-0.70"),
    (0.70, "0.70-0.80"), (0.18, "0.10-0.20"),
])
def test_band(price, band):
    assert pls._band(price) == band


@pytest.mark.parametrize("secs,b", [
    (3600, ">=30m"), (1800, ">=30m"), (1799, "15-30m"), (900, "15-30m"),
    (300, "5-15m"), (120, "2-5m"), (60, "0-2m"),
])
def test_ttr_bucket(secs, b):
    assert pls._ttr_bucket(secs) == b


def test_ttr_bucket_none():
    assert pls._ttr_bucket(None) is None


@pytest.mark.parametrize("size,b", [(8, "0-10"), (10, "10-15"), (17, "15-20"), (25, ">=20")])
def test_size_band(size, b):
    assert pls._size_band(size) == b


# --- Monte-Carlo Bernoulli null (load-bearing) ----------------------------
def test_mc_flags_rigged_segment():
    # 12 trades priced 0.80 (expected loss 0.20) but ALL lost -> residual +0.80
    obs, probs = [1] * 12, [0.2] * 12
    cells = {"seg": list(range(12))}
    p_cell, p_global, stat = pls.mc_pvalues(obs, probs, cells, iters=3000, seed=1)
    assert stat["seg"][3] == pytest.approx(0.8)    # residual = 1.0 - 0.2
    assert p_cell["seg"] < 0.05 and p_global < 0.05


def test_mc_does_not_flag_calibrated_segment():
    # 20 trades priced 0.60 (expected loss 0.40); exactly 8 losses -> residual 0
    obs, probs = [1] * 8 + [0] * 12, [0.4] * 20
    cells = {"seg": list(range(20))}
    p_cell, p_global, stat = pls.mc_pvalues(obs, probs, cells, iters=3000, seed=1)
    assert abs(stat["seg"][3]) < 1e-9
    assert p_cell["seg"] > 0.05


def test_mc_global_is_seeded_deterministic():
    obs, probs = [1] * 6 + [0] * 6, [0.5] * 12
    cells = {"a": list(range(6)), "b": list(range(6, 12))}
    r1 = pls.mc_pvalues(obs, probs, cells, iters=1000, seed=7)[1]
    r2 = pls.mc_pvalues(obs, probs, cells, iters=1000, seed=7)[1]
    assert r1 == r2


# --- loss Pareto ----------------------------------------------------------
def test_pareto_whale_vs_broad():
    whale = [_rec(0.6, -100)] + [_rec(0.6, -1) for _ in range(9)]
    nl, n80, gross, med, mean = pls.pareto_loss(whale)
    assert nl == 10 and n80 == 1 and gross == pytest.approx(109.0)

    broad = [_rec(0.6, -10) for _ in range(10)]
    assert pls.pareto_loss(broad)[1] == 8          # need 8/10 to reach 80%


def test_pareto_ignores_wins():
    assert pls.pareto_loss([_rec(0.6, 5), _rec(0.6, -10)])[0] == 1
