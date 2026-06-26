"""READ-ONLY: does DEPTH-imbalance DYNAMICS (persistence / acceleration) filter contrarian
trades EX-ANTE, beyond the static depth-proxy and price? (the user's #1/#2 microstructure cut).

PRIOR: STATIC YES-side depth imbalance is the KNOWN price proxy (FINDINGS: corr 0.970 w/ price,
collapses under orthogonalization). The OPEN question (untested): does its TRAJECTORY carry info —
"sudden imbalance = spoof/noise" vs "persistent imbalance = real support"?

dimb = (bid_depth-ask_depth)/(bid_depth+ask_depth), YES-perspective (NO-token rows reflected: -dimb),
oriented to the HELD side (dimb_held = dimb_yes * +1 YES / -1 NO; positive = book SUPPORTS the fade).
Features over the 12m pre-entry window (ex-ante): dimb_now (static control), d_dimb (now - ~5m ago =
ACCELERATION), persist (mean = how consistently supportive), stability (-std = steadiness).

Labels = the contrarian ledger (win/loss). DEEP treatment (CLAUDE.md research rule): regime split
(revert/efficient) + chrono train/test + per-regime PLACEBO + BEST-OF-N + the DECISIVE test:
partial(feature, win | price, dimb_now) — does a DYNAMIC feature predict win BEYOND price AND the
static depth proxy? If it collapses → it's just the static proxy re-expressed.

Uses data/bot_bt.db (indexed; the live ledger + book copied 06-20 — a research screen, not authority).
Run:  .venv/Scripts/python.exe research/probe_depth_dynamics.py
"""
import bisect
import math
import random
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import sqlite3
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DB = REPO / "data" / "bot_bt.db"
REVERT_LAST = "2026-06-15"
WINDOW_MIN = 12
LAG_SEC = 300            # ~5m-ago anchor for acceleration
EPS = 1e-6
random.seed(7)
FEATS = ["dimb_now", "d_dimb", "persist", "stability"]   # dimb_now = static control
DYN = ["d_dimb", "persist", "stability"]                  # the genuinely-new dynamics


def ep(s):
    return datetime.fromisoformat(s)


def pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sx = sum((x - mx) ** 2 for x in xs)
    sy = sum((y - my) ** 2 for y in ys)
    if sx <= 0 or sy <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sx * sy)


def ols_resid2(xs, ps, rs):
    """residuals of xs on [1, ps, rs]; None if degenerate."""
    n = len(xs)
    if n < 3:
        return None
    mx, mp, mr = statistics.fmean(xs), statistics.fmean(ps), statistics.fmean(rs)
    spp = sum((p - mp) ** 2 for p in ps)
    srr = sum((r - mr) ** 2 for r in rs)
    spr = sum((p - mp) * (r - mr) for p, r in zip(ps, rs))
    det = spp * srr - spr * spr
    if spp <= 0 or srr <= 0 or det <= 0:
        return None
    sxp = sum((x - mx) * (p - mp) for x, p in zip(xs, ps))
    sxr = sum((x - mx) * (r - mr) for x, r in zip(xs, rs))
    b = (sxp * srr - sxr * spr) / det
    c = (sxr * spp - sxp * spr) / det
    return [(x - mx) - b * (p - mp) - c * (r - mr) for x, p, r in zip(xs, ps, rs)]


def partial_corr(feat, win, c1, c2):
    rf = ols_resid2(feat, c1, c2)
    rw = ols_resid2(win, c1, c2)
    return pearson(rf, rw) if (rf and rw) else None


def auc(scores, labels):
    pairs = [(s, l) for s, l in zip(scores, labels) if s is not None]
    if not pairs:
        return None
    sc = [s for s, _ in pairs]
    lb = [l for _, l in pairs]
    order = sorted(range(len(sc)), key=lambda i: sc[i])
    ranks = [0.0] * len(sc)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and sc[order[j + 1]] == sc[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2.0 + 1.0
        i = j + 1
    P = sum(lb)
    N = len(lb) - P
    if P == 0 or N == 0:
        return None
    return (sum(ranks[i] for i in range(len(lb)) if lb[i]) - P * (P + 1) / 2.0) / (P * N)


def best_of_n_floor(labels, n_feat, iters=1000):
    maxes = []
    m = len(labels)
    for _ in range(iters):
        best = 0.0
        for _ in range(n_feat):
            a = auc([random.random() for _ in range(m)], labels)
            if a is not None:
                best = max(best, abs(a - 0.5))
        maxes.append(best)
    maxes.sort()
    return maxes[int(0.95 * len(maxes))]


def label_yes_assets(conn):
    tally = {}
    for r in conn.execute("SELECT asset_id, price, best_bid, best_ask FROM snapshots "
                          "WHERE source='polymarket' AND event_type='book' AND price IS NOT NULL "
                          "AND asset_id IS NOT NULL"):
        ref = r["best_bid"] if r["best_bid"] is not None else r["best_ask"]
        if ref is None:
            continue
        yes_like = abs(r["price"] - ref) < EPS
        no_like = abs(r["price"] - (1.0 - ref)) < EPS
        if yes_like == no_like:
            continue
        t = tally.setdefault(r["asset_id"], [0, 0])
        t[0 if yes_like else 1] += 1
    return {a for a, (y, n) in tally.items() if y > n}


def main():
    if not DB.exists():
        raise SystemExit(f"need {DB}")
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    yes = label_yes_assets(conn)
    pos = conn.execute("SELECT ts, market_id, side, entry_price, pnl_usdc FROM positions "
                       "WHERE strategy='contrarian' AND status='resolved' AND pnl_usdc IS NOT NULL "
                       "ORDER BY ts").fetchall()

    trades = []
    for p in pos:
        d = 1.0 if p["side"] == "YES" else -1.0
        lo = (ep(p["ts"]) - timedelta(minutes=WINDOW_MIN)).isoformat()
        rows = conn.execute(
            "SELECT ts, asset_id, bid_depth, ask_depth FROM snapshots WHERE market_id=? "
            "AND event_type='book' AND bid_depth IS NOT NULL AND ask_depth IS NOT NULL "
            "AND ts<? AND ts>=? ORDER BY ts", (p["market_id"], p["ts"], lo)).fetchall()
        series = []  # (epoch, dimb_held)
        for r in rows:
            tot = r["bid_depth"] + r["ask_depth"]
            if tot <= 0:
                continue
            dimb = (r["bid_depth"] - r["ask_depth"]) / tot
            dimb_yes = dimb if r["asset_id"] in yes else -dimb     # reflect NO token
            series.append((ep(r["ts"]).timestamp(), dimb_yes * d))
        if len(series) < 2:
            continue
        span = (series[-1][0] - series[0][0])
        if span < 240:
            continue
        ts_arr = [s[0] for s in series]
        vals = [s[1] for s in series]
        now = vals[-1]
        # acceleration: now - value ~5m before entry (at-or-before the lag anchor)
        anchor = ep(p["ts"]).timestamp() - LAG_SEC
        j = bisect.bisect_right(ts_arr, anchor) - 1
        d_dimb = (now - vals[j]) if j >= 0 else None
        trades.append({
            "ts": ep(p["ts"]).timestamp(), "won": 1 if p["pnl_usdc"] > 0 else 0,
            "price": float(p["entry_price"]),
            "regime": "revert" if p["ts"][:10] <= REVERT_LAST else "efficient",
            "feat": {"dimb_now": now, "d_dimb": d_dimb,
                     "persist": statistics.fmean(vals),
                     "stability": -statistics.pstdev(vals) if len(vals) >= 2 else None}})
    conn.close()
    trades.sort(key=lambda t: t["ts"])
    sub = lambda rg: [t for t in trades if rg is None or t["regime"] == rg]

    print("=" * 94)
    print("DEPTH-IMBALANCE DYNAMICS as ex-ante filter on contrarian — beyond static depth + price?")
    print(f"  computable trades: {len(trades)} (>=2 depth rows, span>=4m). dimb_now=static CONTROL (proxy).")
    print("=" * 94)
    for rg in (None, "revert", "efficient"):
        ts_ = sub(rg)
        labels = [t["won"] for t in ts_]
        if sum(labels) == 0 or sum(labels) == len(labels):
            continue
        bon = best_of_n_floor(labels, len(FEATS))
        print(f"\n--- {rg or 'ALL':<9} n={len(ts_)} W={sum(labels)}   BEST-OF-{len(FEATS)} floor |AUC-.5|={bon:.3f} ---")
        price = [t["price"] for t in ts_]
        dnow = [t["feat"]["dimb_now"] for t in ts_]
        for ft in FEATS:
            fv = [t["feat"][ft] for t in ts_]
            a = auc(fv, labels)
            if a is None:
                print(f"    {ft:<10} AUC=n/a"); continue
            dev = abs(a - 0.5)
            mark = " <<beats best-of-N" if dev > bon else ""
            line = f"    {ft:<10} AUC={a:.3f} |dev|={dev:.3f}{mark}"
            if ft in DYN:  # decisive: does it survive controlling price + static dimb?
                ok = [(v, w, pp, dn) for v, w, pp, dn in zip(fv, labels, price, dnow) if v is not None]
                raw = pearson([x[0] for x in ok], [x[1] for x in ok])
                par = partial_corr([x[0] for x in ok], [float(x[1]) for x in ok],
                                   [x[2] for x in ok], [x[3] for x in ok])
                line += f"   | corr(win)={_f(raw)} -> partial(|price,dimb_now)={_f(par)}"
            print(line)

    print("\n" + "=" * 94)
    print("TRAIN/TEST (chrono 60/40) — dynamics only; does AUC hold OOS?")
    print("=" * 94)
    for rg in ("revert", "efficient"):
        ts_ = sub(rg)
        k = int(len(ts_) * 0.6)
        tr, te = ts_[:k], ts_[k:]
        if sum(t["won"] for t in te) in (0, len(te)) or sum(t["won"] for t in tr) in (0, len(tr)):
            print(f"\n--- {rg}: test/train degenerate (all same class) — too thin ---"); continue
        print(f"\n--- {rg} train n={len(tr)}(W{sum(t['won'] for t in tr)}) test n={len(te)}(W{sum(t['won'] for t in te)}) ---")
        for ft in DYN:
            atr = auc([t["feat"][ft] for t in tr], [t["won"] for t in tr])
            ate = auc([t["feat"][ft] for t in te], [t["won"] for t in te])
            if atr is None or ate is None:
                continue
            held = "HOLDS" if (atr - 0.5) * (ate - 0.5) > 0 and abs(ate - 0.5) > 0.07 else ""
            print(f"    {ft:<10} train={atr:.3f} test={ate:.3f} {held}")

    print("\nREAD: a DYNAMIC feature is a real crack ONLY if AUC beats best-of-N AND partial(|price,dimb_now)")
    print("STAYS material (doesn't collapse toward 0) AND holds train→test. If partial collapses → it's just")
    print("the static depth proxy (= price) re-expressed. Efficient n is thin; read direction, not precision.")


def _f(v, d=3):
    return "n/a" if v is None else f"{v:+.{d}f}"


if __name__ == "__main__":
    main()
