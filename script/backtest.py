"""
Backtest v2 — Retroactive analysis untuk strategi baru.

Apply filter + sizing logic terbaru ke historical trades di DB untuk validasi:
- Berapa losing entries akan di-FILTER (event horizon, momentum, ONE_SIDED, regime)
- Berapa PnL improvement dengan aggressive sizing (Kelly 0.7, base 25%)
- Filter mana yang paling impactful

LIMITATIONS:
- Tick-level price data missing → tidak bisa simulate SL T3/T4 mid-life cuts
- Tagged sebagai "FILTER REJECTION ANALYSIS", bukan predicted PnL

Usage: python -m script.backtest
"""
from __future__ import annotations

import io
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

DB_PATH = Path(__file__).parent.parent / "data" / "bot_database.db"
SALDO_AWAL = 120.0

# New strategy sizing config (matches src/utils/config.py defaults)
RISK_BASE_SIZE_PCT = 0.25
RISK_MIN_SIZE_PCT  = 0.08
RISK_MAX_SIZE_PCT  = 0.40
MIN_POSITION       = 10.0
MAX_POSITION       = 75.0
KELLY_MULTIPLIER   = 0.7

# Filter thresholds (matches .env.local defaults)
MOMENTUM_MIN              = 0.0015
MOMENTUM_VOL_FACTOR       = 0.75
CONTRARIAN_MIN_T_MINUTES  = 20   # event horizon contrarian floor
T_TIER_TIGHT_MAX          = 35
T_TIER_CRITICAL_MAX       = 25
ONE_SIDED_HIGH            = 0.82
ONE_SIDED_LOW             = 0.18
MIN_ENTRY_PRICE           = 0.25
MAX_ENTRY_PRICE           = 0.60
CONVICTION_BONUS          = 1.5

_SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"]
_NAME_MAP = {
    "BITCOIN": "BTC",
    "ETHEREUM": "ETH",
    "SOLANA": "SOL",
    "DOGECOIN": "DOGE",
}
_VOL_ANNUAL = {
    "BTC": 0.44, "ETH": 0.55, "SOL": 0.70,
    "XRP": 0.60, "DOGE": 1.00, "BNB": 0.56,
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _extract_symbol(question: str) -> str:
    q = (question or "").upper()
    for name, sym in _NAME_MAP.items():
        if name in q:
            return sym
    for s in _SYMBOLS:
        if s in q:
            return s
    return "?"


def _session(dt: datetime) -> str:
    h = dt.hour + dt.minute / 60.0
    if 13.5 <= h < 15.5:
        return "US_OPEN"
    if 15.5 <= h < 21.0:
        return "US_MAIN"
    if h >= 21.0 or h < 8.0:
        return "ASIA"
    return "EU"


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ── Filter Checks (retroactive) ──────────────────────────────────────────────

@dataclass
class Pos:
    id: int
    question: str
    symbol: str
    outcome: str
    entry_price: float
    exit_price: float
    pnl_usdc: float
    capital_at_risk: float
    strategy_mode: str
    gap_pct: float          # |momentum| at entry
    entry_time: datetime
    exit_time: datetime
    resolve_date: datetime
    market_price_up: float  # derived: entry_price if outcome=Up else 1-entry_price


def _check_filters(p: Pos) -> tuple[str | None, dict]:
    """
    Apply new strategy filters retroactively. Return (rejected_by, debug).
    None = accepted, str = filter that rejected.
    """
    debug: dict = {}

    # 1. Entry price range
    if not (MIN_ENTRY_PRICE <= p.entry_price <= MAX_ENTRY_PRICE):
        return f"entry_price_range ({p.entry_price})", debug

    # 2. Event horizon
    t_remaining_at_entry = (p.resolve_date - p.entry_time).total_seconds() / 60.0
    debug["t_remaining_min"] = round(t_remaining_at_entry, 1)
    if t_remaining_at_entry < CONTRARIAN_MIN_T_MINUTES and "contrarian" in p.strategy_mode:
        return f"event_horizon_contrarian (T={t_remaining_at_entry:.0f}m < {CONTRARIAN_MIN_T_MINUTES}m)", debug

    # 3. Momentum threshold (vol-adjusted)
    vol_annual = _VOL_ANNUAL.get(p.symbol, 0.40)
    vol_15m = vol_annual / (252 * 96) ** 0.5
    mom_thr = max(MOMENTUM_MIN, vol_15m * MOMENTUM_VOL_FACTOR)
    debug["mom_threshold"] = round(mom_thr, 5)
    if "hourly" in p.strategy_mode and p.gap_pct < mom_thr:
        return f"momentum_low ({p.gap_pct:.4f} < {mom_thr:.4f})", debug

    # 4. Per-market ONE_SIDED check
    if p.market_price_up > ONE_SIDED_HIGH:
        return f"one_sided_high (up={p.market_price_up:.3f})", debug
    if p.market_price_up < ONE_SIDED_LOW:
        return f"one_sided_low (up={p.market_price_up:.3f})", debug

    return None, debug


# ── Sizing Recompute ─────────────────────────────────────────────────────────

def _compute_new_sizing(p: Pos, last_pnls: list[float], capital: float, t_min: float, vol_state: str) -> float:
    """Recompute capital_at_risk using new aggressive sizing config."""
    base = max(MIN_POSITION, capital * RISK_BASE_SIZE_PCT)

    # Consecutive results adjustment
    wins, losses = 0, 0
    for pnl in reversed(last_pnls[-5:]):
        if pnl > 0:
            if losses > 0: break
            wins += 1
        else:
            if wins > 0: break
            losses += 1
    if losses >= 2:
        base = max(MIN_POSITION, capital * RISK_MIN_SIZE_PCT)
    elif wins >= 3:
        base = max(MIN_POSITION, capital * RISK_MAX_SIZE_PCT)

    # Kelly fraction (approx: winrate ~0.55 contrarian, ~0.65 GBM)
    if "gbm" in p.strategy_mode:
        winrate = 0.65
    else:
        winrate = 0.55
    b = (1 - p.entry_price) / p.entry_price  # binary payoff ratio
    kelly_fraction = max(0.0, (b * winrate - (1 - winrate)) / b)
    kelly_bet = capital * kelly_fraction * KELLY_MULTIPLIER

    bet = min(base, kelly_bet) if kelly_bet > 0 else base

    # Conviction bonus (WIDE tier + reasonable vol)
    if t_min >= T_TIER_TIGHT_MAX and vol_state in ("NORMAL", "LOW", "EXTREME_LOW"):
        bet *= CONVICTION_BONUS

    return max(MIN_POSITION, min(bet, MAX_POSITION))


# ── Load DB ──────────────────────────────────────────────────────────────────

def load_positions(db_path: Path) -> list[Pos]:
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("""
        SELECT id, question, outcome, entry_price, exit_price, pnl_usdc,
               capital_at_risk, strategy_mode, gap_pct, entry_time, exit_time, resolve_date
        FROM positions WHERE status='closed'
        ORDER BY entry_time ASC
    """).fetchall()
    conn.close()

    positions: list[Pos] = []
    for r in rows:
        try:
            entry_time = _parse_dt(r[9])
            exit_time  = _parse_dt(r[10])
            resolve_dt = _parse_dt(r[11]) if r[11] else exit_time
            outcome    = (r[2] or "").lower()
            entry_p    = float(r[3])
            mkt_up     = entry_p if outcome == "up" else round(1.0 - entry_p, 4)

            positions.append(Pos(
                id=r[0], question=r[1] or "", symbol=_extract_symbol(r[1] or ""),
                outcome=outcome, entry_price=entry_p, exit_price=float(r[4] or 0),
                pnl_usdc=float(r[5] or 0), capital_at_risk=float(r[6] or 0),
                strategy_mode=r[7] or "", gap_pct=abs(float(r[8] or 0)),
                entry_time=entry_time, exit_time=exit_time, resolve_date=resolve_dt,
                market_price_up=mkt_up,
            ))
        except Exception as e:
            print(f"[WARN] Skip pos {r[0]}: {e}")
            continue
    return positions


# ── Backtest Run ─────────────────────────────────────────────────────────────

def backtest(positions: list[Pos]) -> dict:
    results = {"taken": [], "rejected": []}
    rejection_counts: dict[str, int] = defaultdict(int)
    rejection_savings: dict[str, float] = defaultdict(float)
    capital = SALDO_AWAL
    closed_pnls: list[float] = []

    for p in positions:
        rejected_by, _ = _check_filters(p)
        if rejected_by:
            key = rejected_by.split(" ")[0]  # extract filter name
            rejection_counts[key] += 1
            # "Saving" = avoiding the actual loss (if was loss). Wins skipped = loss of opportunity.
            rejection_savings[key] += -p.pnl_usdc  # save positive amount on rejected losses
            results["rejected"].append({"pos": p, "by": rejected_by})
            continue

        # Compute new sizing
        t_min = (p.resolve_date - p.entry_time).total_seconds() / 60.0
        new_size = _compute_new_sizing(p, closed_pnls, capital, t_min, vol_state="NORMAL")
        size_ratio = new_size / max(p.capital_at_risk, 0.01)
        new_pnl = p.pnl_usdc * size_ratio

        results["taken"].append({
            "pos": p, "old_size": p.capital_at_risk, "new_size": new_size,
            "old_pnl": p.pnl_usdc, "new_pnl": new_pnl,
        })
        closed_pnls.append(new_pnl)
        capital += new_pnl

    return {
        "results": results,
        "rejection_counts": dict(rejection_counts),
        "rejection_savings": dict(rejection_savings),
        "final_capital": capital,
    }


# ── Reporting ────────────────────────────────────────────────────────────────

def print_report(positions: list[Pos], outcome: dict) -> None:
    results = outcome["results"]
    rejections = outcome["rejection_counts"]
    savings = outcome["rejection_savings"]

    actual_pnl = sum(p.pnl_usdc for p in positions)
    actual_wins = sum(1 for p in positions if p.pnl_usdc > 0)

    new_pnl = sum(r["new_pnl"] for r in results["taken"])
    new_wins = sum(1 for r in results["taken"] if r["new_pnl"] > 0)

    print("=" * 70)
    print("BACKTEST v2 — Retroactive Filter + Sizing Analysis")
    print("=" * 70)
    print()
    print("NOTE: tick-level data tidak tersedia. T3/T4 SL mid-life cuts tidak")
    print("      di-simulate. Hasil = 'filter rejection analysis', bukan predicted PnL.")
    print()
    print(f"Total historical positions: {len(positions)}")
    print()

    # Actual (no new filter)
    print(f"[ACTUAL] Strategy lama:")
    print(f"  Trades: {len(positions)} | Wins: {actual_wins} ({actual_wins/len(positions)*100:.1f}%)")
    print(f"  Total PnL: ${actual_pnl:+.2f}")
    print()

    # With new filter applied
    print(f"[NEW STRATEGY] Setelah apply filter + sizing baru:")
    print(f"  Filtered (would skip): {len(results['rejected'])} positions")
    print(f"  Taken (would enter):   {len(results['taken'])} positions")
    if results["taken"]:
        wr = new_wins / len(results["taken"]) * 100
        print(f"  Wins on taken: {new_wins} ({wr:.1f}%)")
    print(f"  New PnL on taken:   ${new_pnl:+.2f}")
    total_saved = sum(savings.values())
    print(f"  Saved by filtering: ${total_saved:+.2f} (avoided losses)")
    net_improvement = new_pnl - actual_pnl + total_saved
    print(f"  NET vs actual:      ${net_improvement:+.2f}  (new_pnl - actual_pnl + savings_from_filter)")
    print()

    # Filter breakdown
    print(f"[FILTER BREAKDOWN] (rejections by filter):")
    if not rejections:
        print("  No rejections")
    else:
        for filter_name in sorted(rejections.keys(), key=lambda k: -rejections[k]):
            cnt = rejections[filter_name]
            saving = savings[filter_name]
            print(f"  {filter_name:30s}: {cnt:2d} rejected | saved ${saving:+.2f}")
    print()

    # Sizing comparison on taken positions
    if results["taken"]:
        print(f"[SIZING] On taken positions ({len(results['taken'])} trades):")
        old_total = sum(r["old_size"] for r in results["taken"])
        new_total = sum(r["new_size"] for r in results["taken"])
        avg_ratio = new_total / max(old_total, 0.01)
        print(f"  Old avg size: ${old_total / len(results['taken']):.2f}")
        print(f"  New avg size: ${new_total / len(results['taken']):.2f}")
        print(f"  Ratio:        {avg_ratio:.2f}x")
    print()

    # Show top rejections
    if results["rejected"]:
        print(f"[TOP 5 REJECTED] (worst losses avoided):")
        sorted_rej = sorted(results["rejected"], key=lambda r: r["pos"].pnl_usdc)[:5]
        for r in sorted_rej:
            p = r["pos"]
            print(f"  #{p.id} {p.symbol} {p.outcome:5s} @ {p.entry_price:.3f} "
                  f"| PnL ${p.pnl_usdc:+.2f} | by: {r['by']}")
    print()

    print("=" * 70)
    print(f"Final equity (simulated): ${outcome['final_capital']:.2f} "
          f"(from ${SALDO_AWAL:.2f})")
    print("=" * 70)


# ── Entry ────────────────────────────────────────────────────────────────────

def main() -> None:
    if not DB_PATH.exists():
        print(f"ERROR: DB not found at {DB_PATH}")
        sys.exit(1)

    positions = load_positions(DB_PATH)
    if not positions:
        print("ERROR: No closed positions in DB to backtest.")
        sys.exit(1)

    outcome = backtest(positions)
    print_report(positions, outcome)


if __name__ == "__main__":
    main()
