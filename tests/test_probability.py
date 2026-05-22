"""
Tests for flat probability model (2026-05-23 refactor).

Old SCORE_TO_PROB tests removed — see git history (1c185c0) for the original
6-bin lookup tests. New behavior: calculate_winrate always returns 0.50.
"""
from src.scout.probability import calculate_winrate, FLAT_WINRATE


def _mtf(m_5m=0.0, m_15m=0.0):
    return {"m_5m": m_5m, "m_15m": m_15m}


def test_flat_winrate_constant_is_050():
    assert FLAT_WINRATE == 0.50


def test_returns_flat_regardless_of_signal_strength():
    wr_strong, _ = calculate_winrate(
        symbol="ETH", buy_outcome="Up",
        sym_mtf=_mtf(m_5m=0.002, m_15m=0.005),
        vol_annual=0.40, t_min=25.0, btc_m15m=0.005,
    )
    wr_weak, _ = calculate_winrate(
        symbol="ETH", buy_outcome="Up",
        sym_mtf=_mtf(m_5m=0.0, m_15m=-0.0001),
        vol_annual=0.80, t_min=5.0, btc_m15m=-0.005,
    )
    assert wr_strong == 0.50
    assert wr_weak == 0.50


def test_returns_flat_when_sym_mtf_none():
    wr, _ = calculate_winrate(
        symbol="ETH", buy_outcome="Up",
        sym_mtf=None, vol_annual=0.40, t_min=25.0, btc_m15m=0.005,
    )
    assert wr == 0.50


def test_returns_flat_for_any_symbol():
    for sym in ("BTC", "ETH", "SOL", "DOGE", "XRP", "BNB", "UNKNOWN"):
        wr, _ = calculate_winrate(
            symbol=sym, buy_outcome="Up", sym_mtf=_mtf(),
            vol_annual=0.40, t_min=25.0, btc_m15m=0.0,
        )
        assert wr == 0.50, f"{sym} returned {wr}, expected 0.50"


def test_breakdown_shape_preserved():
    _, bd = calculate_winrate(
        symbol="ETH", buy_outcome="Up", sym_mtf=_mtf(),
        vol_annual=0.40, t_min=25.0, btc_m15m=0.0,
    )
    for key in ("mode", "score", "max_score", "winrate", "reason"):
        assert key in bd
    assert bd["score"] == 0
    assert bd["max_score"] == 6
    assert bd["winrate"] == 0.50
    assert "flat" in bd["mode"]
