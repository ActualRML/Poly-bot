"""READ-ONLY candidate BAKE-OFF: is the 'slow-rise exit' really the best lever, or
does something more SUPERIOR exist? Compare ENTRY filters (skip trades by price —
affects ALL trades, the segment scan hinted entry 0.15-0.18 carries the edge and
>0.22 loses) head-to-head against the slow-rise EXIT, all on ONE honest OOS frame:
pick each family's threshold on a temporal TRAIN split, score ONCE on TEST, rank by
TEST effect vs hold-all.

Also an ORTHOGONALITY check: are the slow-rise-sold trades just high-entry trades in
disguise (then an entry filter subsumes it), or independent?

Effect convention (all comparable to hold-all baseline):
  * EXIT rule: sum(realize - hold) over acted trades.
  * ENTRY filter: skipping a trade contributes 0 instead of its hold pnl, so
    effect = -sum(hold of skipped) — positive iff the skipped band was net-negative.

data/bot.db mode=ro, never writes. One regime caveat stands (params, not regime,
are what train/test guards). Run:
  .venv/Scripts/python.exe research/probe_candidate_bakeoff.py
"""
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
V_SLOW, SLOW_MIN = 0.40, 10
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
        c = dict(side=t["side"], entry=entry, stake=stake, hold=hold, won=hold > 0,
                 shares=shares_of(entry, stake, t["fill_flag"] is None),
                 rt=rt, ev=ev, symbol=t["symbol"], ts_open=_parse(t["ts"]))
        c["slow_idx"] = _slow_idx(c)
        ctxs.append(c)
    return ctxs


def _slow_idx(c):
    if c["entry"] >= V_SLOW:
        return None
    i0 = next((i for i, (ts, v, yb) in enumerate(c["ev"]) if v >= V_SLOW), None)
    if i0 is None:
        return None
    if (c["ev"][i0][0] - c["ts_open"]).total_seconds() / 60.0 > SLOW_MIN:
        return i0
    return None


def realize(c, idx):
    if idx is None:
        return c["hold"]
    _, _, yb = c["ev"][idx]
    proceeds, sold, top = sell_fill(c["side"], c["shares"], yb)
    if top is None:
        return c["hold"]
    settle = 1.0 if c["won"] else 0.0
    return proceeds + (c["shares"] - sold) * settle - c["stake"]


# --- policies: per-trade pnl under the policy (comparable to c["hold"]) -------
def p_hold(c, _):
    return c["hold"]


def p_slowrise(c, _):
    return realize(c, c["slow_idx"])


def p_ceiling(c, X):                 # skip (take nothing) if entry above X
    return c["hold"] if c["entry"] <= X else 0.0


def p_floor(c, X):                   # skip if entry below X
    return c["hold"] if c["entry"] >= X else 0.0


FAMILIES = [
    ("slow-rise EXIT", p_slowrise, [None]),
    ("entry-ceiling", p_ceiling, [0.18, 0.20, 0.22, 0.25, 0.30]),
    ("entry-floor", p_floor, [0.13, 0.15, 0.16, 0.17]),
]


def eff(rows, fn, param):
    return sum(fn(c, param) - c["hold"] for c in rows)


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        ctxs = load_ctxs(conn)
        n = len(ctxs)
        k = int(n * TRAIN_FRAC)
        train, test = ctxs[:k], ctxs[k:]
        h_te = sum(c["hold"] for c in test)
        print("=" * 90)
        print(f"CANDIDATE BAKE-OFF  strategy={STRATEGY}  trades={n}  (effect vs HOLD-ALL, OOS)")
        print(f"  hold-all: total {sum(c['hold'] for c in ctxs):+,.0f}  (test half {h_te:+,.0f})")
        print("=" * 90)
        print(f"  {'candidate':>16} {'best-train param':>16} {'TRAIN_eff':>10} {'TEST_eff':>9} {'note':>22}")
        rows = []
        for name, fn, params in FAMILIES:
            btr = max(params, key=lambda p: eff(train, fn, p))
            etr, ete = eff(train, fn, btr), eff(test, fn, btr)
            rows.append((name, btr, etr, ete))
        for name, btr, etr, ete in sorted(rows, key=lambda r: -r[3]):
            note = "best EXIT lever" if name == "slow-rise EXIT" else ""
            print(f"  {name:>16} {str(btr):>16} {etr:>+10,.0f} {ete:>+9,.0f} {note:>22}")
        print(f"\n  TEST baseline (hold-all) = 0 by definition; any candidate must beat 0 OOS.")
        best = max(rows, key=lambda r: r[3])
        print("=" * 90)
        print(f"MOST SUPERIOR OOS: '{best[0]}' (param {best[1]}) -> TEST effect {best[3]:+,.0f}")
        print("=" * 90)

        # --- combo: does slow-rise ADD on top of the best entry filter? --------
        bc = max((r for r in rows if r[0] == "entry-ceiling"), key=lambda r: r[3])
        Xc = bc[1]
        def p_combo(c, _):
            if c["entry"] > Xc:
                return 0.0                      # entry-filtered out
            return realize(c, c["slow_idx"])    # else apply slow-rise exit
        print(f"\n  COMBO (entry<= {Xc} AND slow-rise exit): "
              f"TRAIN {eff(train, p_combo, None):+,.0f}  TEST {eff(test, p_combo, None):+,.0f}")

        # --- orthogonality: are slow-rise-sold trades just HIGH-entry trades? --
        acted = [c for c in ctxs if c["slow_idx"] is not None]
        print(f"\n  ORTHOGONALITY — entry-price of the {len(acted)} slow-rise-sold trades vs all:")
        def hist(rows):
            b = {"<=0.15": 0, "0.15-0.18": 0, "0.18-0.22": 0, ">0.22": 0}
            for c in rows:
                e = c["entry"]
                b["<=0.15" if e <= 0.15 else "0.15-0.18" if e <= 0.18 else
                  "0.18-0.22" if e <= 0.22 else ">0.22"] += 1
            tot = len(rows) or 1
            return "  ".join(f"{kk}:{vv/tot:.0%}" for kk, vv in b.items())
        print(f"    slow-rise-sold: {hist(acted)}")
        print(f"    all trades    : {hist(ctxs)}")
        print("    (similar distributions => slow-rise is INDEPENDENT of entry price, not a proxy.)")
        print("=" * 90)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
