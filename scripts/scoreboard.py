"""One-command STATUS dashboard for the live paper run — the wait, made visible.

Read-only on data/bot.db. Shows, with the CORRECT pre-registered metrics (so nobody misreads a
mirage): bot health (is it even alive?), contrarian, depth-W progress, the slow-rise canary,
the era-2 test, and a pointer to the FILTER TEST (the live lead). Run anytime:

    .venv/Scripts/python.exe scripts/scoreboard.py

NOTE: the FILTER TEST arm A (persist) needs the indexed copy + book depth, so it is NOT in this
fast dashboard — run `python scripts/make_bt_db.py && python research/filter_test.py`.
"""
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "bot.db"
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
EFFICIENT_FROM = "2026-06-16"   # regime boundary (revert <=06-15)
AMBIG = "2026-06-19"            # ambiguous day excluded from depth-W
ERA2_CUTOFF = "2026-06-12T06:00:00"


def _ts(s):
    return datetime.fromisoformat(s)


def main():
    if not DB.exists():
        sys.exit(f"DB not found: {DB}")
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row

    print("=" * 78)
    print("  POLYMARKET-BOT SCOREBOARD")
    print("=" * 78)

    # 1. BOT HEALTH ---------------------------------------------------------
    mx = c.execute("SELECT MAX(ts) m FROM snapshots").fetchone()["m"]
    now = datetime.now(timezone.utc)
    stale_min = (now - _ts(mx)).total_seconds() / 60 if mx else 1e9
    health = "LIVE" if stale_min < 15 else ("LAGGING" if stale_min < 60 else "STOPPED?")
    opn = c.execute("SELECT COUNT(*) n FROM positions WHERE status='open'").fetchone()["n"]
    print("\n[1] BOT HEALTH")
    print(f"    latest data: {mx}")
    print(f"    staleness:   {stale_min:,.0f} min ago   -> {health}")
    if stale_min >= 60:
        print("    [!] data >1h stale -- bot likely DOWN. Forward test NOT accruing! Restart: python -m src.main")
    print(f"    open positions in-flight: {opn}")

    # 2. CONTRARIAN (LV) ----------------------------------------------------
    def wr(rows):
        n = rows["n"] or 0
        return f"{rows['w']/n*100:.1f}% ({rows['w']}/{n})" if n else "-"
    r = c.execute("SELECT COUNT(*) n, COALESCE(SUM(pnl_usdc>0),0) w FROM positions "
                  "WHERE strategy='contrarian' AND status='resolved'").fetchone()
    rev = c.execute("SELECT COUNT(*) n, COALESCE(SUM(pnl_usdc>0),0) w FROM positions WHERE strategy='contrarian' "
                    "AND status='resolved' AND substr(ts,1,10)<='2026-06-15'").fetchone()
    eff = c.execute("SELECT COUNT(*) n, COALESCE(SUM(pnl_usdc>0),0) w FROM positions WHERE strategy='contrarian' "
                    "AND status='resolved' AND substr(ts,1,10)>=?", (EFFICIENT_FROM,)).fetchone()
    print("\n[2] CONTRARIAN (LV)")
    print(f"    resolved: {wr(r)}   | revert {wr(rev)}  efficient {wr(eff)}")

    # 3. DEPTH-W (efficient winners, the persist/depth fuel) ----------------
    dw = c.execute("SELECT COALESCE(SUM(pnl_usdc>0),0) w FROM positions WHERE strategy='contrarian' "
                   "AND status='resolved' AND substr(ts,1,10)>=? AND substr(ts,1,10)!=?",
                   (EFFICIENT_FROM, AMBIG)).fetchone()["w"]
    bar = "#" * int(dw / 40 * 20)
    print("\n[3] DEPTH-W  (efficient winners, excl 06-19 — fuels persist/depth-AUC)")
    print(f"    {dw}/40  [{bar:<20}]  ({max(0,40-dw)} to go)")

    # 4. SLOW-RISE  (NET vs hold, not the WR mirage) ------------------------
    sr = c.execute("SELECT market_id, side, size_usdc, entry_price, exit_price FROM positions "
                   "WHERE closed_reason='slow_rise_exit'").fetchall()
    killed = salv = unk = 0
    net = 0.0
    for p in sr:
        lp = c.execute("SELECT price FROM snapshots WHERE market_id=? AND event_type IN ('book','last_trade_price') "
                       "AND price IS NOT NULL ORDER BY ts DESC LIMIT 1", (p["market_id"],)).fetchone()
        if lp is None or 0.1 <= lp["price"] <= 0.9:
            unk += 1
            continue
        outcome = "YES" if lp["price"] > 0.9 else "NO"
        shares = p["size_usdc"] / p["entry_price"] if p["entry_price"] else 0
        ex = p["exit_price"] if p["exit_price"] is not None else 0.40
        if outcome == p["side"]:
            killed += 1
            net -= shares * (1.0 - ex)
        else:
            salv += 1
            net += shares * ex
    print("\n[4] SLOW-RISE EXIT  (judge NET-vs-hold, NOT the ~100% WR mirage)")
    print(f"    closes: {len(sr)}/~100   salvaged {salv} / winners-killed {killed} (unk {unk})")
    print(f"    NET vs holding: {net:+,.0f}   (>0 = helps; score the verdict at ~100)")

    # 5. ERA-2 pre-registered test (full pass/fail in research/score_contrarian_test.py) -----
    e2 = c.execute("SELECT COUNT(*) n, COALESCE(SUM(pnl_usdc>0),0) w FROM positions "
                   "WHERE strategy='contrarian' AND status='resolved' AND ts>=?", (ERA2_CUTOFF,)).fetchone()
    print("\n[5] ERA-2 falsification = PASSED 06-14 (n=119; FINDINGS). Re-score: research/score_contrarian_test.py")
    print(f"    (info) contrarian resolved since {ERA2_CUTOFF[:10]}: {wr(e2)}  -- early-exits shrink the resolved pool")

    # 6. FILTER TEST (the lead) — pre-registered/LOCKED 06-25 — pointer -----
    print("\n[6] * FILTER TEST (the LEAD) -- pre-registered/LOCKED 06-25; pass = margin>=break-even+8pp & N>=100/arm")
    print("    arm A persist (Tier1 >-0.169 primer / Tier2 >-0.100), fwd >= 06-22 -- needs the indexed copy:")
    print("      python scripts/make_bt_db.py && python research/filter_test.py   (--engine = gap-#1 cross-check)")
    print("    arm B holistik (discretionary, forward-only):  python scripts/filter_b.py list|call|score")

    print("\n" + "-" * 78)
    print("Disabled/frozen: nothing to tune. Wait + keep the bot ALIVE (panel 1). Confirm the FILTER TEST")
    print("at N>=100/arm (forward); score slow-rise/era-2 at ~100. (MV canary REMOVED 06-23 -- no longer tracked.)")
    c.close()


if __name__ == "__main__":
    main()
