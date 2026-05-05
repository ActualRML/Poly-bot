import sys
import asyncio
import argparse
import math
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiohttp

from src.api.gamma_client import GammaClient
from src.api.binance_client import fetch_klines, fetch_historical_realized_vol
from src.logic.updown_strategy import _norm_cdf

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

SLUG_PREFIXES = {
    "BTC": "bitcoin-up-or-down-",
    "ETH": "ethereum-up-or-down-",
    "SOL": "solana-up-or-down-",
    "XRP": "xrp-up-or-down-",
}

WINDOWED_MARKERS = ("-5m-", "-15m-", "-4h-", "updown-5m", "updown-15m", "updown-4h")

SUPPORTED_ASSETS = list(SLUG_PREFIXES.keys())

ENTRY_MINUTES_BEFORE = 30

DEFAULT_VOL = {"BTC": 0.40, "ETH": 0.55, "SOL": 0.70, "XRP": 0.60}


def calc_prob_up(current: float, reference: float, T_days: float, vol: float) -> float:
    if current <= 0 or reference <= 0 or T_days <= 0 or vol <= 0:
        return 0.5
    mu_adj = -0.5 * vol ** 2
    denom  = vol * math.sqrt(T_days)
    if denom == 0:
        return 0.5
    d2 = (math.log(current / reference) + mu_adj * T_days) / denom
    return max(0.001, min(0.999, _norm_cdf(d2)))


async def fetch_price_at(
    symbol: str,
    session: aiohttp.ClientSession,
    at_time: datetime,
) -> Optional[float]:
    end_ms   = int(at_time.timestamp() * 1000)
    start_ms = end_ms - 3_600_000
    klines   = await fetch_klines(symbol, session, interval="1h", limit=1,
                                   start_ms=start_ms, end_ms=end_ms)
    if not klines:
        return None
    return klines[-1][4]


async def fetch_reference_price_hourly(
    symbol: str,
    session: aiohttp.ClientSession,
    start_date: datetime,
) -> Optional[float]:
    start_ms = int(start_date.timestamp() * 1000)
    end_ms   = start_ms + 3_600_000
    klines   = await fetch_klines(symbol, session, interval="1h", limit=1,
                                   start_ms=start_ms, end_ms=end_ms)
    if not klines:
        return None
    return klines[0][1]


async def fetch_events_for_asset(
    symbol: str,
    session: aiohttp.ClientSession,
    days: int,
    gamma: GammaClient,
) -> list[dict]:
    prefix  = SLUG_PREFIXES[symbol]
    cutoff  = datetime.now(timezone.utc) - timedelta(days=days)
    events  = []
    offset  = 0
    limit   = 200
    max_pages = 30

    for _page in range(max_pages):
        try:
            batch = await gamma._aget(
                "/events",
                session,
                params={
                    "closed":    "true",
                    "limit":     limit,
                    "offset":    offset,
                    "order":     "endDate",
                    "ascending": "false",
                },
            )
        except Exception as e:
            logger.warning(f"[{symbol}] Gamma fetch error: {e}")
            break

        if not isinstance(batch, list) or not batch:
            break

        reached_cutoff = False
        for e in batch:
            slug = (e.get("slug") or "").lower()

            if not slug.startswith(prefix):
                continue
            if any(m in slug for m in WINDOWED_MARKERS):
                continue

            end_date_str   = e.get("endDate") or ""
            start_date_str = e.get("startDate") or ""
            try:
                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            except Exception:
                continue

            if end_date < cutoff:
                reached_cutoff = True
                continue

            try:
                start_date = datetime.fromisoformat(start_date_str.replace("Z", "+00:00"))
            except Exception:
                continue

            meta          = e.get("eventMetadata") or {}
            price_to_beat = meta.get("priceToBeat")
            final_price   = meta.get("finalPrice")

            if final_price is None:
                continue

            e["_symbol"]     = symbol
            e["_end_date"]   = end_date
            e["_start_date"] = start_date
            e["_final_price"] = float(final_price)
            e["_price_to_beat"] = float(price_to_beat) if price_to_beat is not None else None
            events.append(e)

        last_end_str = batch[-1].get("endDate") or ""
        try:
            last_end = datetime.fromisoformat(last_end_str.replace("Z", "+00:00"))
            if last_end < cutoff:
                break
        except Exception:
            pass

        if reached_cutoff or len(batch) < limit:
            break
        offset += limit

    return events


async def analyze_event(
    event: dict,
    session: aiohttp.ClientSession,
) -> Optional[dict]:
    symbol      = event["_symbol"]
    end_date    = event["_end_date"]
    start_date  = event["_start_date"]
    final_price = event["_final_price"]

    entry_time = end_date - timedelta(minutes=ENTRY_MINUTES_BEFORE)
    T_days     = ENTRY_MINUTES_BEFORE / (24 * 60)

    reference = event["_price_to_beat"]
    if reference is None:
        reference = await fetch_reference_price_hourly(symbol, session, start_date)
    if not reference or reference <= 0:
        logger.debug(f"[{symbol}] skip {end_date} — gagal fetch reference price")
        return None

    current = await fetch_price_at(symbol, session, entry_time)
    if not current:
        logger.debug(f"[{symbol}] skip {end_date} — gagal fetch entry price")
        return None

    vol = await fetch_historical_realized_vol(symbol, session, at_time=entry_time, hours=4)
    if not vol or vol <= 0:
        vol = DEFAULT_VOL.get(symbol, 0.50)

    prob_up   = calc_prob_up(current, reference, T_days, vol)
    actual_up = final_price > reference

    pct_from_ref = (current - reference) / reference * 100

    return {
        "symbol":       symbol,
        "end_date":     end_date.strftime("%Y-%m-%d %H:%M"),
        "reference":    reference,
        "current":      current,
        "final_price":  final_price,
        "pct_from_ref": pct_from_ref,
        "vol":          vol,
        "T_days":       T_days,
        "prob_up":      prob_up,
        "actual_up":    actual_up,
        "error":        abs(prob_up - (1.0 if actual_up else 0.0)),
    }


def compute_calibration(results: list[dict]) -> dict:
    from collections import defaultdict

    per_asset: dict[str, list] = defaultdict(list)
    for r in results:
        per_asset[r["symbol"]].append(r)

    report = {}
    for symbol, rows in per_asset.items():
        n         = len(rows)
        mae       = sum(r["error"] for r in rows) / n
        accuracy  = sum(1 for r in rows if (r["prob_up"] >= 0.5) == r["actual_up"]) / n
        avg_pred  = sum(r["prob_up"] for r in rows) / n
        actual_wr = sum(1 for r in rows if r["actual_up"]) / n
        bias      = avg_pred - actual_wr

        report[symbol] = {
            "n":         n,
            "mae":       round(mae, 4),
            "accuracy":  round(accuracy, 4),
            "avg_pred":  round(avg_pred, 4),
            "actual_wr": round(actual_wr, 4),
            "bias":      round(bias, 4),
        }

    return report


def print_report(results: list[dict], calibration: dict, days: int):
    print("=" * 60)
    print(f"BACKTEST UP/DOWN HOURLY — {days} hari terakhir")
    print(f"Total events dianalisis: {len(results)}")
    print(f"Entry: {ENTRY_MINUTES_BEFORE} menit sebelum expiry")
    print(f"Reference: open 1h Binance candle di startDate (atau priceToBeat)")
    print("=" * 60)

    for symbol, m in calibration.items():
        if m["n"] < 10:
            verdict = f"DATA KURANG ({m['n']} events) — belum bisa disimpulkan"
        elif m["mae"] < 0.05:
            verdict = "LAYAK LIVE"
        elif m["mae"] < 0.10:
            verdict = "PAPER TRADE dulu"
        else:
            verdict = "JANGAN DIPAKAI — MAE terlalu tinggi"

        bias_str   = f"+{m['bias']:.3f}" if m["bias"] >= 0 else f"{m['bias']:.3f}"
        bias_label = ("over-confident" if m["bias"] > 0.03
                      else "under-confident" if m["bias"] < -0.03 else "OK")
        print(f"\n  {symbol}  (n={m['n']})")
        print(f"    MAE         : {m['mae']:.1%}   [{verdict}]")
        print(f"    Accuracy    : {m['accuracy']:.1%}")
        print(f"    Model avg   : {m['avg_pred']:.1%}  (predicted P(Up))")
        print(f"    Actual Up % : {m['actual_wr']:.1%}")
        print(f"    Bias        : {bias_str}  ({bias_label})")

    print("\n" + "=" * 60)
    if any(m["n"] >= 10 for m in calibration.values()):
        print("REKOMENDASI CORRECTION:")
        for symbol, m in calibration.items():
            if m["n"] < 10:
                print(f"  {symbol}: data belum cukup")
            elif abs(m["bias"]) > 0.02:
                print(f"  {symbol}: correction = {-m['bias']:+.3f} (kurangi bias {m['bias']:+.3f})")
            else:
                print(f"  {symbol}: tidak perlu correction (bias minimal)")
    print("=" * 60)


async def main(days: int, asset_filter: Optional[str]):
    assets = [asset_filter.upper()] if asset_filter else SUPPORTED_ASSETS

    invalid = [a for a in assets if a not in SUPPORTED_ASSETS]
    if invalid:
        print(f"Asset tidak dikenal: {invalid}. Pilih dari: {SUPPORTED_ASSETS}")
        return

    gamma = GammaClient()
    async with aiohttp.ClientSession() as session:

        all_events = []
        for symbol in assets:
            print(f"Fetching {symbol} Up/Down Hourly events (last {days} days)...")
            evts = await fetch_events_for_asset(symbol, session, days, gamma)
            print(f"  -> {len(evts)} events ditemukan")
            all_events.extend(evts)

        if not all_events:
            print("\nTidak ada events ditemukan.")
            print("Kemungkinan market hourly baru berjalan — coba lagi beberapa hari.")
            return

        print(f"\nMenganalisis {len(all_events)} events...\n")
        results = []
        failed  = 0

        for i, event in enumerate(all_events):
            r = await analyze_event(event, session)
            if r:
                results.append(r)
                status  = "UP"   if r["actual_up"] else "DOWN"
                pred    = f"P(Up)={r['prob_up']:.3f}"
                correct = "OK" if (r["prob_up"] >= 0.5) == r["actual_up"] else "MISS"
                pct     = f"pct={r['pct_from_ref']:+.2f}%"
                print(f"  [{i+1:3d}] {r['symbol']} {r['end_date']}  {pred}  {pct}  actual={status}  [{correct}]")
            else:
                failed += 1

        if not results:
            print("Tidak cukup data untuk kalibrasi.")
            return

        print(f"\n{len(results)} berhasil, {failed} gagal (data tidak lengkap)\n")
        calibration = compute_calibration(results)
        print_report(results, calibration, days)


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser()
    parser.add_argument("--days",  type=int, default=30,   help="Lookback window (default: 30)")
    parser.add_argument("--asset", type=str, default=None, help=f"Filter asset: {', '.join(SLUG_PREFIXES.keys())}")
    args = parser.parse_args()

    asyncio.run(main(days=args.days, asset_filter=args.asset))
