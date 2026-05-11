"""
Backtest Candle Up/Down Strategy.

Logic yang disimulasikan:
  - Entry di menit ke-5 hingga 15 setelah candle 1h Binance dibuka
  - Signal: momentum 15m = (close_now - close_15m_ago) / close_15m_ago
  - Direction: momentum > threshold → BUY Up; < -threshold → BUY Down
  - Settlement: candle 1h hijau = Up win, merah = Down win

Market price proxy:
  - Default: 0.50 flat (pasar 50/50, konservatif)
  - --use_gbm: P(Up) dari GBM di titik entry (lebih realistis)

Output:
  - WR per symbol, per momentum threshold bucket
  - ROI estimate (net of fee)
  - Analisis threshold optimal
  - Perbandingan entry window (5m, 10m, 15m ke dalam candle)

Jalankan:
  python -m script.backtest_candle
  python -m script.backtest_candle --days 60 --threshold 0.002
  python -m script.backtest_candle --days 30 --use_gbm
  python -m script.backtest_candle --asset BTC --days 90
"""
from __future__ import annotations

import sys
import math
import asyncio
import argparse
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiohttp
from src.api.binance_client import fetch_klines
from src.logic.oracle_arb import gbm_prob_above

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

ASSETS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"]
FEE    = 0.018


# ── helpers ──────────────────────────────────────────────────────────────────

async def fetch_all_klines(
    symbol: str,
    session: aiohttp.ClientSession,
    interval: str,
    since_ms: int,
) -> list:
    STEP = {"1h": 3_600_000, "1m": 60_000}
    step  = STEP.get(interval, 60_000)
    out   = []
    start = since_ms
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    while start < now_ms:
        batch = await fetch_klines(symbol, session, interval=interval, limit=1000, start_ms=start)
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 1000:
            break
        last_ms = int(batch[-1][0].timestamp() * 1000)
        start   = last_ms + step

    return out


def index_1m(klines_1m: list) -> dict[int, list]:
    """ms → kline row."""
    return {int(k[0].timestamp() * 1000): k for k in klines_1m}


def rolling_vol_1h(klines_1h: list, idx: int, n: int = 24) -> float:
    if idx < n + 1:
        return 0.40
    log_rets = []
    for i in range(idx - n + 1, idx + 1):
        p = float(klines_1h[i - 1][4])
        c = float(klines_1h[i][4])
        if p > 0 and c > 0:
            log_rets.append(math.log(c / p))
    if len(log_rets) < 2:
        return 0.40
    mean = sum(log_rets) / len(log_rets)
    var  = sum((r - mean) ** 2 for r in log_rets) / len(log_rets)
    return math.sqrt(var * 365 * 24)


def momentum_15m(idx_1m: dict[int, list], entry_ms: int) -> Optional[float]:
    """15m momentum at entry_ms: (close_now - close_15m_ago) / close_15m_ago."""
    now_k  = idx_1m.get(entry_ms)
    ago_k  = idx_1m.get(entry_ms - 15 * 60_000)
    if not now_k or not ago_k:
        # try ±1min tolerance
        for off in range(1, 3):
            now_k  = now_k  or idx_1m.get(entry_ms - off * 60_000)
            ago_k  = ago_k  or idx_1m.get(entry_ms - 15 * 60_000 - off * 60_000)
    if not now_k or not ago_k:
        return None
    c_now = float(now_k[4])
    c_ago = float(ago_k[4])
    if c_ago <= 0:
        return None
    return (c_now - c_ago) / c_ago


# ── simulation ────────────────────────────────────────────────────────────────

def simulate_candle_entry(
    mom: float,
    threshold: float,
    max_buy_price: float,
    vol: float,
    T_remaining_s: float,
    strike: float,
    current: float,
    use_gbm: bool,
) -> Optional[dict]:
    """Return trade dict or None if no entry signal."""
    if abs(mom) < threshold:
        return None

    direction = "Up" if mom > 0 else "Down"

    if use_gbm and strike > 0 and current > 0 and vol > 0 and T_remaining_s > 0:
        p_up       = gbm_prob_above(current, strike, vol, T_remaining_s)
        buy_price  = p_up if direction == "Up" else round(1.0 - p_up, 4)
    else:
        buy_price = 0.50

    if buy_price <= 0 or buy_price >= 1:
        return None
    if buy_price > max_buy_price:
        return None

    return {"outcome": direction, "buy_price": buy_price, "momentum": mom}


def settle(trade: dict, actual_up: bool, fee: float) -> float:
    win = (trade["outcome"] == "Up" and actual_up) or \
          (trade["outcome"] == "Down" and not actual_up)
    if win:
        return (1.0 / trade["buy_price"]) - 1.0 - fee
    return -1.0


# ── per-asset backtest ────────────────────────────────────────────────────────

async def backtest_asset(
    symbol: str,
    session: aiohttp.ClientSession,
    days: int,
    threshold: float,
    max_buy_price: float,
    entry_minutes: list[int],
    use_gbm: bool,
) -> list[dict]:
    since_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)

    print(f"  [{symbol}] Fetching 1h + 1m klines...", flush=True)
    klines_1h, klines_1m = await asyncio.gather(
        fetch_all_klines(symbol, session, "1h", since_ms),
        fetch_all_klines(symbol, session, "1m", since_ms),
    )
    if len(klines_1h) < 5 or len(klines_1m) < 20:
        print(f"  [{symbol}] Data tidak cukup")
        return []

    idx_1m = index_1m(klines_1m)
    results: list[dict] = []

    for i, k in enumerate(klines_1h[1:], start=1):
        open_ms  = int(k[0].timestamp() * 1000)
        close_ms = open_ms + 3_600_000
        strike   = float(k[1])
        final    = float(k[4])

        if strike <= 0 or final <= 0:
            continue

        actual_up = final > strike
        vol       = rolling_vol_1h(klines_1h, i)

        for entry_min in entry_minutes:
            entry_ms      = open_ms + entry_min * 60_000
            T_remaining_s = (close_ms - entry_ms) / 1000.0

            if entry_ms >= close_ms:
                continue

            current_k = idx_1m.get(entry_ms)
            if not current_k:
                current_k = idx_1m.get(entry_ms - 60_000)
            if not current_k:
                continue
            current = float(current_k[4])

            mom = momentum_15m(idx_1m, entry_ms)
            if mom is None:
                continue

            trade = simulate_candle_entry(
                mom           = mom,
                threshold     = threshold,
                max_buy_price = max_buy_price,
                vol           = vol,
                T_remaining_s = T_remaining_s,
                strike        = strike,
                current       = current,
                use_gbm       = use_gbm,
            )
            if trade is None:
                continue

            pnl = settle(trade, actual_up, FEE)
            results.append({
                "symbol":     symbol,
                "ts":         datetime.fromtimestamp(open_ms / 1000, timezone.utc).strftime("%Y-%m-%d %H"),
                "entry_min":  entry_min,
                "outcome":    trade["outcome"],
                "buy_price":  trade["buy_price"],
                "momentum":   mom,
                "actual_up":  actual_up,
                "win":        pnl > 0,
                "pnl":        pnl,
            })

    return results


# ── reporting ─────────────────────────────────────────────────────────────────

def print_report(rows: list[dict], days: int, threshold: float,
                 max_buy_price: float, entry_minutes: list[int], use_gbm: bool):
    W = 72
    print()
    print("=" * W)
    print(f"  CANDLE BACKTEST — {days}d | threshold={threshold:.4f} | max_price={max_buy_price:.2f}")
    print(f"  Entry windows: menit ke-{entry_minutes} | price proxy: {'GBM' if use_gbm else 'flat 0.50'}")
    print("=" * W)

    if not rows:
        print("\n  Tidak ada trade — coba turunkan --threshold.")
        print("=" * W)
        return

    n  = len(rows)
    w  = sum(1 for r in rows if r["win"])
    pnl = sum(r["pnl"] for r in rows)
    wr  = w / n
    roi = pnl / n

    print()
    print(f"  Total entries    : {n}  ({n/days:.1f}/hari)")
    print(f"  Wins / Losses    : {w} / {n - w}")
    print(f"  Winrate          : {wr:.1%}")
    print(f"  Total PnL        : {pnl:+.3f}")
    print(f"  ROI/trade        : {roi:+.4f} per $1 stake")

    # ── Per-symbol ───────────────────────────────────────────────────────────
    print()
    print(f"  PER SYMBOL:")
    print(f"  {'sym':<6} {'n':>6} {'wr':>7} {'roi/t':>9} {'entry/d':>9}")
    print("  " + "-" * 44)
    by_sym = defaultdict(list)
    for r in rows:
        by_sym[r["symbol"]].append(r)
    for sym in ASSETS:
        rs = by_sym.get(sym, [])
        if not rs:
            continue
        sn  = len(rs)
        sw  = sum(1 for r in rs if r["win"])
        sp  = sum(r["pnl"] for r in rs)
        print(f"  {sym:<6} {sn:>6} {sw/sn:>6.1%} {sp/sn:>+8.4f} {sn/days:>8.1f}")

    # ── Per-entry-window ─────────────────────────────────────────────────────
    if len(entry_minutes) > 1:
        print()
        print(f"  PER ENTRY WINDOW:")
        print(f"  {'mnt':>5} {'n':>6} {'wr':>7} {'roi/t':>9}")
        print("  " + "-" * 32)
        by_min = defaultdict(list)
        for r in rows:
            by_min[r["entry_min"]].append(r)
        for em in sorted(entry_minutes):
            rs = by_min.get(em, [])
            if not rs:
                continue
            mn  = len(rs)
            mw  = sum(1 for r in rs if r["win"])
            mp  = sum(r["pnl"] for r in rs)
            print(f"  {em:>5}m {mn:>6} {mw/mn:>6.1%} {mp/mn:>+8.4f}")

    # ── Momentum bucket analysis ─────────────────────────────────────────────
    print()
    print(f"  MOMENTUM BUCKET (|mom|):")
    print(f"  {'range':<14} {'n':>6} {'wr':>7} {'roi/t':>9}")
    print("  " + "-" * 40)
    buckets = [(0.0, 0.001), (0.001, 0.002), (0.002, 0.005),
               (0.005, 0.010), (0.010, 0.020), (0.020, 1.0)]
    for lo, hi in buckets:
        bucket = [r for r in rows if lo <= abs(r["momentum"]) < hi]
        if not bucket:
            continue
        bn  = len(bucket)
        bw  = sum(1 for r in bucket if r["win"])
        bp  = sum(r["pnl"] for r in bucket)
        print(f"  {lo:.3f}–{hi:.3f}     {bn:>6} {bw/bn:>6.1%} {bp/bn:>+8.4f}")

    # ── Threshold sweep ──────────────────────────────────────────────────────
    print()
    print(f"  THRESHOLD SWEEP (impact pada WR dan entries/hari):")
    print(f"  {'thr':>7} {'entries':>9} {'wr':>8} {'roi/t':>9}")
    print("  " + "-" * 38)
    for thr in [0.0005, 0.001, 0.0015, 0.002, 0.003, 0.005, 0.010]:
        subset = [r for r in rows if abs(r["momentum"]) >= thr]
        if not subset:
            continue
        sn = len(subset)
        sw = sum(1 for r in subset if r["win"])
        sp = sum(r["pnl"] for r in subset)
        marker = " ← current" if abs(thr - threshold) < 0.00001 else ""
        print(f"  {thr:>7.4f} {sn:>9} {sw/sn:>7.1%} {sp/sn:>+8.4f}{marker}")

    # ── Drawdown ─────────────────────────────────────────────────────────────
    rs_sorted = sorted(rows, key=lambda r: r["ts"])
    peak, cum, max_dd = 0.0, 0.0, 0.0
    for r in rs_sorted:
        cum += r["pnl"]
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    print()
    print(f"  Max drawdown : -{max_dd:.2f} pnl units")

    # ── Verdict ──────────────────────────────────────────────────────────────
    print()
    if n >= 30:
        if roi >= 0.20:
            verdict = f"✅ EDGE KUAT — avg PnL +{roi:.2%}/trade"
        elif roi >= 0.05:
            verdict = f"🟢 EDGE TERBUKTI — avg PnL +{roi:.2%}/trade"
        elif roi >= 0.0:
            verdict = f"🟡 EDGE TIPIS — avg PnL +{roi:.2%}/trade"
        else:
            verdict = f"❌ TIDAK PROFITABLE — avg PnL {roi:+.2%}/trade"
        wins_pnl = sum(r["pnl"] for r in rows if r["win"])
        loss_pnl = abs(sum(r["pnl"] for r in rows if not r["win"]))
        pf = wins_pnl / loss_pnl if loss_pnl > 0 else float("inf")
        print(f"  VERDICT : {verdict}")
        print(f"  Stats   : winrate {wr:.1%} | ROI {roi:+.2%}/trade | profit factor {pf:.2f}")
    else:
        print(f"  ⚠️  Sample {n} < 30 — naikkan --days untuk reliable estimate")

    print("=" * W)


# ── main ─────────────────────────────────────────────────────────────────────

async def main(args):
    assets        = [args.asset.upper()] if args.asset else ASSETS
    invalid       = [a for a in assets if a not in ASSETS]
    if invalid:
        print(f"Asset tidak dikenal: {invalid}. Pilih dari: {ASSETS}")
        return

    entry_minutes = [5, 10, 15]

    print(f"\nCandle Backtest — {args.days}d | threshold={args.threshold:.4f} | "
          f"max_price={args.max_buy_price:.2f}")
    print(f"Assets: {assets} | Entry windows: menit ke-{entry_minutes}\n")

    since_ms = int((datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp() * 1000)
    _ = since_ms  # used inside backtest_asset

    async with aiohttp.ClientSession() as session:
        all_results: list[dict] = []
        for symbol in assets:
            rs = await backtest_asset(
                symbol        = symbol,
                session       = session,
                days          = args.days,
                threshold     = args.threshold,
                max_buy_price = args.max_buy_price,
                entry_minutes = entry_minutes,
                use_gbm       = args.use_gbm,
            )
            n = len(rs)
            w = sum(1 for r in rs if r["win"])
            wr_str = f"{w/n:.0%}" if n else "—"
            print(f"  [{symbol}] {n} trades | WR {wr_str}")
            all_results.extend(rs)

    print_report(
        all_results,
        days          = args.days,
        threshold     = args.threshold,
        max_buy_price = args.max_buy_price,
        entry_minutes = entry_minutes,
        use_gbm       = args.use_gbm,
    )


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Backtest Candle Up/Down strategy")
    parser.add_argument("--days",          type=int,   default=30,    help="Lookback days (default 30)")
    parser.add_argument("--threshold",     type=float, default=0.0015, help="15m momentum threshold (default 0.0015)")
    parser.add_argument("--max_buy_price", type=float, default=0.70,  help="Max entry price (default 0.70)")
    parser.add_argument("--use_gbm",       action="store_true",       help="Use GBM as market price proxy (default: flat 0.50)")
    parser.add_argument("--asset",         type=str,   default=None,  help=f"Filter asset: {ASSETS}")
    args = parser.parse_args()

    asyncio.run(main(args))
