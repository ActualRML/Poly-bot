"""READ-ONLY autonomous EXIT LEADERBOARD — of every exit family, which (if any)
beats hold-to-resolution OUT OF SAMPLE on the live contrarian ledger?

The user asked: "I have a lot of data now — debug/search it yourself, trial-and-
error, and tell me what's best." This is that search, done HONESTLY so it can't be
fooled by an outlier (the +$650-from-3-trades trap):

  * TRAIN/TEST split (temporal 60/40): pick each family's best params on TRAIN, then
    score that ONE config ONCE on the held-out TEST. Only the OUT-OF-SAMPLE effect
    vs hold counts. A family that's TRAIN-great but TEST-negative is overfit — say so.
  * HONEST SELL FILL (hit the bid + walk depth; empty/one-sided bid => no_exit, can't
    sell) reused from `probe_lastmin_exit` so the cost model can't drift.
  * Families: hold, static-SL, time-gated-SL, trailing-stop, take-profit,
    entry-relative-stop, and 'rose-then-STALLED' (the user's idea).
  * Then a SEGMENT SCAN of the hold ledger (symbol / entry-band) with single-trade
    DOMINANCE so an edge that's really one lucky trade is flagged.

METHOD (project canon): TOUCH-ONLY path via `book` events; held-side value = price
if YES else 1-price; outcome = live ledger sign (API-clean). NOT a bot change —
data/bot.db mode=ro, stdlib + the YES-book reflector, console only, never writes.
One ~2-day macro-regime still applies: train/test guards PARAMETER overfit, NOT
regime overfit. Forward-running is the only real 2nd-regime test.

Run:  .venv/Scripts/python.exe research/probe_exit_search.py
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
                         rt=rt, ev=ev, symbol=t["symbol"]))
    return ctxs


# --- exit policies: take a ctx, return exit event index or None (hold) -------
def pol_hold(ctx):
    return None


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
        mx = max(mx, v)
        if v <= mx - delta:
            return i
    return None


def pol_tp(ctx, th):
    if ctx["entry"] >= th:
        return None
    for i, (ts, v, yb) in enumerate(ctx["ev"]):
        if v >= th:
            return i
    return None


def pol_entryrel(ctx, delta):
    for i, (ts, v, yb) in enumerate(ctx["ev"]):
        if v <= ctx["entry"] - delta:
            return i
    return None


def pol_stall(ctx, V, band, T):
    """Rose to V (from a lower entry) then stayed in [V+/-band] for >= T min -> sell
    at the confirmation point. A break-out up or down => not a stall (hold)."""
    if ctx["entry"] >= V:
        return None
    ev = ctx["ev"]
    i0 = next((i for i, (ts, v, yb) in enumerate(ev) if v >= V), None)
    if i0 is None:
        return None
    t0 = ev[i0][0]
    for j in range(i0, len(ev)):
        ts, v, _ = ev[j]
        if v < V - band or v > V + band:
            return None
        if (ts - t0).total_seconds() >= T * 60:
            return j
    return None


def grid(**kw):
    keys = list(kw)
    return [dict(zip(keys, vals)) for vals in itertools.product(*[kw[k] for k in keys])]


FAMILIES = [
    ("static-SL", pol_static, grid(th=[0.05, 0.07, 0.10, 0.12, 0.15])),
    ("timegated-SL", pol_timegated, grid(th=[0.07, 0.10, 0.12], W=[1, 2, 3, 5])),
    ("trailing-stop", pol_trailing, grid(delta=[0.05, 0.10, 0.15, 0.20])),
    ("take-profit", pol_tp, grid(th=[0.50, 0.60, 0.70, 0.80, 0.90])),
    ("entry-rel-stop", pol_entryrel, grid(delta=[0.03, 0.05, 0.07])),
    ("rose-then-stall", pol_stall, grid(V=[0.40, 0.50, 0.60, 0.70], band=[0.05], T=[3, 5])),
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
    net = 0.0
    ex = noex = wk = 0
    for ctx in ctxs:
        pnl, flag = realize(ctx, fn(ctx, **params))
        net += pnl
        if flag == "exit":
            ex += 1
            wk += ctx["won"]
        elif flag == "no_exit":
            noex += 1
    return dict(net=net, exits=ex, no_exit=noex, win_killed=wk)


def segment_scan(ctxs):
    print("\n" + "=" * 92)
    print("SEGMENT SCAN of the HOLD ledger — where does the profit live, and is it one lucky trade?")
    print("=" * 92)
    def show(title, keyfn):
        groups = {}
        for c in ctxs:
            groups.setdefault(keyfn(c), []).append(c)
        print(f"\n  by {title}:")
        print(f"    {'group':>12} {'n':>4} {'wins':>5} {'WR':>6} {'hold_net':>10} {'top1_share':>10}")
        for k in sorted(groups, key=lambda g: -sum(c["hold"] for c in groups[g])):
            g = groups[k]
            net = sum(c["hold"] for c in g)
            wins = sum(c["won"] for c in g)
            pos = [c["hold"] for c in g if c["hold"] > 0]
            top1 = (max(pos) / sum(pos)) if pos else 0.0
            flag = "  <-1-trade!" if top1 > 0.40 and net > 0 else ""
            print(f"    {str(k):>12} {len(g):>4} {wins:>5} {wins/len(g):>6.1%} "
                  f"{net:>+10,.0f} {top1:>9.0%}{flag}")
    show("symbol", lambda c: c["symbol"])
    def band(e):
        for lo, hi in ((0, .12), (.12, .15), (.15, .18), (.18, .22), (.22, .30), (.30, 1.0)):
            if lo <= e < hi:
                return f"{lo:.2f}-{hi:.2f}"
        return ">=1"
    show("entry band", lambda c: band(c["entry"]))


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
        print(f"EXIT LEADERBOARD  (TRAIN/TEST anti-overfit, honest sell fills)  strategy={STRATEGY}")
        print("=" * 92)
        print(f"trades={len(ctxs)}  train={len(train)} (hold {h_tr:+,.0f})  "
              f"test={len(test)} (hold {h_te:+,.0f})")
        print(f"\n  {'family':>16} {'best-train params':>26} {'train_eff':>10} {'TEST_eff':>9} "
              f"{'exits':>6} {'no_exit':>8} {'win_kill':>9}")
        rows = []
        for name, fn, gp in FAMILIES:
            btr = max(gp, key=lambda p: run(train, fn, p)["net"])
            rtr = run(train, fn, btr)
            rte = run(test, fn, btr)
            eff_tr, eff_te = rtr["net"] - h_tr, rte["net"] - h_te
            ps = ",".join(f"{kk}={vv}" for kk, vv in btr.items())
            rows.append((name, ps, eff_tr, eff_te, rte))
        for name, ps, eff_tr, eff_te, rte in sorted(rows, key=lambda r: -r[3]):
            print(f"  {name:>16} {ps:>26} {eff_tr:>+10,.0f} {eff_te:>+9,.0f} "
                  f"{rte['exits']:>6} {rte['no_exit']:>8} {rte['win_killed']:>9}")
        print(f"\n  TEST baseline (hold) = {h_te:+,.0f}   <- any family must beat 0 OUT OF SAMPLE")
        best = max(rows, key=lambda r: r[3])
        verdict = "BEATS hold OOS" if best[3] > 0 else "NOTHING beats hold OOS"
        print("=" * 92)
        print(f"BEST OUT-OF-SAMPLE: '{best[0]}' ({best[1]}) -> TEST effect {best[3]:+,.0f}  =>  {verdict}")
        if best[3] > 0:
            print(f"  (exits={best[4]['exits']}, winners killed={best[4]['win_killed']}, "
                  f"no_exit={best[4]['no_exit']}; check it's not 1-2 trades before believing it.)")
        print("  Caveat: one macro-regime; train/test guards param-overfit, not regime-overfit.")
        print("=" * 92)
        segment_scan(ctxs)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
