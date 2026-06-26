"""READ-ONLY: is a LAST-MINUTE / EARLY exit (SL or TP) worth building for contrarian?

Extends `probe_stoploss_path.py` with the three things that probe could NOT answer:
  1. TIME-GATING — only act inside the final W minutes (the user's "cut when the
     direction is fixed" idea), vs the old probe's any-time first-cross.
  2. HONEST SELL FILL — the old probe sold at the optimistic *touch/mid*. Selling a
     position actually HITS THE BID and walks bid depth (price gets WORSE as you
     dump size). If the held side's bid is EMPTY (one-sided book — ~96% of late
     rows per FINDINGS *One-sided depth*) you simply CANNOT exit; the salvage is
     fictional. Modelled here from the captured book.
  3. SPOT-VS-STRIKE LAG — the only place a taker edge could hide: at T-X is Binance
     spot already decisively past the hour-open while Polymarket YES still lags?

NOT a bot change. Opens data/bot.db mode=ro, stdlib + one import of the YES-book
reflection helper, console output. Never writes anything.

Held-side VALUE path is reconstructed TOUCH-ONLY (book + last_trade_price); the
SELL FILL needs quotes+depth so it uses `book` events only. Outcome = live ledger
sign (pnl_usdc), API-clean by construction.

Run:  .venv/Scripts/python.exe research/probe_lastmin_exit.py
"""
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from src.execute.fill import yes_book_from_token  # reflect raw per-token book -> YES

try:                       # Windows consoles default to cp1252; keep output robust
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DB_PATH = REPO_ROOT / "data" / "bot.db"
STRATEGY = "contrarian"
WINDOWS = (1, 2, 5, 10)         # minutes before resolve_time to allow an exit
SL_THS = (0.05, 0.10, 0.20)     # exit if held-side value <= this (stop-loss)
TP_THS = (0.80, 0.90)           # exit if held-side value >= this (take-profit)
SPOT_TOL_SEC = 180              # nearest-binance match tolerance


def _parse(s):
    return datetime.fromisoformat(s)


def shares_of(entry, stake, old_era):
    """Held quantity, reproducing stored pnl: old-era pnl used a 0.03 buffer; the
    realistic-fill era (fill_flag set) used 0.0. Branch so the parity check holds."""
    buf = 0.03 if old_era else 0.0
    eff = min(entry + buf, 1.0)
    return stake / eff if eff else 0.0


def infer_outcome(bb, ba, yp):
    """Stored best_bid/ask are the RAW ticking token; price is YES-normalized.
    Decide which token this row is so we can reflect it to YES-perspective."""
    if bb is None or ba is None or yp is None:
        return "YES"
    raw_mid = (bb + ba) / 2.0
    return "YES" if abs(raw_mid - yp) <= abs((1.0 - raw_mid) - yp) else "NO"


def sell_fill(side, shares, yb):
    """Proceeds from SELLING `shares` of the held side into the captured book.
      YES sell -> hit the YES bid (walk yes_bid depth).
      NO  sell -> hit the NO bid = 1 - yes_ask (walk yes_ask depth).
    Returns (proceeds, sold_shares, top_bid). top_bid None => no bid => CANNOT exit.
    Walk goes DOWN one spread per level (a seller eats progressively worse bids)."""
    if side == "YES":
        p0, size, depth = yb.yes_bid, yb.yes_bid_size, yb.yes_bid_depth
    else:
        p0 = (1.0 - yb.yes_ask) if yb.yes_ask is not None else None
        size, depth = yb.yes_ask_size, yb.yes_ask_depth
    if p0 is None or p0 <= 0.0:
        return 0.0, 0.0, None  # empty/one-sided -> no exit possible
    spread = (yb.yes_ask - yb.yes_bid) if (yb.yes_ask is not None and yb.yes_bid is not None) else None
    if size is None or depth is None or size <= 0 or depth <= 0 or spread is None or spread <= 0:
        return shares * p0, shares, p0  # top-of-book only (optimistic: no walk data)
    sold = proceeds = 0.0
    remaining = depth
    k = 0
    while sold < shares - 1e-9 and remaining > 1e-9:
        price = p0 - k * spread
        if price <= 0.0:
            break
        lvl = min(size, remaining, shares - sold)
        proceeds += lvl * price
        sold += lvl
        remaining -= lvl
        k += 1
    return proceeds, sold, p0


def book_path(conn, market_id, lo_iso, hi_iso):
    """[(value, yesbook)] from `book` events in (lo, hi], reflected to YES-perspective."""
    rows = conn.execute(
        """
        SELECT ts, price, best_bid, best_ask, bid_size, ask_size, bid_depth, ask_depth
          FROM snapshots
         WHERE market_id = ? AND event_type = 'book' AND price IS NOT NULL
           AND (best_bid IS NOT NULL OR best_ask IS NOT NULL)
           AND ts > ? AND ts <= ? ORDER BY ts
        """,
        (market_id, lo_iso, hi_iso),
    ).fetchall()
    out = []
    for r in rows:
        oc = infer_outcome(r["best_bid"], r["best_ask"], r["price"])
        yb = yes_book_from_token(oc, r["best_bid"], r["best_ask"],
                                 r["bid_size"], r["ask_size"], r["bid_depth"], r["ask_depth"])
        out.append((r["price"], yb))  # store YES price; caller flips for NO side
    return out


def spot_near(conn, symbol, t_iso):
    r = conn.execute(
        """
        SELECT price FROM snapshots
         WHERE source='binance' AND symbol=? AND price IS NOT NULL
           AND ABS(strftime('%s',ts) - strftime('%s',?)) <= ?
         ORDER BY ABS(strftime('%s',ts) - strftime('%s',?)) LIMIT 1
        """,
        (symbol, t_iso, SPOT_TOL_SEC, t_iso),
    ).fetchone()
    return float(r["price"]) if r else None


def main():
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        return
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        trades = conn.execute(
            """
            SELECT id, ts, market_id, symbol, side, entry_price, size_usdc, pnl_usdc,
                   resolve_time, fill_flag
              FROM positions
             WHERE strategy=? AND status='resolved' AND pnl_usdc IS NOT NULL
               AND resolve_time IS NOT NULL
             ORDER BY ts
            """,
            (STRATEGY,),
        ).fetchall()
        if not trades:
            print("no resolved contrarian trades w/ resolve_time")
            return

        rec = []
        for t in trades:
            entry = float(t["entry_price"])
            stake = float(t["size_usdc"])
            hold_pnl = float(t["pnl_usdc"])
            old_era = t["fill_flag"] is None
            rec.append(dict(
                side=t["side"], symbol=t["symbol"], entry=entry, stake=stake,
                hold_pnl=hold_pnl, won=hold_pnl > 0,
                shares=shares_of(entry, stake, old_era),
                market_id=t["market_id"], rt=_parse(t["resolve_time"]),
            ))
        nW = sum(r["won"] for r in rec)
        net_hold = sum(r["hold_pnl"] for r in rec)
        # parity: shares*1 - stake should reproduce stored pnl for winners
        par = sum((r["shares"] - r["stake"]) for r in rec if r["won"]) - sum(r["hold_pnl"] for r in rec if r["won"])

        print("=" * 78)
        print("LAST-MINUTE / EARLY EXIT PROBE - resolved contrarian (READ-ONLY, honest sell)")
        print("=" * 78)
        print(f"trades={len(rec)}  winners={nW}  losers={len(rec)-nW}  hold-to-resolve net={net_hold:+,.2f}")
        print(f"shares parity (winners): reconstructed-minus-stored = {par:+.2f} (~0 = ok)\n")

        def exit_value(side, yes_price):
            return yes_price if side == "YES" else 1.0 - yes_price

        def simulate(kind, th, W):
            """kind='SL' exits on first in-window book value<=th; 'TP' on first value>=th."""
            net = win_killed = los_soft = acted = no_exit = 0
            net = 0.0
            profit_lost = savings = 0.0
            for r in rec:
                lo = (r["rt"] - timedelta(minutes=W)).isoformat()
                cands = book_path(conn, r["market_id"], lo, r["rt"].isoformat())
                hit = None
                for yp, yb in cands:
                    v = exit_value(r["side"], yp)
                    if (kind == "SL" and v <= th) or (kind == "TP" and v >= th):
                        hit = (v, yb)
                        break
                if hit is None:
                    net += r["hold_pnl"]
                    continue
                _, yb = hit
                proceeds, sold, top = sell_fill(r["side"], r["shares"], yb)
                if top is None:           # bid empty -> cannot exit -> ride to resolution
                    no_exit += 1
                    net += r["hold_pnl"]
                    continue
                acted += 1
                settle = 1.0 if r["won"] else 0.0
                ex_pnl = proceeds + (r["shares"] - sold) * settle - r["stake"]
                net += ex_pnl
                delta = ex_pnl - r["hold_pnl"]
                if r["won"]:
                    win_killed += 1
                    profit_lost += -delta
                else:
                    los_soft += 1
                    savings += delta
            return dict(net=net, eff=net - net_hold, acted=acted, no_exit=no_exit,
                        win_killed=win_killed, los_soft=los_soft,
                        profit_lost=profit_lost, savings=savings)

        # ---- Section 1: STOP-LOSS, time-gated, honest fill ----
        print("--- STOP-LOSS: exit on first book value<=th inside the final W min (honest bid fill) ---")
        print(f"  {'W':>3} {'th':>5} {'acted':>5} {'no_exit':>7} {'win_kill':>8} {'los_soft':>8} "
              f"{'prof_lost':>10} {'savings':>9} {'net':>11} {'effect':>10}")
        best = None
        for W in WINDOWS:
            for th in SL_THS:
                s = simulate("SL", th, W)
                tag = (W, th, "SL")
                if best is None or s["eff"] > best[1]["eff"]:
                    best = (tag, s)
                print(f"  {W:>3} {th:>5.2f} {s['acted']:>5} {s['no_exit']:>7} {s['win_killed']:>8} "
                      f"{s['los_soft']:>8} {s['profit_lost']:>10,.1f} {s['savings']:>9,.1f} "
                      f"{s['net']:>11,.1f} {s['eff']:>+10,.1f}")
        print(f"  hold-to-resolve baseline net = {net_hold:+,.2f}\n")

        # ---- Section 2: TAKE-PROFIT, time-gated, honest fill ----
        print("--- TAKE-PROFIT: exit on first book value>=th inside the final W min (honest bid fill) ---")
        print(f"  {'W':>3} {'th':>5} {'acted':>5} {'no_exit':>7} {'win_kill':>8} {'los_soft':>8} "
              f"{'prof_lost':>10} {'savings':>9} {'net':>11} {'effect':>10}")
        for W in WINDOWS:
            for th in TP_THS:
                s = simulate("TP", th, W)
                if s["eff"] > best[1]["eff"]:
                    best = ((W, th, "TP"), s)
                print(f"  {W:>3} {th:>5.2f} {s['acted']:>5} {s['no_exit']:>7} {s['win_killed']:>8} "
                      f"{s['los_soft']:>8} {s['profit_lost']:>10,.1f} {s['savings']:>9,.1f} "
                      f"{s['net']:>11,.1f} {s['eff']:>+10,.1f}")
        print()

        # ---- Section 3: SPOT-VS-STRIKE LAG (does spot 'fix' before the price?) ----
        print("--- SPOT-VS-STRIKE LAG @ T-X: is Binance spot already decided while price lags? ---")
        print(f"  {'X(min)':>6} {'matched':>7} {'spot_pred_acc':>13} {'lag_cases':>9} {'salvageable$':>12}")
        for X in (1, 2, 5):
            matched = correct = lag = 0
            salv = 0.0
            for r in rec:
                t_x = (r["rt"] - timedelta(minutes=X)).isoformat()
                t_open = (r["rt"] - timedelta(hours=1)).isoformat()
                s_x = spot_near(conn, r["symbol"], t_x)
                s_open = spot_near(conn, r["symbol"], t_open)
                if s_x is None or s_open is None:
                    continue
                matched += 1
                up = s_x > s_open
                # held side's spot-predicted win: YES wins on up, NO wins on down
                pred_win = up if r["side"] == "YES" else (not up)
                if pred_win == r["won"]:
                    correct += 1
                # lag/salvage: spot says we LOSE but our price hasn't collapsed yet
                cands = book_path(conn, r["market_id"],
                                  (r["rt"] - timedelta(minutes=X)).isoformat(), r["rt"].isoformat())
                v_now = exit_value(r["side"], cands[0][0]) if cands else None
                if (not pred_win) and v_now is not None and v_now > 0.10:
                    lag += 1
                    salv += v_now * r["shares"]   # crude upper bound on salvage if we could sell at v
            acc = f"{correct/matched:.0%}" if matched else "-"
            print(f"  {X:>6} {matched:>7} {acc:>13} {lag:>9} {salv:>12,.1f}")
        print("  (spot_pred_acc = how often spot-direction-at-T-X matches the outcome;")
        print("   lag_cases = spot says LOSE but our price still >0.10; salvageable$ = crude UPPER bound)\n")

        # ---- Caveats + verdict ----
        print("--- CAVEATS ---")
        print("  (a) ONE regime; throttled stream UNDER-captures intra-second wicks -> overstates exits.")
        print("  (b) sell fill walks captured depth, but late books are often ONE-SIDED (no bid) ->")
        print("      those are counted 'no_exit' (the honest answer: you can't dump into an empty bid).")
        print("  (c) salvageable$ is an UPPER bound (assumes you sell the whole position at v, no walk).")
        print("  (d) old-era (215) pnl used a 0.03 buffer; shares branch on era for parity.\n")

        tag, s = best
        word = "HELPS" if s["eff"] > 0 else "HURTS/NEUTRAL"
        print("=" * 78)
        print(f"BEST CELL: {tag[2]} th={tag[1]:.2f} W={tag[0]}min -> net effect {s['eff']:+,.2f} "
              f"vs hold ({word}); acted={s['acted']}, no_exit={s['no_exit']}, winners_killed={s['win_killed']}.")
        print("Decision gate: effect <= 0 (or only via fictional empty-bid exits) => DON'T build Phase B.")
        print("=" * 78)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
