"""READ-ONLY rigorous OOS test of the ONE pattern the hunt surfaced: 'momentum
exhaustion' — a longshot that rose to V but then FAILS to keep rising tends to
revert (win below V). The reached-level hunt showed this consistently IN-SAMPLE
(new-high cells win above V; stall/slow/roll-over win below). Here we ask the only
question that matters: does ANY momentum-exit rule beat hold OUT OF SAMPLE?

Discipline: pick each family's best params on a temporal TRAIN split, score that ONE
config ONCE on the held-out TEST. Honest sell fill (hit bid + walk depth; empty bid
=> no_exit). A real edge is positive in BOTH halves; a TRAIN+/TEST- (or sign-flip)
is overfit/noise — exactly what an efficient 0.5 predicts.

data/bot.db mode=ro, never writes.  Run:
  .venv/Scripts/python.exe research/probe_momentum_exit.py
"""
import itertools
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "research"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from probe_lastmin_exit import infer_outcome, sell_fill, shares_of
from src.execute.fill import yes_book_from_token

DB = REPO / "data" / "bot.db"
STRATEGY = "contrarian"
BAND = 0.05
TRAIN_FRAC = 0.60


def _parse(s):
    return datetime.fromisoformat(s)


def book_events(conn, mid, side, lo_iso, hi_iso):
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


def load_ctxs(conn):
    trades = conn.execute(
        """SELECT market_id, symbol, side, entry_price, size_usdc, pnl_usdc, ts, resolve_time, fill_flag
             FROM positions WHERE strategy=? AND status='resolved'
              AND pnl_usdc IS NOT NULL AND resolve_time IS NOT NULL ORDER BY ts""",
        (STRATEGY,),
    ).fetchall()
    ctxs = []
    for t in trades:
        rt = _parse(t["resolve_time"])
        ev = book_events(conn, t["market_id"], t["side"], t["ts"], rt.isoformat())
        if not ev:
            continue
        entry, stake = float(t["entry_price"]), float(t["size_usdc"])
        hold = float(t["pnl_usdc"])
        ctxs.append(dict(side=t["side"], entry=entry, stake=stake, hold=hold, won=hold > 0,
                         shares=shares_of(entry, stake, t["fill_flag"] is None),
                         rt=rt, ev=ev, symbol=t["symbol"], ts_open=_parse(t["ts"])))
    return ctxs


def _reach(ev, V):
    return next((i for i, (ts, v, yb) in enumerate(ev) if v >= V), None)


def _jafter(ev, i0, obs):
    ts0 = ev[i0][0]
    j = i0
    for k in range(i0, len(ev)):
        if (ev[k][0] - ts0).total_seconds() <= obs * 60:
            j = k
        else:
            break
    return j


def pol_nonewhigh(ctx, V, obs):
    """Reached V; if within obs min it never prints V+band (momentum stalled) -> sell
    at reach+obs. Else hold."""
    if ctx["entry"] >= V:
        return None
    ev = ctx["ev"]
    i0 = _reach(ev, V)
    if i0 is None:
        return None
    ts0 = ev[i0][0]
    win = [v for ts, v, _ in ev[i0:] if (ts - ts0).total_seconds() <= obs * 60]
    if max(win) >= V + BAND:
        return None                      # momentum continued -> hold
    return _jafter(ev, i0, obs)          # stalled -> sell


def pol_brokedown(ctx, V, obs):
    """Reached V; if within obs min it dips to V-band (rolled over) -> sell there."""
    if ctx["entry"] >= V:
        return None
    ev = ctx["ev"]
    i0 = _reach(ev, V)
    if i0 is None:
        return None
    ts0 = ev[i0][0]
    for k in range(i0, len(ev)):
        if (ev[k][0] - ts0).total_seconds() > obs * 60:
            break
        if ev[k][1] <= V - BAND:
            return k
    return None


def pol_slow(ctx, V, mins):
    """Reached V but the climb entry->V took > mins (weak momentum) -> sell at reach."""
    if ctx["entry"] >= V:
        return None
    ev = ctx["ev"]
    i0 = _reach(ev, V)
    if i0 is None:
        return None
    if (ev[i0][0] - ctx["ts_open"]).total_seconds() / 60.0 > mins:
        return i0
    return None


def grid(**kw):
    keys = list(kw)
    return [dict(zip(keys, vals)) for vals in itertools.product(*[kw[k] for k in keys])]


FAMILIES = [
    ("no-new-high", pol_nonewhigh, grid(V=[0.40, 0.50, 0.60], obs=[3, 5, 10])),
    ("broke-down", pol_brokedown, grid(V=[0.40, 0.50, 0.60], obs=[3, 5, 10])),
    ("slow-rise", pol_slow, grid(V=[0.40, 0.50, 0.60], mins=[8, 10, 15])),
]


def realize(ctx, idx):
    if idx is None:
        return ctx["hold"], "hold"
    _, _, yb = ctx["ev"][idx]
    proceeds, sold, top = sell_fill(ctx["side"], ctx["shares"], yb)
    if top is None:
        return ctx["hold"], "no_exit"
    settle = 1.0 if ctx["won"] else 0.0
    return proceeds + (ctx["shares"] - sold) * settle - ctx["stake"], "exit"


def run(ctxs, fn, params):
    net = sold_n = noex = wk = 0.0
    for ctx in ctxs:
        idx = fn(ctx, **params)
        pnl, flag = realize(ctx, idx)
        net += pnl
        if flag == "exit":
            sold_n += 1
            wk += ctx["won"]
        elif flag == "no_exit":
            noex += 1
    return dict(net=net, exits=int(sold_n), no_exit=int(noex), win_killed=int(wk))


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        ctxs = load_ctxs(conn)
        k = int(len(ctxs) * TRAIN_FRAC)
        train, test = ctxs[:k], ctxs[k:]
        h_tr = sum(c["hold"] for c in train)
        h_te = sum(c["hold"] for c in test)
        print("=" * 92)
        print(f"MOMENTUM-EXIT OOS TEST  strategy={STRATEGY}  trades={len(ctxs)}  "
              f"train={len(train)} test={len(test)}")
        print(f"  hold: train {h_tr:+,.0f}  test {h_te:+,.0f}   (a real rule beats hold in BOTH)")
        print("=" * 92)
        print(f"  {'family':>14} {'best-train params':>22} {'TRAIN_eff':>10} {'TEST_eff':>9} "
              f"{'exits':>6} {'no_exit':>8} {'win_kill':>9}")
        results = []
        for name, fn, gp in FAMILIES:
            btr = max(gp, key=lambda p: run(train, fn, p)["net"])
            rtr, rte = run(train, fn, btr), run(test, fn, btr)
            eff_tr, eff_te = rtr["net"] - h_tr, rte["net"] - h_te
            ps = ",".join(f"{kk}={vv}" for kk, vv in btr.items())
            results.append((name, ps, eff_tr, eff_te, rte))
            print(f"  {name:>14} {ps:>22} {eff_tr:>+10,.0f} {eff_te:>+9,.0f} "
                  f"{rte['exits']:>6} {rte['no_exit']:>8} {rte['win_killed']:>9}")
        best = max(results, key=lambda r: r[3])
        both_pos = best[2] > 0 and best[3] > 0
        print("=" * 92)
        if both_pos:
            print(f"SURVIVOR: '{best[0]}' ({best[1]}) -> TRAIN {best[2]:+,.0f} AND TEST {best[3]:+,.0f} "
                  f"both positive. Worth a forward paper canary (NOT live).")
        else:
            print(f"NO SURVIVOR: best OOS '{best[0]}' TEST {best[3]:+,.0f} but TRAIN {best[2]:+,.0f} "
                  f"=> not positive in BOTH halves. The momentum pattern does NOT beat hold")
            print("  out-of-sample -> it's already priced in (efficient at the mid). Don't build.")
        print("=" * 92)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
