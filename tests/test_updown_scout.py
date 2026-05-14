from src.scout.updown_scout import score_updown_market


def test_all_signals_pass():
    r = score_updown_market(
        symbol="BTC",
        buy_outcome="Up",
        gbm_edge=0.10,
        adj_min_edge=0.04,
        sym_mtf={"m_1m": 0.01, "m_5m": 0.01, "m_15m": 0.01},
        vol_annual=0.40,
        minutes_left=30,
        macro_htf_dir="up",
    )
    assert r.score == r.max_score


def test_edge_fail():
    r = score_updown_market(
        symbol="BTC",
        buy_outcome="Up",
        gbm_edge=0.03,
        adj_min_edge=0.04,  # 0.03 < 0.04*2 = 0.08 → fail
        sym_mtf=None,
        vol_annual=0.40,
        minutes_left=30,
        macro_htf_dir=None,
    )
    assert r.breakdown["edge"] is False


def test_time_fail_too_short():
    r = score_updown_market(
        symbol="BTC",
        buy_outcome="Up",
        gbm_edge=0.10,
        adj_min_edge=0.04,
        sym_mtf=None,
        vol_annual=0.40,
        minutes_left=5,  # < 20 → fail
        macro_htf_dir=None,
    )
    assert r.breakdown["time"] is False


def test_time_fail_too_long():
    r = score_updown_market(
        symbol="BTC",
        buy_outcome="Up",
        gbm_edge=0.10,
        adj_min_edge=0.04,
        sym_mtf=None,
        vol_annual=0.40,
        minutes_left=55,  # > 50 → fail
        macro_htf_dir=None,
    )
    assert r.breakdown["time"] is False


def test_vol_regime_too_high():
    r = score_updown_market(
        symbol="DOGE",
        buy_outcome="Up",
        gbm_edge=0.10,
        adj_min_edge=0.04,
        sym_mtf=None,
        vol_annual=0.95,  # > 0.80 → fail
        minutes_left=30,
        macro_htf_dir=None,
    )
    assert r.breakdown["vol_regime"] is False


def test_vol_regime_too_low():
    r = score_updown_market(
        symbol="BTC",
        buy_outcome="Up",
        gbm_edge=0.10,
        adj_min_edge=0.04,
        sym_mtf=None,
        vol_annual=0.10,  # < 0.20 → fail
        minutes_left=30,
        macro_htf_dir=None,
    )
    assert r.breakdown["vol_regime"] is False


def test_macro_skipped_when_flat():
    r = score_updown_market(
        symbol="BTC",
        buy_outcome="Up",
        gbm_edge=0.10,
        adj_min_edge=0.04,
        sym_mtf=None,
        vol_annual=0.40,
        minutes_left=30,
        macro_htf_dir="flat",  # flat → not scored
    )
    assert r.breakdown["macro"] is None


def test_momentum_alignment_down():
    r = score_updown_market(
        symbol="BTC",
        buy_outcome="Down",
        gbm_edge=0.10,
        adj_min_edge=0.04,
        sym_mtf={"m_1m": -0.01, "m_5m": -0.008, "m_15m": 0.001},  # 2/3 aligned down
        vol_annual=0.40,
        minutes_left=30,
        macro_htf_dir=None,
    )
    assert r.breakdown["momentum"] is True


def test_max_score_without_optional_signals():
    r = score_updown_market(
        symbol="BTC",
        buy_outcome="Up",
        gbm_edge=0.10,
        adj_min_edge=0.04,
        sym_mtf=None,        # no momentum data
        vol_annual=0.40,
        minutes_left=30,
        macro_htf_dir=None,  # no macro data
    )
    assert r.max_score == 3  # only edge + vol_regime + time
    assert r.score == 3
