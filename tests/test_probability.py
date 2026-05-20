from src.scout.probability import calculate_winrate, SCORE_TO_PROB


def _mtf(m_5m=0.0, m_15m=0.0):
    return {"m_5m": m_5m, "m_15m": m_15m}


def test_all_signals_pass_returns_080():
    wr, bd = calculate_winrate(
        symbol="ETH", buy_outcome="Up",
        sym_mtf=_mtf(m_5m=0.002, m_15m=0.005),
        vol_annual=0.40, t_min=25.0, btc_m15m=0.005,
    )
    assert wr == 0.80
    assert bd["score"] == 6


def test_no_signals_pass_returns_020():
    wr, bd = calculate_winrate(
        symbol="ETH", buy_outcome="Up",
        sym_mtf=_mtf(m_5m=0.0001, m_15m=-0.0001),
        vol_annual=0.80, t_min=5.0, btc_m15m=-0.005,
    )
    assert wr == 0.20
    assert bd["score"] == 0


def test_three_signals_pass_returns_050():
    wr, bd = calculate_winrate(
        symbol="ETH", buy_outcome="Up",
        sym_mtf=_mtf(m_5m=0.002, m_15m=0.005),
        vol_annual=0.80, t_min=5.0, btc_m15m=-0.005,
    )
    assert wr == 0.50
    assert bd["score"] == 3
    assert bd["mom_15m_strong_aligned"] and bd["mom_5m_strong_aligned"] and bd["mtf_aligned"]


def test_none_sym_mtf_returns_base():
    wr, bd = calculate_winrate(
        symbol="ETH", buy_outcome="Up",
        sym_mtf=None, vol_annual=0.40, t_min=25.0, btc_m15m=0.005,
    )
    assert wr == 0.20
    assert bd["reason"] == "no_momentum_data"


def test_btc_symbol_btc_corr_always_passes():
    wr, bd = calculate_winrate(
        symbol="BTC", buy_outcome="Up",
        sym_mtf=_mtf(m_5m=0.0001, m_15m=-0.0001),
        vol_annual=0.80, t_min=5.0, btc_m15m=None,
    )
    assert bd["btc_corr_aligned"] is True
    assert bd["score"] == 1
    assert wr == 0.30


def test_non_btc_missing_btc_m15m_fails_signal():
    wr, bd = calculate_winrate(
        symbol="ETH", buy_outcome="Up",
        sym_mtf=_mtf(m_5m=0.0001, m_15m=-0.0001),
        vol_annual=0.80, t_min=5.0, btc_m15m=None,
    )
    assert bd["btc_corr_aligned"] is False
    assert bd["score"] == 0


def test_mom_15m_boundary():
    base = dict(symbol="ETH", buy_outcome="Up", vol_annual=0.80,
                t_min=5.0, btc_m15m=-0.005)
    _, bd_eq = calculate_winrate(sym_mtf=_mtf(m_5m=0.0, m_15m=0.003), **base)
    assert bd_eq["mom_15m_strong_aligned"] is False
    _, bd_above = calculate_winrate(sym_mtf=_mtf(m_5m=0.0, m_15m=0.0031), **base)
    assert bd_above["mom_15m_strong_aligned"] is True


def test_mom_5m_boundary():
    base = dict(symbol="ETH", buy_outcome="Up", vol_annual=0.80,
                t_min=5.0, btc_m15m=-0.005)
    _, bd_eq = calculate_winrate(sym_mtf=_mtf(m_5m=0.001, m_15m=0.0), **base)
    assert bd_eq["mom_5m_strong_aligned"] is False
    _, bd_above = calculate_winrate(sym_mtf=_mtf(m_5m=0.0011, m_15m=0.0), **base)
    assert bd_above["mom_5m_strong_aligned"] is True


def test_vol_regime_boundaries():
    base = dict(symbol="ETH", buy_outcome="Up", sym_mtf=_mtf(),
                t_min=5.0, btc_m15m=-0.005)
    assert calculate_winrate(vol_annual=0.20, **base)[1]["vol_normal"] is True
    assert calculate_winrate(vol_annual=0.60, **base)[1]["vol_normal"] is True
    assert calculate_winrate(vol_annual=0.19, **base)[1]["vol_normal"] is False
    assert calculate_winrate(vol_annual=0.61, **base)[1]["vol_normal"] is False


def test_time_sweet_spot_boundaries():
    base = dict(symbol="ETH", buy_outcome="Up", sym_mtf=_mtf(),
                vol_annual=0.80, btc_m15m=-0.005)
    assert calculate_winrate(t_min=15.0, **base)[1]["time_sweet_spot"] is True
    assert calculate_winrate(t_min=40.0, **base)[1]["time_sweet_spot"] is True
    assert calculate_winrate(t_min=14.9, **base)[1]["time_sweet_spot"] is False
    assert calculate_winrate(t_min=40.1, **base)[1]["time_sweet_spot"] is False


def test_buy_outcome_down_aligns_with_negative_momentum():
    wr, bd = calculate_winrate(
        symbol="ETH", buy_outcome="Down",
        sym_mtf=_mtf(m_5m=-0.002, m_15m=-0.005),
        vol_annual=0.40, t_min=25.0, btc_m15m=-0.005,
    )
    assert bd["mom_15m_strong_aligned"] and bd["mom_5m_strong_aligned"]
    assert bd["btc_corr_aligned"] is True
    assert wr == 0.80


def test_momentum_strong_but_opposed_fails():
    wr, bd = calculate_winrate(
        symbol="ETH", buy_outcome="Down",
        sym_mtf=_mtf(m_5m=0.002, m_15m=0.005),
        vol_annual=0.80, t_min=5.0, btc_m15m=0.005,
    )
    assert bd["mom_15m_strong_aligned"] is False
    assert bd["mom_5m_strong_aligned"] is False
    assert bd["btc_corr_aligned"] is False


def test_mtf_aligned_independent_of_buy_outcome():
    _, bd_up = calculate_winrate(
        symbol="ETH", buy_outcome="Up",
        sym_mtf=_mtf(m_5m=-0.002, m_15m=-0.005),
        vol_annual=0.80, t_min=5.0, btc_m15m=0.005,
    )
    assert bd_up["mtf_aligned"] is True
    _, bd_mixed = calculate_winrate(
        symbol="ETH", buy_outcome="Up",
        sym_mtf=_mtf(m_5m=0.002, m_15m=-0.005),
        vol_annual=0.80, t_min=5.0, btc_m15m=0.005,
    )
    assert bd_mixed["mtf_aligned"] is False


def test_score_to_prob_mapping_complete():
    assert SCORE_TO_PROB == {0: 0.20, 1: 0.30, 2: 0.40, 3: 0.50,
                             4: 0.60, 5: 0.70, 6: 0.80}


def test_breakdown_contains_all_signal_keys():
    _, bd = calculate_winrate(
        symbol="ETH", buy_outcome="Up", sym_mtf=_mtf(m_5m=0.002, m_15m=0.005),
        vol_annual=0.40, t_min=25.0, btc_m15m=0.005,
    )
    for key in ("mom_15m_strong_aligned", "mom_5m_strong_aligned", "mtf_aligned",
                "vol_normal", "btc_corr_aligned", "time_sweet_spot",
                "score", "max_score", "winrate"):
        assert key in bd
