"""READ-ONLY rigor stack for the ONE candidate the search surfaced:
   'reached 0.40 but the climb entry->0.40 took > 10 min (weak momentum) -> SELL at 0.40'.

The momentum-exit OOS test made this the only rule positive in BOTH train and test.
Before believing it, run the SAME gauntlet the time-gated SL passed, so a single
lucky trade or a fold accident can't masquerade as edge:
  * whole-sample effect vs hold + sold-subset WR (must be well below 0.40)
  * 5-fold temporal CV (how many folds positive?)
  * reversed split
  * DOMINANCE — top trade's share of the positive effect (>40% from one trade = fragile)
  * FILL-STRESS — knock 1-3c off every sell; does it stay positive?
  * hour spread of the acted trades
CAVEAT this can't fix: the rule was SELECTED partly on this data, and it's ONE
~2-day regime. CV here measures stability, not a clean 2nd regime — only forward
paper running can. So the ceiling on the verdict is 'promising candidate', never
'proven'.

data/bot.db mode=ro, never writes.  Run:
  .venv/Scripts/python.exe research/probe_slowrise_validate.py
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
V = 0.40           # the surfaced level
SLOW_MIN = 10      # climb entry->V slower than this => weak momentum => sell


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


def sell_idx(ctx):
    """The rule's sell index, or None (hold). Sell at the first reach of V iff the
    climb entry->V was slower than SLOW_MIN minutes."""
    if ctx["entry"] >= V:
        return None
    ev = ctx["ev"]
    i0 = next((i for i, (ts, v, yb) in enumerate(ev) if v >= V), None)
    if i0 is None:
        return None
    if (ev[i0][0] - ctx["ts_open"]).total_seconds() / 60.0 > SLOW_MIN:
        return i0
    return None


def realize(ctx, idx, cents=0.0):
    """PnL under the rule; `cents` knocks that much off the per-share sell price."""
    if idx is None:
        return ctx["hold"], "hold"
    _, _, yb = ctx["ev"][idx]
    proceeds, sold, top = sell_fill(ctx["side"], ctx["shares"], yb)
    if top is None:
        return ctx["hold"], "no_exit"
    proceeds = max(0.0, proceeds - cents * sold)         # fill-stress
    settle = 1.0 if ctx["won"] else 0.0
    return proceeds + (ctx["shares"] - sold) * settle - ctx["stake"], "exit"


def effect(ctxs, cents=0.0):
    return sum(realize(c, sell_idx(c), cents)[0] - c["hold"] for c in ctxs)


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        ctxs = load_ctxs(conn)
        acted = [c for c in ctxs if sell_idx(c) is not None]
        n = len(ctxs)
        print("=" * 88)
        print(f"SLOW-RISE RULE VALIDATION — sell at {V:.2f} if climb entry->{V:.2f} took > {SLOW_MIN} min")
        print(f"  strategy={STRATEGY}  trades={n}  rule acts on {len(acted)}")
        print("=" * 88)

        # sold-subset WR + win/loss split
        w = sum(c["won"] for c in acted)
        wr = w / len(acted) if acted else 0.0
        print(f"\n  sold-subset: n={len(acted)}  WR={wr:.1%}  (must be << {V:.0%} for the rule to add value)")
        deltas = [(realize(c, sell_idx(c))[0] - c["hold"], c) for c in acted]
        tot = sum(d for d, _ in deltas)
        salvaged = sum(d for d, c in deltas if not c["won"])     # losers we sold early
        killed = sum(d for d, c in deltas if c["won"])           # winners we capped (negative)
        print(f"  total effect vs hold = {tot:+,.0f}   (loser salvage {salvaged:+,.0f}, "
              f"winner cap {killed:+,.0f}; {sum(1 for d,c in deltas if c['won'])} winners capped)")

        # dominance
        pos = sorted((d for d, _ in deltas if d > 0), reverse=True)
        sp = sum(pos)
        if sp > 0:
            top1, top3 = pos[0] / sp, sum(pos[:3]) / sp
            print(f"  dominance: top-1 trade = {top1:.0%} of positive effect, top-3 = {top3:.0%}"
                  f"  {'<-- fragile (1 trade)' if top1 > 0.40 else '(broad)'}")

        # 5-fold temporal CV
        folds = 5
        sz = n // folds
        print("\n  5-fold temporal CV (effect vs hold per fold):")
        npos = 0
        for i in range(folds):
            seg = ctxs[i * sz:(i + 1) * sz] if i < folds - 1 else ctxs[i * sz:]
            e = effect(seg)
            npos += e > 0
            print(f"    fold {i + 1}: n={len(seg):>3}  effect {e:>+8,.0f}")
        print(f"    => {npos}/{folds} folds positive")

        # reversed split (last 60% as 'train', first 40% as 'test')
        k = int(n * 0.40)
        print(f"\n  reversed split: first-40% effect {effect(ctxs[:k]):+,.0f}  |  "
              f"last-60% effect {effect(ctxs[k:]):+,.0f}")

        # fill-stress
        print("\n  fill-stress (knock cents off every sell):")
        for c in (0.0, 0.01, 0.02, 0.03):
            print(f"    -{c*100:>2.0f}c: effect {effect(ctxs, c):+,.0f}")

        # hour spread
        hrs = {}
        for c in acted:
            hrs.setdefault(c["rt"].strftime("%m-%d %HZ"), 0)
            hrs[c["rt"].strftime("%m-%d %HZ")] += 1
        print(f"\n  acted trades span {len(hrs)} distinct resolve-hours "
              f"(max {max(hrs.values()) if hrs else 0} in one hour)")

        print("\n" + "=" * 88)
        print("READ: believe it only if WR<<0.40, effect stays + under fill-stress AND across folds,")
        print("and it's NOT one trade. Even then it's a CANDIDATE for a forward PAPER canary (the 2nd")
        print("regime), never live capital — same bar the time-gated SL had to clear.")
        print("=" * 88)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
