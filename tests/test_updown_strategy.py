import math
import pytest
from hypothesis import given, strategies as st

from src.logic.updown_strategy import _norm_cdf, detect_updown_market

@given(x=st.floats(allow_nan=False, allow_infinity=False, min_value=-1e10, max_value=1e10))
def test_norm_cdf_always_in_01(x):
    assert 0.0 <= _norm_cdf(x) <= 1.0

def test_norm_cdf_at_zero_is_half():
    assert abs(_norm_cdf(0.0) - 0.5) < 1e-10

def test_norm_cdf_monotone():
    assert _norm_cdf(-3.0) < _norm_cdf(-1.0) < _norm_cdf(0.0) < _norm_cdf(1.0) < _norm_cdf(3.0)

def test_norm_cdf_symmetry():
    for x in [0.5, 1.0, 2.0, 3.0]:
        assert abs(_norm_cdf(x) + _norm_cdf(-x) - 1.0) < 1e-10

@given(
    current=st.floats(0.01, 1_000_000.0, allow_nan=False, allow_infinity=False),
    reference=st.floats(0.01, 1_000_000.0, allow_nan=False, allow_infinity=False),
    T_days=st.floats(0.001, 2.0, allow_nan=False, allow_infinity=False),
    vol=st.floats(0.05, 3.0, allow_nan=False, allow_infinity=False),
)
def test_calc_prob_up_range(current, reference, T_days, vol):
    mu_adj = -0.5 * vol ** 2
    denom = vol * math.sqrt(T_days)
    if denom == 0:
        return
    d2 = (math.log(current / reference) + mu_adj * T_days) / denom
    prob_up = _norm_cdf(d2)
    assert 0.0 <= prob_up <= 1.0


# ── detect_updown_market ──────────────────────────────────────────────────────

@pytest.mark.parametrize("question,expected_symbol", [
    ("BTC Up or Down Daily",                           "BTC"),
    ("Bitcoin Up or Down Daily",                       "BTC"),
    ("bitcoin up or down - may 6, 2026, 1am et",       "BTC"),
    ("ETH Up or Down Daily",                           "ETH"),
    ("Ethereum Up or Down Daily",                      "ETH"),
    ("Solana Up or Down Daily",                        "SOL"),
    ("SOL Up or Down Daily",                           "SOL"),
    ("XRP Up or Down Daily",                           "XRP"),
    ("Ripple Up or Down Daily",                        "XRP"),
    ("Dogecoin Up or Down Daily",                      "DOGE"),
    ("DOGE Up or Down Daily",                          "DOGE"),
    ("BNB Up or Down Daily",                           "BNB"),
    ("Binance Coin Up or Down Daily",                  "BNB"),
])
def test_detect_updown_market_known_symbols(question, expected_symbol):
    result = detect_updown_market(question)
    assert result is not None
    assert result == (expected_symbol, "Up")


@pytest.mark.parametrize("question", [
    "Will BTC be above $80,000?",
    "Who will win the 2024 election?",
    "Is ETH going to moon?",
    "",
])
def test_detect_updown_market_no_updown_phrase_returns_none(question):
    assert detect_updown_market(question) is None


def test_detect_updown_market_unknown_symbol_returns_none():
    assert detect_updown_market("PEPE Up or Down Daily") is None


def test_detect_updown_market_with_valid_outcomes():
    result = detect_updown_market("BTC Up or Down Daily", outcomes=["Up", "Down"])
    assert result == ("BTC", "Up")


def test_detect_updown_market_with_invalid_outcomes_returns_none():
    assert detect_updown_market("BTC Up or Down Daily", outcomes=["Yes", "No"]) is None


def test_detect_updown_market_outcomes_overrides_phrase_check():
    # No "up or down" phrase but outcomes are valid — should still find symbol
    result = detect_updown_market("Bitcoin price direction", outcomes=["Up", "Down"])
    assert result == ("BTC", "Up")


def test_detect_updown_market_case_insensitive_outcomes():
    result = detect_updown_market("BTC Up or Down Daily", outcomes=["UP", "DOWN"])
    assert result == ("BTC", "Up")
