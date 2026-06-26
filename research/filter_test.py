"""PRE-REGISTERED Filter Test — ARM A (persist), the deterministic/backtestable arm.

Locked 2026-06-25 (see the pre-registration). Filter is an OVERLAY on the UNCHANGED
contrarian entry engine: it may only SKIP a candidate, never create one. Hypothesis:
the persist overlay raises EV net-of-spread ABOVE plain contrarian.

LOCKED CRITERIA (do NOT edit after seeing results — that is goal-post shift, not a gate):
  * Rule    : persist > TIER1 (primary) ; TIER2 reported secondary. Tiers = FROZEN
              2026-06-21 values, fit IN-SAMPLE (<=06-21); this scorer GATES on the
              OUT-OF-SAMPLE forward window only (opened >= FORWARD_FROM).
  * Fill    : realistic ask-and-walk, NO mid-fill. The forward window is ENTIRELY era-2,
              whose ledger entry_price IS the effective taker fill (ask + depth walk) — so
              the forward gate is realistic-fill BY CONSTRUCTION. `--engine` adds the raw
              backtest reconstruction (uniform fill, NO live-gate selection) as the gap-#1
              (selection-favorability) cross-check; it LAGS the data edge (recovery excludes
              in-flight markets), so it fills in for the forward window only as markets age.
  * Pass    : an arm passes iff  margin >= BUFFER (+8pp)  AND  N>=100.  margin =
              realized_WR - mean(entry_price); break-even WR for a 1/0 payout bought at
              effective price p IS p, so margin>0 == EV>0 and +8pp == clearly-above-the-line.
  * Sample  : 100 trades/arm minimum. Below 100 => NO CONCLUSION.
  * Stopping: evaluate ONCE at the locked sample. No peek-and-extend, no added market/
              param/horizon after seeing results.

Run (rebuild the indexed copy first so the forward window is current):
    python scripts/make_bt_db.py && .venv/Scripts/python.exe research/filter_test.py [--engine]
"""
import argparse
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import sqlite3

from src.backtest.engine import _build_outcome_map, run_backtest

DB = REPO / "data" / "bot_bt.db"
EPS = 1e-6
# --- LOCKED 2026-06-25 — DO NOT EDIT (see module docstring) ---
TIER1, TIER2 = -0.169, -0.100      # frozen 2026-06-21 persist tiers
FORWARD_FROM = "2026-06-22"        # OOS gate window = opened >= this
BUFFER_PP = 0.08                   # +8pp margin above break-even
MIN_N = 100
# --------------------------------------------------------------


def _persist(conn, yes, market_id, opened_iso, side):
    """mean held-side-oriented depth imbalance over the 12m pre-entry window,
    IDENTICAL to probe_persist_forward.py. None if the window is too thin/short."""
    d = 1.0 if side == "YES" else -1.0
    lo = (datetime.fromisoformat(opened_iso) - timedelta(minutes=12)).isoformat()
    rows = conn.execute(
        "SELECT asset_id,bid_depth,ask_depth,ts FROM snapshots WHERE market_id=? "
        "AND event_type='book' AND bid_depth IS NOT NULL AND ask_depth IS NOT NULL "
        "AND ts<? AND ts>=? ORDER BY ts", (market_id, opened_iso, lo)).fetchall()
    vals = []
    for r in rows:
        tot = r["bid_depth"] + r["ask_depth"]
        if tot > 0:
            di = (r["bid_depth"] - r["ask_depth"]) / tot
            vals.append((di if r["asset_id"] in yes else -di) * d)
    ok = len(vals) >= 2 and (
        datetime.fromisoformat(rows[-1]["ts"]) - datetime.fromisoformat(rows[0]["ts"])
    ).total_seconds() >= 240
    return statistics.fmean(vals) if ok else None


def _arm(trades):
    n = len(trades)
    if n == 0:
        return None
    wins = sum(1 for t in trades if t["won"])
    wr = wins / n
    be = statistics.fmean(t["entry"] for t in trades)         # break-even WR == mean eff fill
    rois = [t["pnl"] / t["size"] for t in trades if t["size"]]
    return {"n": n, "wr": wr, "be": be, "margin": wr - be,
            "ev_roi": statistics.fmean(rois) if rois else 0.0,
            "ev_usd": statistics.fmean(t["pnl"] for t in trades)}


def _show(label, a, base=None):
    if a is None:
        print(f"  {label:<20} (0 trades)")
        return
    d = (f"   Δmargin={a['margin'] - base['margin']:+.3f}  ΔEV/trade={a['ev_usd'] - base['ev_usd']:+.3f}"
         if base is not None else "")
    print(f"  {label:<20} N={a['n']:<4} rawWR={a['wr']*100:4.1f}%  break-even={a['be']*100:4.1f}%  "
          f"margin={a['margin']*100:+5.1f}pp  EV/trade={a['ev_usd']:+.3f} (roi {a['ev_roi']:+.3f}){d}")


def _verdict(label, a):
    have = a["n"] if a else 0
    if have < MIN_N:
        print(f"  {label}: N={have} < {MIN_N} -> NO CONCLUSION (accruing; need {MIN_N - have} more)")
    else:
        print(f"  {label}: N={a['n']}  margin={a['margin']*100:+.1f}pp  vs break-even+{BUFFER_PP*100:.0f}pp  "
              f"-> {'PASS' if a['margin'] >= BUFFER_PP else 'FAIL'}")


def _ledger_arms(conn, yes):
    rows = conn.execute(
        "SELECT ts,market_id,symbol,side,entry_price,size_usdc,pnl_usdc FROM positions "
        "WHERE strategy='contrarian' AND status='resolved' AND pnl_usdc IS NOT NULL ORDER BY ts").fetchall()
    trades = []
    for r in rows:
        p = _persist(conn, yes, r["market_id"], r["ts"], r["side"])
        trades.append({"ts": r["ts"], "won": r["pnl_usdc"] > 0, "entry": float(r["entry_price"]),
                       "size": float(r["size_usdc"]), "pnl": float(r["pnl_usdc"]), "persist": p})
    nowin = sum(1 for t in trades if t["persist"] is None)
    print(f"[LEDGER · realistic fill] {len(trades)} contrarian resolved; {nowin} lack a persist window\n")
    for label, lo, hi, gate in (("OUT-OF-SAMPLE  [THE GATE]", FORWARD_FROM, "9999", True),
                                ("in-sample (tiers fit here — reference, NOT the gate)", "0000", FORWARD_FROM, False)):
        grp = [t for t in trades if lo <= t["ts"][:10] < hi]
        base = _arm(grp)
        t1 = _arm([t for t in grp if t["persist"] is not None and t["persist"] > TIER1])
        t2 = _arm([t for t in grp if t["persist"] is not None and t["persist"] > TIER2])
        print(f"--- {label}  (total {len(grp)}) ---")
        _show("Baseline (control)", base)
        _show("Filter A · Tier1", t1, base)
        _show("Filter A · Tier2", t2, base)
        if gate:
            print()
            _verdict("Tier1 (PRIMARY)", t1)
            _verdict("Tier2 (secondary)", t2)
        print()


def _engine_arms(conn, yes):
    print("[ENGINE · raw reconstruction — gap-#1 cross-check; LAGS the data edge]")
    print("  replaying (uniform realistic fill, no live-gate selection)...")
    settled = [p for p in run_backtest(DB, ["contrarian"], slippages=(0.0,))[0].settled
               if p.strategy == "contrarian" and p.won is not None]
    trades = []
    for p in settled:
        pers = _persist(conn, yes, p.market_id, p.opened_ts.isoformat(), p.side)
        trades.append({"ts": p.opened_ts.isoformat(), "won": p.won, "entry": p.entry_price,
                       "size": p.size_usdc, "pnl": p.pnl_usdc, "persist": pers})
    mx = max((t["ts"][:10] for t in trades), default="-")
    print(f"  engine opened {len(trades)} trades; latest recoverable open = {mx} "
          f"(forward gate fills in as markets age past the edge)\n")
    for label, lo, hi in (("OOS (recoverable so far)", FORWARD_FROM, "9999"), ("in-sample", "0000", FORWARD_FROM)):
        grp = [t for t in trades if lo <= t["ts"][:10] < hi]
        base = _arm(grp)
        print(f"--- ENGINE · {label}  (total {len(grp)}) ---")
        _show("Baseline (control)", base)
        _show("Filter A · Tier1", _arm([t for t in grp if t["persist"] is not None and t["persist"] > TIER1]), base)
        _show("Filter A · Tier2", _arm([t for t in grp if t["persist"] is not None and t["persist"] > TIER2]), base)
        print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", action="store_true", help="also run the raw-engine gap-#1 cross-check (slow)")
    args = ap.parse_args()
    if not DB.exists():
        sys.exit(f"{DB} not found — run: python scripts/make_bt_db.py")
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yes = {a for a, o in _build_outcome_map(conn).items() if o == "YES"}
        print("=" * 100)
        print("PRE-REGISTERED FILTER TEST — ARM A (persist) — gate = OOS forward >= " + FORWARD_FROM)
        print(f"  rule: persist>{TIER1} (Tier1, PRIMARY) | persist>{TIER2} (Tier2, sec)   "
              f"pass: margin>=+{BUFFER_PP*100:.0f}pp & N>={MIN_N}")
        print("=" * 100)
        _ledger_arms(conn, yes)
        if args.engine:
            _engine_arms(conn, yes)
    finally:
        conn.close()
    print("-" * 100)
    print("LOCKED: do not edit tiers/buffer/window. Re-run after accrual: make_bt_db.py then this.")


if __name__ == "__main__":
    main()
