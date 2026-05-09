"""
Backtest GBM Probability Strategy untuk Up/Down Hourly.

Simulasi entry bot:
  - Setiap 1h candle Binance diperlakukan sebagai 1 market resolved (open=strike, close=actual)
  - Bot enter pada T-entry_min menit sebelum close
  - Polymarket price diasumsikan dari model GBM dengan LAG (lag_min menit)
    → simulates real-world Polymarket update lag yang bot exploit
  - Edge_up   = P(Up)_now - P(Up)_lag - fee
  - Edge_down = P(Down)_now - P(Down)_lag - fee
  - Trade kalau edge ≥ min_edge

Output:
  - Total trade per edge threshold
  - Winrate per asset
  - ROI estimate (assume market_up = P(Up)_lag)
  - Vol floor on/off comparison

Jalankan:
  python -m script.backtest_gbm
  python -m script.backtest_gbm --days 60 --entry_min 30 --lag_min 15
  python -m script.backtest_gbm --asset BTC --vol_floor 0.0
"""
from __future__ import annotations

import sys
import math
import asyncio
import argparse
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiohttp
from src.api.binance_client import fetch_klines
from src.logic.oracle_arb import gbm_prob_above

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

ASSETS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"]


# ── helpers ──────────────────────────────────────────────────────────────────

def rolling_vol(klines_1h: list, idx: int, n: int = 4) -> float:
    """Annualized realized vol from last n hourly closes."""
    if idx < n + 1:
        return 0.0
    log_rets = []
    for i in range(idx - n + 1, idx + 1):
        prev = float(klines_1h[i - 1][4])
        curr = float(klines_1h[i][4])
        if prev > 0 and curr > 0:
            log_rets.append(math.log(curr / prev))
    if len(log_rets) < 2:
        return 0.0
    mean = sum(log_rets) / len(log_rets)
    var  = sum((r - mean) ** 2 for r in log_rets) / len(log_rets)
    return math.sqrt(var * 365 * 24)


async def fetch_all_klines(
    symbol: str,
    session: aiohttp.ClientSession,
    interval: str,
    since_ms: int,
) -> list:
    STEP = {"1h": 3_600_000, "5m": 300_000, "1m": 60_000}
    step    = STEP.get(interval, 3_600_000)
    results = []
    start   = since_ms
    now_ms  = int(datetime.now(timezone.utc).timestamp() * 1000)

    while start < now_ms:
        batch = await fetch_klines(symbol, session, interval=interval,
                                   limit=1000, start_ms=start)
        if not batch:
            break
        results.extend(batch)
        if len(batch) < 1000:
            break
        last_ms = int(batch[-1][0].timestamp() * 1000)
        start   = last_ms + step

    return results


def index_5m(klines_5m: list) -> dict[int, float]:
    """ms (open_time) → close price."""
    return {int(k[0].timestamp() * 1000): float(k[4]) for k in klines_5m}


def lookup_price(idx: dict[int, float], ts_ms: int, max_offset_min: int = 10) -> Optional[float]:
    """Find closest 5m close to ts_ms within ±max_offset_min."""
    q = (ts_ms // 300_000) * 300_000
    for offset_min in range(0, max_offset_min + 5, 5):
        for sign in (-1, +1):
            probe = q + sign * offset_min * 60_000
            p = idx.get(probe)
            if p and p > 0:
                return p
    return None


# ── strategy simulation ──────────────────────────────────────────────────────

def simulate_trade(
    current: float,
    lagged: float,
    strike: float,
    vol: float,
    T_remaining_s: float,
    fee: float,
    min_edge: float,
    min_price: float = 0.20,
    max_price: float = 0.45,
) -> Optional[dict]:
    """
    Simulate GBM strategy entry at (current, lagged_polymarket_proxy, strike, vol, T).

    P(Up)_now    = GBM with current price (model truth at entry)
    P(Up)_market = GBM with lagged price (proxy for Polymarket price)
                   → market_price_up ≈ P(Up)_market (assumes market = lagged model)

    edge_up   = P(Up)_now - market_price_up - fee
    edge_down = (1-P(Up)_now) - (1-market_price_up) - fee

    Returns trade dict or None (no edge).
    """
    p_up_now    = gbm_prob_above(current, strike, vol, T_remaining_s)
    p_up_market = gbm_prob_above(lagged, strike, vol, T_remaining_s)

    market_price_up   = p_up_market
    market_price_down = 1.0 - p_up_market

    edge_up   = p_up_now - market_price_up - fee
    edge_down = (1.0 - p_up_now) - market_price_down - fee

    if edge_up >= min_edge and edge_up >= edge_down:
        if not (min_price <= market_price_up <= max_price):
            return None  # mirror config UPDOWN_HOURLY_{MIN,MAX}_ENTRY_PRICE
        return {
            "outcome":      "Up",
            "buy_price":    market_price_up,
            "edge":         edge_up,
            "p_up_now":     p_up_now,
            "p_up_market":  p_up_market,
        }
    if edge_down >= min_edge:
        if not (min_price <= market_price_down <= max_price):
            return None
        return {
            "outcome":      "Down",
            "buy_price":    market_price_down,
            "edge":         edge_down,
            "p_up_now":     p_up_now,
            "p_up_market":  p_up_market,
        }
    return None


def settle(trade: dict, actual_up: bool) -> float:
    """
    Compute PnL per $1 capital. At market price p, buy 1/p shares.
    Win → 1 share = $1 payout. Lose → $0.
    Fee already baked into edge calc — here just compute realized profit.
    """
    win = (trade["outcome"] == "Up" and actual_up) or (trade["outcome"] == "Down" and not actual_up)
    if win:
        return (1.0 / trade["buy_price"]) - 1.0  # net profit on $1 stake
    return -1.0


# ── per-asset backtest ────────────────────────────────────────────────────────

async def backtest_asset(
    symbol: str,
    session: aiohttp.ClientSession,
    days: int,
    entry_min: int,
    lag_min: int,
    fee: float,
    min_edge: float,
    vol_floor: float,
    min_price: float,
    max_price: float,
) -> list[dict]:
    since_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)

    print(f"  [{symbol}] Fetching 1h + 5m klines...", flush=True)
    klines_1h, klines_5m = await asyncio.gather(
        fetch_all_klines(symbol, session, "1h", since_ms),
        fetch_all_klines(symbol, session, "5m", since_ms),
    )
    if len(klines_1h) < 5 or len(klines_5m) < 5:
        print(f"  [{symbol}] Data tidak cukup")
        return []

    idx5 = index_5m(klines_5m)

    results: list[dict] = []
    for i, k in enumerate(klines_1h[1:], start=1):
        open_time  = int(k[0].timestamp() * 1000)
        close_time = open_time + 3_600_000
        strike     = float(k[1])  # 1h candle open = strike
        final      = float(k[4])  # 1h candle close = settlement

        if strike <= 0 or final <= 0:
            continue

        actual_up = final > strike
        T_remaining_s = entry_min * 60.0

        entry_ms  = close_time - entry_min * 60_000
        lagged_ms = entry_ms - lag_min * 60_000
        if lagged_ms <= open_time:
            continue

        current = lookup_price(idx5, entry_ms)
        lagged  = lookup_price(idx5, lagged_ms)
        if not current or not lagged:
            continue

        vol = rolling_vol(klines_1h, i)
        if vol <= 0:
            vol = 0.40
        if vol_floor > 0:
            vol = max(vol, vol_floor)

        trade = simulate_trade(
            current=current, lagged=lagged, strike=strike,
            vol=vol, T_remaining_s=T_remaining_s,
            fee=fee, min_edge=min_edge,
            min_price=min_price, max_price=max_price,
        )
        if trade is None:
            continue

        pnl = settle(trade, actual_up)
        results.append({
            "symbol":    symbol,
            "ts":        datetime.fromtimestamp(open_time / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "outcome":   trade["outcome"],
            "buy_price": trade["buy_price"],
            "edge":      trade["edge"],
            "p_now":     trade["p_up_now"],
            "p_mkt":     trade["p_up_market"],
            "actual_up": actual_up,
            "win":       pnl > 0,
            "pnl":       pnl,
            "vol":       vol,
        })
    return results


# ── reporting ────────────────────────────────────────────────────────────────

def print_report(rows: list[dict], days: int, entry_min: int, lag_min: int,
                 fee: float, min_edge: float, vol_floor: float):
    print()
    print("=" * 72)
    print(f"  GBM STRATEGY BACKTEST — {days}d | entry T-{entry_min}m | lag {lag_min}m")
    print(f"  fee={fee:.3f} | min_edge={min_edge:.2f} | vol_floor={vol_floor:.2f}")
    print("=" * 72)

    if not rows:
        print("\n  Tidak ada trade — coba turunkan min_edge atau tambah days.")
        return

    by_asset: dict[str, list] = defaultdict(list)
    for r in rows:
        by_asset[r["symbol"]].append(r)

    print()
    print(f"  {'asset':<6} {'trades':>7} {'wins':>5} {'wr':>7} {'avg pnl':>9} {'total':>8} {'roi':>8}")
    print("  " + "-" * 60)

    total_n = total_w = 0
    total_pnl = 0.0
    for symbol in ASSETS:
        rs = by_asset.get(symbol, [])
        if not rs:
            continue
        n = len(rs)
        w = sum(1 for r in rs if r["win"])
        sym_pnl = sum(r["pnl"] for r in rs)
        roi = sym_pnl / n
        print(f"  {symbol:<6} {n:>7} {w:>5} {w/n:>6.1%} {sym_pnl/n:>+9.4f} {sym_pnl:>+8.2f} {roi:>+7.2%}")
        total_n += n
        total_w += w
        total_pnl += sym_pnl

    print("  " + "-" * 60)
    if total_n:
        print(f"  {'TOTAL':<6} {total_n:>7} {total_w:>5} {total_w/total_n:>6.1%} "
              f"{total_pnl/total_n:>+9.4f} {total_pnl:>+8.2f} {total_pnl/total_n:>+7.2%}")

    # edge bucket analysis
    buckets = [(0.05, 0.07), (0.07, 0.10), (0.10, 0.20), (0.20, 1.0)]
    print()
    print("  EDGE BUCKETS:")
    print(f"  {'range':<14} {'n':>5} {'wr':>7} {'avg pnl':>9} {'total':>8}")
    print("  " + "-" * 50)
    for lo, hi in buckets:
        bucket = [r for r in rows if lo <= r["edge"] < hi]
        if not bucket:
            continue
        n = len(bucket)
        w = sum(1 for r in bucket if r["win"])
        bp = sum(r["pnl"] for r in bucket)
        label = f"{lo:.2f}–{hi:.2f}"
        print(f"  {label:<14} {n:>5} {w/n:>6.1%} {bp/n:>+9.4f} {bp:>+8.2f}")

    # max drawdown estimation (sequential)
    print()
    rs_sorted = sorted(rows, key=lambda r: r["ts"])
    peak = 0.0
    cum = 0.0
    max_dd = 0.0
    for r in rs_sorted:
        cum += r["pnl"]
        peak = max(peak, cum)
        dd = peak - cum
        max_dd = max(max_dd, dd)
    avg_per_trade = total_pnl / total_n if total_n else 0.0
    print(f"  Avg PnL/trade : {avg_per_trade:+.4f} (per $1 stake)")
    print(f"  Max drawdown  : -{max_dd:.2f} ($ units)")
    print(f"  Trade rate    : {total_n / days:.1f} per day per all assets")

    # interpretation — use ROI (avg PnL / trade) as primary KPI, not raw winrate
    # because at low buy_prices (e.g. 0.30) winning 50% can still be very profitable.
    print()
    if total_n >= 50:
        wr = total_w / total_n
        roi = total_pnl / total_n
        if roi >= 0.30:
            verdict = f"✅ EDGE KUAT — avg PnL +{roi:.2%}/trade, ready paper trade"
        elif roi >= 0.10:
            verdict = f"🟢 EDGE TERBUKTI — avg PnL +{roi:.2%}/trade"
        elif roi >= 0.0:
            verdict = f"🟡 EDGE TIPIS — avg PnL +{roi:.2%}/trade, fee margin tipis"
        else:
            verdict = f"❌ TIDAK PROFITABLE — avg PnL {roi:+.2%}/trade"
        print(f"  VERDICT: {verdict}")
        print(f"  Stats   : winrate {wr:.1%} | ROI {roi:+.2%}/trade | profit factor "
              f"{(sum(r['pnl'] for r in rows if r['win']) / abs(sum(r['pnl'] for r in rows if not r['win']))) if any(not r['win'] for r in rows) else float('inf'):.2f}")
    else:
        print(f"  ⚠️  Sample size {total_n} < 50 — naikkan --days untuk reliable estimate")
    print("=" * 72)


# ── main ────────────────────────────────────────────────────────────────────

async def main(args):
    assets = [args.asset.upper()] if args.asset else ASSETS
    invalid = [a for a in assets if a not in ASSETS]
    if invalid:
        print(f"Asset tidak dikenal: {invalid}. Pilih dari: {ASSETS}")
        return

    print(f"\nGBM Backtest — {args.days}d, entry T-{args.entry_min}m, lag {args.lag_min}m")
    print(f"Assets: {assets}\n")

    async with aiohttp.ClientSession() as session:
        all_results = []
        for symbol in assets:
            rs = await backtest_asset(
                symbol=symbol, session=session,
                days=args.days, entry_min=args.entry_min,
                lag_min=args.lag_min, fee=args.fee,
                min_edge=args.min_edge, vol_floor=args.vol_floor,
                min_price=args.min_price, max_price=args.max_price,
            )
            print(f"  [{symbol}] {len(rs)} trades simulated")
            all_results.extend(rs)

    print_report(
        all_results, days=args.days, entry_min=args.entry_min,
        lag_min=args.lag_min, fee=args.fee,
        min_edge=args.min_edge, vol_floor=args.vol_floor,
    )


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Backtest GBM hourly strategy")
    parser.add_argument("--days",      type=int,   default=30, help="Lookback days (default 30)")
    parser.add_argument("--entry_min", type=int,   default=30, help="Entry N min before close (default 30)")
    parser.add_argument("--lag_min",   type=int,   default=15, help="Polymarket assumed lag (default 15)")
    parser.add_argument("--fee",       type=float, default=0.018, help="Taker fee (default 0.018)")
    parser.add_argument("--min_edge",  type=float, default=0.05, help="Min edge gate (default 0.05)")
    parser.add_argument("--vol_floor", type=float, default=0.50, help="Min annualized vol (default 0.50, set 0 to disable)")
    parser.add_argument("--min_price", type=float, default=0.20, help="Min entry price (mirror config, default 0.20)")
    parser.add_argument("--max_price", type=float, default=0.45, help="Max entry price (mirror config, default 0.45)")
    parser.add_argument("--asset",     type=str,   default=None, help=f"Filter: {ASSETS}")
    args = parser.parse_args()

    asyncio.run(main(args))
