"""READ-ONLY scoring of the PRE-REGISTERED contrarian era-2 test (2026-06-12).

Registered (before the data) in FINDINGS.md *Contrarian era-2 run*:

  Population : contrarian positions OPENED after 2026-06-12 06:00 UTC.
  Evaluate   : at >= 100 new resolved contrarian trades.
  PASS needs BOTH:
    (a) win rate  >  cost-breakeven WR @ 3c slippage  (= mean(entry+0.03))
    (b) NO-side actual WR  >  NO-side implied  (= mean(NO entry))   [drift control]
        scored on hour-clustered units.
  Fail EITHER  -> verdict "window-drift artifact, no contrarian edge".

`entry_price` is stored held-side (portfolio.open_position writes decision.price,
the held-side price), so implied P(win) for a position == its entry_price.
won == pnl_usdc > 0 ; loss == pnl_usdc < 0 ; void/open excluded from WR.

READ-ONLY: opens data/bot.db mode=ro, never writes. Run:
    .venv/Scripts/python.exe research/score_contrarian_test.py
"""
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.execute.portfolio import SLIPPAGE_BUFFER  # noqa: E402

DB_PATH = REPO_ROOT / "data" / "bot.db"
CUTOFF = "2026-06-12T06:00:00"  # opened-after boundary (UTC), per the registration
MIN_N = 100                     # evaluate the test at this many resolved trades


def _binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k) for X~Binom(n,p) — exact, the registration's significance read."""
    return sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k, n + 1))


def main() -> None:
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        return
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """
        SELECT ts, side, entry_price, size_usdc, status, pnl_usdc
          FROM positions
         WHERE strategy = 'contrarian' AND ts >= ?
         ORDER BY ts
        """,
        (CUTOFF,),
    ).fetchall()
    conn.close()

    n_open = sum(r["status"] == "open" for r in rows)
    n_void = sum(r["status"] == "void" for r in rows)
    res = [r for r in rows if r["status"] == "resolved"]

    print("=" * 68)
    print(f"PRE-REGISTERED CONTRARIAN TEST  (opened after {CUTOFF} UTC)")
    print("=" * 68)
    print(f"  opened after cutoff : {len(rows)}  "
          f"(resolved {len(res)}, open {n_open}, void {n_void})")

    if len(res) < MIN_N:
        print(f"\n  >>> NOT YET EVALUABLE: {len(res)}/{MIN_N} resolved trades.")
        print(f"      Need {MIN_N - len(res)} more resolved contrarian trades.")
        print("      Knobs stay FROZEN; do not score pass/fail yet.")
        _descriptive(res)
        return

    _descriptive(res, evaluate=True)


def _descriptive(res, evaluate: bool = False) -> None:
    if not res:
        return
    n = len(res)
    wins = sum(r["pnl_usdc"] > 0 for r in res)
    wr = wins / n
    net = sum(r["pnl_usdc"] for r in res)
    be = sum(min(r["entry_price"] + SLIPPAGE_BUFFER, 1.0) for r in res) / n  # cost-breakeven WR
    avg_entry = sum(r["entry_price"] for r in res) / n

    def side_stats(side):
        s = [r for r in res if r["side"] == side]
        if not s:
            return 0, 0, 0.0, 0.0, 0.0
        w = sum(r["pnl_usdc"] > 0 for r in s)
        implied = sum(r["entry_price"] for r in s) / len(s)
        pnl = sum(r["pnl_usdc"] for r in s)
        return len(s), w, w / len(s), implied, pnl

    yn, yw, ywr, yimp, ypnl = side_stats("YES")
    nn, nw, nwr, nimp, npnl = side_stats("NO")

    # hour clustering
    by_hour = defaultdict(list)
    for r in res:
        by_hour[r["ts"][:13]].append(r)
    hours = len(by_hour)
    win_hours = sorted(((sum(x["pnl_usdc"] > 0 for x in v), h) for h, v in by_hour.items()),
                       reverse=True)
    top5_wins = sum(c for c, _ in win_hours[:5])

    print(f"\n  --- resolved sample (n={n}) ---")
    print(f"  net pnl @3c buffer  : {net:+.2f}")
    print(f"  win rate            : {wins}/{n} = {wr:.1%}")
    print(f"  cost-breakeven @3c  : {be:.1%}   (avg entry {avg_entry:.3f})")
    print(f"  distinct hours      : {hours}   (10 wins in top-5 hrs? {top5_wins}/{wins})")
    print(f"  YES side : {yw}/{yn} = {ywr:.1%}  vs implied {yimp:.1%}  pnl {ypnl:+.2f}")
    print(f"  NO  side : {nw}/{nn} = {nwr:.1%}  vs implied {nimp:.1%}  pnl {npnl:+.2f}")

    if wins and n:
        p_raw = _binom_sf(wins, n, be)
        print(f"  exact binomial P(X>={wins} | be={be:.3f}) = {p_raw:.3f} (uncorrected)")

    if not evaluate:
        return

    cond_a = wr > be
    cond_b = nwr > nimp
    print("\n  --- PRE-REGISTERED PASS/FAIL ---")
    print(f"  (a) WR {wr:.1%} > breakeven {be:.1%}            : {'PASS' if cond_a else 'FAIL'}")
    print(f"  (b) NO actual {nwr:.1%} > NO implied {nimp:.1%}  : {'PASS' if cond_b else 'FAIL'}")
    verdict = ("PASS — contrarian shows edge beyond drift" if (cond_a and cond_b)
               else 'FAIL -> "window-drift artifact, no contrarian edge"')
    print(f"\n  ==> VERDICT: {verdict}")


if __name__ == "__main__":
    main()
