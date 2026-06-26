"""READ-ONLY robustness battery for the 'late-entry (10-15min) + wide-spread' EV hypothesis (bot.db).

Corrected conventions: YES prob from `price` (normalized); spread = best_ask-best_bid (width, YES/NO
invariant); outcome yes_won from (side, exit_price) [position-relative, verified]. Entry = buy the
CHEAP side at realistic taker fill (cheap_price + half-spread). EV_fill = payout - fill (tradeable);
EV_mid = payout - cheap_price (NO cost = pure calibration / is the PRICE itself mispriced?).
Sections: (1) benchmarks, (2) distribution, (3) liquidity buckets, (4) calibration by prob,
(5) spread-threshold sweep, (6) regime x wide, (7) n+CI everywhere.
"""
import math
import sqlite3
import statistics as st
from datetime import datetime, timedelta

DB = "data/bot.db"
SPLIT = "2026-06-16T00:00:00+00:00"


def pt(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def ci(xs):
    n = len(xs)
    if n == 0:
        return 0, 0.0, 0.0
    m = st.mean(xs)
    return n, m, (2 * st.pstdev(xs) / math.sqrt(n) if n > 1 else 0.0)


def line(name, xs):
    n, m, c = ci(xs)
    print(f"  {name:<26} n={n:<4} mean={m:+.4f} +-{c:.4f}")


def main():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    mk = {}
    for r in c.execute("SELECT market_id,side,exit_price,resolve_time FROM positions "
                       "WHERE exit_price IN (0,1,0.0,1.0) AND resolve_time IS NOT NULL"):
        yw = (r["side"] == "YES" and r["exit_price"] == 1) or (r["side"] == "NO" and r["exit_price"] == 0)
        rt = pt(r["resolve_time"])
        if rt:
            mk[r["market_id"]] = (rt, yw)

    E = []
    for m, (rt, yw) in mk.items():
        lo = (rt - timedelta(minutes=15)).isoformat()
        hi = (rt - timedelta(minutes=10)).isoformat()
        s = c.execute("SELECT price,best_bid,best_ask,bid_depth,ask_depth,bid_size,ask_size FROM snapshots "
                      "WHERE market_id=? AND price IS NOT NULL AND best_bid IS NOT NULL AND best_ask IS NOT NULL "
                      "AND ts>=? AND ts<=? AND event_type IN ('book','last_trade') ORDER BY ts DESC LIMIT 1",
                      (m, lo, hi)).fetchone()
        if not s:
            continue
        yp, b, a = s["price"], s["best_bid"], s["best_ask"]
        if not (0 < yp < 1) or a <= b or (a - b) > 0.5:
            continue
        spread = a - b
        cheap = min(yp, 1 - yp)
        cheap_yes = yp <= 0.5
        cheap_won = yw if cheap_yes else (not yw)
        fav = 1 - cheap
        depth = (s["bid_depth"] or s["bid_size"] or 0) + (s["ask_depth"] or s["ask_size"] or 0)
        E.append({
            "cheap": cheap, "spread": spread, "depth": depth,
            "era": "revert" if rt < pt(SPLIT) else "efficient", "cheap_won": cheap_won,
            "ev_fill_cheap": (1 if cheap_won else 0) - (cheap + spread / 2),
            "ev_mid_cheap": (1 if cheap_won else 0) - cheap,
            "ev_fill_fav": (1 if not cheap_won else 0) - (fav + spread / 2),
        })
    print(f"total entries (10-15min window, w/ book) = {len(E)}")
    if len(E) < 30:
        print("INSUFFICIENT DATA"); return

    print("\n[1] BENCHMARKS (EV/share)")
    line("cheap-side @fill", [e["ev_fill_cheap"] for e in E])
    line("favorite-side @fill", [e["ev_fill_fav"] for e in E])
    line("random side @fill", [(e["ev_fill_cheap"] + e["ev_fill_fav"]) / 2 for e in E])
    line("cheap-side @MID (calib)", [e["ev_mid_cheap"] for e in E])
    print("   (hold/no-trade = 0 by definition; @MID ~0 => price is FAIR, any -EV is just spread cost)")

    print("\n[2] DISTRIBUTION of cheap-side @fill (bounded loss = -fill; tail = wins)")
    xs = sorted(e["ev_fill_cheap"] for e in E)
    q = lambda p: xs[min(len(xs) - 1, int(p * len(xs)))]
    print(f"  n={len(xs)} mean={st.mean(xs):+.4f} median={st.median(xs):+.4f} "
          f"IQR=[{q(.25):+.3f},{q(.75):+.3f}] p90={q(.9):+.3f} max={xs[-1]:+.3f} min={xs[0]:+.3f}")

    print("\n[3] LIQUIDITY buckets (by total depth, terciles)")
    de = sorted(e["depth"] for e in E if e["depth"] > 0)
    if len(de) > 9:
        d1, d2 = de[len(de) // 3], de[2 * len(de) // 3]
        for nm, sel in [("thin", lambda d: 0 < d <= d1), ("medium", lambda d: d1 < d < d2), ("liquid", lambda d: d >= d2)]:
            line(f"{nm} @fill", [e["ev_fill_cheap"] for e in E if sel(e["depth"])])
            line(f"{nm} @MID", [e["ev_mid_cheap"] for e in E if sel(e["depth"])])
        print(f"   (depth terciles: thin<={d1:.0f}, liquid>={d2:.0f})")
    else:
        print("  depth field sparse -> skip")

    print("\n[4] CALIBRATION by entry prob (cheap-side win-rate - price; <0 = overpriced longshot)")
    for lo, hi in [(0, 0.05), (0.05, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.5)]:
        g = [e for e in E if lo <= e["cheap"] < hi]
        if len(g) < 8:
            print(f"  p[{lo:.2f}-{hi:.2f}) n={len(g):<4} (too few)"); continue
        wr = sum(e["cheap_won"] for e in g) / len(g)
        ap = st.mean(e["cheap"] for e in g)
        cic = 2 * math.sqrt(wr * (1 - wr) / len(g))
        print(f"  p[{lo:.2f}-{hi:.2f}) n={len(g):<4} win-rate={wr:.3f} price={ap:.3f} resid={wr-ap:+.3f} +-{cic:.3f}")

    print("\n[5] SPREAD-THRESHOLD sweep (cheap-side @fill)")
    for thr in (0.005, 0.01, 0.02, 0.05):
        line(f"spread>={thr:.3f}", [e["ev_fill_cheap"] for e in E if e["spread"] >= thr])

    print("\n[6] REGIME x WIDE(spread>=0.01), cheap-side @fill + @MID")
    for era in ("revert", "efficient"):
        line(f"{era} wide @fill", [e["ev_fill_cheap"] for e in E if e["spread"] >= 0.01 and e["era"] == era])
        line(f"{era} wide @MID", [e["ev_mid_cheap"] for e in E if e["spread"] >= 0.01 and e["era"] == era])


if __name__ == "__main__":
    main()
