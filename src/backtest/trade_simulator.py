"""
src/backtest/trade_simulator.py
===============================
Simulator full-lifecycle untuk strategy crypto mispricing.

Untuk tiap closed market:
1. Parse question → asset, target, direction, model
2. Iterasi per-day dari start ke end:
   - hitung model prob (probability.py) pakai historical IV + spot
   - bandingkan ke YES market price → gap
   - kalau no posisi & |gap| > threshold → entry (Kelly sizing)
   - kalau ada posisi → evaluate exit (trailing stop / lock profit / stale)
3. Resolve di end_date — sisa posisi close ke outcome (0 atau 1)

Output: list[SimulatedTrade] untuk dianalisis di reporting layer.

Reuse:
- src.logic.probability.CryptoProbabilityCalculator
- src.logic.kelly.KellySizer
- src.logic.exit_strategy.ExitEvaluator + Position

Tidak touch main.py / live system. Pure offline.
"""

from __future__ import annotations

import math
import asyncio
import logging
from decimal import Decimal
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta, date
from typing import Optional

import aiohttp

from src.logic.probability import CryptoProbabilityCalculator
from src.logic.kelly import KellySizer
from src.logic.exit_strategy import ExitEvaluator, Position, ExitSignal
from src.logic.pricing import ke_decimal

from src.backtest.polymarket_history import (
    ClosedMarket, PricePoint, fetch_price_history,
)
from src.backtest.iv_history import fetch_iv_history, get_iv_at, _fetch_coingecko_history

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# DATA MODELS
# ─────────────────────────────────────────────

@dataclass
class SimulatedTrade:
    market_question: str
    asset: str
    target_price: float
    direction: str
    model: str

    outcome_bought: str        # "Yes" or "No"
    entry_date: datetime
    entry_market_price: float
    entry_model_prob: float
    entry_gap: float           # base_rate - market_price (signed)
    bet_usdc: float
    shares: float

    exit_date: datetime
    exit_market_price: float
    exit_signal: str           # "RESOLVE_WIN", "RESOLVE_LOSE", "EXIT_TRAILING", etc.
    pnl_usdc: float
    pnl_pct: float

    days_held: int

    def to_row(self) -> dict:
        return {
            "asset":            self.asset,
            "target":           self.target_price,
            "direction":        self.direction,
            "model":            self.model,
            "outcome":          self.outcome_bought,
            "entry_date":       self.entry_date.date().isoformat(),
            "entry_price":      round(self.entry_market_price, 4),
            "entry_prob":       round(self.entry_model_prob, 4),
            "gap":              round(self.entry_gap, 4),
            "bet_usdc":         round(self.bet_usdc, 2),
            "exit_date":        self.exit_date.date().isoformat(),
            "exit_price":       round(self.exit_market_price, 4),
            "exit_signal":      self.exit_signal,
            "pnl_usdc":         round(self.pnl_usdc, 2),
            "pnl_pct":          round(self.pnl_pct, 4),
            "days_held":        self.days_held,
            "question":         self.market_question[:80],
        }


@dataclass
class SimulatorConfig:
    threshold: float            = 0.15      # min gap untuk entry
    capital_per_trade: float    = 100.0     # virtual modal per trade (independent positions)
    trailing_stop_pct: float    = 0.15
    profit_threshold: float     = 0.85
    tight_trailing_pct: float   = 0.07
    days_hold_to_resolve: int   = 3
    max_days_stale: int         = 21
    # Realism corrections (default ON — set 0 untuk reproduce old/biased behavior)
    execution_lag_days: int     = 1         # decide at T, fill at T+lag (0 = look-ahead)
    slippage_pct: float         = 0.01      # bid-ask haircut per side (0 = mid-price)


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _build_yes_price_lookup(history: list[PricePoint]) -> dict[date, float]:
    """Index price history by date — kalau multiple points di hari sama, ambil terakhir."""
    out: dict[date, float] = {}
    for p in sorted(history, key=lambda x: x.timestamp):
        out[p.timestamp.date()] = p.price
    return out


def _build_spot_price_lookup(prices: list[tuple[date, float]]) -> dict[date, float]:
    return {d: p for d, p in prices}


def _get_price_at(lookup: dict[date, float], target: date, max_lookback: int = 7) -> Optional[float]:
    """Ambil harga di tanggal target, fallback ke nearest preceding (max 7 hari)."""
    if target in lookup:
        return lookup[target]
    for delta in range(1, max_lookback + 1):
        prev = target - timedelta(days=delta)
        if prev in lookup:
            return lookup[prev]
    return None


def _compute_rolling_drift(
    spot_lookup: dict[date, float], at_date: date, window: int = 30,
) -> float:
    """Annualized drift dari log return window hari ke at_date."""
    end_price = _get_price_at(spot_lookup, at_date)
    start_price = _get_price_at(spot_lookup, at_date - timedelta(days=window))
    if not end_price or not start_price or start_price <= 0 or end_price <= 0:
        return 0.0
    n_days = window
    return math.log(end_price / start_price) / (n_days / 365.0)


def _get_execution_price(
    lookup: dict[date, float], decision_date: date, lag_days: int, max_search: int = 3,
) -> Optional[float]:
    """
    Cari harga eksekusi di decision_date + lag_days, fallback ke beberapa hari ke depan
    kalau weekend/data gap.
    """
    target = decision_date + timedelta(days=lag_days)
    for delta in range(0, max_search + 1):
        d = target + timedelta(days=delta)
        if d in lookup:
            return lookup[d]
    return None


def _apply_slippage(quoted_price: float, slippage: float, side: str) -> float:
    """
    side='buy'  → bayar lebih (cross spread): quoted + slippage
    side='sell' → terima lebih sedikit: quoted - slippage
    Clamp ke (0.001, 0.999) — gak bisa di luar range.
    """
    if side == "buy":
        return min(0.999, quoted_price + slippage)
    elif side == "sell":
        return max(0.001, quoted_price - slippage)
    raise ValueError(f"side must be 'buy' or 'sell', got {side}")


def _resolve_pnl(outcome_bought: str, yes_won: bool, entry_price: float, shares: float, bet_usdc: float) -> tuple[float, str]:
    """Return (pnl_usdc, signal_string) saat market resolve."""
    if outcome_bought == "Yes":
        won = yes_won
    else:
        won = not yes_won

    final_share_value = 1.0 if won else 0.0
    pnl = shares * final_share_value - bet_usdc
    signal = "RESOLVE_WIN" if won else "RESOLVE_LOSE"
    return pnl, signal


# ─────────────────────────────────────────────
# SINGLE-MARKET SIMULATION
# ─────────────────────────────────────────────

async def simulate_market(
    market: ClosedMarket,
    parsed,                              # ParsedMarket dari question_parser
    iv_history_lookup: dict[date, float],
    spot_history_lookup: dict[date, float],
    yes_price_lookup: dict[date, float],
    cfg: SimulatorConfig,
) -> list[SimulatedTrade]:
    """
    Simulasi satu market full lifecycle.
    Return list trade — biasanya 0 atau 1 (bisa lebih kalau re-entry, tapi default cuma 1).
    """
    trades: list[SimulatedTrade] = []

    calc     = CryptoProbabilityCalculator()
    sizer    = KellySizer()
    exit_eval = ExitEvaluator(
        trailing_stop_pct       = cfg.trailing_stop_pct,
        profit_threshold        = cfg.profit_threshold,
        tight_trailing_stop_pct = cfg.tight_trailing_pct,
        days_hold_to_resolve    = cfg.days_hold_to_resolve,
        max_days_stale          = cfg.max_days_stale,
    )

    yes_won = market.yes_outcome_price >= 0.5

    cursor = market.start_date
    end    = market.end_date

    open_position: Optional[Position] = None
    open_outcome: Optional[str] = None
    open_entry_market_price: Optional[float] = None
    open_entry_model_prob: Optional[float] = None
    open_entry_gap: Optional[float] = None
    open_bet_usdc: Optional[float] = None
    open_entry_date: Optional[datetime] = None

    while cursor.date() <= end.date():
        days_remaining = (end - cursor).days
        if days_remaining <= 0:
            break

        d = cursor.date()
        spot = _get_price_at(spot_history_lookup, d)
        iv   = get_iv_at(iv_history_lookup, d, fallback=0.65)
        yes_market_price = _get_price_at(yes_price_lookup, d)

        if spot is None or yes_market_price is None:
            cursor += timedelta(days=1)
            continue

        # Degenerate: posisi terbuka harus tetap di-evaluate (force-exit di saturasi),
        # tapi entry baru gak boleh dibuka. Used to skip everything (bug).
        is_degenerate = yes_market_price <= 0.02 or yes_market_price >= 0.98

        drift = _compute_rolling_drift(spot_history_lookup, d, window=30)

        prob_result = calc.calculate(
            asset          = parsed.asset,
            current_price  = spot,
            target_price   = parsed.target_price,
            days_remaining = days_remaining,
            volatility     = iv,
            direction      = parsed.direction,
            use_barrier    = parsed.use_barrier,
            drift          = drift,
        )
        model_prob = prob_result.probability

        # ── Update open position (kalau ada) ──────────────────────────────
        if open_position is not None:
            # Compute current price untuk outcome yang dibeli (decision-side, untuk eval)
            if open_outcome == "Yes":
                current_decision = yes_market_price
            else:
                current_decision = 1.0 - yes_market_price

            open_position.current_price = ke_decimal(current_decision)
            open_position = exit_eval.update_highest_price(open_position)

            # Hack: ExitEvaluator pakai datetime.now() untuk days_to_resolve & days_held.
            # Kita override resolve_date & entry_time agar match cursor sekarang.
            # Tambahin offset agar (resolve_date - now()) == days_remaining_at_cursor.
            now = datetime.now(timezone.utc)
            open_position.resolve_date = now + timedelta(days=days_remaining)
            entry_age_days = (cursor - open_entry_date).days if open_entry_date else 0
            open_position.entry_time = now - timedelta(days=entry_age_days)

            decision = exit_eval.evaluate(open_position)
            forced_signal = None

            # Force-exit kalau price saturated (live system would have closed via lock_profit)
            if not decision.should_exit and is_degenerate:
                if current_decision >= 0.98:
                    forced_signal = "exit_lock_profit"
                elif current_decision <= 0.02:
                    forced_signal = "exit_trailing"

            if decision.should_exit or forced_signal:
                # Exit execution: cari harga di T+lag pada decision-side
                if cfg.execution_lag_days > 0:
                    exec_yes = _get_execution_price(
                        yes_price_lookup, d, cfg.execution_lag_days, max_search=3,
                    )
                    if exec_yes is None:
                        exec_yes = yes_market_price  # fallback ke today
                    exit_decision_price = exec_yes if open_outcome == "Yes" else 1.0 - exec_yes
                else:
                    exit_decision_price = current_decision

                # Apply slippage (sell side — terima lebih sedikit)
                exit_price = _apply_slippage(exit_decision_price, cfg.slippage_pct, side="sell")

                shares     = float(open_position.shares)
                pnl_usdc   = shares * exit_price - open_bet_usdc
                pnl_pct    = pnl_usdc / open_bet_usdc if open_bet_usdc > 0 else 0.0
                days_held  = (cursor - open_entry_date).days if open_entry_date else 0
                signal_value = forced_signal if forced_signal else decision.signal.value

                trades.append(SimulatedTrade(
                    market_question     = market.question,
                    asset               = parsed.asset,
                    target_price        = parsed.target_price,
                    direction           = parsed.direction,
                    model               = "barrier" if parsed.use_barrier else "at_expiry",
                    outcome_bought      = open_outcome,
                    entry_date          = open_entry_date,
                    entry_market_price  = open_entry_market_price,
                    entry_model_prob    = open_entry_model_prob,
                    entry_gap           = open_entry_gap,
                    bet_usdc            = open_bet_usdc,
                    shares              = shares,
                    exit_date           = cursor,
                    exit_market_price   = exit_price,
                    exit_signal         = signal_value,
                    pnl_usdc            = pnl_usdc,
                    pnl_pct             = pnl_pct,
                    days_held           = days_held,
                ))
                # Reset
                open_position = None
                open_outcome = None
                open_entry_market_price = None
                open_entry_model_prob = None
                open_entry_gap = None
                open_bet_usdc = None
                open_entry_date = None

        # ── Cek entry (kalau no posisi & price tidak degenerate) ──────────
        if open_position is None and not is_degenerate:
            gap = model_prob - yes_market_price

            if abs(gap) > cfg.threshold:
                if gap > 0:
                    outcome_bought = "Yes"
                    decision_price = yes_market_price
                    winrate        = model_prob
                else:
                    outcome_bought = "No"
                    decision_price = 1.0 - yes_market_price
                    winrate        = 1.0 - model_prob

                # Eksekusi di T+lag (kalau lag>0). Decision tetap di T (signal aja).
                if cfg.execution_lag_days > 0:
                    exec_yes = _get_execution_price(
                        yes_price_lookup, d, cfg.execution_lag_days, max_search=3,
                    )
                    if exec_yes is None:
                        cursor += timedelta(days=1)
                        continue
                    exec_decision_price = exec_yes if outcome_bought == "Yes" else 1.0 - exec_yes
                else:
                    exec_decision_price = decision_price

                # Apply slippage (buy side — bayar lebih)
                buy_price = _apply_slippage(exec_decision_price, cfg.slippage_pct, side="buy")

                # Skip kalau exec price degenerate post-shift
                if not (0.01 < buy_price < 0.99):
                    cursor += timedelta(days=1)
                    continue

                kelly = sizer.calculate(
                    winrate      = winrate,
                    market_price = buy_price,
                    capital      = cfg.capital_per_trade,
                )

                if kelly.bet_usdc > Decimal("0"):
                    bet_usdc = float(kelly.bet_usdc)
                    shares   = float(kelly.shares)

                    # Build Position
                    open_position = Position(
                        condition_id    = market.condition_id,
                        outcome         = outcome_bought,
                        entry_price     = ke_decimal(buy_price),
                        current_price   = ke_decimal(buy_price),
                        highest_price   = ke_decimal(buy_price),
                        shares          = ke_decimal(shares),
                        capital_at_risk = ke_decimal(bet_usdc),
                        resolve_date    = end,           # placeholder, di-override per-iteration
                        entry_time      = cursor,        # placeholder
                        question        = market.question,
                        token_id        = market.yes_token_id if outcome_bought == "Yes" else market.no_token_id,
                    )
                    open_outcome = outcome_bought
                    open_entry_market_price = buy_price
                    open_entry_model_prob = model_prob
                    open_entry_gap = gap
                    open_bet_usdc = bet_usdc
                    open_entry_date = cursor

        cursor += timedelta(days=1)

    # ── Resolve sisa posisi di end ──────────────────────────────────────
    if open_position is not None:
        shares = float(open_position.shares)
        pnl_usdc, signal = _resolve_pnl(
            outcome_bought = open_outcome,
            yes_won        = yes_won,
            entry_price    = open_entry_market_price,
            shares         = shares,
            bet_usdc       = open_bet_usdc,
        )
        pnl_pct   = pnl_usdc / open_bet_usdc if open_bet_usdc > 0 else 0.0
        days_held = (end - open_entry_date).days if open_entry_date else 0
        final_yes_price = _get_price_at(yes_price_lookup, end.date()) or market.yes_outcome_price
        if open_outcome == "No":
            final_market_price = 1.0 - final_yes_price
        else:
            final_market_price = final_yes_price

        trades.append(SimulatedTrade(
            market_question     = market.question,
            asset               = parsed.asset,
            target_price        = parsed.target_price,
            direction           = parsed.direction,
            model               = "barrier" if parsed.use_barrier else "at_expiry",
            outcome_bought      = open_outcome,
            entry_date          = open_entry_date,
            entry_market_price  = open_entry_market_price,
            entry_model_prob    = open_entry_model_prob,
            entry_gap           = open_entry_gap,
            bet_usdc            = open_bet_usdc,
            shares              = shares,
            exit_date           = end,
            exit_market_price   = final_market_price,
            exit_signal         = signal,
            pnl_usdc            = pnl_usdc,
            pnl_pct             = pnl_pct,
            days_held           = days_held,
        ))

    return trades


# ─────────────────────────────────────────────
# BATCH ORCHESTRATOR
# ─────────────────────────────────────────────

async def run_simulation(
    markets: list[ClosedMarket],
    cfg: SimulatorConfig,
    session: aiohttp.ClientSession,
) -> list[SimulatedTrade]:
    """
    Loop semua markets, fetch IV/spot/price history, simulate, gather trades.
    Pre-fetch IV history per asset (cache satu kali per asset) untuk efisiensi.
    """
    from src.backtest.question_parser import parse_market

    if not markets:
        return []

    # Pre-fetch IV per asset, span = full window
    earliest = min(m.start_date for m in markets)
    latest   = max(m.end_date for m in markets)
    # Tambah buffer 30 hari biar rolling drift di start punya prior data
    fetch_start = earliest - timedelta(days=35)

    iv_per_asset: dict[str, dict[date, float]] = {}
    spot_per_asset: dict[str, dict[date, float]] = {}

    assets_present = set(_classify_safe(m.question) for m in markets)
    assets_present.discard(None)

    for asset in assets_present:
        iv_per_asset[asset]   = await fetch_iv_history(asset, fetch_start, latest, session)
        # Spot history dari CoinGecko (cap 365 oleh free tier)
        span_days = (latest - fetch_start).days
        prices    = await _fetch_coingecko_history(asset, min(span_days, 365), session)
        spot_per_asset[asset] = _build_spot_price_lookup(prices)
        await asyncio.sleep(0.5)

    all_trades: list[SimulatedTrade] = []
    skipped_parse = 0
    skipped_no_price = 0

    for i, m in enumerate(markets):
        parsed = parse_market(m.question)
        if parsed is None or parsed.asset not in iv_per_asset:
            skipped_parse += 1
            continue

        history = await fetch_price_history(m.yes_token_id, session)
        if len(history) < 3:
            skipped_no_price += 1
            continue

        yes_price_lookup = _build_yes_price_lookup(history)

        trades = await simulate_market(
            market               = m,
            parsed               = parsed,
            iv_history_lookup    = iv_per_asset[parsed.asset],
            spot_history_lookup  = spot_per_asset[parsed.asset],
            yes_price_lookup     = yes_price_lookup,
            cfg                  = cfg,
        )
        all_trades.extend(trades)

        if (i + 1) % 20 == 0:
            logger.info(f"[SIM] Processed {i+1}/{len(markets)} markets, {len(all_trades)} trades so far")

        # Throttle CLOB API
        await asyncio.sleep(0.2)

    logger.info(
        f"[SIM] Done. {len(all_trades)} trades from {len(markets)} markets "
        f"(skip parse: {skipped_parse}, skip no-price: {skipped_no_price})"
    )
    return all_trades


def _classify_safe(question: str) -> Optional[str]:
    """Quick asset classifier (longgar) untuk pre-fetch IV per-asset."""
    from src.backtest.question_parser import parse_market
    p = parse_market(question)
    return p.asset if p else None
