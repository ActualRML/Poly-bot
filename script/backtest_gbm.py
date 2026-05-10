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

Output (tanpa dan dengan technical filter, side-by-side):
  - Total trade per edge threshold
  - Winrate per asset
  - ROI estimate (assume market_up = P(Up)_lag)
  - Filter breakdown: filter mana yang block berapa entry

Jalankan:
  python -m script.backtest_gbm
  python -m script.backtest_gbm --days 60 --entry_min 30 --lag_min 15
  python -m script.backtest_gbm --days 90 --min_edge 0.03
  python -m script.backtest_gbm --no-filters   # baseline tanpa filter
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
from src.api.binance_client import fetch_klines, fetch_klines_extended
from src.logic.oracle_arb import gbm_prob_above
from src.logic.technical import (
    compute_rsi, compute_zscore, detect_volume_spike,
    compute_trend, compute_trend_bias,
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

ASSETS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"]

# ── Technical filter thresholds (mirror live config) ─────────────────────────
RSI_OB       = 70.0   # mean-reversion: skip Up when overbought
RSI_OS       = 30.0   # mean-reversion: skip Down when oversold
RSI_MOM_OB   = 68.0   # momentum: skip Down when RSI >= 68 (uptrend)
RSI_MOM_OS   = 35.0   # momentum: skip Up when RSI <= 35 (downtrend)
ZSCORE_THR   = 2.5    # mean-reversion: extreme tails
ZSCORE_MOM   = 2.0    # momentum: don't fight trend beyond 2σ
VOL_MULT     = 3.0    # volume spike multiplier
BTC_CORR_THR = 0.005  # BTC 1h momentum threshold for correlation filter
MACRO_THR    = 0.02   # BTC 4h trend threshold for macro gate


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
        if interval == "1h":
            # Use extended klines for 1h to get volume data (needed for vol spike filter)
            batch = await fetch_klines_extended(symbol, session, interval=interval,
                                               limit=1000, start_ms=start)
        else:
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


# ── technical filter logic ────────────────────────────────────────────────────

def apply_tech_filters_local(
    outcome: str,
    closes: list[float],
    volumes: list[float],
    btc_closes: list[float],
    symbol: str,
) -> list[str]:
    """
    Apply technical filters to a potential entry. Returns list of triggered filter names.
    Empty list = PASS. Non-empty = BLOCKED (at least one filter triggered).
    """
    triggered = []
    if not closes:
        return triggered

    vol_spike = detect_volume_spike(volumes, VOL_MULT) if len(volumes) >= 5 else False
    if vol_spike:
        triggered.append("VOL_SPIKE")

    rsi = compute_rsi(closes, 14)
    if rsi is not None:
        if outcome == "Up" and rsi >= RSI_OB:
            triggered.append(f"RSI_OB({rsi:.1f})")
        elif outcome == "Down" and rsi <= RSI_OS:
            triggered.append(f"RSI_OS({rsi:.1f})")
        elif outcome == "Down" and rsi >= RSI_MOM_OB:
            triggered.append(f"RSI_MOM_UP({rsi:.1f})")
        elif outcome == "Up" and rsi <= RSI_MOM_OS:
            triggered.append(f"RSI_MOM_DOWN({rsi:.1f})")

    z = compute_zscore(closes, 20)
    if z is not None:
        if outcome == "Up" and z >= ZSCORE_THR:
            triggered.append(f"Z_HIGH({z:.2f})")
        elif outcome == "Down" and z <= -ZSCORE_THR:
            triggered.append(f"Z_LOW({z:.2f})")
        if outcome == "Down" and z >= ZSCORE_MOM:
            triggered.append(f"Z_MOM_UP({z:.2f})")
        elif outcome == "Up" and z <= -ZSCORE_MOM:
            triggered.append(f"Z_MOM_DOWN({z:.2f})")

    # EMA trend bias: would it oppose the direction?
    if len(closes) >= 25:
        bias = compute_trend_bias(closes)
        if outcome == "Up" and bias < -0.05:
            triggered.append(f"EMA_BIAS_DOWN({bias:+.2f})")
        elif outcome == "Down" and bias > 0.05:
            triggered.append(f"EMA_BIAS_UP({bias:+.2f})")

    # BTC correlation (for non-BTC symbols)
    if symbol != "BTC" and len(btc_closes) >= 2:
        btc_mom = (btc_closes[-1] - btc_closes[-2]) / btc_closes[-2] if btc_closes[-2] > 0 else 0.0
        if abs(btc_mom) >= BTC_CORR_THR:
            btc_dir = "Up" if btc_mom > 0 else "Down"
            if btc_dir != outcome:
                triggered.append(f"BTC_CORR({btc_mom:+.2%})")

    # Macro trend gate (BTC 4h trend)
    btc_src = btc_closes if symbol != "BTC" else closes
    btc_trend = compute_trend(btc_src, 4) if len(btc_src) >= 5 else None
    if btc_trend is not None:
        if btc_trend > MACRO_THR and outcome == "Down":
            triggered.append(f"MACRO_UP({btc_trend:+.2%})")
        elif btc_trend < -MACRO_THR and outcome == "Up":
            triggered.append(f"MACRO_DOWN({btc_trend:+.2%})")

    return triggered


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
    """
    p_up_now    = gbm_prob_above(current, strike, vol, T_remaining_s)
    p_up_market = gbm_prob_above(lagged, strike, vol, T_remaining_s)

    market_price_up   = p_up_market
    market_price_down = 1.0 - p_up_market

    edge_up   = p_up_now - market_price_up - fee
    edge_down = (1.0 - p_up_now) - market_price_down - fee

    if edge_up >= min_edge and edge_up >= edge_down:
        if not (min_price <= market_price_up <= max_price):
            return None
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
    """Compute PnL per $1 capital."""
    win = (trade["outcome"] == "Up" and actual_up) or (trade["outcome"] == "Down" and not actual_up)
    if win:
        return (1.0 / trade["buy_price"]) - 1.0
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
    btc_klines_1h: list | None = None,
    use_filters: bool = True,
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

    # Pre-index BTC klines for fast lookup
    btc_ts_map: dict[int, float] = {}
    if btc_klines_1h:
        for k in btc_klines_1h:
            btc_ts_map[int(k[0].timestamp() * 1000)] = float(k[4])

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

        # ── Technical filter simulation ──────────────────────────────────────
        filters_hit: list[str] = []
        if use_filters:
            # Slice 1h closes and volumes up to (not including) current candle
            _hist     = klines_1h[max(0, i - 30): i]
            _closes   = [float(h[4]) for h in _hist]
            _volumes  = [float(h[5]) for h in _hist if len(h) > 5]

            # BTC closes at same time window (for altcoin correlation)
            if symbol != "BTC" and btc_klines_1h:
                _btc_hist   = [btc_ts_map.get(int(h[0].timestamp() * 1000), 0.0) for h in _hist]
                _btc_closes = [c for c in _btc_hist if c > 0]
            else:
                _btc_closes = _closes  # for BTC, use self

            filters_hit = apply_tech_filters_local(
                outcome    = trade["outcome"],
                closes     = _closes,
                volumes    = _volumes,
                btc_closes = _btc_closes,
                symbol     = symbol,
            )

        pnl = settle(trade, actual_up)
        results.append({
            "symbol":      symbol,
            "ts":          datetime.fromtimestamp(open_time / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "outcome":     trade["outcome"],
            "buy_price":   trade["buy_price"],
            "edge":        trade["edge"],
            "p_now":       trade["p_up_now"],
            "p_mkt":       trade["p_up_market"],
            "actual_up":   actual_up,
            "win":         pnl > 0,
            "pnl":         pnl,
            "vol":         vol,
            "filters_hit": filters_hit,
        })
    return results


# ── reporting ────────────────────────────────────────────────────────────────

def _stats(rows: list[dict]) -> tuple[int, int, float]:
    n   = len(rows)
    w   = sum(1 for r in rows if r["win"])
    pnl = sum(r["pnl"] for r in rows)
    return n, w, pnl


def print_report(rows: list[dict], days: int, entry_min: int, lag_min: int,
                 fee: float, min_edge: float, vol_floor: float, use_filters: bool):

    base_rows = rows
    filt_rows = [r for r in rows if not r["filters_hit"]]

    W = 75
    print()
    print("=" * W)
    print(f"  GBM BACKTEST — {days}d | entry T-{entry_min}m | lag {lag_min}m | fee={fee:.3f} | min_edge={min_edge:.2f}")
    print(f"  vol_floor={vol_floor:.2f} | assets={len(ASSETS)} | {'FILTERS ON' if use_filters else 'FILTERS OFF (baseline)'}")
    print("=" * W)

    if not rows:
        print("\n  Tidak ada trade — coba turunkan --min_edge atau tambah --days.")
        return

    # ── Side-by-side summary ─────────────────────────────────────────────────
    bn, bw, bpnl = _stats(base_rows)
    fn, fw, fpnl = _stats(filt_rows)
    blocked       = [r for r in rows if r["filters_hit"]]
    bln, blw      = len(blocked), sum(1 for r in blocked if r["win"])

    print()
    print(f"  {'Metric':<28} {'Tanpa Filter':>14} {'Dengan Filter':>14}")
    print("  " + "-" * 56)
    print(f"  {'Total Entries':<28} {bn:>14} {fn:>14}")
    print(f"  {'Entries/hari':<28} {bn/days:>14.1f} {fn/days:>14.1f}")
    print(f"  {'Wins':<28} {bw:>14} {fw:>14}")
    print(f"  {'Losses':<28} {bn-bw:>14} {fn-fw:>14}")
    print(f"  {'Winrate':<28} {bw/bn:>13.1%} {fw/fn:>13.1%}" if fn and bn else "")
    print(f"  {'Total PnL (per $1 stake)':<28} ${bpnl:>+12.3f} ${fpnl:>+12.3f}")
    roi_b = bpnl / bn if bn else 0
    roi_f = fpnl / fn if fn else 0
    print(f"  {'ROI/trade':<28} {roi_b:>+13.2%} {roi_f:>+13.2%}")
    if use_filters:
        print(f"  {'Blocked by filters':<28} {'—':>14} {len(blocked):>14}")
        print(f"  {'  Blocked W/L':<28} {'—':>14} {blw}/{len(blocked)-blw:>12}")

    # ── Per-asset breakdown ──────────────────────────────────────────────────
    print()
    print(f"  ENTRIES PER HARI (dengan filter):")
    by_asset: dict[str, list] = defaultdict(list)
    for r in filt_rows:
        by_asset[r["symbol"]].append(r)

    sym_rates = []
    for symbol in ASSETS:
        rs = by_asset.get(symbol, [])
        n  = len(rs)
        w  = sum(1 for r in rs if r["win"])
        wr = f"{w/n:.0%}" if n else "—"
        rate = n / days
        sym_rates.append((symbol, rate, n, wr))

    line = "  "
    for sym, rate, n, wr in sym_rates:
        line += f"{sym}: {rate:.1f}/d ({wr})   "
    print(line)

    # ── Edge bucket analysis ─────────────────────────────────────────────────
    buckets = [(0.03, 0.05), (0.05, 0.07), (0.07, 0.10), (0.10, 0.20), (0.20, 1.0)]
    print()
    print(f"  EDGE BUCKETS (dengan filter):")
    print(f"  {'range':<12} {'n':>6} {'wr':>7} {'roi/trade':>10} {'total pnl':>10}")
    print("  " + "-" * 50)
    for lo, hi in buckets:
        bucket = [r for r in filt_rows if lo <= r["edge"] < hi]
        if not bucket:
            continue
        n  = len(bucket)
        w  = sum(1 for r in bucket if r["win"])
        bp = sum(r["pnl"] for r in bucket)
        print(f"  {lo:.2f}–{hi:.2f}     {n:>6} {w/n:>6.1%} {bp/n:>+10.4f} {bp:>+10.2f}")

    # ── Filter breakdown ─────────────────────────────────────────────────────
    if use_filters and blocked:
        print()
        print(f"  FILTER BREAKDOWN ({len(blocked)} entries diblok):")
        fc: dict[str, list[bool]] = defaultdict(list)
        for r in blocked:
            for f in r["filters_hit"]:
                name = f.split("(")[0]
                fc[name].append(r["win"])

        print(f"  {'Filter':<22} {'blocked':>8} {'%':>5} {'WR jika masuk':>14}")
        print("  " + "-" * 52)
        for name, wins in sorted(fc.items(), key=lambda kv: -len(kv[1])):
            n   = len(wins)
            pct = n / len(blocked) * 100
            wr  = sum(wins) / n if n else 0
            print(f"  {name:<22} {n:>8} {pct:>4.0f}%  {wr:>13.1%}")

    # ── Max drawdown ─────────────────────────────────────────────────────────
    print()
    rs_sorted = sorted(filt_rows, key=lambda r: r["ts"])
    peak, cum, max_dd = 0.0, 0.0, 0.0
    for r in rs_sorted:
        cum += r["pnl"]
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    print(f"  Avg PnL/trade (filtered) : {roi_f:+.4f} per $1 stake")
    print(f"  Max drawdown  (filtered) : -{max_dd:.2f} pnl units")
    print(f"  Trade rate    (filtered) : {fn/days:.1f}/hari (all symbols)")

    # ── Verdict ──────────────────────────────────────────────────────────────
    print()
    target = filt_rows if use_filters else base_rows
    tn, tw, tpnl = _stats(target)
    if tn >= 50:
        roi = tpnl / tn
        wr  = tw / tn
        if roi >= 0.30:
            verdict = f"✅ EDGE KUAT — avg PnL +{roi:.2%}/trade"
        elif roi >= 0.10:
            verdict = f"🟢 EDGE TERBUKTI — avg PnL +{roi:.2%}/trade"
        elif roi >= 0.0:
            verdict = f"🟡 EDGE TIPIS — avg PnL +{roi:.2%}/trade"
        else:
            verdict = f"❌ TIDAK PROFITABLE — avg PnL {roi:+.2%}/trade"
        wins_pnl = sum(r["pnl"] for r in target if r["win"])
        loss_pnl = abs(sum(r["pnl"] for r in target if not r["win"]))
        pf = wins_pnl / loss_pnl if loss_pnl > 0 else float("inf")
        print(f"  VERDICT : {verdict}")
        print(f"  Stats   : winrate {wr:.1%} | ROI {roi:+.2%}/trade | profit factor {pf:.2f}")
    else:
        print(f"  ⚠️  Sample size {tn} < 50 — naikkan --days untuk reliable estimate")

    print("=" * W)


# ── main ────────────────────────────────────────────────────────────────────

async def main(args):
    assets = [args.asset.upper()] if args.asset else ASSETS
    invalid = [a for a in assets if a not in ASSETS]
    if invalid:
        print(f"Asset tidak dikenal: {invalid}. Pilih dari: {ASSETS}")
        return

    use_filters = not args.no_filters

    print(f"\nGBM Backtest — {args.days}d, entry T-{args.entry_min}m, lag {args.lag_min}m")
    print(f"Assets: {assets} | Filters: {'ON' if use_filters else 'OFF'}\n")

    since_ms = int((datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp() * 1000)

    async with aiohttp.ClientSession() as session:
        # Fetch BTC 1h klines once (needed for correlation filter on altcoins)
        altcoins = [a for a in assets if a != "BTC"]
        btc_klines_1h = None
        if use_filters and altcoins:
            print("  [BTC] Fetching 1h klines (for correlation filter)...", flush=True)
            btc_klines_1h = await fetch_all_klines("BTC", session, "1h", since_ms)

        all_results = []
        for symbol in assets:
            # If BTC is in assets, backtest_asset will use its own klines; pass None for btc_klines
            sym_btc = None if symbol == "BTC" else btc_klines_1h
            rs = await backtest_asset(
                symbol=symbol, session=session,
                days=args.days, entry_min=args.entry_min,
                lag_min=args.lag_min, fee=args.fee,
                min_edge=args.min_edge, vol_floor=args.vol_floor,
                min_price=args.min_price, max_price=args.max_price,
                btc_klines_1h=sym_btc,
                use_filters=use_filters,
            )
            filt_n = sum(1 for r in rs if not r["filters_hit"])
            if use_filters:
                print(f"  [{symbol}] {len(rs)} raw trades → {filt_n} passed filters "
                      f"({len(rs)-filt_n} blocked)")
            else:
                print(f"  [{symbol}] {len(rs)} trades simulated")
            all_results.extend(rs)

    print_report(
        all_results, days=args.days, entry_min=args.entry_min,
        lag_min=args.lag_min, fee=args.fee,
        min_edge=args.min_edge, vol_floor=args.vol_floor,
        use_filters=use_filters,
    )


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Backtest GBM hourly strategy")
    parser.add_argument("--days",      type=int,   default=30,    help="Lookback days (default 30)")
    parser.add_argument("--entry_min", type=int,   default=30,    help="Entry N min before close (default 30)")
    parser.add_argument("--lag_min",   type=int,   default=15,    help="Polymarket assumed lag (default 15)")
    parser.add_argument("--fee",       type=float, default=0.018, help="Taker fee (default 0.018)")
    parser.add_argument("--min_edge",  type=float, default=0.03,  help="Min edge gate (default 0.03)")
    parser.add_argument("--vol_floor", type=float, default=0.50,  help="Min annualized vol (default 0.50)")
    parser.add_argument("--min_price", type=float, default=0.20,  help="Min entry price (default 0.20)")
    parser.add_argument("--max_price", type=float, default=0.45,  help="Max entry price (default 0.45)")
    parser.add_argument("--asset",     type=str,   default=None,  help=f"Filter: {ASSETS}")
    parser.add_argument("--no-filters", action="store_true",      help="Disable technical filters (baseline)")
    args = parser.parse_args()

    asyncio.run(main(args))
