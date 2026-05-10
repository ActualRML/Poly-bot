"""
Backtest impact of technical filters + EMA trend bias on historical Up/Down hourly predictions.

Filters simulated:
  1. RSI exhaustion      — skip if RSI >= OB (buying Up) or RSI <= OS (buying Down)
  2. Z-score extreme     — skip if |Z| >= threshold in direction of trade
  3. Volume spike        — skip if Binance 1h volume > N x prior average
  4. BTC correlation     — skip if BTC 15m momentum >= 0.5% and opposes direction
  5. Macro trend gate    — skip counter-trend bets when BTC 4h trend >= 2%
  6. RSI momentum align  — skip if RSI >= 68 (uptrend) and betting Down, or RSI <= 35 Down
  7. Z-score momentum    — skip if Z >= 2.0 and betting Down (or <= -2.0 and betting Up)
  8. EMA trend bias      — adjust GBM P(Up) by ±0.10 based on 6h+24h EMA alignment

Fetches historical Binance klines at each prediction_date timestamp.
"""

import asyncio
import sys
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import aiohttp
from src.logic.technical import (
    compute_rsi, compute_zscore, detect_volume_spike, compute_trend,
    compute_ema, compute_trend_bias,
)

# ── Thresholds (mirror live config defaults) ───────────────────────────────────
RSI_OVERBOUGHT  = 70.0   # mean-reversion: skip Up when overbought
RSI_OVERSOLD    = 30.0   # mean-reversion: skip Down when oversold
RSI_MOM_OB      = 68.0   # momentum: skip Down when uptrend RSI >= 68 (relaxed from 65)
RSI_MOM_OS      = 35.0   # momentum: skip Up when downtrend RSI <= 35
RSI_PERIOD      = 14
ZSCORE_THR      = 2.5    # mean-reversion: extreme tails
ZSCORE_MOM_THR  = 2.0    # momentum: don't fight trend beyond 2.0σ (relaxed from 1.5)
ZSCORE_WINDOW   = 20
VOL_SPIKE_MULT  = 3.0
BTC_CORR_THR    = 0.005
MACRO_TREND_THR = 0.02
MACRO_TREND_H   = 4
BET_USDC        = 20.0

SYMBOL_MAP = {
    "bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL",
    "xrp": "XRP", "dogecoin": "DOGE", "bnb": "BNB",
}

# ── Helpers ────────────────────────────────────────────────────────────────────

def extract_symbol(question: str) -> str | None:
    q = question.lower()
    for kw, sym in SYMBOL_MAP.items():
        if kw in q:
            return sym
    return None


def parse_dt(s) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def simulate_pnl(buy_price: float, resolve_price, actual_outcome) -> float | None:
    if actual_outcome is None or resolve_price is None or buy_price <= 0:
        return None
    shares = BET_USDC / buy_price
    return round(float(resolve_price) * shares - BET_USDC, 2)


# ── Historical Binance fetchers ────────────────────────────────────────────────

async def get_tech_at(
    symbol: str,
    pred_dt: datetime,
    session: aiohttp.ClientSession,
) -> dict | None:
    from src.api.binance_client import fetch_klines_extended
    end_ms = int(pred_dt.timestamp() * 1000)
    limit  = max(ZSCORE_WINDOW, RSI_PERIOD, MACRO_TREND_H, 28) + 3
    klines = await fetch_klines_extended(symbol, session, interval="1h", limit=limit, end_ms=end_ms)
    if not klines:
        return None
    closes  = [k[4] for k in klines]
    volumes = [k[5] for k in klines] if len(klines[0]) > 5 else []
    return {
        "rsi":         compute_rsi(closes, RSI_PERIOD),
        "zscore":      compute_zscore(closes, ZSCORE_WINDOW),
        "vol_spike":   detect_volume_spike(volumes, VOL_SPIKE_MULT) if volumes else False,
        "trend_4h":    compute_trend(closes, MACRO_TREND_H),
        "trend_bias":  compute_trend_bias(closes) if len(closes) >= 25 else 0.0,
    }


async def get_btc_mom_at(
    pred_dt: datetime,
    session: aiohttp.ClientSession,
) -> float | None:
    from src.api.binance_client import fetch_klines
    end_ms = int(pred_dt.timestamp() * 1000)
    klines = await fetch_klines("BTC", session, interval="15m", limit=3, end_ms=end_ms)
    if len(klines) < 2:
        return None
    c = [k[4] for k in klines]
    prev, curr = c[-2], c[-1]
    return (curr - prev) / prev if prev > 0 else None


# ── Filter logic ───────────────────────────────────────────────────────────────

def apply_tech_filters(
    outcome: str,
    tech: dict | None,
    btc_mom: float | None,
    btc_tech: dict | None,
    symbol: str,
) -> list[str]:
    if tech is None:
        return []
    triggered = []

    if tech.get("vol_spike"):
        triggered.append("VOL_SPIKE")

    rsi = tech.get("rsi")
    if rsi is not None:
        # Mean-reversion exhaustion
        if outcome == "Up" and rsi >= RSI_OVERBOUGHT:
            triggered.append(f"RSI_OB({rsi:.1f})")
        elif outcome == "Down" and rsi <= RSI_OVERSOLD:
            triggered.append(f"RSI_OS({rsi:.1f})")
        # Momentum alignment
        elif outcome == "Down" and rsi >= RSI_MOM_OB:
            triggered.append(f"RSI_MOM_UP({rsi:.1f})")
        elif outcome == "Up" and rsi <= RSI_MOM_OS:
            triggered.append(f"RSI_MOM_DOWN({rsi:.1f})")

    z = tech.get("zscore")
    if z is not None:
        # Mean-reversion extreme
        if z is not None and ZSCORE_THR > 0:
            if outcome == "Up" and z >= ZSCORE_THR:
                triggered.append(f"Z_HIGH({z:.2f})")
            elif outcome == "Down" and z <= -ZSCORE_THR:
                triggered.append(f"Z_LOW({z:.2f})")
        # Momentum alignment
        if ZSCORE_MOM_THR > 0:
            if outcome == "Down" and z >= ZSCORE_MOM_THR:
                triggered.append(f"Z_MOM_UP({z:.2f})")
            elif outcome == "Up" and z <= -ZSCORE_MOM_THR:
                triggered.append(f"Z_MOM_DOWN({z:.2f})")

    if symbol != "BTC" and btc_mom is not None and abs(btc_mom) >= BTC_CORR_THR:
        btc_dir = "Up" if btc_mom > 0 else "Down"
        if btc_dir != outcome:
            triggered.append(f"BTC_CORR({btc_mom:+.2%})")

    # Macro trend gate: BTC 4h trend
    _btc_src = btc_tech if symbol != "BTC" else tech
    btc_trend = (_btc_src or {}).get("trend_4h") if _btc_src else None
    if btc_trend is not None and MACRO_TREND_THR > 0:
        if btc_trend > MACRO_TREND_THR and outcome == "Down":
            triggered.append(f"MACRO_UP({btc_trend:+.2%})")
        elif btc_trend < -MACRO_TREND_THR and outcome == "Up":
            triggered.append(f"MACRO_DOWN({btc_trend:+.2%})")

    # EMA trend bias — would have flipped GBM direction
    bias = tech.get("trend_bias", 0.0)
    if bias != 0.0:
        if outcome == "Up" and bias < -0.05:
            triggered.append(f"EMA_BIAS_DOWN({bias:+.2f})")
        elif outcome == "Down" and bias > 0.05:
            triggered.append(f"EMA_BIAS_UP({bias:+.2f})")

    return triggered


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    conn = sqlite3.connect(str(_ROOT / "data" / "bot_database.db"))
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT condition_id, question, outcome, market_price,
               prediction_date, actual_outcome, resolve_price
        FROM predictions
        WHERE actual_outcome IS NOT NULL
        ORDER BY prediction_date
    """)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    updown_rows = [r for r in rows if str(r["outcome"]).lower() in ("up", "down")]

    print("=" * 75)
    print(f"  BACKTEST TECHNICAL FILTERS v2 — {len(updown_rows)} Up/Down resolved entries")
    print(f"  RSI OB={RSI_OVERBOUGHT:.0f} OS={RSI_OVERSOLD:.0f} MOM_OB={RSI_MOM_OB:.0f} | "
          f"Z-thr={ZSCORE_THR} Z-mom={ZSCORE_MOM_THR} | VolSpike={VOL_SPIKE_MULT:.0f}x")
    print(f"  BTCCorr>={BTC_CORR_THR:.1%} | MacroTrend>={MACRO_TREND_THR:.0%} in {MACRO_TREND_H}h | "
          f"EMA bias ±0.05 per component")
    print(f"  Fetching historical Binance klines per entry...")
    print("=" * 75)

    async with aiohttp.ClientSession() as session:
        results = []
        for i, r in enumerate(updown_rows):
            symbol  = extract_symbol(r["question"])
            pred_dt = parse_dt(r["prediction_date"])
            if not symbol or pred_dt is None:
                continue

            buy_price = float(r["market_price"] or 0)
            if buy_price <= 0:
                continue

            ao  = int(r["actual_outcome"])
            rp  = float(r["resolve_price"]) if r["resolve_price"] is not None else None
            pnl = simulate_pnl(buy_price, rp, ao)
            if pnl is None:
                continue

            outcome  = str(r["outcome"])
            tech     = await get_tech_at(symbol, pred_dt, session)
            btc_mom  = await get_btc_mom_at(pred_dt, session) if symbol != "BTC" else None
            btc_tech = await get_tech_at("BTC", pred_dt, session) if symbol != "BTC" else tech
            filters_hit = apply_tech_filters(outcome, tech, btc_mom, btc_tech, symbol)

            results.append({
                "r": r, "symbol": symbol, "outcome": outcome,
                "pnl": pnl, "tech": tech, "btc_mom": btc_mom,
                "btc_tech": btc_tech, "filters_hit": filters_hit,
            })
            print(f"  [{i+1:2}/{len(updown_rows)}] {symbol:4} {outcome:4} "
                  f"RSI={str(tech['rsi'] if tech else 'N/A'):5} "
                  f"Z={str(tech['zscore'] if tech else 'N/A'):6} "
                  f"BTC={btc_mom:+.2%} " if btc_mom is not None else
                  f"  [{i+1:2}/{len(updown_rows)}] {symbol:4} {outcome:4} "
                  f"RSI={str(tech['rsi'] if tech else 'N/A'):5} "
                  f"Z={str(tech['zscore'] if tech else 'N/A'):6} "
                  f"BTC=N/A  "
                  , end="")
            print(f"-> {'BLOCK: ' + ', '.join(filters_hit) if filters_hit else 'PASS'}")

            if i % 8 == 7:
                await asyncio.sleep(0.3)

    blocked = [x for x in results if x["filters_hit"]]
    passed  = [x for x in results if not x["filters_hit"]]

    base_n   = len(results)
    base_w   = sum(1 for x in results if x["pnl"] > 0)
    base_pnl = sum(x["pnl"] for x in results)
    base_wr  = base_w / base_n * 100 if base_n else 0

    filt_n   = len(passed)
    filt_w   = sum(1 for x in passed if x["pnl"] > 0)
    filt_pnl = sum(x["pnl"] for x in passed)
    filt_wr  = filt_w / filt_n * 100 if filt_n else 0

    blk_w    = sum(1 for x in blocked if x["pnl"] > 0)
    blk_l    = sum(1 for x in blocked if x["pnl"] <= 0)
    blk_pnl  = sum(x["pnl"] for x in blocked)

    print(f"\n{'-'*75}")
    print(f"  BLOCKED ({len(blocked)} entries)")
    print(f"{'-'*75}")
    for x in blocked:
        result    = "W" if x["pnl"] > 0 else "L"
        rsi       = x["tech"]["rsi"] if x["tech"] else None
        z         = x["tech"]["zscore"] if x["tech"] else None
        btc_src   = x["btc_tech"] if x["symbol"] != "BTC" else x["tech"]
        bt        = (btc_src or {}).get("trend_4h")
        q         = str(x["r"]["question"])[-38:]
        print(f"  {result} {x['outcome']:4} RSI={str(rsi)[:5]:5} Z={str(z)[:6]:6} "
              f"BTC4h={bt:+.2%} " if bt is not None else
              f"  {result} {x['outcome']:4} RSI={str(rsi)[:5]:5} Z={str(z)[:6]:6} BTC4h=N/A  ",
              end="")
        print(f"pnl={x['pnl']:+.2f} | {q}")
        print(f"       -> {', '.join(x['filters_hit'])}")

    print(f"\n{'-'*75}")
    print(f"  PASSED ({len(passed)} entries)")
    print(f"{'-'*75}")
    for x in passed:
        result  = "W" if x["pnl"] > 0 else "L"
        rsi     = x["tech"]["rsi"] if x["tech"] else None
        z       = x["tech"]["zscore"] if x["tech"] else None
        btc_src = x["btc_tech"] if x["symbol"] != "BTC" else x["tech"]
        bt      = (btc_src or {}).get("trend_4h")
        q       = str(x["r"]["question"])[-38:]
        print(f"  {result} {x['outcome']:4} RSI={str(rsi)[:5]:5} Z={str(z)[:6]:6} "
              f"BTC4h={bt:+.2%} " if bt is not None else
              f"  {result} {x['outcome']:4} RSI={str(rsi)[:5]:5} Z={str(z)[:6]:6} BTC4h=N/A  ",
              end="")
        print(f"pnl={x['pnl']:+.2f} | {q}")

    print(f"\n{'='*75}")
    print(f"  SUMMARY")
    print(f"{'='*75}")
    print(f"  {'Metric':<25} {'Baseline':>12} {'With Tech Filters':>18}")
    print(f"  {'-'*55}")
    print(f"  {'Entries':<25} {base_n:>12} {filt_n:>18}")
    print(f"  {'Wins':<25} {base_w:>12} {filt_w:>18}")
    print(f"  {'Losses':<25} {base_n-base_w:>12} {filt_n-filt_w:>18}")
    print(f"  {'Winrate':<25} {base_wr:>11.1f}% {filt_wr:>17.1f}%")
    print(f"  {'Total PnL':<25} ${base_pnl:>+10.2f} ${filt_pnl:>+16.2f}")
    print(f"  {'Blocked':<25} {'':>12} {len(blocked):>18}")
    print(f"    Blocked W/L          {'':>12}  {blk_w}/{blk_l}  (PnL ${blk_pnl:+.2f})")
    print(f"{'='*75}")

    print(f"\n  FILTER BREAKDOWN")
    fc: dict[str, int] = {}
    for x in blocked:
        for f in x["filters_hit"]:
            name = f.split("(")[0]
            fc[name] = fc.get(name, 0) + 1
    for name, cnt in sorted(fc.items(), key=lambda kv: -kv[1]):
        print(f"    {name:<30} {cnt} entries")
    print()


if __name__ == "__main__":
    asyncio.run(main())
