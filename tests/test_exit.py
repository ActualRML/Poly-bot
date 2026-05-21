import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.execute.exit import (
    _vol_scale_from_question, _SYMBOL_VOL, _VOL_BTC_BASELINE,
    ExitEvaluator, ExitSignal, Position,
)


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


def _hourly_pos(pnl_pct, mins):
    entry = Decimal("0.30")
    current = (entry * Decimal(str(1 + pnl_pct / 100))).quantize(Decimal("0.0001"))
    now = datetime.now(timezone.utc)
    return Position(
        condition_id="0xtest",
        outcome="Down",
        entry_price=entry,
        current_price=current,
        highest_price=current,
        shares=Decimal("100"),
        capital_at_risk=Decimal("30"),
        resolve_date=now + timedelta(minutes=mins),
        entry_time=now - timedelta(minutes=15),
        question="Solana Up or Down - May 21, 9AM ET",
        strategy_mode="updown_hourly_momentum_dry_run",
    )


def test_anytime_tp_fires_low_time_high_profit():
    d = ExitEvaluator().evaluate(_hourly_pos(160, 5))
    assert d.should_exit
    assert d.reason == "exit_lock_profit_anytime"


def test_anytime_tp_fires_high_time_high_profit():
    d = ExitEvaluator().evaluate(_hourly_pos(160, 45))
    assert d.should_exit
    assert d.reason == "exit_lock_profit_anytime"


def test_anytime_tp_no_fire_below_threshold():
    d = ExitEvaluator().evaluate(_hourly_pos(140, 5))
    assert not d.should_exit
    assert d.reason != "exit_lock_profit_anytime"


def test_anytime_tp_fires_at_boundary():
    d = ExitEvaluator().evaluate(_hourly_pos(150, 30))
    assert d.should_exit
    assert d.reason == "exit_lock_profit_anytime"


def test_anytime_tp_exit_reason_tag():
    d = ExitEvaluator().evaluate(_hourly_pos(200, 8))
    assert d.signal == ExitSignal.EXIT_LOCK_PROFIT
    assert d.reason == "exit_lock_profit_anytime"
