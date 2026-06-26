"""READ-ONLY spread-structure probe -- feasibility check for the maker pivot.

The directional search is exhausted (no taker edge). The one edge-class that is both
UNTESTED and offline-measurable from our data is top-of-book SPREAD: `best_bid` /
`best_ask` are stored on `book` events. This probe characterizes that spread to answer
ONE question before any maker strategy is built:

    Is there a structurally WIDE, STABLE spread pocket a maker could harvest,
    or is the book uniformly ~1c (=> maker margin marginal too)?

It does NOT predict direction, place orders, or backtest a strategy -- it measures the
book's spread distribution overall and by symbol / price_zone / time-to-resolution, plus
a deliberately crude maker half-spread vs adverse-selection sketch.

HONESTY CAVEAT: we store only TOP-OF-BOOK price, never depth/size. So a wide spread from a
genuinely-thin two-sided market is indistinguishable here from a near-empty book with two
placeholder quotes. Very-wide spreads (>~10c) are almost certainly empty-book noise, NOT a
harvestable pocket; the real signal to watch is MODERATE widening (~2-5c) that concentrates
in a stable segment.

Read-only: opens the DB `mode=ro` and never writes it (mirrors analyze_trades.py). Run:

    .venv/Scripts/python.exe research/probe_spread_structure.py
"""
import argparse
import logging
import math
import sqlite3
import statistics
import sys
from datetime import datetime
from pathlib import Path

# make `import src.*` resolve regardless of cwd (mirrors analyze_trades.py)
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.backtest.diagnostics import to_csv  # noqa: E402
from src.backtest.recovery import recover_resolutions  # noqa: E402

# time-to-resolution buckets (label, lo_secs_inclusive, hi_secs_exclusive), far -> near.
# No 120s floor here (that is a trade gate, not a data gate): book ticks right up to :00.
_TTR_BUCKETS = (
    (">30m", 30 * 60, math.inf),
    ("15-30m", 15 * 60, 30 * 60),
    ("5-15m", 5 * 60, 15 * 60),
    ("2-5m", 2 * 60, 5 * 60),
    ("0-2m", 0, 2 * 60),
)
_TTR_ORDER = [b[0] for b in _TTR_BUCKETS]

# spread-magnitude histogram edges in cents (the last bin is open-ended).
_HIST_EDGES = [0.0, 1.0, 2.0, 3.0, 5.0, 10.0, math.inf]

# adverse-selection proxy horizon: only compare consecutive book updates this close
# in time, so |mid move| tracks a short maker horizon (not multi-minute drift).
MAX_ADV_GAP_S = 15.0


def _ttr_bucket(secs: float) -> str | None:
    for label, lo, hi in _TTR_BUCKETS:
        if lo <= secs < hi:
            return label
    return None


def _pct(vals: list[float], p: float) -> float:
    """p-th percentile (0..100) via stdlib quantiles (inclusive)."""
    if not vals:
        return float("nan")
    if len(vals) == 1:
        return float(vals[0])
    qs = statistics.quantiles(vals, n=100, method="inclusive")
    return qs[min(max(int(round(p)) - 1, 0), len(qs) - 1)]


def _stats(spreads_c: list[float]) -> dict | None:
    if not spreads_c:
        return None
    return {
        "n": len(spreads_c),
        "median": statistics.median(spreads_c),
        "mean": statistics.fmean(spreads_c),
        "p25": _pct(spreads_c, 25),
        "p75": _pct(spreads_c, 75),
        "p90": _pct(spreads_c, 90),
        "p95": _pct(spreads_c, 95),
        "min": min(spreads_c),
        "max": max(spreads_c),
    }


def _histogram(spreads_c: list[float]) -> list[tuple[str, int, float]]:
    n = len(spreads_c)
    out = []
    for lo, hi in zip(_HIST_EDGES, _HIST_EDGES[1:]):
        label = f">{lo:.0f}c" if hi == math.inf else f"{lo:.0f}-{hi:.0f}c"
        cnt = sum(1 for s in spreads_c if lo < s <= hi) if lo > 0 else \
            sum(1 for s in spreads_c if lo <= s <= hi)
        out.append((label, cnt, cnt / n * 100 if n else 0.0))
    return out


_COUNT_SQL = {
    "total_poly": "SELECT COUNT(*) FROM snapshots WHERE source='polymarket'",
    "book_total": "SELECT COUNT(*) FROM snapshots WHERE source='polymarket' AND event_type='book'",
    "two_sided": ("SELECT COUNT(*) FROM snapshots WHERE source='polymarket' AND event_type='book' "
                  "AND best_bid IS NOT NULL AND best_ask IS NOT NULL"),
    "both_null": ("SELECT COUNT(*) FROM snapshots WHERE source='polymarket' AND event_type='book' "
                  "AND best_bid IS NULL AND best_ask IS NULL"),
}

_STREAM_SQL = """
    SELECT ts, symbol, market_id, asset_id, best_bid, best_ask, price_zone
      FROM snapshots
     WHERE source='polymarket' AND event_type='book'
       AND best_bid IS NOT NULL AND best_ask IS NOT NULL
     ORDER BY asset_id, ts
"""


def _md_stats_row(key: str, s: dict | None, min_rows: int) -> str:
    if s is None:
        return f"| {key} | 0 | | | | | |"
    flag = "" if s["n"] >= min_rows else " [thin]"
    return (f"| {key}{flag} | {s['n']} | {s['median']:.2f} | {s['p75']:.2f} | {s['p90']:.2f} "
            f"| {s['min']:.2f} | {s['max']:.2f} |")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python research/probe_spread_structure.py",
                                     description=__doc__)
    parser.add_argument("--db", type=Path, default=REPO_ROOT / "data" / "bot.db")
    parser.add_argument("--min-rows", type=int, default=50,
                        help="flag segments thinner than this (default: 50)")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "research" / "diagnostics")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not args.db.exists():
        raise SystemExit(f"snapshot DB not found: {args.db}")

    resolutions = recover_resolutions(args.db).usable

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        counts = {k: conn.execute(q).fetchone()[0] for k, q in _COUNT_SQL.items()}

        overall: list[float] = []
        half_spreads: list[float] = []
        adverse: list[float] = []
        adv_gaps: list[float] = []
        by_symbol: dict[str, list[float]] = {}
        by_zone: dict[str, list[float]] = {}
        by_ttr: dict[str, list[float]] = {}
        crossed = no_resolve = post_resolve = 0
        markets_seen: set[str] = set()
        prev_mid = prev_asset = prev_ts = None

        for r in conn.execute(_STREAM_SQL):
            spread = r["best_ask"] - r["best_bid"]
            if spread <= 0:                       # crossed/locked book -> impossible/garbage
                crossed += 1
                continue
            res = resolutions.get(r["market_id"])
            if res is None:                       # market never decisively resolved
                no_resolve += 1
                continue
            now_ts = datetime.fromisoformat(r["ts"])
            secs = (res.resolve_ts - now_ts).total_seconds()
            if secs < 0:                          # post-settlement dead-token lingering book
                post_resolve += 1
                continue

            spread_c = spread * 100.0
            mid = (r["best_bid"] + r["best_ask"]) / 2.0
            overall.append(spread_c)
            half_spreads.append(spread_c / 2.0)
            by_symbol.setdefault(r["symbol"] or "(none)", []).append(spread_c)
            by_zone.setdefault(r["price_zone"] or "(none)", []).append(spread_c)
            bucket = _ttr_bucket(secs)
            if bucket:
                by_ttr.setdefault(bucket, []).append(spread_c)
            # adverse proxy: |mid move| to the next book update of the SAME TOKEN (asset_id,
            # not market_id -- a market has two tokens with separate books), only when they
            # are <= MAX_ADV_GAP_S apart (a short maker horizon, not long drift).
            if r["asset_id"] is not None and r["asset_id"] == prev_asset and prev_mid is not None:
                gap = (now_ts - prev_ts).total_seconds()
                if 0 < gap <= MAX_ADV_GAP_S:
                    adverse.append(abs(mid - prev_mid) * 100.0)
                    adv_gaps.append(gap)
            prev_mid, prev_asset, prev_ts = mid, r["asset_id"], now_ts
            markets_seen.add(r["market_id"])
    finally:
        conn.close()

    # ---- assemble report ---------------------------------------------------
    L: list[str] = ["# Spread-structure probe", ""]
    L.append(f"- data: {args.db}")
    L.append(f"- usable spread rows: {len(overall)} over {len(markets_seen)} markets")
    L.append("")

    L += ["## 1. Data sufficiency", "",
          f"- total polymarket snapshots: {counts['total_poly']}",
          f"- book events: {counts['book_total']} "
          f"(two-sided {counts['two_sided']}, both-null {counts['both_null']}, "
          f"one-sided {counts['book_total'] - counts['two_sided'] - counts['both_null']})",
          f"- dropped: crossed/locked {crossed}, no-resolve market {no_resolve}, "
          f"post-resolution {post_resolve}",
          ""]

    ov = _stats(overall)
    L += ["## 2. Overall spread distribution (cents)", ""]
    if ov:
        L += ["| n | median | mean | p25 | p75 | p90 | p95 | max |",
              "|--:|--:|--:|--:|--:|--:|--:|--:|",
              f"| {ov['n']} | {ov['median']:.2f} | {ov['mean']:.2f} | {ov['p25']:.2f} "
              f"| {ov['p75']:.2f} | {ov['p90']:.2f} | {ov['p95']:.2f} | {ov['max']:.2f} |", ""]
        L += ["**Magnitude histogram** (>10c almost certainly empty-book, not harvestable):", "",
              "| bin | rows | % |", "|---|--:|--:|"]
        for label, cnt, pct in _histogram(overall):
            L.append(f"| {label} | {cnt} | {pct:.1f} |")
        L.append("")
    else:
        L += ["_(no usable spread rows)_", ""]

    def _seg_table(title: str, d: dict[str, list[float]], order=None) -> None:
        keys = order or sorted(d, key=lambda k: -(_stats(d[k]) or {"median": 0})["median"])
        L.append(f"### By {title}")
        L.append("")
        L.extend(["| key | n | median | p75 | p90 | min | max |", "|---|--:|--:|--:|--:|--:|--:|"])
        for k in keys:
            if k in d:
                L.append(_md_stats_row(k, _stats(d[k]), args.min_rows))
        L.append("")

    L += ["## 3. Spread by segment (cents)", ""]
    _seg_table("symbol", by_symbol)
    _seg_table("price_zone", by_zone)
    _seg_table("time_to_resolve", by_ttr, order=_TTR_ORDER)

    # ---- maker feasibility sketch -----------------------------------------
    med_half = statistics.median(half_spreads) if half_spreads else float("nan")
    med_adv = statistics.median(adverse) if adverse else float("nan")
    med_gap = statistics.median(adv_gaps) if adv_gaps else float("nan")
    L += ["## 4. Maker feasibility sketch (CRUDE -- not a backtest)", "",
          f"- median half-spread (gross capture if filled at touch, no adverse move): "
          f"**{med_half:.2f}c**",
          f"- median |mid move| to the next book update within {MAX_ADV_GAP_S:.0f}s "
          f"(adverse-selection proxy): **{med_adv:.2f}c**  (n={len(adverse)}, median gap {med_gap:.1f}s)",
          "- No queue/fill model and no depth data -- an upper-bound-ish read only.",
          ""]
    capture_ok = (not math.isnan(med_half) and not math.isnan(med_adv) and med_half > med_adv)
    L.append(f"- gross capture {'>' if capture_ok else '<='} adverse proxy -> "
             f"maker capture looks {'plausible' if capture_ok else 'eaten by adverse selection'} "
             f"at the median.")
    L.append("")

    # ---- verdict -----------------------------------------------------------
    gmed = ov["median"] if ov else float("nan")
    seg_stats = {f"{scope}:{k}": _stats(v)
                 for scope, d in (("symbol", by_symbol), ("zone", by_zone), ("ttr", by_ttr))
                 for k, v in d.items()}
    wide = {k: s for k, s in seg_stats.items()
            if s and s["n"] >= args.min_rows and not math.isnan(gmed) and s["median"] >= 2 * gmed}
    L += ["## 5. Verdict -- is there a harvestable spread pocket?", ""]
    if wide:
        L.append(f"**MAYBE.** {len(wide)} adequately-sampled segment(s) show median spread "
                 f">= 2x the global median ({gmed:.2f}c):")
        for k, s in sorted(wide.items(), key=lambda kv: -kv[1]["median"]):
            L.append(f"  - {k}: median {s['median']:.2f}c (n={s['n']})")
        L.append("")
        L.append("Next: confirm these are real two-sided markets (needs depth capture) and build a "
                 "maker-aware harness to test capture vs adverse selection properly.")
    else:
        L.append(f"**NO structurally-wide stable pocket.** No adequately-sampled (>= {args.min_rows}) "
                 f"segment has a median spread >= 2x the global median ({gmed:.2f}c). The book is "
                 f"uniformly tight, so the maker margin is marginal too -- consistent with the "
                 f"efficient-market through-line. Re-run as more regimes accumulate.")
    L.append("")

    markdown = "\n".join(L)

    # ---- CSV (per-segment spread stats) -----------------------------------
    csv_header = ["scope", "key", "n", "median_c", "mean_c", "p25_c", "p75_c", "p90_c",
                  "p95_c", "min_c", "max_c"]
    csv_rows = []
    for scope, d in (("overall", {"all": overall}), ("symbol", by_symbol),
                     ("price_zone", by_zone), ("time_to_resolve", by_ttr)):
        for k, v in d.items():
            s = _stats(v)
            if s:
                csv_rows.append([scope, k, s["n"], round(s["median"], 3), round(s["mean"], 3),
                                 round(s["p25"], 3), round(s["p75"], 3), round(s["p90"], 3),
                                 round(s["p95"], 3), round(s["min"], 3), round(s["max"], 3)])

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "spread_structure.md").write_text(markdown + "\n", encoding="utf-8")
    (args.out / "spread_structure.csv").write_text(to_csv(csv_header, csv_rows), encoding="utf-8")

    print()
    print(markdown)
    print(f"wrote: {args.out / 'spread_structure.md'}")
    print(f"wrote: {args.out / 'spread_structure.csv'}")


if __name__ == "__main__":
    main()
