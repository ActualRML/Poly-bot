"""READ-ONLY: is the mid/high-vol FADE edge REAL or a flat-slippage backtest mirage?
v2 — debugged: now uses the OFFICIAL recovery (validated touch-only outcome +
resolve_ts) AND applies the live MIN_TIME_TO_RESOLVE_SEC=120 gate (the bot skips
entries in the final 2 min; v1 wrongly included those near-decided losers, which is
why v1 scored even low_vol contrarian at −$1.4k vs the +$8k live truth).

Sanity anchor: with these fixes the low_vol row should move TOWARD the live ledger.
If it does, the probe is trustworthy and the mid/high_vol verdict can be believed.

Fill = LIVE model (simulate_taker_fill: ask + walk captured depth, entry_ceiling cap,
nofill on no quote) — the binding test the flat-slippage backtest skips. Fixed $50
stake. Entry = first `book` event per market in (extreme zone × target vol regime).

Run on the INDEXED copy:  .venv/Scripts/python.exe research/probe_fade_vol_validate.py
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
from probe_lastmin_exit import infer_outcome
from src.backtest.recovery import recover_resolutions
from src.execute.fill import simulate_taker_fill, yes_book_from_token

DB = REPO / "data" / "bot_bt.db"
STAKE = 50.0
FLOOR, CEILING = 0.15, 0.30
MIN_TTR = 120          # live MIN_TIME_TO_RESOLVE_SEC — skip the final 2 min
TRAIN_FRAC = 0.60


def _parse(s):
    return datetime.fromisoformat(s)


def first_entries(conn, vol):
    rows = conn.execute(
        """SELECT market_id m, ts, price p, best_bid bb, best_ask ba,
                  bid_size bs, ask_size a_s, bid_depth bd, ask_depth ad, symbol sym, price_zone z
             FROM snapshots
            WHERE source='polymarket' AND event_type='book' AND vol_regime=?
              AND price_zone IN ('extreme_low','extreme_high') AND price IS NOT NULL
              AND (best_bid IS NOT NULL OR best_ask IS NOT NULL)
            ORDER BY ts""", (vol,)).fetchall()
    seen = {}
    for r in rows:
        seen.setdefault(r["m"], r)
    return seen


def realistic_trade(r, res, gate):
    z, price = r["z"], float(r["p"])
    side = "YES" if z == "extreme_low" else "NO"
    signal = price if side == "YES" else 1.0 - price
    if signal < FLOOR:
        return ("floored", 0.0, None)
    if gate and (res.resolve_ts - _parse(r["ts"])).total_seconds() < MIN_TTR:
        return ("too_late", 0.0, None)
    oc = infer_outcome(r["bb"], r["ba"], price)
    yb = yes_book_from_token(oc, r["bb"], r["ba"], r["bs"], r["a_s"], r["bd"], r["ad"])
    fill = simulate_taker_fill(side, STAKE, yb, max_price=CEILING)
    if fill.flag == "nofill":
        return ("nofill", 0.0, None)
    won = (res.outcome == side)
    payout = fill.filled_usdc / fill.avg_price if won else 0.0
    return ("fill", payout - fill.filled_usdc, won)


def run(conn, usable, vol, label, gate):
    ents = first_entries(conn, vol)
    trades = []
    cnt = {"fill": 0, "nofill": 0, "floored": 0, "too_late": 0, "no_resolution": 0}
    for m, r in ents.items():
        res = usable.get(m)
        if res is None:
            cnt["no_resolution"] += 1
            continue
        flag, pnl, won = realistic_trade(r, res, gate)
        cnt[flag] += 1
        if flag == "fill":
            trades.append((r["ts"], pnl, won, r["sym"]))
    trades.sort()
    n = len(trades)
    print(f"\n=== {label}  (vol={vol}, time-gate={'ON' if gate else 'OFF'}) ===")
    print(f"  candidates={len(ents)}  filled={cnt['fill']}  nofill={cnt['nofill']}  "
          f"floored={cnt['floored']}  too_late={cnt['too_late']}  no_res={cnt['no_resolution']}")
    if n == 0:
        print("  no fills."); return
    tot = sum(p for _, p, _, _ in trades)
    w = sum(1 for _, _, won, _ in trades if won)
    k = int(n * TRAIN_FRAC)
    tr = sum(p for _, p, _, _ in trades[:k]); te = sum(p for _, p, _, _ in trades[k:])
    print(f"  REALISTIC-FILL: n={n}  WR={w/n:.0%}  PnL={tot:+,.0f}  per-trade={tot/n:+.1f}  "
          f"|  train {tr:+,.0f}  TEST {te:+,.0f}")
    from collections import defaultdict
    bs = defaultdict(lambda: [0, 0.0])
    for _, p, _, s in trades:
        bs[s][0] += 1; bs[s][1] += p
    print("  per-symbol: " + "  ".join(f"{s}:{v[1]:+.0f}(n{v[0]})"
          for s, v in sorted(bs.items(), key=lambda x: -x[1][1])))


def main():
    if not DB.exists():
        raise SystemExit(f"need indexed copy: {DB}")
    print("recovering resolutions (official touch-only)...", flush=True)
    rec = recover_resolutions(DB)
    usable = rec.usable
    print(f"  usable resolutions: {len(usable)}")
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True); conn.row_factory = sqlite3.Row
    try:
        print("\n" + "=" * 86)
        print("FADE VALIDATION v2 — realistic fill + official outcomes + live 120s time-gate")
        print("=" * 86)
        # low_vol with gate is the ANCHOR: should approach the +$8k live truth if the probe is right.
        run(conn, usable, "low_vol", "contrarian (ANCHOR vs live +$8k)", gate=True)
        run(conn, usable, "low_vol", "contrarian (gate OFF, for contrast)", gate=False)
        run(conn, usable, "mid_vol", "contrarian_mid", gate=True)
        run(conn, usable, "high_vol", "contrarian_hv", gate=True)
        print("\n" + "=" * 86)
        print("READ: if low_vol+gate is now POSITIVE/near-live, trust the probe -> mid/hv verdict stands.")
        print("If low_vol is STILL negative, the probe is still wrong and the mid/hv numbers are unsafe.")
        print("=" * 86)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
