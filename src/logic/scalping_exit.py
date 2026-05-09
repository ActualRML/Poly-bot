"""
Dynamic Exit & Risk Sizing Engine for binary prediction markets.

All functions are pure math — no I/O, fully testable.
Prices are on the 0.0001–0.9999 Polymarket binary-outcome scale.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


# ── ATR for binary market prices ─────────────────────────────────────────────

def market_price_atr(prices: list[float], period: int = 14) -> Optional[float]:
    """
    ATR proxy for a binary market: mean absolute price change
    over the last `period` observations (no high/low available, only last price).
    """
    if len(prices) < period + 1:
        return None
    deltas = [abs(prices[i] - prices[i - 1]) for i in range(1, len(prices))]
    return sum(deltas[-period:]) / period


# ── Dynamic binary trailing stop ─────────────────────────────────────────────

def binary_trailing_stop(
    highest_price: float,
    atr: float,
    multiplier: float = 2.5,
    ceiling: float = 0.95,
) -> float:
    """
    ATR-based trailing stop for a binary market position.
    Tightens to 1.5× near the ceiling to avoid noise-triggered stop-out.
    Returns the stop level on the 0–1 scale.
    """
    eff_mult = 1.5 if highest_price >= ceiling else multiplier
    return max(0.001, round(highest_price - eff_mult * atr, 4))


def atr_trailing_active(unrealized_pnl_pct: float, activate_at: float = 50.0) -> bool:
    """Returns True once unrealized profit exceeds the activation threshold (%)."""
    return unrealized_pnl_pct >= activate_at


# ── Net profit with fees & slippage ──────────────────────────────────────────

def net_profit_usdc(
    entry_price: float,
    exit_price: float,
    capital_usdc: float,
    taker_fee_pct: float = 0.018,
    slippage_usdc: float = 0.0,
) -> float:
    """
    Net profit after taker fee (charged on proceeds) and estimated slippage.
    Net = shares*(exit-entry) - shares*exit*fee - slippage
    """
    if entry_price <= 0 or capital_usdc <= 0:
        return 0.0
    shares = capital_usdc / entry_price
    gross = shares * (exit_price - entry_price)
    fee = shares * exit_price * taker_fee_pct
    return round(gross - fee - slippage_usdc, 4)


def min_exit_price_for_profit(
    entry_price: float,
    capital_usdc: float,
    taker_fee_pct: float = 0.018,
    min_net_profit: float = 0.0,
) -> float:
    """
    Minimum exit price to achieve min_net_profit after taker fee.
    Derived by solving net_profit_usdc = min_net_profit for exit_price.
    """
    if entry_price <= 0 or capital_usdc <= 0:
        return 1.0
    shares = capital_usdc / entry_price
    # net = shares*exit*(1-fee) - shares*entry = min_net_profit
    denominator = shares * (1.0 - taker_fee_pct)
    if denominator <= 0:
        return 1.0
    floor = (min_net_profit + shares * entry_price) / denominator
    return round(min(0.9999, max(0.0001, floor)), 4)


# ── Order book liquidity depth ────────────────────────────────────────────────

def expected_fill_and_slippage(
    bids: list[tuple[float, float]],
    size_shares: float,
) -> tuple[float, float]:
    """
    Walk down the bid stack to simulate filling size_shares.
    bids: [(price, size), ...] sorted descending (best bid first).
    Returns (avg_fill_price, slippage_pct_vs_best_bid).
    """
    if not bids or size_shares <= 0:
        return 0.0, 1.0
    best_bid = bids[0][0]
    remaining, total_value, total_filled = size_shares, 0.0, 0.0
    for price, size in bids:
        if remaining <= 0:
            break
        fill = min(remaining, size)
        total_value += fill * price
        total_filled += fill
        remaining -= fill
    if total_filled <= 0:
        return 0.0, 1.0
    avg_fill = total_value / total_filled
    slippage = (best_bid - avg_fill) / best_bid if best_bid > 0 else 1.0
    return round(avg_fill, 4), round(max(0.0, slippage), 4)


def liquidity_check(
    bids: list[tuple[float, float]],
    size_shares: float,
    entry_price: float,
    capital_usdc: float,
    min_net_profit_usdc: float = 0.0,
    taker_fee_pct: float = 0.018,
    slippage_warn_threshold: float = 0.03,
) -> dict:
    """
    Pre-execution liquidity validation.
    Returns {ok, expected_fill, slippage_pct, slippage_usdc, net_profit_usdc, warning}.
    """
    avg_fill, slippage_pct = expected_fill_and_slippage(bids, size_shares)
    slippage_usdc = round(slippage_pct * capital_usdc, 4)
    net = net_profit_usdc(entry_price, avg_fill, capital_usdc, taker_fee_pct, slippage_usdc)

    warning: Optional[str] = None
    if slippage_pct > slippage_warn_threshold:
        warning = (
            f"Slippage kemungkinan besar > {slippage_warn_threshold:.0%} "
            f"karena likuiditas tipis ({slippage_pct:.1%} est.)"
        )

    ok = net >= min_net_profit_usdc
    if not ok and warning is None:
        warning = (
            f"Net profit {net:.4f} USDC negatif setelah fee+slippage "
            f"— batalkan take profit"
        )

    return {
        "ok": ok,
        "expected_fill": avg_fill,
        "slippage_pct": slippage_pct,
        "slippage_usdc": slippage_usdc,
        "net_profit_usdc": net,
        "warning": warning,
    }


# ── Volatility-adjusted Kelly multiplier ─────────────────────────────────────

def volatility_kelly_mult(atr_curr: float, atr_avg: float) -> float:
    """
    Returns a sizing multiplier based on ATR vs its rolling average.
    0.0 = hard stop (ATR > 3× avg — Momentum Ignition risk).
    0.5 = half size (ATR 2–3× avg).
    0.75 = reduced (ATR 1.5–2× avg).
    1.0 = normal.
    """
    if atr_avg <= 0:
        return 1.0
    ratio = atr_curr / atr_avg
    if ratio > 3.0:
        return 0.0
    if ratio > 2.0:
        return 0.5
    if ratio > 1.5:
        return 0.75
    return 1.0


# ── Session drawdown tracker ──────────────────────────────────────────────────

@dataclass
class SessionRiskTracker:
    """
    Tracks session equity and triggers a cool-down when max drawdown is breached.
    Thread-safe for single-process use; not safe across multiple processes.
    """
    starting_equity: float
    max_drawdown_pct: float = 0.05
    cooldown_minutes: int = 60
    peak_equity: float = field(init=False)
    current_equity: float = field(init=False)
    _cooldown_until_ts: Optional[float] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.peak_equity = self.starting_equity
        self.current_equity = self.starting_equity

    def update(self, current_equity: float) -> bool:
        """Update equity. Returns True if cool-down was just triggered."""
        self.current_equity = current_equity
        if current_equity > self.peak_equity:
            self.peak_equity = current_equity
        if self._drawdown() >= self.max_drawdown_pct and not self.in_cooldown():
            self._cooldown_until_ts = (
                datetime.now(timezone.utc).timestamp() + self.cooldown_minutes * 60
            )
            return True
        return False

    def in_cooldown(self) -> bool:
        if self._cooldown_until_ts is None:
            return False
        return datetime.now(timezone.utc).timestamp() < self._cooldown_until_ts

    def can_trade(self) -> bool:
        return not self.in_cooldown()

    def reset_cooldown(self) -> None:
        self._cooldown_until_ts = None

    def _drawdown(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return (self.peak_equity - self.current_equity) / self.peak_equity

    def dashboard(self) -> dict:
        dd = self._drawdown()
        return {
            "starting_equity":      self.starting_equity,
            "current_equity":       self.current_equity,
            "peak_equity":          self.peak_equity,
            "session_drawdown_pct": round(dd, 4),
            "session_drawdown_usdc": round(self.peak_equity - self.current_equity, 2),
            "max_drawdown_pct":     self.max_drawdown_pct,
            "cooldown_active":      self.in_cooldown(),
            "can_trade":            self.can_trade(),
        }


# ── Risk dashboard summary ────────────────────────────────────────────────────

def risk_dashboard(
    pos_entry: float,
    pos_current: float,
    pos_highest: float,
    pos_capital: float,
    market_atr: float,
    atr_avg: float,
    taker_fee_pct: float = 0.018,
    atr_multiplier: float = 2.5,
) -> dict:
    """
    Consolidated risk view for a single open position.
    Suitable for logging the 'Risk Dashboard' output.
    """
    if pos_entry <= 0 or pos_capital <= 0:
        return {}
    pnl_pct = (pos_current - pos_entry) / pos_entry * 100
    trailing_active = atr_trailing_active(pnl_pct)
    stop_level = (
        binary_trailing_stop(pos_highest, market_atr, atr_multiplier)
        if trailing_active else None
    )
    kelly_mult = volatility_kelly_mult(market_atr, atr_avg)
    shares = pos_capital / pos_entry
    slip_approx = market_atr * shares
    net = net_profit_usdc(pos_entry, pos_current, pos_capital, taker_fee_pct, slip_approx)
    atr_ratio = round(market_atr / atr_avg, 2) if atr_avg > 0 else None

    if kelly_mult == 0.0:
        action_hint = "HARD_STOP_VOLATILITY"
    elif kelly_mult < 1.0:
        action_hint = "HALF_SIZE"
    elif trailing_active:
        action_hint = "ATR_TRAILING_ACTIVE"
    else:
        action_hint = "NORMAL"

    return {
        "unrealized_pnl_pct":   round(pnl_pct, 2),
        "trailing_stop_active": trailing_active,
        "stop_level":           stop_level,
        "atr_multiplier":       atr_multiplier,
        "atr_ratio_vs_avg":     atr_ratio,
        "kelly_multiplier":     kelly_mult,
        "net_profit_expectancy": net,
        "action_hint":          action_hint,
    }
