"""
Backtest filter changes against historical predictions table.

Simulates impact of 3 new filters on past predictions:
  1. GBM edge cap    — skip if GBM edge > MAX_EDGE (default 25%)
  2. Consensus floor — skip if market prices opposite side < CONSENSUS_FLOOR (default 10%)
  3. Min T floor     — skip if time-to-resolve at entry < MIN_T_MINUTES (default 20m)

Uses predictions table (actual_outcome=1 means correct, =0 means wrong).
Assumes flat bet size = BET_USDC per trade (configurable).
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import sqlite3
from datetime import datetime, timezone

# -- Configurable thresholds ---------------------------------------------------
MAX_EDGE        = 0.25   # 25% — skip entries where GBM edge > this
CONSENSUS_FLOOR = 0.90   # skip if market prices opposite side < 1 - 0.90 = 10%
MIN_T_MINUTES   = 20     # skip if < 20 min to resolve at entry time
BET_USDC        = 20.0   # simulated flat bet size per trade

# -- Helpers -------------------------------------------------------------------

def normalize_gap(gap_raw) -> float:
    """gap_pct stored inconsistently: sometimes decimal (0.02), sometimes percent (52.16).
    Normalize to decimal fraction."""
    if gap_raw is None:
        return 0.0
    g = float(gap_raw)
    # If > 1, it was stored as percentage (multiply by 100 was applied in code)
    return g / 100.0 if g > 1.0 else g


def parse_dt(s) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def minutes_to_resolve(prediction_date_str, resolve_date_str) -> float | None:
    pd = parse_dt(prediction_date_str)
    rd = parse_dt(resolve_date_str)
    if pd is None or rd is None:
        return None
    secs = (rd - pd).total_seconds()
    return secs / 60.0


def market_price_up_from_row(outcome: str, market_price: float) -> float:
    """Reconstruct market_price_up: stored as buy_price of chosen side."""
    if outcome == "Up":
        return market_price
    else:  # Down
        return 1.0 - market_price


def apply_filters(row: dict) -> list[str]:
    """Return list of triggered filter names. Empty = entry passes all filters."""
    triggered = []

    outcome      = str(row["outcome"])
    market_price = float(row["market_price"] or 0)
    gap_pct_raw  = row["gap_pct"]
    pred_date    = row["prediction_date"]
    resolve_date = row["resolve_date"]

    # Reconstruct market_price_up
    mkt_up = market_price_up_from_row(outcome, market_price)

    # Filter 1: GBM edge cap
    edge = normalize_gap(gap_pct_raw)
    if edge > MAX_EDGE:
        triggered.append(f"EDGE_CAP({edge:.2%}>{MAX_EDGE:.0%})")

    # Filter 2: Market consensus floor
    if outcome == "Down" and mkt_up > CONSENSUS_FLOOR:
        triggered.append(f"CONSENSUS_UP({mkt_up:.2f}>{CONSENSUS_FLOOR:.0%})")
    if outcome == "Up" and mkt_up < (1.0 - CONSENSUS_FLOOR):
        triggered.append(f"CONSENSUS_DOWN({mkt_up:.2f}<{1-CONSENSUS_FLOOR:.0%})")

    # Filter 3: Min T floor
    t_min = minutes_to_resolve(pred_date, resolve_date)
    if t_min is not None and t_min < MIN_T_MINUTES:
        triggered.append(f"MIN_T({t_min:.0f}m<{MIN_T_MINUTES}m)")

    return triggered


def simulate_pnl(buy_price: float, resolve_price: float | None, actual_outcome: int | None) -> float | None:
    """PnL in USDC for a flat BET_USDC wager at buy_price."""
    if actual_outcome is None or resolve_price is None:
        return None
    # actual_outcome=1 → token resolves to ~1.0 (win), =0 → ~0 (loss)
    shares = BET_USDC / buy_price
    final  = float(resolve_price) * shares
    return round(final - BET_USDC, 2)


# -- Main ----------------------------------------------------------------------

def main():
    conn = sqlite3.connect(str(_ROOT / "data" / "bot_database.db"))
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("""
        SELECT condition_id, question, outcome, predicted_prob, market_price,
               gap_pct, prediction_date, resolve_date,
               actual_outcome, resolve_price, resolved_at
        FROM predictions
        ORDER BY prediction_date
    """)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    total      = len(rows)
    resolved   = [r for r in rows if r["actual_outcome"] is not None]
    unresolved = [r for r in rows if r["actual_outcome"] is None]

    print(f"{'='*70}")
    print(f"  BACKTEST FILTER IMPACT — {total} predictions ({len(resolved)} resolved, {len(unresolved)} unresolved)")
    print(f"  Filters: edge<={MAX_EDGE:.0%} | consensus>={CONSENSUS_FLOOR:.0%} | T>={MIN_T_MINUTES}m | bet=${BET_USDC}")
    print(f"{'='*70}")

    # -- BASELINE (no filters) -------------------------------------------------
    base_entries, base_wins, base_losses = 0, 0, 0
    base_pnl = 0.0
    for r in resolved:
        buy_price = float(r["market_price"] or 0)
        if buy_price <= 0:
            continue
        ao = int(r["actual_outcome"])
        rp = float(r["resolve_price"]) if r["resolve_price"] is not None else None
        pnl = simulate_pnl(buy_price, rp, ao)
        if pnl is None:
            continue
        base_entries += 1
        if pnl > 0:
            base_wins += 1
        else:
            base_losses += 1
        base_pnl += pnl

    base_wr = base_wins / base_entries * 100 if base_entries else 0

    # -- WITH FILTERS ----------------------------------------------------------
    filt_entries, filt_wins, filt_losses = 0, 0, 0
    filt_pnl = 0.0
    blocked  = []
    passed   = []

    for r in resolved:
        buy_price = float(r["market_price"] or 0)
        if buy_price <= 0:
            continue
        ao = int(r["actual_outcome"])
        rp = float(r["resolve_price"]) if r["resolve_price"] is not None else None
        pnl = simulate_pnl(buy_price, rp, ao)
        if pnl is None:
            continue

        filters_hit = apply_filters(r)
        sym         = str(r["question"])[-38:]

        if filters_hit:
            blocked.append((r, filters_hit, pnl))
        else:
            passed.append((r, pnl))
            filt_entries += 1
            if pnl > 0:
                filt_wins += 1
            else:
                filt_losses += 1
            filt_pnl += pnl

    filt_wr = filt_wins / filt_entries * 100 if filt_entries else 0

    # -- BLOCKED entries detail ------------------------------------------------
    print(f"\n{'-'*70}")
    print(f"  BLOCKED BY FILTERS ({len(blocked)} entries)")
    print(f"{'-'*70}")
    blocked_wins, blocked_losses, blocked_pnl_total = 0, 0, 0.0
    for r, filters_hit, pnl in blocked:
        result = "W" if pnl > 0 else "L"
        q      = str(r["question"])[-40:]
        edge   = normalize_gap(r["gap_pct"])
        t_min  = minutes_to_resolve(r["prediction_date"], r["resolve_date"])
        t_str  = f"{t_min:.0f}m" if t_min else "?"
        print(f"  {result} {r['outcome']:4} edge={edge:.2%} T={t_str:4} pnl={pnl:+.2f} | {q}")
        print(f"       -> {', '.join(filters_hit)}")
        blocked_pnl_total += pnl
        if pnl > 0:
            blocked_wins += 1
        else:
            blocked_losses += 1

    # -- PASSED entries detail -------------------------------------------------
    print(f"\n{'-'*70}")
    print(f"  PASSED FILTERS ({len(passed)} entries)")
    print(f"{'-'*70}")
    for r, pnl in passed:
        result = "W" if pnl > 0 else "L"
        q      = str(r["question"])[-40:]
        edge   = normalize_gap(r["gap_pct"])
        t_min  = minutes_to_resolve(r["prediction_date"], r["resolve_date"])
        t_str  = f"{t_min:.0f}m" if t_min else "?"
        mkt_up = market_price_up_from_row(r["outcome"], float(r["market_price"] or 0))
        print(f"  {result} {r['outcome']:4} edge={edge:.2%} T={t_str:4} mkt_up={mkt_up:.2f} pnl={pnl:+.2f} | {q}")

    # -- SUMMARY ---------------------------------------------------------------
    saved_loss  = blocked_pnl_total  # negative = losses avoided
    print(f"\n{'='*70}")
    print(f"  SUMMARY COMPARISON")
    print(f"{'='*70}")
    print(f"  {'Metric':<25} {'Baseline':>12} {'With Filters':>14}")
    print(f"  {'-'*51}")
    print(f"  {'Entries (resolved)':<25} {base_entries:>12} {filt_entries:>14}")
    print(f"  {'Wins':<25} {base_wins:>12} {filt_wins:>14}")
    print(f"  {'Losses':<25} {base_losses:>12} {filt_losses:>14}")
    print(f"  {'Winrate':<25} {base_wr:>11.1f}% {filt_wr:>13.1f}%")
    print(f"  {'Total PnL (${BET_USDC}/trade)':<25} ${base_pnl:>+10.2f} ${filt_pnl:>+12.2f}")
    print(f"  {'PnL saved by blocking':<25} {'':>12} ${-blocked_pnl_total if blocked_pnl_total < 0 else blocked_pnl_total:>+12.2f}")
    print(f"  {'Blocked entries':<25} {'':>12} {len(blocked):>14}")
    print(f"    Blocked W/L           {'':>12} {blocked_wins}/{blocked_losses}")
    print(f"{'='*70}")

    # -- Filter breakdown ------------------------------------------------------
    print(f"\n  FILTER BREAKDOWN")
    filter_counts = {}
    for _, filters_hit, _ in blocked:
        for f in filters_hit:
            name = f.split("(")[0]
            filter_counts[name] = filter_counts.get(name, 0) + 1
    for name, cnt in sorted(filter_counts.items(), key=lambda x: -x[1]):
        print(f"    {name:<30} {cnt} entries")

    print()


if __name__ == "__main__":
    main()
