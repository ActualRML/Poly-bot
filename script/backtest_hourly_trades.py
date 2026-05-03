"""
script/backtest_hourly_trades.py
=================================
Backtest strategy crypto untuk short-term / hourly mode.

Pendekatan: masuk ke multi-day crypto market saat last available price point
masih dalam window X jam sebelum expiry. CLOB historical hanya punya daily
granularity untuk resolved markets, jadi entry biasanya ~5-30h sebelum expiry
(bukan persis 1h). Tetap valid untuk validasi model short-term.

Flow:
1. Fetch closed multi-day crypto markets dari Gamma
2. Fetch price history (fidelity=1440, daily) untuk tiap market
3. Cari last price point dengan:
   - price antara [min_price, max_price] — filter market yang sudah saturasi
   - max_hours_before jam sebelum expiry
4. Hitung model prob dengan Binance historical klines (vol + drift)
5. Jika |gap| > threshold → entry → hold to resolve
6. Output: trades CSV + summary

Catatan: untuk backtest 1-hour granularity yang akurat, butuh paper trade data.
Historical CLOB data hanya tersedia daily untuk resolved markets.

Run:
    PYTHONIOENCODING=utf-8 python -m script.backtest_hourly_trades
    PYTHONIOENCODING=utf-8 python -m script.backtest_hourly_trades --days 180
    PYTHONIOENCODING=utf-8 python -m script.backtest_hourly_trades --days 365 --max-hours-before 48
"""

from __future__ import annotations

import asyncio
import argparse
import csv
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import aiohttp

# ── Path fix ──────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.logic.probability import CryptoProbabilityCalculator
from src.logic.kelly import KellySizer
from src.backtest.polymarket_history import (
    ClosedMarket,
    PricePoint,
    fetch_closed_crypto_markets,
    fetch_price_history,
)
from src.backtest.question_parser import parse_market
from src.api.binance_client import (
    fetch_historical_realized_vol,
    fetch_historical_short_drift,
)

logging.basicConfig(
    level    = logging.INFO,
    format   = "%(asctime)s %(levelname)s %(message)s",
    handlers = [logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

RESULTS_DIR = ROOT / "data" / "backtest_results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

FALLBACK_VOL = {"BTC": 0.45, "ETH": 0.60, "SOL": 0.80, "BNB": 0.55}


# ─────────────────────────────────────────────
# DATA MODEL
# ─────────────────────────────────────────────

@dataclass
class HourlyTrade:
    market_question  : str
    asset            : str
    direction        : str
    model            : str
    outcome_bought   : str
    entry_price      : float
    entry_model_prob : float
    entry_gap        : float
    minutes_remaining: float
    bet_usdc         : float
    shares           : float
    exit_signal      : str
    pnl_usdc         : float
    pnl_pct          : float
    vol_used         : float
    drift_used       : float
    end_date         : str

    def to_row(self) -> dict:
        return {
            "asset"            : self.asset,
            "question"         : self.market_question[:80],
            "direction"        : self.direction,
            "model"            : self.model,
            "outcome"          : self.outcome_bought,
            "entry_price"      : round(self.entry_price, 4),
            "entry_prob"       : round(self.entry_model_prob, 4),
            "gap"              : round(self.entry_gap, 4),
            "minutes_remaining": round(self.minutes_remaining, 1),
            "bet_usdc"         : round(self.bet_usdc, 2),
            "exit_signal"      : self.exit_signal,
            "pnl_usdc"         : round(self.pnl_usdc, 2),
            "pnl_pct"          : round(self.pnl_pct, 4),
            "vol_24h"          : round(self.vol_used, 4),
            "drift_4h"         : round(self.drift_used, 4),
            "end_date"         : self.end_date,
        }


# ─────────────────────────────────────────────
# HELPER
# ─────────────────────────────────────────────

def _find_entry_price(
    history         : list[PricePoint],
    end_date        : datetime,
    max_hours_before: float = 48.0,
    min_price       : float = 0.04,
    max_price       : float = 0.96,
) -> Optional[tuple[PricePoint, float]]:
    """
    Cari last price point yang:
    1. Masih dalam window max_hours_before sebelum end_date
    2. Price antara [min_price, max_price] — filter market yang sudah saturasi

    Return (point, hours_remaining) atau None.

    Catatan: CLOB daily data membuat last point biasanya 5-30h sebelum expiry.
    """
    if not history:
        return None

    # Ambil semua point dalam window, filter by price range
    candidates = []
    for p in history:
        hours_remaining = (end_date - p.timestamp).total_seconds() / 3600
        if hours_remaining <= 0:
            continue
        if hours_remaining > max_hours_before:
            continue
        if not (min_price <= p.price <= max_price):
            continue
        candidates.append((p, hours_remaining))

    if not candidates:
        return None

    # Ambil yang paling dekat ke expiry (sisa waktu terkecil, tapi > 0)
    candidates.sort(key=lambda x: x[1])
    return candidates[0]


# ─────────────────────────────────────────────
# SINGLE MARKET SIMULATION
# ─────────────────────────────────────────────

async def simulate_market(
    market          : ClosedMarket,
    session         : aiohttp.ClientSession,
    threshold       : float,
    slippage        : float,
    capital         : float,
    max_hours_before: float,
    vol_hours       : int,
    drift_hours     : int,
) -> Optional[HourlyTrade]:
    parsed = parse_market(market.question)
    if parsed is None:
        return None

    # Fetch price history daily (fidelity=1440) — satu-satunya yang tersedia untuk resolved markets
    history = await fetch_price_history(
        market.yes_token_id, session, fidelity_minutes=1440, use_cache=True,
    )
    if len(history) < 2:
        return None

    entry = _find_entry_price(
        history,
        market.end_date,
        max_hours_before = max_hours_before,
        min_price        = 0.04,
        max_price        = 0.96,
    )
    if entry is None:
        return None

    entry_point, hours_remaining = entry
    yes_market_price = entry_point.price
    at_time          = entry_point.timestamp

    # Fetch Binance historical vol + drift pada titik entry
    vol   = await fetch_historical_realized_vol(parsed.asset, session, at_time=at_time, hours=vol_hours)
    drift = await fetch_historical_short_drift(parsed.asset, session, at_time=at_time, hours=drift_hours)

    if vol is None:
        vol = FALLBACK_VOL.get(parsed.asset, 0.65)
    if drift is None:
        drift = 0.0

    # Spot price dari Binance historical klines pada titik entry
    from src.api.binance_client import fetch_klines
    end_ms     = int(at_time.timestamp() * 1000)
    klines     = await fetch_klines(parsed.asset, session, interval="1h", limit=2, end_ms=end_ms)
    if not klines:
        return None
    spot_price = klines[-1][4]

    days_remaining = max(0.001, hours_remaining / 24.0)

    calc = CryptoProbabilityCalculator()
    prob = calc.calculate(
        asset          = parsed.asset,
        current_price  = spot_price,
        target_price   = parsed.target_price,
        days_remaining = days_remaining,
        volatility     = vol,
        direction      = parsed.direction,
        use_barrier    = parsed.use_barrier,
        drift          = drift,
    )
    model_prob = prob.probability

    gap = model_prob - yes_market_price
    if abs(gap) <= threshold:
        return None

    if gap > 0:
        outcome_bought = "Yes"
        decision_price = yes_market_price
        winrate        = model_prob
    else:
        outcome_bought = "No"
        decision_price = 1.0 - yes_market_price
        winrate        = 1.0 - model_prob

    buy_price = min(0.999, decision_price + slippage)
    if not (0.01 < buy_price < 0.99):
        return None

    sizer = KellySizer()
    kelly = sizer.calculate(winrate=winrate, market_price=buy_price, capital=capital)
    if not kelly.is_positive_ev or float(kelly.bet_usdc) <= 0:
        return None

    bet_usdc = float(kelly.bet_usdc)
    shares   = float(kelly.shares)

    yes_won  = market.yes_outcome_price >= 0.5
    won      = yes_won if outcome_bought == "Yes" else (not yes_won)
    pnl_usdc = shares * (1.0 if won else 0.0) - bet_usdc
    pnl_pct  = pnl_usdc / bet_usdc if bet_usdc > 0 else 0.0
    signal   = "RESOLVE_WIN" if won else "RESOLVE_LOSE"

    return HourlyTrade(
        market_question   = market.question,
        asset             = parsed.asset,
        direction         = parsed.direction,
        model             = "barrier" if parsed.use_barrier else "at_expiry",
        outcome_bought    = outcome_bought,
        entry_price       = buy_price,
        entry_model_prob  = model_prob,
        entry_gap         = gap,
        minutes_remaining = hours_remaining * 60,
        bet_usdc          = bet_usdc,
        shares            = shares,
        exit_signal       = signal,
        pnl_usdc          = pnl_usdc,
        pnl_pct           = pnl_pct,
        vol_used          = vol,
        drift_used        = drift,
        end_date          = market.end_date.date().isoformat(),
    )


# ─────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────

def print_summary(trades: list[HourlyTrade], args) -> None:
    if not trades:
        print("\n[SUMMARY] Tidak ada trade — threshold terlalu tinggi atau pasar tidak punya data hourly.")
        print("Tips: coba --threshold 0.08 atau --entry-minutes 90")
        return

    total_bet = sum(t.bet_usdc for t in trades)
    total_pnl = sum(t.pnl_usdc for t in trades)
    roi       = total_pnl / total_bet if total_bet > 0 else 0
    wins      = sum(1 for t in trades if t.pnl_usdc > 0)
    win_rate  = wins / len(trades)

    by_asset: dict[str, list[HourlyTrade]] = {}
    for t in trades:
        by_asset.setdefault(t.asset, []).append(t)

    print(f"\n{'='*65}")
    print(f"SHORT-TERM BACKTEST — {args.days}d | max {args.max_hours_before:.0f}h before expiry | threshold={args.threshold:.0%}")
    print(f"{'='*65}")
    print(f"Total trades : {len(trades)}")
    print(f"Win rate     : {win_rate:.1%}  ({wins}/{len(trades)})")
    print(f"Total bet    : ${total_bet:.2f}")
    print(f"Total PnL    : ${total_pnl:+.2f}")
    print(f"ROI          : {roi:+.1%}")

    print(f"\nPer asset:")
    for asset, ts in sorted(by_asset.items()):
        a_bet = sum(t.bet_usdc for t in ts)
        a_pnl = sum(t.pnl_usdc for t in ts)
        a_roi = a_pnl / a_bet if a_bet > 0 else 0
        a_wr  = sum(1 for t in ts if t.pnl_usdc > 0) / len(ts)
        avg_v = sum(t.vol_used for t in ts) / len(ts)
        print(f"  {asset}: {len(ts):3d} trades | ROI {a_roi:+.1%} | WR {a_wr:.0%} | avg_vol {avg_v:.0%}")

    yes_trades = [t for t in trades if t.outcome_bought == "Yes"]
    no_trades  = [t for t in trades if t.outcome_bought == "No"]
    print(f"\nOutcome split: {len(yes_trades)} YES / {len(no_trades)} NO")

    avg_min = sum(t.minutes_remaining for t in trades) / len(trades)
    avg_gap = sum(abs(t.entry_gap) for t in trades) / len(trades)
    print(f"Avg minutes remaining at entry: {avg_min:.1f}")
    print(f"Avg |gap| at entry            : {avg_gap:.1%}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

async def main(args) -> None:
    async with aiohttp.ClientSession() as session:
        logger.info(f"Fetching closed crypto markets (last {args.days}d)...")
        markets = await fetch_closed_crypto_markets(
            days_back = args.days,
            session   = session,
        )

        if not markets:
            print("Tidak ada closed crypto markets ditemukan.")
            return

        logger.info(
            f"Simulating {len(markets)} markets "
            f"(max {args.max_hours_before:.0f}h before expiry, threshold={args.threshold:.0%}, "
            f"slippage={args.slippage:.0%})..."
        )

        trades  : list[HourlyTrade] = []
        skipped = 0
        no_price_history = 0

        for i, m in enumerate(markets):
            try:
                trade = await simulate_market(
                    market           = m,
                    session          = session,
                    threshold        = args.threshold,
                    slippage         = args.slippage,
                    capital          = args.capital,
                    max_hours_before = args.max_hours_before,
                    vol_hours        = args.vol_hours,
                    drift_hours      = args.drift_hours,
                )
            except Exception as e:
                logger.debug(f"Skip market {m.condition_id[:8]}: {e}")
                trade = None

            if trade is None:
                skipped += 1
            else:
                trades.append(trade)

            if (i + 1) % 25 == 0:
                logger.info(
                    f"Progress {i+1}/{len(markets)} | trades: {len(trades)} | skipped: {skipped}"
                )
            await asyncio.sleep(0.05)

        logger.info(f"Done. {len(trades)} trades, {skipped} skipped/no-signal")
        print_summary(trades, args)

        if trades:
            from datetime import datetime
            ts_str   = datetime.now().strftime("%Y%m%d_%H%M")
            out_path = RESULTS_DIR / f"shortterm_backtest_{args.days}d_{args.max_hours_before:.0f}h_{ts_str}.csv"
            with open(out_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=trades[0].to_row().keys())
                writer.writeheader()
                writer.writerows(t.to_row() for t in trades)
            print(f"\nTrades saved: {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest hourly crypto — masuk di T-N menit sebelum resolve")
    parser.add_argument("--days",             type=int,   default=90,   help="Lookback window (default: 90)")
    parser.add_argument("--max-hours-before",type=float, default=48.0, help="Max jam sebelum expiry untuk entry (default: 48)")
    parser.add_argument("--threshold",        type=float, default=0.12, help="Min |gap| untuk entry (default: 0.12)")
    parser.add_argument("--slippage",         type=float, default=0.01, help="Slippage per side (default: 0.01)")
    parser.add_argument("--capital",          type=float, default=100,  help="Virtual capital per trade (default: 100)")
    parser.add_argument("--vol-hours",        type=int,   default=24,   help="Realized vol lookback hours (default: 24)")
    parser.add_argument("--drift-hours",      type=int,   default=4,    help="Drift lookback hours (default: 4)")
    args = parser.parse_args()
    asyncio.run(main(args))
