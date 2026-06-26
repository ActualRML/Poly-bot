"""STRUCTURAL-CORNER calibration sweep -- price vs actual win rate, counting only.

PREMISE (FINDINGS, ground-truth labels): every PREDICTIVE signal is dead.
Contrarian -- a STRUCTURAL corner bet (longshot zone), found via the honest-label
ledger -- is the only live candidate. This sweep asks ONE question with the SAME
plain method that vindicated contrarian: are there OTHER structural corners in
the data we already have? Implied (mean price) vs actual (truth labels), cell by
cell. NO correlations, NO residual regressions, NO return-conditioning.

PRE-REGISTERED CORNER FAMILIES (all enumerated, all computed, all reported --
no cherry-picking; the full table ships even where nothing fires):
  F1 PRICE-EXTREME x TIME   bands {<0.10, 0.10-0.15, 0.15-0.20, 0.80-0.85,
                            0.85-0.90, >0.90} x anchors T-45/T-30/T-15/T-5.
  F2 SYMBOL x EXTREME       same bands x BTC/ETH/SOL/XRP/DOGE/BNB (anchors
                            pooled), plus zone-touch frequency per symbol.
  F3 SPREAD-STATE x EXTREME post-restart book rows only: spread {<=1c, 2-3c,
                            >=4c} at the anchor x {cheap <0.20, rich >=0.80}.
  F4 ONE-SIDED-BOOK         post-restart: book one-sided vs two-sided at the
                            anchor x {cheap, rich} (does an empty side leave
                            the surviving quote stale-rich/stale-cheap?).
  F5 TIME-OF-DAY            resolve hour UTC 6h-buckets x {cheap, rich}.
  F6 VOL-REGIME x EXTREME   stored vol_regime at the anchor x {cheap, rich}
                            (contrarian only fires in low_vol BY RULE -- is the
                            mispricing better where it never looks?).

METHOD (canon, reused -- not reimplemented):
  * LABELS: src.backtest.recovery.recover_resolutions -- the touch-only
    decisive rule (book/last_trade_price only, NEVER price_change) + the
    in-flight data-edge guard (fix 2026-06-12). Optional --labels CSV swaps in
    API ground truth (truth-only; anchors stay on stream resolve_ts -- FINDINGS:
    do NOT swap anchors to API end times).
  * NULL: research/audit_checks.py clustered machinery -- one outcome draw per
    MARKET, within-resolve-hour Gaussian copula at a latent rho calibrated to
    the observed within-hour z-correlation (the 6 coins share resolve hours).
    Per-cell two-sided MC p, FAMILY-global max-stat p, and ONE sweep-wide
    max-stat p across all six families (the honest cost of asking 6 questions).
  * Anchor prices are TOUCH rows only (same hygiene as the labels; the stored
    price is the YES-perspective bid-basis price -- the basis the ledger and
    the zone classifier actually used). Spread/one-sidedness from any book row
    at the anchor: spread and "one side empty" are token-side invariant
    (YES_ask = 1 - NO_bid, so width and emptiness mirror across sides).

PER CELL: n obs (one per market x anchor), distinct markets, distinct resolve
HOURS, implied%, actual%, gap (pt), hour-clustered MC p, max single-hour
dominance share, [thin] if hours < 8.

GATES for CANDIDATE_FOR_PREREGISTRATION (explicitly NOT an edge -- the next
step would be its own forward test, exactly like contrarian's):
  cell p < 0.05  AND  family p < 0.05  AND  sweep p < 0.05
  AND dominance <= 40%  AND hours >= 8.
Expected count: ZERO.

READ-ONLY: opens data/bot.db mode=ro; writes ONLY
research/diagnostics/corner_sweep.{md,csv}. Stdlib only. ASCII console.

    python research/sweep_corners.py            # full sweep (default 3000 MC iters)
    python research/sweep_corners.py --quick    # 800 iters
"""
import argparse
import csv
import sqlite3
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from statistics import fmean

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# Canon label rule: touch-only decisive price + in-flight guard live HERE --
# reused, not replicated (CLAUDE.md working rule).
from src.backtest.recovery import recover_resolutions  # noqa: E402

# Clustered-copula null machinery -- reused from the audit, not reimplemented.
# (research/ is sys.path[0] when this script runs, so a plain import works.)
from audit_checks import (  # noqa: E402
    calibrate_rho,
    cell_meta,
    clustered_mc,
    market_table,
    rho_z_from,
)

DB = REPO_ROOT / "data" / "bot.db"
OUT_DIR = REPO_ROOT / "research" / "diagnostics"
OUT_MD = OUT_DIR / "corner_sweep.md"
OUT_CSV = OUT_DIR / "corner_sweep.csv"

TOUCH = ("book", "last_trade_price")
ANCHORS = [("T-45", 45), ("T-30", 30), ("T-15", 15), ("T-5", 5)]   # minutes
RESTART_ISO = "2026-06-09T06:00:00+00:00"   # depth-capture restart (FINDINGS)
ZONE_TOUCH_MIN_TTR_MIN = 15                 # "touched the corner with time left"
THIN_HOURS = 8
DOMINANCE_CAP = 0.40
SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"]

# Bands are half-open [lo, hi); ">0.90" means p >= 0.90 (mirror of "<0.10").
BANDS = [("<0.10", 0.0, 0.10), ("0.10-0.15", 0.10, 0.15), ("0.15-0.20", 0.15, 0.20),
         ("0.80-0.85", 0.80, 0.85), ("0.85-0.90", 0.85, 0.90), (">0.90", 0.90, 1.01)]
CHEAP_HI, RICH_LO = 0.20, 0.80
TOD_BUCKETS = ["00-06", "06-12", "12-18", "18-24"]
VOLS = ["low_vol", "mid_vol", "high_vol"]
SPREADS = ["<=1c", "2-3c", ">=4c"]


def _band(p):
    for label, lo, hi in BANDS:
        if lo <= p < hi:
            return label
    return None


def _extreme(p):
    if p < CHEAP_HI:
        return "cheap"
    if p >= RICH_LO:
        return "rich"
    return None


def _spread_bucket(spread):
    if spread <= 0.015:
        return "<=1c"
    if spread < 0.035:
        return "2-3c"
    return ">=4c"


def _pct(v):
    return "n/a" if v is None else f"{v * 100:5.1f}"


def _pfmt(v):
    return "  n/a" if v is None else f"{v:.3f}"


# --------------------------------------------------------------------------- #
# data assembly: one streaming pass, slot-per-(market, anchor)
# --------------------------------------------------------------------------- #
def load_usable(db_path, labels_csv):
    """Canon recovery -> {mid: {resolve_ts, outcome(0/1), hour}}. Optional API
    ground-truth override (truth-only: markets without a truth row are dropped;
    anchors KEEP the stream resolve_ts -- never the administrative API end time)."""
    res = recover_resolutions(db_path)
    usable = {
        mid: {"resolve_ts": r.resolve_ts, "outcome": 1 if r.outcome == "YES" else 0,
              "hour": r.resolve_ts.isoformat()}
        for mid, r in res.usable.items()
    }
    note = (f"labels: canon touch-only recovery (+in-flight guard): "
            f"usable={len(usable)} excluded={len(res.excluded)}")
    if labels_csv is not None:
        truth = {}
        with open(labels_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                mid = (row.get("market_id") or "").strip()
                t = (row.get("truth") or "").strip().upper()
                if mid and t in ("YES", "NO"):
                    truth[mid] = 1 if t == "YES" else 0
        before = len(usable)
        usable = {mid: dict(info, outcome=truth[mid])
                  for mid, info in usable.items() if mid in truth}
        note += (f"; --labels override: {len(usable)} kept of {before} "
                 f"(truth-only; stream anchors retained)")
    return usable, note


def build_obs(conn, usable, staleness_sec):
    """One pass over polymarket rows. For each (market, anchor) keep the latest
    TOUCH row at-or-before the anchor (price basis) and the latest BOOK row
    (spread / one-sidedness), both within the staleness window. Also tallies the
    majority symbol per market and the zone-touch flags (price in a corner band
    with >= ZONE_TOUCH_MIN_TTR_MIN minutes still to run).

    All hot-loop time comparisons are ISO-string compares (one stored format,
    UTC) -- no datetime parsing per row."""
    plan = {}      # mid -> list of (anchor_label, lo_iso, hi_iso)
    flag_iso = {}  # mid -> ttr>=15m cutoff iso
    for mid, info in usable.items():
        rts = info["resolve_ts"]
        plan[mid] = [
            (label, (rts - timedelta(minutes=mins, seconds=staleness_sec)).isoformat(),
             (rts - timedelta(minutes=mins)).isoformat())
            for label, mins in ANCHORS
        ]
        flag_iso[mid] = (rts - timedelta(minutes=ZONE_TOUCH_MIN_TTR_MIN)).isoformat()

    price_slot = {}   # (mid, anchor) -> [ts_iso, price, vol_regime]
    book_slot = {}    # (mid, anchor) -> [ts_iso, best_bid, best_ask]
    sym_tally = defaultdict(lambda: defaultdict(int))
    touch_flags = defaultdict(lambda: [False, False])   # mid -> [cheap, rich]

    cur = conn.execute(
        "SELECT ts, market_id, symbol, event_type, price, best_bid, best_ask, "
        "vol_regime FROM snapshots "
        "WHERE source='polymarket' AND market_id IS NOT NULL"
    )
    for ts, mid, sym, etype, price, bid, ask, vol in cur:
        anchors = plan.get(mid)
        if anchors is None:
            continue
        if sym:
            sym_tally[mid][sym] += 1
        is_touch = etype in TOUCH
        if is_touch and price is not None and ts <= flag_iso[mid]:
            if price < CHEAP_HI:
                touch_flags[mid][0] = True
            elif price >= RICH_LO:
                touch_flags[mid][1] = True
        for label, lo, hi in anchors:
            if not (lo <= ts <= hi):
                continue
            if is_touch and price is not None:
                key = (mid, label)
                slot = price_slot.get(key)
                if slot is None or ts > slot[0]:
                    price_slot[key] = [ts, price, vol]
            if etype == "book":
                key = (mid, label)
                slot = book_slot.get(key)
                if slot is None or ts > slot[0]:
                    book_slot[key] = [ts, bid, ask]

    market_symbol = {mid: max(t, key=t.get) for mid, t in sym_tally.items()}

    obs = []
    for (mid, anchor), (ts, price, vol) in price_slot.items():
        p = min(1.0, max(0.0, float(price)))
        band = _band(p)
        if band is None:                       # mid-range price: not a corner
            continue
        info = usable[mid]
        spread_b = onesided = None
        bs = book_slot.get((mid, anchor))
        if bs is not None and bs[0] >= RESTART_ISO:      # post-restart book only
            bid, ask = bs[1], bs[2]
            if bid is not None and ask is not None:
                spread_b = _spread_bucket(float(ask) - float(bid))
                onesided = "two-sided"
            elif (bid is None) != (ask is None):
                onesided = "one-sided"
        hr_utc = info["resolve_ts"].hour
        obs.append({
            "market_id": mid,
            "symbol": market_symbol.get(mid, "(none)"),
            "anchor": anchor,
            "price": p,
            "outcome": info["outcome"],
            "hour": info["hour"],
            "band": band,
            "extreme": _extreme(p),
            "spread": spread_b,
            "onesided": onesided,
            "vol": vol if vol in VOLS else None,
            "tod": TOD_BUCKETS[hr_utc // 6],
        })
    return obs, market_symbol, touch_flags


# --------------------------------------------------------------------------- #
# cell construction (every pre-registered cell enumerated, even when empty)
# --------------------------------------------------------------------------- #
def build_families(obs):
    """-> [(family_name, {cell_name: idx_list}), ...] with EVERY pre-registered
    cell present (idx possibly empty)."""
    f1 = {f"{b}|{a}": [] for b, _, _ in BANDS for a, _ in ANCHORS}
    f2 = {f"{s}|{b}": [] for s in SYMBOLS for b, _, _ in BANDS}
    f3 = {f"{sp}|{e}": [] for sp in SPREADS for e in ("cheap", "rich")}
    f4 = {f"{o}|{e}": [] for o in ("one-sided", "two-sided") for e in ("cheap", "rich")}
    f5 = {f"{t}|{e}": [] for t in TOD_BUCKETS for e in ("cheap", "rich")}
    f6 = {f"{v}|{e}": [] for v in VOLS for e in ("cheap", "rich")}
    for i, o in enumerate(obs):
        b, e = o["band"], o["extreme"]
        f1[f"{b}|{o['anchor']}"].append(i)
        if o["symbol"] in SYMBOLS:
            f2[f"{o['symbol']}|{b}"].append(i)
        if e is not None:
            if o["spread"] is not None:
                f3[f"{o['spread']}|{e}"].append(i)
            if o["onesided"] is not None:
                f4[f"{o['onesided']}|{e}"].append(i)
            f5[f"{o['tod']}|{e}"].append(i)
            if o["vol"] is not None:
                f6[f"{o['vol']}|{e}"].append(i)
    return [
        ("F1 price-extreme x time", f1),
        ("F2 symbol x extreme", f2),
        ("F3 spread-state x extreme (post-restart)", f3),
        ("F4 one-sided book x extreme (post-restart)", f4),
        ("F5 time-of-day (UTC) x extreme", f5),
        ("F6 vol-regime x extreme", f6),
    ]


def cell_stats(obs, idx):
    n, nm, nh = cell_meta(obs, idx)
    if n == 0:
        return {"n": 0, "mkts": 0, "hours": 0, "implied": None, "actual": None,
                "gap_pt": None, "dom": None, "thin": True}
    implied = fmean(obs[i]["price"] for i in idx)
    actual = fmean(obs[i]["outcome"] for i in idx)
    per_hour = defaultdict(int)
    for i in idx:
        per_hour[obs[i]["hour"]] += 1
    dom = max(per_hour.values()) / n
    return {"n": n, "mkts": nm, "hours": nh, "implied": implied, "actual": actual,
            "gap_pt": (actual - implied) * 100.0, "dom": dom, "thin": nh < THIN_HOURS}


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(prog="python research/sweep_corners.py",
                                 description=__doc__)
    ap.add_argument("--db", type=Path, default=DB)
    ap.add_argument("--iters", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=20260612)
    ap.add_argument("--staleness", type=float, default=120.0,
                    help="max anchor-join staleness in seconds (touch rows only; "
                         "wider than audit_checks' 30s any-event joins)")
    ap.add_argument("--labels", type=Path, default=None,
                    help="optional label_truth.csv (API ground truth, truth-only "
                         "override; anchors stay on stream resolve_ts)")
    ap.add_argument("--quick", action="store_true", help="iters=800")
    args = ap.parse_args()
    iters = 800 if args.quick else args.iters

    if not args.db.exists():
        print(f"DB not found: {args.db}")
        return

    usable, label_note = load_usable(args.db, args.labels)
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        obs, market_symbol, touch_flags = build_obs(conn, usable, args.staleness)
    finally:
        conn.close()

    lines = []                                    # mirrored to console + md

    def say(s=""):
        print(s)
        lines.append(s)

    say(f"corner sweep: db={args.db}  iters={iters}  seed={args.seed}  "
        f"staleness={args.staleness:.0f}s")
    say(label_note)
    say(f"corner obs (market x anchor, price in a corner band): {len(obs)}  "
        f"markets={len({o['market_id'] for o in obs})}  "
        f"resolve-hours={len({o['hour'] for o in obs})}")

    if len(obs) < 20:
        say("too few corner observations -- nothing to sweep yet.")
        return

    # ---- clustered null: one rho for the whole sweep (same market set) ----
    mk = market_table(obs)
    rho_z = rho_z_from([(m["hour"], m["p"], obs[m["idx"][0]]["outcome"])
                        for m in mk.values()])
    rho = calibrate_rho(mk, rho_z, args.seed)
    say(f"hour-clustered null: observed within-hour z-corr rho_z={rho_z:+.3f} "
        f"-> latent copula rho={rho:.2f} (audit_checks machinery, one outcome "
        f"draw per market)")

    families = build_families(obs)

    # MC per family (its own max-stat global) + one sweep-wide run over all cells.
    results = {}          # family -> (stats per cell, p_cell, p_family)
    sweep_cells = {}
    for fi, (fname, cells) in enumerate(families):
        mc_cells = {name: {"kind": "gap", "idx": idx}
                    for name, idx in cells.items() if idx}
        if mc_cells:
            _, p_cell, p_family = clustered_mc(obs, mk, mc_cells, rho, iters,
                                               args.seed + 100 + fi)
        else:
            p_cell, p_family = {}, None
        results[fname] = (cells, p_cell, p_family)
        for name, idx in cells.items():
            if idx:
                sweep_cells[f"{fname[:2]}:{name}"] = {"kind": "gap", "idx": idx}
    _, _, p_sweep = clustered_mc(obs, mk, sweep_cells, rho, iters, args.seed + 999)

    # ---- report ----
    csv_rows = []
    candidates = []
    for fname, (cells, p_cell, p_family) in results.items():
        say()
        say(f"== {fname} ==  family-global max-stat p = {_pfmt(p_family)}")
        say(f"  {'cell':<22} {'n':>5} {'mkts':>5} {'hours':>5} {'impl%':>6} "
            f"{'act%':>6} {'gap(pt)':>8} {'p_cell':>7} {'dom%':>5}  flags")
        for name in cells:
            st = cell_stats(obs, cells[name])
            pc = p_cell.get(name)
            flags = []
            if st["n"] == 0:
                say(f"  {name:<22} {'0':>5} {'-':>5} {'-':>5} {'-':>6} {'-':>6} "
                    f"{'-':>8} {'-':>7} {'-':>5}  [no obs]")
                csv_rows.append([fname, name, 0, 0, 0, "", "", "", "", "", "",
                                 "no_obs", "", ""])
                continue
            if st["thin"]:
                flags.append("[thin]")
            clears = (pc is not None and pc < 0.05
                      and p_family is not None and p_family < 0.05
                      and p_sweep is not None and p_sweep < 0.05
                      and st["dom"] <= DOMINANCE_CAP and not st["thin"])
            if clears:
                flags.append("CANDIDATE_FOR_PREREGISTRATION")
                candidates.append((fname, name, st, pc))
            gp = st["gap_pt"]
            say(f"  {name:<22} {st['n']:>5} {st['mkts']:>5} {st['hours']:>5} "
                f"{_pct(st['implied']):>6} {_pct(st['actual']):>6} "
                f"{gp:>+8.1f} {_pfmt(pc):>7} {st['dom'] * 100:>5.0f}  "
                f"{' '.join(flags)}")
            csv_rows.append([
                fname, name, st["n"], st["mkts"], st["hours"],
                f"{st['implied']:.4f}", f"{st['actual']:.4f}", f"{gp:.2f}",
                "" if pc is None else f"{pc:.4f}",
                "" if p_family is None else f"{p_family:.4f}",
                "" if p_sweep is None else f"{p_sweep:.4f}",
                "thin" if st["thin"] else "ok", f"{st['dom']:.3f}",
                "CANDIDATE_FOR_PREREGISTRATION" if clears else "",
            ])

    # F2 extra: zone-touch frequency per symbol (descriptive; no p).
    say()
    say("== F2 extra: corner-touch frequency per symbol (ttr >= "
        f"{ZONE_TOUCH_MIN_TTR_MIN}m; descriptive, no test) ==")
    by_sym = defaultdict(list)
    for mid in usable:
        by_sym[market_symbol.get(mid, "(none)")].append(mid)
    say(f"  {'sym':<6} {'markets':>8} {'touched<0.20':>13} {'touched>=0.80':>14}")
    for s in SYMBOLS + sorted(set(by_sym) - set(SYMBOLS)):
        mids = by_sym.get(s, [])
        if not mids:
            say(f"  {s:<6} {'0':>8} {'-':>13} {'-':>14}")
            continue
        ch = sum(1 for m in mids if touch_flags.get(m, [False, False])[0])
        ri = sum(1 for m in mids if touch_flags.get(m, [False, False])[1])
        say(f"  {s:<6} {len(mids):>8} {ch:>9} ({ch / len(mids) * 100:3.0f}%) "
            f"{ri:>9} ({ri / len(mids) * 100:3.0f}%)")
    say("  (a symbol with corners that exist but are never traded by contrarian")
    say("   = selection gap; a symbol with NO corner touches = corners never form)")

    # ---- verdict + caveats ----
    say()
    say(f"SWEEP-WIDE global max-stat p (all six families, all cells) = {_pfmt(p_sweep)}")
    say()
    if candidates:
        say(f"VERDICT: {len(candidates)} corner(s) clear ALL gates "
            f"(cell p<0.05, family p<0.05, sweep p<0.05, dominance<=40%, hours>="
            f"{THIN_HOURS}):")
        for fname, name, st, pc in candidates:
            say(f"  CANDIDATE_FOR_PREREGISTRATION: {fname} :: {name}  "
                f"gap={st['gap_pt']:+.1f}pt n={st['n']} hours={st['hours']} "
                f"p={pc:.3f} -- NOT an edge; next step = its own forward "
                f"pre-registered test, like contrarian's.")
    else:
        say("VERDICT: 0 corners clear ALL gates (cell p, family p, sweep p, "
            "dominance, not-thin) -- as expected under the efficient-market "
            "through-line. The full table above is the record.")
    say()
    say("CAVEATS (mandatory):")
    say("  * ONE regime-pair of data; every verdict is regime-limited. A corner")
    say("    that fires here still needs its own forward pre-registered test.")
    say("  * Six families asked at once: only the SWEEP-WIDE p pays the full")
    say("    multiple-comparisons tax; per-cell p alone is NOT evidence.")
    say("  * Fill/spread reality is UNMODELED: a 0.08 longshot in a 4c-spread")
    say("    book may be unbuyable at 0.08; stored price is the YES bid basis.")
    say("    Gaps smaller than the spread are not capturable.")
    say("  * F3/F4 use post-restart book data only (young, one regime).")
    say("  * Ceiled markets (stream died before the resolve hour) are included")
    say("    per the canon recovery rule; their anchors shift early.")
    say("  * Counting only, by design: no correlations, residuals, or return-")
    say("    conditioning anywhere in this script.")

    # ---- files ----
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["family", "cell", "n", "markets", "hours", "implied", "actual",
                    "gap_pt", "p_cell", "p_family", "p_sweep", "thin", "dominance",
                    "label"])
        w.writerows(csv_rows)
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("# Structural-corner calibration sweep\n\n```\n")
        f.write("\n".join(lines))
        f.write("\n```\n")
    print(f"\nwrote {OUT_MD}")
    print(f"wrote {OUT_CSV}")
    print("done (DB opened read-only; only the two diagnostics files were written).")


if __name__ == "__main__":
    main()
