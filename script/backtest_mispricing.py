"""
script/backtest_mispricing.py
==============================
Backtest validasi model probabilitas mispricing.

Simulasi: ambil historical price, hitung apa yang model kita prediksi
untuk berbagai price target, lalu cek apakah prediksi akurat.

Jalankan:
    python -m script.backtest_mispricing
    python -m script.backtest_mispricing --days 90 --asset ETH
    python -m script.backtest_mispricing --days 90 --asset XRP
    python -m script.backtest_mispricing --days 90 --asset DOGE
"""

import sys
import asyncio
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import math
import aiohttp
from src.logic.probability import CryptoProbabilityCalculator, CALIBRATION_CORRECTION


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

COINGECKO_IDS = {
    "BTC":  "bitcoin",
    "ETH":  "ethereum",
    "SOL":  "solana",
    "XRP":  "ripple",
    "DOGE": "dogecoin",
    "BNB":  "binancecoin",
}


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def compute_realized_vol(prices: list[dict], window: int = 30) -> float:
    """
    Hitung realized annualized volatility dari log-returns harian.
    Pakai `window` data terakhir. Kalau window > len(prices), pakai semua.
    """
    n_use        = min(window + 1, len(prices))
    close_prices = [p["price"] for p in prices[-n_use:]]
    if len(close_prices) < 2:
        return 0.45  # fallback

    log_returns = [
        math.log(close_prices[i] / close_prices[i - 1])
        for i in range(1, len(close_prices))
    ]
    n    = len(log_returns)
    mean = sum(log_returns) / n
    var  = sum((r - mean) ** 2 for r in log_returns) / max(n - 1, 1)
    return math.sqrt(var) * math.sqrt(365)


def compute_rolling_drift(prices: list[dict], end_idx: int, window: int = 30) -> float:
    """
    Hitung annualized drift dari window hari sebelum end_idx.
    Dipakai agar model ikuti trend aktual (bearish/bullish) di tiap titik evaluasi.
    """
    start_idx = max(0, end_idx - window)
    if end_idx - start_idx < 2:
        return 0.0
    p_start = prices[start_idx]["price"]
    p_end   = prices[end_idx]["price"]
    n_days  = end_idx - start_idx
    return math.log(p_end / p_start) / (n_days / 365.0)


# ─────────────────────────────────────────────
# DATA FETCH
# ─────────────────────────────────────────────

async def fetch_historical_prices(asset: str, days: int, session: aiohttp.ClientSession) -> list[dict]:
    """Ambil historical daily close prices dari CoinGecko."""
    coin_id = COINGECKO_IDS.get(asset.upper())
    if not coin_id:
        print(f"Asset {asset} tidak didukung. Pilih: {list(COINGECKO_IDS.keys())}")
        return []

    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
    params = {"vs_currency": "usd", "days": str(days), "interval": "daily"}

    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        resp.raise_for_status()
        data = await resp.json()

    prices = []
    for ts_ms, price in data.get("prices", []):
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        prices.append({"date": dt, "price": float(price)})

    return prices


# ─────────────────────────────────────────────
# SIMULASI
# ─────────────────────────────────────────────

async def run_backtest(asset: str, days: int, horizon_days: int, barrier_mode: bool = False):
    """
    Untuk setiap titik di historical, simulasikan:
    - Harga sekarang = S
    - Target = S * (1 + target_pct) untuk berbagai target_pct
    - Model prediksi probabilitas mencapai target dalam horizon_days
    - Cek apakah target actually dicapai dalam horizon_days berikutnya
    """
    target_pcts = [0.03, 0.05, 0.08, 0.10, 0.15]  # +3%, +5%, +8%, +10%, +15%

    print(f"\n{'='*65}")
    print(f"  MISPRICING MODEL BACKTEST — {asset} | {days} hari historical")
    print(f"  Horizon prediksi: {horizon_days} hari ke depan")
    print(f"{'='*65}\n")

    # CoinGecko free API max 365 hari — auto-cap agar tidak 401
    COINGECKO_MAX_DAYS = 365
    fetch_days = days + horizon_days
    if fetch_days > COINGECKO_MAX_DAYS:
        fetch_days = COINGECKO_MAX_DAYS
        days = fetch_days - horizon_days
        print(f"  ⚠️  CoinGecko free API max {COINGECKO_MAX_DAYS}d — analysis window disesuaikan ke {days} hari\n")

    async with aiohttp.ClientSession() as session:
        prices = await fetch_historical_prices(asset, fetch_days, session)

    if len(prices) < horizon_days + 5:
        print("Data tidak cukup untuk backtest.")
        return

    # Hitung realized vol dari seluruh periode historis (bukan rolling — rolling terlalu noisy)
    hist_prices   = prices[:days]
    vol_full      = compute_realized_vol(hist_prices, window=len(hist_prices))
    vol_30d       = compute_realized_vol(hist_prices, window=30)

    drift_full = compute_rolling_drift(hist_prices, len(hist_prices) - 1, window=len(hist_prices))
    print(f"  Realized vol — {days}d: {vol_full:.1%} | 30d trailing: {vol_30d:.1%}  "
          f"[fallback BTC: 45%]")
    print(f"  Drift {days}d: {drift_full:+.1%} annualized")
    model_label = "barrier (any-touch)" if barrier_mode else "at-expiry (final day)"
    model_key   = "barrier" if barrier_mode else "at_expiry"
    corrections = CALIBRATION_CORRECTION.get(model_key, {}).get(asset, {})
    corr_str    = ", ".join(f"+{int(k*100)}%: -{v*100:.0f}%" for k, v in sorted(corrections.items())) if corrections else "tidak ada"
    print(f"  → Pakai vol {days}d + rolling drift 30d per titik | model: {model_label}")
    print(f"  Calibration correction [{model_key}]: {corr_str}\n")

    calc    = CryptoProbabilityCalculator()
    results = {pct: {"total": 0, "predicted_sum": 0.0, "actual_hits": 0} for pct in target_pcts}

    # Iterasi setiap hari kecuali horizon_days terakhir (butuh future data)
    usable = prices[:-horizon_days]

    for i, entry in enumerate(usable):
        current_price = entry["price"]
        future_prices = [p["price"] for p in prices[i+1 : i+1+horizon_days]]

        if len(future_prices) < horizon_days:
            continue

        # Rolling drift 30 hari — agar model ikuti trend aktual di tiap titik
        rolling_drift = compute_rolling_drift(prices, i, window=30)

        for tpct in target_pcts:
            target = current_price * (1 + tpct)

            result = calc.calculate(
                asset          = asset,
                current_price  = current_price,
                target_price   = target,
                days_remaining = horizon_days,
                volatility     = vol_full,
                direction      = "above",
                use_barrier    = barrier_mode,
                drift          = rolling_drift,
            )
            predicted_prob = result.probability

            # barrier mode: cek apakah target dicapai di hari manapun (any-touch)
            # at-expiry mode: cek harga penutupan hari ke-N saja
            actual_hit = (
                any(p >= target for p in future_prices)
                if barrier_mode
                else future_prices[-1] >= target
            )

            results[tpct]["total"]         += 1
            results[tpct]["predicted_sum"] += predicted_prob
            results[tpct]["actual_hits"]   += int(actual_hit)

    # ── Tampilkan hasil ──────────────────────────────────────────────
    print(f"{'Target':>8} | {'Pred Avg':>9} | {'Actual':>7} | {'Diff':>6} | {'Samples':>7} | {'Verdict'}")
    print("-" * 65)

    total_mae = 0.0
    count     = 0

    for tpct in target_pcts:
        r = results[tpct]
        if r["total"] == 0:
            continue

        pred_avg   = r["predicted_sum"] / r["total"]
        actual_pct = r["actual_hits"] / r["total"]
        diff       = pred_avg - actual_pct
        mae        = abs(diff)
        total_mae += mae
        count     += 1

        if mae < 0.05:
            verdict = "✅ Akurat"
        elif mae < 0.12:
            verdict = "⚠️  Cukup akurat"
        elif diff > 0:
            verdict = "❌ Over-estimate"
        else:
            verdict = "❌ Under-estimate"

        print(
            f"  +{tpct*100:.0f}%   | "
            f"{pred_avg*100:>7.1f}%  | "
            f"{actual_pct*100:>5.1f}%  | "
            f"{diff*100:>+5.1f}%  | "
            f"{r['total']:>7}  | {verdict}"
        )

    if count > 0:
        avg_mae = total_mae / count
        print(f"\n  MAE rata-rata: {avg_mae*100:.1f}%")
        if avg_mae < 0.08:
            print("  → Model LAYAK untuk live trading")
        elif avg_mae < 0.15:
            print("  → Model CUKUP akurat, perlu monitoring ketat saat live")
        else:
            print("  → Model PERLU perbaikan sebelum live trading")

    print(f"\n{'='*65}\n")


def parse_args():
    p = argparse.ArgumentParser(description="Mispricing Model Backtest")
    p.add_argument("--days",    type=int,  default=90,    help="Jumlah hari historical (default: 90)")
    p.add_argument("--horizon", type=int,  default=7,     help="Horizon prediksi dalam hari (default: 7)")
    p.add_argument("--asset",   type=str,  default="BTC", help="Asset: BTC/ETH/SOL/XRP/DOGE (default: BTC)")
    p.add_argument("--barrier", action="store_true",      help="Pakai barrier model + any-touch validation")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(run_backtest(args.asset.upper(), args.days, args.horizon, args.barrier))
