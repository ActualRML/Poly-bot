"""READ-ONLY: does a stop-loss kill contrarian winners? Settle it on the path.

NOT a bot change. Opens data/bot.db mode=ro, stdlib-only, console output. Never
writes anything.

For EVERY resolved contrarian trade we reconstruct the position-VALUE path between
entry and resolution from stored snapshots:
    position_value = YES_price            for a YES position
                   = 1 - YES_price        for a NO  position
(snapshots.price is already YES-normalized by the orchestrator, main.py:380-382.)

PATH = TOUCH-ONLY events (book + last_trade_price). price_change rows are EXCLUDED
on purpose: they carry the CHANGED LEVEL's price, not the touch (the same deep-level
artifact that made offline labels 52.9% corrupt) -> they would manufacture false
dips. An all-events run is shown as a sensitivity so the opposing bias is visible.

Outcome = live ledger sign (pnl_usdc), API-clean by construction.

Run yourself:
    .venv/Scripts/python.exe research/probe_stoploss_path.py
"""
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

DB_PATH = REPO_ROOT / "data" / "bot.db"
STRATEGY = "contrarian"
SLIPPAGE_BUFFER = 0.03                # mirrors portfolio.py (kept stdlib-only)
THRESHOLDS = (0.05, 0.07, 0.10)
TOUCH_EVENTS = ("book", "last_trade_price")


def _shares(stake, entry):
    """Shares bought, matching the resolver: stake / (entry + buffer). With this,
    held-to-resolution pnl == stored pnl_usdc exactly (win: shares-stake; loss: -stake)."""
    eff = min(entry + SLIPPAGE_BUFFER, 1.0)
    return stake / eff if eff else 0.0


def load_trades(conn):
    return conn.execute(
        """
        SELECT id, ts, market_id, symbol, side, entry_price, size_usdc, pnl_usdc,
               resolved_ts, resolve_time
          FROM positions
         WHERE strategy = ? AND status = 'resolved' AND pnl_usdc IS NOT NULL
         ORDER BY ts
        """,
        (STRATEGY,),
    ).fetchall()


def value_path(conn, market_id, side, entry_ts, bound_ts, touch_only=True):
    """[(ts, position_value)] strictly AFTER entry, BEFORE resolution."""
    if touch_only:
        ev = " AND event_type IN (%s)" % ",".join("?" * len(TOUCH_EVENTS))
        params = (market_id, entry_ts, bound_ts, *TOUCH_EVENTS)
    else:
        ev = ""
        params = (market_id, entry_ts, bound_ts)
    rows = conn.execute(
        f"""
        SELECT ts, price FROM snapshots
         WHERE market_id = ? AND price IS NOT NULL AND ts > ? AND ts < ?{ev}
         ORDER BY ts
        """,
        params,
    ).fetchall()
    out = []
    for r in rows:
        p = float(r["price"])
        out.append((r["ts"], p if side == "YES" else 1.0 - p))
    return out


def first_cross(path, th):
    """First (ts, value) where value <= th, else None."""
    for ts, v in path:
        if v <= th:
            return ts, v
    return None


def dipped_then_recovered(path, entry):
    """True if value went below entry and LATER came back above entry."""
    seen_below = False
    for _, v in path:
        if v < entry:
            seen_below = True
        elif seen_below and v > entry:
            return True
    return False


def main():
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        return
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        trades = load_trades(conn)
        if not trades:
            print("no resolved contrarian trades")
            return

        rec = []   # per-trade reconstructed record
        empty = 0
        path_lens = []
        for t in trades:
            entry = float(t["entry_price"])
            stake = float(t["size_usdc"])
            hold_pnl = float(t["pnl_usdc"])
            won = hold_pnl > 0
            bound = t["resolve_time"] or t["resolved_ts"]
            path = value_path(conn, t["market_id"], t["side"], t["ts"], bound, touch_only=True)
            path_all = value_path(conn, t["market_id"], t["side"], t["ts"], bound, touch_only=False)
            if not path:
                empty += 1
            path_lens.append(len(path))
            rec.append(dict(
                side=t["side"], symbol=t["symbol"], entry=entry, stake=stake,
                won=won, hold_pnl=hold_pnl, shares=_shares(stake, entry),
                path=path, path_all=path_all,
                min_val=min((v for _, v in path), default=None),
            ))

        winners = [r for r in rec if r["won"]]
        losers = [r for r in rec if not r["won"]]
        nW, nL = len(winners), len(losers)
        avg_len = sum(path_lens) / len(path_lens) if path_lens else 0

        print("=" * 74)
        print("STOP-LOSS PATH ANALYSIS  —  resolved contrarian (READ-ONLY, paper prices)")
        print("=" * 74)
        print(f"trades={len(rec)}  winners={nW}  losers={nL}  "
              f"hold-to-resolve net={sum(r['hold_pnl'] for r in rec):+,.2f}")
        print(f"path source = TOUCH-ONLY (book+last_trade_price); "
              f"avg path pts/trade={avg_len:.1f}; empty-path trades={empty} "
              f"(can't dip -> counted as 'never hit')\n")

        # ---- Q1: winners that touched <= 0.07 ----
        print("--- Q1  KEY QUESTION: winners that touched position-value <= 0.07 ---")
        w07 = [r for r in winners if first_cross(r["path"], 0.07)]
        w07_all = [r for r in winners if first_cross(r["path_all"], 0.07)]
        pc = (len(w07) / nW * 100) if nW else 0.0
        pc_all = (len(w07_all) / nW * 100) if nW else 0.0
        print(f"  touch-only : {len(w07)}/{nW} winners dipped to <=0.07  ({pc:.1f}%)")
        print(f"  all-events : {len(w07_all)}/{nW}  ({pc_all:.1f}%)  "
              f"[price_change-inflated UPPER bound — over-counts via deep-level prints]")
        verdict_q1 = ("MOST winners dipped -> a 0.07 stop would CUT them (EV-destroying)"
                      if pc > 50 else
                      "FEW winners dipped -> a 0.07 stop rarely touches winners")
        print(f"  => {verdict_q1}\n")

        # ---- Q2: the mirror — losers that recovered above entry after dipping ----
        print("--- Q2  MIRROR: losers that dipped below entry then RECOVERED above it ---")
        lrec = [r for r in losers if dipped_then_recovered(r["path"], r["entry"])]
        plc = (len(lrec) / nL * 100) if nL else 0.0
        print(f"  {len(lrec)}/{nL} losers looked 'alive again' (false hope a stop avoids)  ({plc:.1f}%)\n")

        # ---- Q3/Q4: direct stop-loss simulation at each threshold ----
        print("--- Q3/Q4  STOP-LOSS SIMULATION (exit at first value<=th, 'worst available') ---")
        net_without = sum(r["hold_pnl"] for r in rec)
        print(f"  hold-to-resolve baseline net = {net_without:+,.2f}")
        print(f"  {'th':>5} {'hits':>5} {'win_killed':>10} {'los_soft':>9} "
              f"{'profit_lost':>12} {'savings':>10} {'net_w/stop':>11} {'net_effect':>11}")
        results = {}
        for th in THRESHOLDS:
            net_with = 0.0
            win_killed = los_soft = hits = 0
            profit_lost = savings = 0.0
            for r in rec:
                cx = first_cross(r["path"], th)
                if cx is None:
                    net_with += r["hold_pnl"]
                    continue
                hits += 1
                _, v_exit = cx
                stop_pnl = r["shares"] * v_exit - r["stake"]   # sell the position at the stop touch
                net_with += stop_pnl
                delta = stop_pnl - r["hold_pnl"]
                if r["won"]:
                    win_killed += 1
                    profit_lost += -delta        # winner: delta<0, profit given up (positive)
                else:
                    los_soft += 1
                    savings += delta             # loser: delta>0, loss avoided
            net_effect = net_with - net_without
            results[th] = dict(net_with=net_with, net_effect=net_effect,
                               win_killed=win_killed, los_soft=los_soft,
                               profit_lost=profit_lost, savings=savings, hits=hits)
            print(f"  {th:>5.2f} {hits:>5} {win_killed:>10} {los_soft:>9} "
                  f"{profit_lost:>12,.2f} {savings:>10,.2f} {net_with:>11,.2f} {net_effect:>+11,.2f}")
        print("  net_effect = savings on softened losers  MINUS  profit lost on killed winners\n")

        # ---- Q5: honest framing ----
        print("--- Q5  HONEST FRAMING ---")
        print("  (a) Stored prices are throttled (~10s; ~1s while a position is open). Intra-second")
        print("      wicks (the 0.02->0.23-in-38s pattern) are UNDER-captured, so the path UNDER-states")
        print("      how often winners dipped -> this makes the stop look BETTER than live reality.")
        print("  (b) PAPER price: a live stop exit also pays the bid/ask SPREAD (worse fill than the")
        print("      modelled touch), and a NO position's true exit is the NO bid, not 1-YES_bid.")
        print("  (c) ONE regime (a few-day bull/chop window) — not a general verdict.")
        print("  (d) Touch-only excludes price_change deep-level prints (which would OVER-count dips);")
        print("      the all-events row in Q1 is the inflated upper bound, shown only for contrast.\n")

        # ---- one-line verdict (best = highest net_effect) ----
        best = max(results, key=lambda k: results[k]["net_effect"])
        b = results[best]
        word = "HELPS" if b["net_effect"] > 0 else "HURTS"
        amt = abs(b["net_effect"])
        print("=" * 74)
        print(f"VERDICT: stop-loss at {best:.2f} {word} ({'+' if b['net_effect']>0 else '-'}${amt:,.2f}) "
              f"vs hold-to-resolve; {b['win_killed']} winners killed.")
        print("=" * 74)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
