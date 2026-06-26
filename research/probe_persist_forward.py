"""READ-ONLY: FORWARD-SCORING harness for the `persist` filter — PRE-REGISTERED, FROZEN thresholds.

Pre-registered 2026-06-21 (FINDINGS *Session 2026-06-21 cont.*). DO NOT change the thresholds or the
freeze boundary after seeing forward results — shifting a cutoff post-hoc contaminates the evidence
(that is the whole point of freezing). If these constants are ever edited, the forward test is VOID.

  Tier 1 (broad/robust):  persist > -0.169   (P50 of efficient in-sample — "less adverse than typical")
  Tier 2 (strong):        persist > -0.100   (P75 of efficient in-sample — near the profitable tercile)
  IN-SAMPLE = trades opened <= 2026-06-21 ;  FORWARD = opened >= 2026-06-22 (regime: efficient-era).

NOTE on sign: persist is mostly NEGATIVE (at an extreme the book is heavier AGAINST the fade); HIGH
persist = LESS adverse than usual, NOT net-supportive. persist = mean YES-side depth imbalance over the
12m pre-entry window, oriented to the held/fade side (built exactly as the other depth probes).

Reports per tier: n_trades, n_winners, WR (+95% Wilson CI), edge, avg_roi (+95% bootstrap CI) — because
at small n a +0.50/+0.70 point estimate can have a CI that still spans 0. Re-run loop (as forward trades
accrue): `python scripts/make_bt_db.py` then this. Confirm at 30-50 FORWARD high-persist efficient trades.
Run:  .venv/Scripts/python.exe research/probe_persist_forward.py
"""
import math
import random
import statistics
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DB = REPO / "data" / "bot_bt.db"
EPS = 1e-6
# --- FROZEN 2026-06-21 — DO NOT EDIT (see module docstring) ---
TIER1, TIER2 = -0.169, -0.100
FORWARD_FROM = "2026-06-22"        # forward = opened >= this; in-sample = before
EFFICIENT_FROM = "2026-06-16"
# -------------------------------------------------------------
BOOT = 2000
random.seed(7)


def ep(s):
    return datetime.fromisoformat(s)


def wilson(k, n, z=1.96):
    if n == 0:
        return (None, None)
    ph = k / n
    den = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / den
    h = z / den * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n))
    return (max(0.0, c - h), min(1.0, c + h))


def boot_ci(vals):
    if len(vals) < 2:
        return (None, None)
    rnd = random.Random(7)
    n = len(vals)
    means = sorted(sum(vals[rnd.randrange(n)] for _ in range(n)) / n for _ in range(BOOT))
    return (means[int(0.025 * BOOT)], means[int(0.975 * BOOT)])


def block(g):
    n = len(g)
    if n == 0:
        return None
    w = sum(t["won"] for t in g)
    wr = w / n
    edge = wr - statistics.fmean(t["entry"] for t in g)
    rois = [t["pnl"] / t["size"] for t in g if t["size"]]
    return {"n": n, "w": w, "wr": wr, "wrci": wilson(w, n), "edge": edge,
            "roi": statistics.fmean(rois), "roici": boot_ci(rois)}


def show(label, s):
    if s is None:
        print(f"    {label:<16} (0 trades)")
        return
    lo, hi = s["wrci"]
    rlo, rhi = s["roici"]
    wrci = f"[{lo*100:.0f}-{hi*100:.0f}]" if lo is not None else "[-]"
    rci = f"[{rlo:+.2f},{rhi:+.2f}]" if rlo is not None else "[-]"
    print(f"    {label:<16} n={s['n']:<3} W={s['w']:<3} WR={s['wr']*100:4.1f}% {wrci:<9} "
          f"edge={s['edge']*100:+5.1f}pp  avg_roi={s['roi']:+.3f} {rci}")


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    tally = {}
    for r in conn.execute("SELECT asset_id,price,best_bid,best_ask FROM snapshots WHERE source='polymarket' "
                          "AND event_type='book' AND price IS NOT NULL AND asset_id IS NOT NULL"):
        ref = r["best_bid"] if r["best_bid"] is not None else r["best_ask"]
        if ref is None:
            continue
        yl = abs(r["price"] - ref) < EPS; nl = abs(r["price"] - (1.0 - ref)) < EPS
        if yl == nl:
            continue
        t = tally.setdefault(r["asset_id"], [0, 0]); t[0 if yl else 1] += 1
    yes = {a for a, (y, n) in tally.items() if y > n}

    pos = conn.execute("SELECT ts,market_id,symbol,side,entry_price,size_usdc,pnl_usdc FROM positions "
                       "WHERE strategy='contrarian' AND status='resolved' AND pnl_usdc IS NOT NULL ORDER BY ts").fetchall()
    T = []
    for p in pos:
        if p["ts"][:10] < EFFICIENT_FROM:      # forward test is the efficient regime only
            continue
        d = 1.0 if p["side"] == "YES" else -1.0
        lo = (ep(p["ts"]) - timedelta(minutes=12)).isoformat()
        rows = conn.execute("SELECT asset_id,bid_depth,ask_depth,ts FROM snapshots WHERE market_id=? "
                            "AND event_type='book' AND bid_depth IS NOT NULL AND ask_depth IS NOT NULL "
                            "AND ts<? AND ts>=? ORDER BY ts", (p["market_id"], p["ts"], lo)).fetchall()
        vals = []
        for r in rows:
            tot = r["bid_depth"] + r["ask_depth"]
            if tot > 0:
                dimb = (r["bid_depth"] - r["ask_depth"]) / tot
                vals.append((dimb if r["asset_id"] in yes else -dimb) * d)
        if len(vals) < 2 or (ep(rows[-1]["ts"]) - ep(rows[0]["ts"])).total_seconds() < 240:
            continue
        T.append({"won": 1 if p["pnl_usdc"] > 0 else 0, "entry": float(p["entry_price"]),
                  "size": float(p["size_usdc"]), "pnl": float(p["pnl_usdc"]), "coin": p["symbol"],
                  "period": "forward" if p["ts"][:10] >= FORWARD_FROM else "in-sample",
                  "persist": statistics.fmean(vals)})
    conn.close()

    print("=" * 90)
    print("persist FORWARD-SCORING (FROZEN 2026-06-21) — efficient regime only")
    print(f"  Tier1 persist>{TIER1}  |  Tier2 persist>{TIER2}  |  forward = opened >= {FORWARD_FROM}")
    print("=" * 90)
    for pop, coins in (("EFFICIENT", None), ("BTC/ETH-eff", {"BTC", "ETH"})):
        for period in ("in-sample", "forward"):
            g = [t for t in T if t["period"] == period and (coins is None or t["coin"] in coins)]
            print(f"\n--- {pop}  [{period}]  (total {len(g)}) ---")
            show("ALL", block(g))
            show("Tier1 >-0.169", block([t for t in g if t["persist"] > TIER1]))
            show("Tier2 >-0.100", block([t for t in g if t["persist"] > TIER2]))
            show("below T1", block([t for t in g if t["persist"] <= TIER1]))

    print("\nREAD: confirm = FORWARD Tier1 & Tier2 avg_roi CI clearly > 0 (and WR CI > entry) at n~30-50.")
    print("If forward is empty, the harness is ready — re-run after efficient trades accrue (rebuild bot_bt.db first).")
    print("THRESHOLDS ARE FROZEN: do not edit TIER1/TIER2/FORWARD_FROM after seeing forward results (voids the test).")


if __name__ == "__main__":
    main()
