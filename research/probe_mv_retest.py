"""READ-ONLY accurate RE-TEST of the contrarian fade family by REGIME.

WHY. "contrarian_mv is dead" rests on (a) the EFFICIENT-regime live canary
(−$1350, n=23) and (b) backtest avg_roi −0.36 — but avg_roi is fill-DEPENDENT and
this engine UNDERSTATES (it scores the known-live-+EV low_vol contrarian negative
too). The OPEN cell is MV in a REVERTING regime on the fill-INDEPENDENT signal.
This probe answers it accurately:

  * faithful triggers — the REAL src.backtest engine (book + price_change +
    last_trade, the load-bearing trigger set), not a book-only proxy;
  * NO cross-strategy contamination — SimPortfolio.is_held is per-MARKET (not
    per-strategy like live), so each strategy is replayed in its OWN pass; running
    LV/MV/HV together would let whoever triggers first STEAL the market;
  * sizing-neutral — fixed $25 stake + huge bank so the never-settle replay
    balance can't deplete and skip late opens (the ~40-trade depletion artifact);
  * TWO metrics per cell — edge = WR − avg_entry (fill-INDEPENDENT signal, the
    canonical +15.5pp metric) AND avg_roi (fill-DEPENDENT reality);
  * REGIME split — revert (opened ≤ 2026-06-15) vs efficient (≥ 06-16), FINDINGS
    canon (low_vol: revert +15.5pp / efficient −5.6pp);
  * LV = CALIBRATION ANCHOR — low_vol contrarian's live edge is KNOWN (+15.5pp
    revert), so its backtest numbers fix the understatement lens; MV/HV are read
    RELATIVE to it, never as absolute backtest truth.

Backtest = NEGATIVE SCREEN: it can fail to kill MV (-> needs a live read) or
confirm it dead; it CANNOT certify it alive. Run:
    .venv/Scripts/python.exe research/probe_mv_retest.py
"""
import math
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from src.backtest.engine import run_backtest          # noqa: E402
from src.backtest.recovery import recover_resolutions  # noqa: E402
import src.strategy.contrarian_mv as _cmv             # noqa: E402

# RESEARCH = all coins. The LIVE canary scopes contrarian_mv to BTC/ETH (SYMBOLS);
# disable that here so the re-test characterizes EVERY coin (the live finding's basis).
_cmv.SYMBOLS = None

DB = REPO / "data" / "bot_bt.db"
STRATS = ["contrarian", "contrarian_mv"]   # LV anchor + MV (HV reconfirmed dead, file removed 06-20)
LABEL = {"contrarian": "LV (low_vol)", "contrarian_mv": "MV (mid_vol)"}
REVERT_LAST = date(2026, 6, 15)   # opened <= this = reverting; >= 06-16 = efficient
FIXED_BET = 25.0
BIG_BANK = 10_000_000.0           # huge so the never-settle replay balance can't deplete


def regime_of(p):
    return "revert" if p.opened_ts.date() <= REVERT_LAST else "efficient"


def _wr_z(wins, n, p_implied):
    """z for WR > implied (H0: WR == avg_entry) — the FINDINGS significance read."""
    if n == 0 or not (0 < p_implied < 1):
        return float("nan")
    se = math.sqrt(p_implied * (1 - p_implied) / n)
    return ((wins / n) - p_implied) / se if se else float("nan")


def summarize(rows):
    rows = [p for p in rows if p.decision_price is not None and p.size_usdc]
    n = len(rows)
    if not n:
        return None
    wins = sum(1 for p in rows if p.won)
    wr = wins / n
    avg_mid = sum(p.decision_price for p in rows) / n     # implied P(win) = price paid (mid)
    avg_fill = sum(p.entry_price for p in rows) / n        # realistic ask+walk effective fill
    avg_roi = sum((p.pnl_usdc or 0.0) / p.size_usdc for p in rows) / n
    net = sum(p.pnl_usdc or 0.0 for p in rows)
    return {"n": n, "wr": wr, "avg_mid": avg_mid, "edge": wr - avg_mid,
            "avg_fill": avg_fill, "fillcost": avg_fill - avg_mid,
            "avg_roi": avg_roi, "net": net, "z": _wr_z(wins, n, avg_mid)}


def _row(label, s):
    if s is None:
        print(f"  {label:<22} (no trades)")
        return
    print(f"  {label:<22} n={s['n']:<4} WR={s['wr']*100:4.1f}%  mid={s['avg_mid']*100:4.1f}%  "
          f"edge={s['edge']*100:+5.1f}pp(z={s['z']:+.1f})  "
          f"fill={s['avg_fill']*100:4.1f}%(+{s['fillcost']*100:.1f})  "
          f"avg_roi={s['avg_roi']:+.3f}  net={s['net']:+,.0f}")


def main():
    if not DB.exists():
        raise SystemExit(f"need indexed copy (run scripts/make_bt_db.py): {DB}")
    print("recovering resolutions once (reused across strategies)...", flush=True)
    rec = recover_resolutions(DB)
    print(f"  usable resolutions: {len(rec.usable)}")

    settled_by = {}
    for strat in STRATS:
        print(f"replaying {strat} (own pass, $25 sizing-neutral)...", flush=True)
        res = run_backtest(DB, [strat], slippages=(0.0,), starting_balance=BIG_BANK,
                           fixed_bet_usdc=FIXED_BET, recovery=rec)
        settled_by[strat] = res[0].settled

    print("\n" + "=" * 104)
    print("CONTRARIAN FADE FAMILY by REGIME — edge=WR-avg_entry (fill-INDEPENDENT) | avg_roi (after fills)")
    print("  revert = opened <= 2026-06-15   |   efficient = opened >= 2026-06-16   (FINDINGS canon)")
    print("=" * 104)
    for strat in STRATS:
        buckets = defaultdict(list)
        for p in settled_by[strat]:
            buckets[regime_of(p)].append(p)
        print(f"\n{LABEL[strat]}  [{strat}]")
        for regime in ("revert", "efficient"):
            _row(regime, summarize(buckets[regime]))
        _row("ALL", summarize(settled_by[strat]))

    lv_rev = summarize([p for p in settled_by["contrarian"] if regime_of(p) == "revert"])
    mv_rev = summarize([p for p in settled_by["contrarian_mv"] if regime_of(p) == "revert"])
    print("\n" + "-" * 104)
    print("CALIBRATION ANCHOR — low_vol contrarian, REVERT regime:")
    if lv_rev:
        print(f"  backtest edge {lv_rev['edge']*100:+.1f}pp (z={lv_rev['z']:+.1f})  "
              f"vs FINDINGS LIVE +15.5pp (n=234, z=5.0).")
        print("  Backtest UNDERSTATES (different market selection than live) -> read MV/HV RELATIVE")
        print("  to this anchor, NOT as absolute backtest truth.")
    if lv_rev and mv_rev and lv_rev["edge"] != 0:
        print(f"  MV revert edge {mv_rev['edge']*100:+.1f}pp  =  {mv_rev['edge']/lv_rev['edge']*100:+.0f}% "
              f"of LV revert edge (same lens).")
    print("-" * 104)

    print("\nMV (mid_vol) REVERT regime — by coin (thin-book overpay diagnostic):")
    by_coin = defaultdict(list)
    for p in settled_by["contrarian_mv"]:
        if regime_of(p) == "revert":
            by_coin[p.symbol or "?"].append(p)
    for sym, rows in sorted(by_coin.items(), key=lambda kv: -len(kv[1])):
        _row(sym, summarize(rows))

    print("\nREAD: MV is a CANDIDATE only if its REVERT edge is comparable to LV's REVERT edge")
    print("(same lens) AND survives the fill cost. NEGATIVE SCREEN: this can fail-to-kill (->")
    print("live read) or confirm dead; it CANNOT certify MV alive.")


if __name__ == "__main__":
    main()
