"""Calibrate / validate price_zone thresholds against research/calibration.db.

READ-ONLY analysis (NOT part of the bot). The fetcher stored, per resolved Up/Down
market, the Up(=YES) price at T-60/30/15/5 min before resolution plus the final
outcome (1 = Up won). For each lead-time this buckets the 491 markets by
`classify_price_zone(up_price)` and reports per zone: count + empirical YES
win-rate. It then sweeps a few candidate threshold sets and flags which stay
monotonic + adequately populated, and prints a recommendation.

The default 0.20/0.40/0.60/0.80 boundaries validate here: at T-30 they give
monotonic ~10/31/53/82/98% win-rate buckets (Brier 0.131). Run:

    .venv/Scripts/python.exe research/calibrate_zones.py
"""
import sqlite3
import sys
from pathlib import Path

# make `import src.*` resolve regardless of cwd (mirrors analyze_trades.py)
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.classify.price_zone import ZoneThresholds, classify_price_zone  # noqa: E402

DB_PATH = REPO_ROOT / "research" / "calibration.db"
LEAD_COLS = ("price_t60", "price_t30", "price_t15", "price_t5")
REF_COL = "price_t30"               # reference lead-time for the sweep
REPORT_ZONES = ("extreme_low", "low", "uncertain", "high", "extreme_high")
MIN_ZONE_N = 10                     # a zone with fewer rows is too thin to trust

# A handful of candidate boundary sets to stress the current defaults against.
CANDIDATES: dict[str, ZoneThresholds] = {
    "current   (0.20/0.40/0.60/0.80)": ZoneThresholds(0.20, 0.40, 0.60, 0.80),
    "tight-ext (0.15/0.40/0.60/0.85)": ZoneThresholds(0.15, 0.40, 0.60, 0.85),
    "wide-ext  (0.25/0.40/0.60/0.75)": ZoneThresholds(0.25, 0.40, 0.60, 0.75),
    "narrow-mid(0.20/0.45/0.55/0.80)": ZoneThresholds(0.20, 0.45, 0.55, 0.80),
}


def _load(conn, col) -> list[tuple[float, int]]:
    rows = conn.execute(
        f"SELECT {col} AS p, outcome AS o FROM calibration_data WHERE {col} IS NOT NULL"
    ).fetchall()
    return [(float(r["p"]), int(r["o"])) for r in rows]


def _zone_stats(data, zones) -> dict[str, list[int]]:
    """zone -> [n, wins]; wins counts outcome==1 (Up/YES won)."""
    agg = {z: [0, 0] for z in REPORT_ZONES}
    for p, o in data:
        z = classify_price_zone(p, zones)
        if z in agg:
            agg[z][0] += 1
            agg[z][1] += o
    return agg


def _brier(data) -> float:
    return sum((p - o) ** 2 for p, o in data) / len(data) if data else float("nan")


def _rates(agg) -> list[float]:
    """Win-rates of adequately-populated zones, in zone order."""
    return [agg[z][1] / agg[z][0] for z in REPORT_ZONES if agg[z][0] >= MIN_ZONE_N]


def _monotonic(agg) -> bool:
    r = _rates(agg)
    return all(a < b for a, b in zip(r, r[1:]))


def _print_calibration(data, zones, label) -> None:
    agg = _zone_stats(data, zones)
    print(f"--- {label}: n={len(data)}  Brier={_brier(data):.4f} ---")
    print(f"  {'zone':<13}{'n':>5}{'YES_winrate':>13}")
    for z in REPORT_ZONES:
        n, w = agg[z]
        print(f"  {z:<13}{n:>5}{(f'{w / n * 100:.1f}%' if n else '-'):>13}")


def _sweep(data, label) -> None:
    print(f"=== threshold sweep @ {label} (n={len(data)}) ===")
    print(f"  {'candidate':<34}{'monotonic':>10}{'min_n':>7}   win-rate% by zone")
    for name, zones in CANDIDATES.items():
        agg = _zone_stats(data, zones)
        mono = "yes" if _monotonic(agg) else "NO"
        min_n = min(agg[z][0] for z in REPORT_ZONES)
        wr = " ".join(f"{agg[z][1] / agg[z][0] * 100:4.0f}" if agg[z][0] else "   -" for z in REPORT_ZONES)
        print(f"  {name:<34}{mono:>10}{min_n:>7}   {wr}")
    print(f"  (zones: {' '.join(z[:4] for z in REPORT_ZONES)})")


def main() -> None:
    if not DB_PATH.exists():
        print(f"calibration.db not found: {DB_PATH}")
        return
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        print("=== price_zone calibration - current thresholds (0.20/0.40/0.60/0.80) ===\n")
        for col in LEAD_COLS:
            _print_calibration(_load(conn, col), ZoneThresholds(), col)
            print()

        ref = _load(conn, REF_COL)
        _sweep(ref, REF_COL)

        # Recommendation: keep the monotonic, best-populated candidate. The
        # default set is expected to win (it's already clean) — only flag a
        # change if another candidate is monotonic AND better separated.
        ok = {n: z for n, z in CANDIDATES.items() if _monotonic(_zone_stats(ref, z))}
        best = max(ok, key=lambda n: min(_zone_stats(ref, CANDIDATES[n])[z][0] for z in REPORT_ZONES)) if ok else None
        print()
        if best and best.startswith("current"):
            print("RECOMMENDATION: keep current 0.20/0.40/0.60/0.80 - monotonic + "
                  "well-populated on calibration.db (now sourced from config.py).")
        elif best:
            print(f"RECOMMENDATION: consider '{best}' — monotonic + better populated "
                  f"than current at {REF_COL}. Inspect before changing config.py.")
        else:
            print("RECOMMENDATION: no candidate is cleanly monotonic — investigate the data.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
