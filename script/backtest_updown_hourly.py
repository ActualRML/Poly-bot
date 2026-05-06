"""
Backtest model probabilitas untuk Up/Down Hourly strategy.

Ground truth: Binance 1h klines — tidak butuh Gamma API.
  - reference = open 1h candle  (= harga saat market buka)
  - final     = close 1h candle (= harga saat market tutup)
  - actual_up = final > reference

Entry simulation: 5m Binance klines
  - price saat (candle_close - entry_min menit)

Jalankan:
  python -m script.backtest_updown_hourly
  python -m script.backtest_updown_hourly --days 30 --entry_min 30
  python -m script.backtest_updown_hourly --asset BTC --entry_min 15
  python -m script.backtest_updown_hourly --days 60 --entry_min 45
"""

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
from src.logic.updown_strategy import _norm_cdf

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

ASSETS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"]

DEFAULT_VOL = {
    "BTC": 0.40, "ETH": 0.55, "SOL": 0.70,
    "XRP": 0.60, "DOGE": 0.80, "BNB": 0.35,
}


# ── helpers ──────────────────────────────────────────────────────────────────

def calc_prob_up(current: float, reference: float, T_days: float, vol: float) -> float:
    if current <= 0 or reference <= 0 or T_days <= 0 or vol <= 0:
        return 0.5
    mu_adj = -0.5 * vol ** 2
    denom  = vol * math.sqrt(T_days)
    if denom == 0:
        return 0.5
    d2 = (math.log(current / reference) + mu_adj * T_days) / denom
    return max(0.001, min(0.999, _norm_cdf(d2)))


def rolling_vol(klines_1h: list, idx: int, n: int = 4) -> float:
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
    # fetch_klines returns (datetime, open, high, low, close) tuples
    STEP = {"1h": 3_600_000, "5m": 300_000}
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


# ── core backtest ─────────────────────────────────────────────────────────────

async def backtest_asset(
    symbol: str,
    session: aiohttp.ClientSession,
    days: int,
    entry_min: int,
) -> list[dict]:

    since_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)

    print(f"  [{symbol}] Fetching 1h klines...", flush=True)
    klines_1h = await fetch_all_klines(symbol, session, "1h", since_ms)
    if len(klines_1h) < 5:
        print(f"  [{symbol}] Data 1h tidak cukup")
        return []

    print(f"  [{symbol}] Fetching 5m klines...", flush=True)
    klines_5m = await fetch_all_klines(symbol, session, "5m", since_ms)

    # index 5m by open_time in ms (quantized to 5m boundary)
    idx_5m: dict[int, float] = {}
    for k in klines_5m:
        ts_ms = int(k[0].timestamp() * 1000)
        idx_5m[ts_ms] = float(k[4])  # close price

    T_days          = entry_min / (24 * 60)
    entry_offset_ms = entry_min * 60 * 1000

    results = []
    default_vol = DEFAULT_VOL.get(symbol, 0.50)

    for i, k in enumerate(klines_1h[1:], start=1):
        open_time  = int(k[0].timestamp() * 1000)
        close_time = open_time + 3_600_000
        reference  = float(k[1])
        final      = float(k[4])

        if reference <= 0 or final <= 0:
            continue

        actual_up = final > reference

        # entry time: entry_min menit sebelum candle close
        entry_ms = close_time - entry_offset_ms
        if entry_ms <= open_time:
            continue  # entry_min >= 60 → tidak valid

        # cari 5m close terdekat di entry_ms (± 2 candle = ±10 menit)
        entry_price: Optional[float] = None
        q = (entry_ms // 300_000) * 300_000  # quantize ke boundary 5m
        for probe in (q, q - 300_000, q + 300_000, q - 600_000, q + 600_000):
            p = idx_5m.get(probe)
            if p and p > 0:
                entry_price = p
                break

        if entry_price is None:
            continue

        vol = rolling_vol(klines_1h, i)
        if vol <= 0:
            vol = default_vol
        vol = max(vol, 0.05)

        prob_up  = calc_prob_up(entry_price, reference, T_days, vol)
        pred_up  = prob_up >= 0.5
        correct  = pred_up == actual_up
        pct_move = (entry_price - reference) / reference * 100

        results.append({
            "symbol":    symbol,
            "ts":        datetime.fromtimestamp(open_time / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "reference": reference,
            "entry":     entry_price,
            "final":     final,
            "pct_move":  pct_move,
            "vol":       vol,
            "prob_up":   prob_up,
            "actual_up": actual_up,
            "correct":   correct,
            "error":     abs(prob_up - (1.0 if actual_up else 0.0)),
        })

    return results


# ── reporting ──────────────────────────────────────────────────────────────────

def stats(rows: list[dict]) -> dict:
    n         = len(rows)
    mae       = sum(r["error"] for r in rows) / n
    accuracy  = sum(1 for r in rows if r["correct"]) / n
    avg_pred  = sum(r["prob_up"] for r in rows) / n
    actual_wr = sum(1 for r in rows if r["actual_up"]) / n
    bias      = avg_pred - actual_wr
    avg_vol   = sum(r["vol"] for r in rows) / n
    avg_move  = sum(abs(r["pct_move"]) for r in rows) / n
    return dict(n=n, mae=mae, accuracy=accuracy, avg_pred=avg_pred,
                actual_wr=actual_wr, bias=bias, avg_vol=avg_vol, avg_move=avg_move)


def print_report(all_results: list[dict], entry_min: int, days: int):
    by_asset: dict[str, list] = defaultdict(list)
    for r in all_results:
        by_asset[r["symbol"]].append(r)

    print()
    print("=" * 65)
    print(f"BACKTEST UP/DOWN HOURLY — {days} hari | entry T-{entry_min}m sebelum close")
    print(f"Ground truth : Binance 1h klines (open=reference, close=final)")
    print("=" * 65)

    for symbol in ASSETS:
        rows = by_asset.get(symbol)
        if not rows:
            continue
        s = stats(rows)

        if s["n"] < 20:
            verdict = f"DATA KURANG ({s['n']} candles)"
        elif s["accuracy"] >= 0.60 and s["mae"] < 0.15:
            verdict = "✅ ADA EDGE — lanjut paper trade"
        elif s["accuracy"] >= 0.55:
            verdict = "⚠️  Edge tipis — perlu lebih banyak data"
        else:
            verdict = "❌ TIDAK ADA EDGE — jangan dipakai"

        bias_dir = ("over-confident" if s["bias"] > 0.03
                    else "under-confident" if s["bias"] < -0.03 else "OK")

        print(f"\n  {symbol}  (n={s['n']})")
        print(f"    Accuracy    : {s['accuracy']:.1%}   {verdict}")
        print(f"    MAE         : {s['mae']:.1%}")
        print(f"    Model avg   : {s['avg_pred']:.1%}  |  Actual Up%: {s['actual_wr']:.1%}")
        print(f"    Bias        : {s['bias']:+.3f}  ({bias_dir})")
        print(f"    Avg |move|  : {s['avg_move']:.2f}% saat entry")
        print(f"    Avg vol     : {s['avg_vol']:.0%} annualized")

    # analisis per-edge-bucket
    edge_rows_5  = [r for r in all_results if abs(r["prob_up"] - 0.5) >= 0.05]
    edge_rows_10 = [r for r in all_results if abs(r["prob_up"] - 0.5) >= 0.10]

    total = len(all_results)
    print()
    print("─" * 65)
    print("DISTRIBUSI EDGE:")
    print(f"  Semua  candles : {total}")
    if edge_rows_5:
        s5 = stats(edge_rows_5)
        print(f"  Edge ≥ 5%      : {len(edge_rows_5)} ({len(edge_rows_5)/total:.0%}) | accuracy: {s5['accuracy']:.1%}")
    if edge_rows_10:
        s10 = stats(edge_rows_10)
        print(f"  Edge ≥ 10%     : {len(edge_rows_10)} ({len(edge_rows_10)/total:.0%}) | accuracy: {s10['accuracy']:.1%}")

    print()
    print("CATATAN:")
    print("  - Accuracy > 60% pada candles dengan edge ≥ 5% = ada edge nyata")
    print("  - MAE tinggi (~43%) normal untuk near-50/50 market — pakai Accuracy")
    print("  - Backtest ini menggunakan Binance close sebagai ground truth")
    print("    (sama persis dengan cara Polymarket resolve market Up/Down Hourly)")
    print("=" * 65)


# ── main ──────────────────────────────────────────────────────────────────────

async def main(days: int, entry_min: int, asset_filter: Optional[str]):
    if not (1 <= entry_min <= 59):
        print("ERROR: --entry_min harus antara 1–59 (market durasi 60 menit)")
        return

    assets = [asset_filter.upper()] if asset_filter else ASSETS
    invalid = [a for a in assets if a not in ASSETS]
    if invalid:
        print(f"Asset tidak dikenal: {invalid}. Pilih dari: {ASSETS}")
        return

    print(f"\nBacktest Up/Down Hourly — {days} hari, entry T-{entry_min}m")
    print(f"Assets: {assets}\n")

    async with aiohttp.ClientSession() as session:
        all_results = []
        for symbol in assets:
            rows = await backtest_asset(symbol, session, days, entry_min)
            print(f"  [{symbol}] {len(rows)} candles dianalisis")
            all_results.extend(rows)

    if not all_results:
        print("Tidak ada data — coba tambah --days.")
        return

    print_report(all_results, entry_min, days)


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Backtest Up/Down Hourly strategy")
    parser.add_argument("--days",      type=int, default=30,
                        help="Lookback days (default: 30)")
    parser.add_argument("--entry_min", type=int, default=30,
                        help="Entry N menit sebelum close, 1-59 (default: 30)")
    parser.add_argument("--asset",     type=str, default=None,
                        help=f"Filter satu asset: {', '.join(ASSETS)}")
    args = parser.parse_args()

    asyncio.run(main(days=args.days, entry_min=args.entry_min, asset_filter=args.asset))
