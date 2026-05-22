"""
Tests for EvGateFilter (2026-05-23 refactor).

With flat winrate=0.50 and default EV_GATE_MIN_MARGIN=0.02, threshold=0.48.
Reject when buy_price > 0.48.
"""
from src.scout.filters.signal import EvGateFilter


class _Ctx:
    """Minimal ScoutContext stand-in — only fields EvGateFilter reads."""
    def __init__(self, buy_price: float, buy_winrate: float = 0.50):
        self.buy_price = buy_price
        self.buy_winrate = buy_winrate


def test_rejects_expensive_buy_price():
    f = EvGateFilter()
    result = f.evaluate(_Ctx(buy_price=0.55))
    assert result.passed is False
    assert "no edge" in result.reason


def test_rejects_at_boundary_plus_epsilon():
    f = EvGateFilter()
    # 0.50 winrate - 0.02 margin = 0.48 threshold; 0.481 > 0.48 → reject
    result = f.evaluate(_Ctx(buy_price=0.481))
    assert result.passed is False


def test_accepts_cheap_buy_price():
    f = EvGateFilter()
    result = f.evaluate(_Ctx(buy_price=0.40))
    assert result.passed is True


def test_accepts_at_threshold():
    f = EvGateFilter()
    # 0.48 is NOT > 0.48 → passes
    result = f.evaluate(_Ctx(buy_price=0.48))
    assert result.passed is True


def test_disabled_flag_passes_everything(monkeypatch):
    from src.utils import config as cfgmod
    monkeypatch.setattr(cfgmod.config, "EV_GATE_ENABLED", False, raising=False)
    f = EvGateFilter()
    result = f.evaluate(_Ctx(buy_price=0.95))
    assert result.passed is True
    assert "disabled" in result.reason


def test_margin_change_shifts_threshold(monkeypatch):
    from src.utils import config as cfgmod
    monkeypatch.setattr(cfgmod.config, "EV_GATE_ENABLED", True, raising=False)
    monkeypatch.setattr(cfgmod.config, "EV_GATE_MIN_MARGIN", 0.10, raising=False)
    # threshold = 0.50 - 0.10 = 0.40
    f = EvGateFilter()
    assert f.evaluate(_Ctx(buy_price=0.45)).passed is False
    assert f.evaluate(_Ctx(buy_price=0.35)).passed is True
