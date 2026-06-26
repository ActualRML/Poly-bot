"""READ-ONLY: does the `persist` filter convert separation into MONEY? (avg_roi after realistic fill).

Follows the confound tests (persist survived within-coin + logistic + BTC/ETH). AUC = separation;
this asks the money question: high-persist vs low-persist contrarian — avg_roi / WR / edge / net.

KEY: the EFFICIENT regime (>=06-16) is ERA-2 = realistic recorded fills (ask+walk), so its avg_roi from
the live ledger is HONEST. The revert regime (<=06-15) is era-1 MID-FILL INFLATED (FINDINGS) → its
avg_roi is OVERSTATED (flagged). So the decisive cell — EFFICIENT persist tercile — is clean.

avg_roi = mean(pnl_usdc/size_usdc); edge = WR - mean(entry). persist built exactly as the other depth
probes (mean YES-side depth imbalance over 12m pre-entry, oriented to the held/fade side). bot_bt.db.
Run:  .venv/Scripts/python.exe research/probe_depth_persist_pnl.py
"""
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
REVERT_LAST = "2026-06-15"
WINDOW_MIN = 12
EPS = 1e-6


def ep(s):
    return datetime.fromisoformat(s)


def stats(g):
    n = len(g)
    if not n:
        return None
    W = sum(t["won"] for t in g)
    wr = W / n
    avg_entry = statistics.fmean(t["entry"] for t in g)
    avg_roi = statistics.fmean(t["pnl"] / t["size"] for t in g if t["size"])
    net = sum(t["pnl"] for t in g)
    return {"n": n, "W": W, "wr": wr, "edge": wr - avg_entry, "avg_entry": avg_entry,
            "avg_roi": avg_roi, "net": net}


def show(label, s, note=""):
    if s is None:
        print(f"    {label:<14} (none)"); return
    print(f"    {label:<14} n={s['n']:<3} WR={s['wr']*100:4.1f}%  edge={s['edge']*100:+5.1f}pp  "
          f"avg_entry={s['avg_entry']:.3f}  avg_roi={s['avg_roi']:+.3f}  net={s['net']:+,.0f}{note}")


def terciles(trades):
    vals = sorted(t["persist"] for t in trades)
    if len(vals) < 6:
        return None
    q1, q2 = vals[len(vals) // 3], vals[2 * len(vals) // 3]
    bk = {"low": [], "mid": [], "high": []}
    for t in trades:
        bk["low" if t["persist"] <= q1 else ("high" if t["persist"] > q2 else "mid")].append(t)
    return bk


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    tally = {}
    for r in conn.execute("SELECT asset_id, price, best_bid, best_ask FROM snapshots "
                          "WHERE source='polymarket' AND event_type='book' AND price IS NOT NULL AND asset_id IS NOT NULL"):
        ref = r["best_bid"] if r["best_bid"] is not None else r["best_ask"]
        if ref is None:
            continue
        yl = abs(r["price"] - ref) < EPS; nl = abs(r["price"] - (1.0 - ref)) < EPS
        if yl == nl:
            continue
        t = tally.setdefault(r["asset_id"], [0, 0]); t[0 if yl else 1] += 1
    yes = {a for a, (y, n) in tally.items() if y > n}

    pos = conn.execute("SELECT ts, market_id, symbol, side, entry_price, size_usdc, pnl_usdc FROM positions "
                       "WHERE strategy='contrarian' AND status='resolved' AND pnl_usdc IS NOT NULL ORDER BY ts").fetchall()
    T = []
    for p in pos:
        d = 1.0 if p["side"] == "YES" else -1.0
        lo = (ep(p["ts"]) - timedelta(minutes=WINDOW_MIN)).isoformat()
        rows = conn.execute("SELECT asset_id, bid_depth, ask_depth, ts FROM snapshots WHERE market_id=? "
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
                  "regime": "revert" if p["ts"][:10] <= REVERT_LAST else "efficient",
                  "persist": statistics.fmean(vals)})
    conn.close()

    print("=" * 92)
    print(f"persist -> MONEY (avg_roi after realistic fill) — n={len(T)} computable contrarian trades")
    print("  efficient = ERA-2 realistic fills (HONEST) ; revert = era-1 mid-fill INFLATED (flagged)")
    print("=" * 92)

    print("\n--- STEP 1: persist HIGH vs LOW (median split) by regime ---")
    for rg in ("revert", "efficient"):
        g = [t for t in T if t["regime"] == rg]
        med = statistics.median(t["persist"] for t in g)
        note = "   [era-1 INFLATED]" if rg == "revert" else "   [era-2 realistic]"
        print(f"  {rg}{note}:")
        show("LOW persist", stats([t for t in g if t["persist"] <= med]))
        show("HIGH persist", stats([t for t in g if t["persist"] > med]))

    print("\n--- STEP 2: EFFICIENT regime persist TERCILE (the money cell) ---")
    eff = [t for t in T if t["regime"] == "efficient"]
    bk = terciles(eff)
    if bk:
        for b in ("low", "mid", "high"):
            show(b + " persist", stats(bk[b]))
    print("  BTC/ETH-only efficient (cleanest — persist strongest + liquid + era-2):")
    effbe = [t for t in eff if t["coin"] in ("BTC", "ETH")]
    bk2 = terciles(effbe)
    if bk2:
        for b in ("low", "mid", "high"):
            show("  " + b, stats(bk2[b]))

    print("\nREAD (Step 2 is decisive): filter WORKS for money iff LOW-persist efficient avg_roi < 0 AND")
    print("HIGH-persist efficient avg_roi >= ~breakeven. n per tercile ~20-25 (≈5 winners) = THIN → read")
    print("DIRECTION not precision; one window; needs forward paper (30-50 high-persist) to confirm.")


if __name__ == "__main__":
    main()
