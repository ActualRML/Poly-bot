import math

from src.execute.exit import _vol_scale_from_question, _SYMBOL_VOL, _VOL_BTC_BASELINE


def _expected(symbol):
    return math.sqrt(_SYMBOL_VOL[symbol] / _VOL_BTC_BASELINE)


def test_vol_scale_matches_full_asset_names():
    cases = {
        "Bitcoin Up or Down - May 21, 5PM ET":  "BTC",
        "Ethereum Up or Down - May 21, 5PM ET": "ETH",
        "Solana Up or Down - May 21, 5PM ET":   "SOL",
        "Dogecoin Up or Down - May 21, 5PM ET": "DOGE",
        "XRP Up or Down - May 21, 5PM ET":      "XRP",
        "BNB Up or Down - May 21, 5PM ET":      "BNB",
    }
    for question, symbol in cases.items():
        assert _vol_scale_from_question(question) == _expected(symbol), question


def test_vol_scale_matches_tickers():
    assert _vol_scale_from_question("DOGE Up or Down") == _expected("DOGE")
    assert _vol_scale_from_question("Ripple Up or Down") == _expected("XRP")


def test_vol_scale_fallback_for_unknown_market():
    assert _vol_scale_from_question("Random Market About Nothing") == 1.0
    assert _vol_scale_from_question("") == 1.0
