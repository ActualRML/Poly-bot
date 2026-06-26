"""READ-ONLY autonomous STOP-LOSS search with TRAIN/TEST anti-overfit + honest fills.

The "learning machine": tries several principled SL variants, picks each variant's
params on a TRAIN split, then scores that ONE config ONCE on a held-out TEST split.
Honest sell fill (hit the bid + walk depth; empty book = no_exit) reused from
`probe_lastmin_exit` so the cost model can't drift. Reports the best OUT-OF-SAMPLE
result — which may be "hold-to-resolution still wins."

Run:  .venv/Scripts/python.exe research/probe_sl_search.py
"""
import itertools
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "research"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from probe_lastmin_exit import sell_fill, shares_of, infer_outcome  # honest fill, parity shares
from src.execute.fill import yes_book_from_token

DB = REPO / "data" / "bot.db"
TOUCH = ("book", "last_trade_price")
TRAIN_FRAC = 0.60


def _parse(s):
    return datetime.fromisoformat(s)


def book_events(conn, mid, side, lo_iso, hi_iso):
    """[(ts, value, yesbook)] from book events; value is held-side perspective."""
    rows = conn.execute(
        """SELECT ts, price, best_bid, best_ask, bid_size, ask_size, bid_depth, ask_depth
             FROM snapshots WHERE market_id=? AND event_type='book' AND price IS NOT NULL
              AND (best_bid IS NOT NULL OR best_ask IS NOT NULL) AND ts>? AND ts<=? ORDER BY ts""",
        (mid, lo_iso, hi_iso),
    ).fetchall()
    out = []
    for r in rows:
        oc = infer_outcome(r["best_bid"], r["best_ask"], r["price"])
        yb = yes_book_from_token(oc, r["best_bid"], r["best_ask"],
                                 r["bid_size"], r["ask_size"], r["bid_depth"], r["ask_depth"])
        v = r["price"] if side == "YES" else 1.0 - r["price"]
        out.append((_parse(r["ts"]), v, yb))
    return out


def spot_cross_times(conn, symbol, lo_iso, hi_iso):
    rows = conn.execute(
        """SELECT ts, price FROM snapshots WHERE source='binance' AND symbol=? AND price IS NOT NULL
              AND ts>=? AND ts<=? ORDER BY ts""", (symbol, lo_iso, hi_iso)).fetchall()
    if len(rows) < 2:
        return []
    open_px = rows[0]["price"]
    ct = []
    for i in range(1, len(rows)):
        if (rows[i - 1]["price"] - open_px) * (rows[i]["price"] - open_px) < 0:
            ct.append(_parse(rows[i]["ts"]))
    return ct


# --- SL policies: take a trade context, return the exit event index or None (hold) ---
def pol_static(ctx, th):
    for i, (ts, v, yb) in enumerate(ctx["ev"]):
        if v <= th:
            return i
    return None


def pol_timegated(ctx, th, W):
    for i, (ts, v, yb) in enumerate(ctx["ev"]):
        if (ctx["rt"] - ts).total_seconds() <= W * 60 and v <= th:
            return i
    return None


def pol_trailing(ctx, delta):
    mx = -1.0
    for i, (ts, v, yb) in enumerate(ctx["ev"]):
        if v > mx:
            mx = v
        if v <= mx - delta:
            return i
    return None


def pol_entryrel(ctx, delta):
    for i, (ts, v, yb) in enumerate(ctx["ev"]):
        if v <= ctx["entry"] - delta:
            return i
    return None


def pol_regime(ctx, th, maxcross):
    # cut only when value<=th AND spot has been TRENDING so far (few crossings) -> unlikely to revert
    ct = ctx["cross"]
    for i, (ts, v, yb) in enumerate(ctx["ev"]):
        if v <= th and sum(1 for c in ct if c <= ts) <= maxcross:
            return i
    return None


def grid(**kw):
    keys = list(kw)
    return [dict(zip(keys, vals)) for vals in itertools.product(*[kw[k] for k in keys])]


VARIANTS = [
    ("static price", pol_static, grid(th=[0.03, 0.05, 0.07, 0.10, 0.12])),
    ("time-gated", pol_timegated, grid(th=[0.05, 0.07, 0.10], W=[2, 5, 10, 20])),
    ("trailing", pol_trailing, grid(delta=[0.05, 0.10, 0.15, 0.20])),
    ("entry-relative", pol_entryrel, grid(delta=[0.03, 0.05, 0.07])),
    ("regime-gated", pol_regime, grid(th=[0.07, 0.10, 0.12], maxcross=[0, 1, 2])),
]


def realize(ctx, idx):
    if idx is None:
        return ctx["hold"], "hold"
    _, _, yb = ctx["ev"][idx]
    proceeds, sold, top = sell_fill(ctx["side"], ctx["shares"], yb)
    if top is None:
        return ctx["hold"], "no_exit"           # empty bid -> can't sell -> ride to resolution
    settle = 1.0 if ctx["won"] else 0.0
    return proceeds + (ctx["shares"] - sold) * settle - ctx["stake"], "exit"


def run(ctxs, polfn, params):
    net = 0.0
    nex = noex = wk = 0
    for ctx in ctxs:
        pnl, flag = realize(ctx, polfn(ctx, **params))
        net += pnl
        if flag == "exit":
            nex += 1
            wk += ctx["won"]
        elif flag == "no_exit":
            noex += 1
    return dict(net=net, exits=nex, no_exit=noex, win_killed=wk)


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        trades = conn.execute(
            """SELECT market_id, symbol, side, entry_price, size_usdc, pnl_usdc, ts, resolve_time, fill_flag
                 FROM positions WHERE strategy='contrarian' AND status='resolved'
                  AND pnl_usdc IS NOT NULL AND resolve_time IS NOT NULL ORDER BY ts""").fetchall()
        ctxs = []
        for t in trades:
            rt = _parse(t["resolve_time"])
            open_iso = (rt - timedelta(hours=1)).isoformat()
            ev = book_events(conn, t["market_id"], t["side"], t["ts"], rt.isoformat())
            cross = spot_cross_times(conn, t["symbol"], open_iso, rt.isoformat())
            entry, stake = float(t["entry_price"]), float(t["size_usdc"])
            hold = float(t["pnl_usdc"])
            ctxs.append(dict(side=t["side"], entry=entry, stake=stake, hold=hold, won=hold > 0,
                             shares=shares_of(entry, stake, t["fill_flag"] is None),
                             rt=rt, ev=ev, cross=cross, symbol=t["symbol"]))

        k = int(len(ctxs) * TRAIN_FRAC)
        train, test = ctxs[:k], ctxs[k:]
        h_tr = sum(c["hold"] for c in train)
        h_te = sum(c["hold"] for c in test)
        par = sum((c["shares"] - c["stake"]) for c in ctxs if c["won"]) - sum(c["hold"] for c in ctxs if c["won"])
        print("=" * 80)
        print("AUTONOMOUS SL SEARCH  (TRAIN/TEST anti-overfit, honest sell fills)  ROUND 1")
        print("=" * 80)
        print(f"trades={len(ctxs)}  train={len(train)} (hold {h_tr:+,.0f})  test={len(test)} (hold {h_te:+,.0f})")
        print(f"shares parity (winners, all): {par:+.1f} (~0 ok)\n")
        print(f"  {'variant':>15} {'best-train params':>26} {'train_eff':>10} {'TEST_eff':>9} "
              f"{'test_exits':>10} {'no_exit':>8} {'win_kill':>8}")
        best = None
        for name, fn, gridp in VARIANTS:
            btr = max(gridp, key=lambda p: run(train, fn, p)["net"])
            rtr = run(train, fn, btr)
            rte = run(test, fn, btr)
            eff_tr = rtr["net"] - h_tr
            eff_te = rte["net"] - h_te
            ps = ",".join(f"{kk}={vv}" for kk, vv in btr.items())
            print(f"  {name:>15} {ps:>26} {eff_tr:>+10,.0f} {eff_te:>+9,.0f} "
                  f"{rte['exits']:>10} {rte['no_exit']:>8} {rte['win_killed']:>8}")
            if best is None or eff_te > best[1]:
                best = (name, eff_te, ps, rte)
        print(f"\n  TEST baseline (hold) = {h_te:+,.0f}")
        nm, eff, ps, rte = best
        verdict = "BEATS hold OOS" if eff > 0 else "does NOT beat hold OOS"
        print("=" * 80)
        print(f"BEST OUT-OF-SAMPLE: '{nm}' ({ps}) -> TEST effect {eff:+,.0f} vs hold ({verdict});")
        print(f"  test exits={rte['exits']}, no_exit(empty bid)={rte['no_exit']}, winners killed={rte['win_killed']}.")
        print("  Caveat: one macro-regime (OOS-in-window != 2nd regime); throttle overstates exits.")
        print("=" * 80)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
