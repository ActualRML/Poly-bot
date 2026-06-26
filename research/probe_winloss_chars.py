"""READ-ONLY: WHEN does MV win / when does it lose? then LV. (win/loss map).

Answers "jalanin MV sepanjang data, liat menang dikala apa kalah dikala apa, baru
LV". Runs each fade strategy across the WHOLE 06-07->06-20 ledger (its OWN replay)
and breaks its trades down MARGINALLY by every condition we can attach to a
position — regime, coin, side, entry-price band, runway (time-to-resolve at entry),
fill flag — reporting WR / edge(WR-avg_entry) / avg_roi / n per bucket. High
edge/WR buckets = "wins when ___"; negative = "loses when ___". MV first, then LV.

Same accuracy rules as probe_mv_retest.py (faithful engine, own replay per
strategy, $25 sizing-neutral). Marginal single-axis cuts only — per CLAUDE.md read
AGGREGATE direction, not thin multi-way cells (those manufacture train-winners).

CAVEAT: regime is post-hoc LABEL (undetectable ex-ante, FINDINGS); avg_roi is the
understating backtest lens — compare MV vs LV RELATIVELY. Run:
    .venv/Scripts/python.exe research/probe_winloss_chars.py
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
# disable that here so the win/loss map characterizes EVERY coin.
_cmv.SYMBOLS = None

DB = REPO / "data" / "bot_bt.db"
STRATS = [("contrarian_mv", "MV (mid_vol)"), ("contrarian", "LV (low_vol)")]  # MV first
REVERT_LAST = date(2026, 6, 15)
FIXED_BET, BIG_BANK = 25.0, 10_000_000.0
COIN_ORDER = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE"]


def _wr_z(wins, n, p):
    if n == 0 or not (0 < p < 1):
        return float("nan")
    se = math.sqrt(p * (1 - p) / n)
    return ((wins / n) - p) / se if se else float("nan")


def summarize(rows):
    rows = [p for p in rows if p.decision_price is not None and p.size_usdc]
    n = len(rows)
    if not n:
        return None
    wins = sum(1 for p in rows if p.won)
    wr, avg_mid = wins / n, sum(p.decision_price for p in rows) / n
    avg_roi = sum((p.pnl_usdc or 0.0) / p.size_usdc for p in rows) / n
    return {"n": n, "wr": wr, "edge": wr - avg_mid, "avg_roi": avg_roi,
            "z": _wr_z(wins, n, avg_mid)}


def _line(label, s, thin=8):
    if s is None:
        print(f"    {label:<16} (none)")
        return
    flag = "  <thin>" if s["n"] < thin else ""
    print(f"    {label:<16} n={s['n']:<4} WR={s['wr']*100:4.1f}%  "
          f"edge={s['edge']*100:+5.1f}pp(z{s['z']:+.1f})  avg_roi={s['avg_roi']:+.3f}{flag}")


def breakdown(title, rows, keyfunc, order=None):
    groups = defaultdict(list)
    for p in rows:
        groups[keyfunc(p)].append(p)
    keys = [k for k in (order or sorted(groups, key=lambda k: -len(groups[k]))) if k in groups]
    print(f"  by {title}:")
    for k in keys:
        _line(str(k), summarize(groups[k]))


def entry_band(p):
    x = p.decision_price
    if x < 0.165:
        return "deep <.165"
    if x < 0.18:
        return ".165-.18"
    if x < 0.20:
        return ".18-.20"
    return ">=.20"


def runway_band_factory(resolve_ts):
    def band(p):
        rt = resolve_ts.get(p.market_id)
        if rt is None:
            return "?"
        m = (rt - p.opened_ts).total_seconds() / 60.0
        if m < 20:
            return "10-20m"
        if m < 30:
            return "20-30m"
        if m < 45:
            return "30-45m"
        return "45-60m"
    return band


def main():
    if not DB.exists():
        raise SystemExit(f"need indexed copy (run scripts/make_bt_db.py): {DB}")
    print("recovering resolutions once...", flush=True)
    rec = recover_resolutions(DB)
    resolve_ts = {m: r.resolve_ts for m, r in rec.usable.items()}
    runway_band = runway_band_factory(resolve_ts)
    print(f"  usable resolutions: {len(rec.usable)}")

    for strat, label in STRATS:
        print(f"replaying {strat}...", flush=True)
        settled = run_backtest(DB, [strat], slippages=(0.0,), starting_balance=BIG_BANK,
                               fixed_bet_usdc=FIXED_BET, recovery=rec)[0].settled
        s_all = summarize(settled)
        print("\n" + "=" * 80)
        print(f"{label}  [{strat}]  — WHEN it wins / loses   (all coins, n={s_all['n'] if s_all else 0})")
        print("=" * 80)
        breakdown("REGIME", settled,
                  lambda p: "revert" if p.opened_ts.date() <= REVERT_LAST else "efficient",
                  order=["revert", "efficient"])
        breakdown("COIN", settled, lambda p: p.symbol or "?", order=COIN_ORDER)
        breakdown("SIDE", settled,
                  lambda p: "YES (fade dip)" if p.side == "YES" else "NO (fade pump)",
                  order=["YES (fade dip)", "NO (fade pump)"])
        breakdown("ENTRY PRICE", settled, entry_band,
                  order=["deep <.165", ".165-.18", ".18-.20", ">=.20"])
        breakdown("RUNWAY (ttr)", settled, runway_band,
                  order=["10-20m", "20-30m", "30-45m", "45-60m"])
        breakdown("FILL", settled, lambda p: p.fill_flag, order=["ok", "walk", "partial"])

    print("\n" + "-" * 80)
    print("READ: edge>0 buckets = wins-when; edge<0 = loses-when. <thin> = n<8, ignore as noise.")
    print("Marginal cuts only (don't stack into thin multi-way cells). avg_roi = understating")
    print("backtest lens; regime = post-hoc label, NOT a switch (undetectable ex-ante).")


if __name__ == "__main__":
    main()
