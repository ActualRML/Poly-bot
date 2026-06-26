"""READ-ONLY: TAKE-PROFIT on the live contrarian ledger — does locking a gain
(especially a 'rose then STALLED' longshot) beat holding to resolution?

THE USER'S SCENARIO: bought a longshot ~0.20, it climbed to ~0.50, then sat flat
at 0.50. Better to take profit or hold? At a CALIBRATED market price == P(win), so
selling at 0.50 locks ~0.50 while holding's EV from there is also ~0.50 — minus the
spread you pay to sell. So TP is EV-NEUTRAL-TO-NEGATIVE *unless the price is
MISCALIBRATED at the stall* (the realized win-rate of trades sitting at level V is
BELOW V — i.e. a spike to 0.50 tends to fall back). This probe MEASURES exactly
that, three ways:

  1. CALIBRATION-OF-LEVEL — among trades whose value first REACHED level V (a
     genuine rise from a lower entry), what fraction actually WON, vs V? WR < V =>
     reaching V is over-priced => selling at V is +EV (the only way TP wins).
  2. ROSE-THEN-STALLED — restrict to trades that reached V from entry then stayed
     inside [V-band, V+band] for >= T minutes without breaking out (the user's
     literal 'stuck at 0.50'). WR vs V there, and TP-at-stall net vs hold. Does
     stalling PREDICT a fall (WR < V) or not?
  3. HONEST TP SWEEP — sell the first time value >= th (HIT THE BID + walk depth;
     empty/one-sided bid => no_exit => you can't sell), net vs hold + winners
     capped. Reconfirms / extends the project's prior 'TP is dead' verdict.

METHOD (project canon): TOUCH-ONLY path via `book` events (value + the sell book
come from the SAME fresh row); held-side value = price if YES else 1-price; outcome
= live ledger sign (pnl_usdc), API-clean by construction; the honest sell fill is
reused from `probe_lastmin_exit` so the cost model can't drift. NOT a bot change —
opens data/bot.db mode=ro, stdlib + the YES-book reflector, console output only,
never writes.

Run:  .venv/Scripts/python.exe research/probe_take_profit.py
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
from probe_lastmin_exit import infer_outcome, sell_fill, shares_of  # honest fill + parity
from src.execute.fill import yes_book_from_token

DB = REPO / "data" / "bot.db"
STRATEGY = "contrarian"           # the user's 0.2->0.5 case is a contrarian longshot
LEVELS = (0.30, 0.40, 0.50, 0.60, 0.70, 0.80)
TP_THS = (0.50, 0.60, 0.70, 0.80, 0.90)
STALL_BAND = 0.05                 # "flat" = value stays within +/- this of the level
STALL_MINS = (5, 10)              # must stay flat this long to count as a stall


def _parse(s):
    return datetime.fromisoformat(s)


def book_events(conn, mid, side, lo_iso, hi_iso):
    """[(ts, value, yesbook)] from `book` events in (lo, hi]; value = held-side."""
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
                         rt=rt, ev=ev, symbol=t["symbol"]))
    return ctxs


def realize_tp(ctx, idx):
    """PnL if we SELL at event `idx` (None => hold-to-resolution). Honest fill: hit
    the bid + walk depth; an empty/one-sided bid => no_exit (can't sell, ride out).
    Unsold shares (depth-exhausted) settle at the known outcome."""
    if idx is None:
        return ctx["hold"], "hold"
    _, _, yb = ctx["ev"][idx]
    proceeds, sold, top = sell_fill(ctx["side"], ctx["shares"], yb)
    if top is None:
        return ctx["hold"], "no_exit"
    settle = 1.0 if ctx["won"] else 0.0
    return proceeds + (ctx["shares"] - sold) * settle - ctx["stake"], "exit"


def first_reach(ctx, V):
    """First event index where value >= V, for a genuine RISE (entry < V)."""
    if ctx["entry"] >= V:
        return None
    for i, (ts, v, yb) in enumerate(ctx["ev"]):
        if v >= V:
            return i
    return None


def stalled_at(ctx, V, band, mins):
    """Confirmation index of a STALL at V: value first reaches V (from a lower
    entry) and then stays inside [V-band, V+band] for >= `mins` minutes WITHOUT
    breaking out up or down. Returns that index (where we'd sell after observing the
    stall), or None. A break-out up (kept rising) or down (reverted) => not a stall."""
    i0 = first_reach(ctx, V)
    if i0 is None:
        return None
    ev = ctx["ev"]
    t0 = ev[i0][0]
    for j in range(i0, len(ev)):
        ts, v, _ = ev[j]
        if v < V - band or v > V + band:
            return None                       # broke out before confirming
        if (ts - t0).total_seconds() >= mins * 60:
            return j                           # stayed flat long enough -> stall
    return None                                # ran out of data before T minutes


def _bucket(sub):
    n = len(sub)
    w = sum(c["won"] for c in sub)
    wr = w / n if n else 0.0
    avg_entry = sum(c["entry"] for c in sub) / n if n else 0.0
    return n, w, wr, avg_entry


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        ctxs = load_ctxs(conn)
        n = len(ctxs)
        wins = sum(c["won"] for c in ctxs)
        hold_net = sum(c["hold"] for c in ctxs)
        print("=" * 88)
        print(f"TAKE-PROFIT PROBE — strategy={STRATEGY}, resolved trades with a book path = {n}")
        print(f"  baseline: wins {wins}/{n} ({wins/n:.1%})  |  hold-to-resolution net = {hold_net:+,.0f}")
        print("  (held-side value path = book events only; outcome = live ledger sign)")
        print("=" * 88)

        # --- 1. CALIBRATION OF LEVEL + TP-at-first-reach ----------------------
        print("\n[1] CALIBRATION — trades that FIRST REACHED level V (a rise from entry):")
        print("    if realized WR < V, the spike is over-priced => selling at V is +EV.\n")
        print(f"  {'V':>5} {'reached':>8} {'avg_entry':>10} {'WR@reach':>9} {'resid(WR-V)':>12} "
              f"{'TP_net-hold':>12} {'won_capped':>11} {'no_exit':>8}")
        for V in LEVELS:
            sub = [c for c in ctxs if first_reach(c, V) is not None]
            cnt, w, wr, ae = _bucket(sub)
            if cnt == 0:
                print(f"  {V:>5.2f} {0:>8}  (none reached)")
                continue
            tp = hold = 0.0
            capped = noex = 0
            for c in sub:
                pnl, flag = realize_tp(c, first_reach(c, V))
                tp += pnl
                hold += c["hold"]
                if flag == "exit" and c["won"]:
                    capped += 1
                elif flag == "no_exit":
                    noex += 1
            print(f"  {V:>5.2f} {cnt:>8} {ae:>10.3f} {wr:>9.1%} {wr - V:>+12.3f} "
                  f"{tp - hold:>+12,.0f} {capped:>11} {noex:>8}")

        # --- 2. ROSE-THEN-STALLED (the user's exact 'stuck at 0.5') -----------
        print(f"\n[2] ROSE-THEN-STALLED — reached V then stayed in [V+/-{STALL_BAND}] for >= T min,")
        print("    then we SELL. Does stalling predict a fall (WR<V) or not?\n")
        print(f"  {'V':>5} {'T(min)':>6} {'stalled':>8} {'WR':>7} {'resid':>8} "
              f"{'TP_net-hold':>12} {'won_capped':>11} {'no_exit':>8}")
        for V in LEVELS:
            for T in STALL_MINS:
                sub = []
                for c in ctxs:
                    j = stalled_at(c, V, STALL_BAND, T)
                    if j is not None:
                        sub.append((c, j))
                if not sub:
                    print(f"  {V:>5.2f} {T:>6} {0:>8}  (none)")
                    continue
                cnt = len(sub)
                w = sum(c["won"] for c, _ in sub)
                wr = w / cnt
                tp = hold = 0.0
                capped = noex = 0
                for c, j in sub:
                    pnl, flag = realize_tp(c, j)
                    tp += pnl
                    hold += c["hold"]
                    if flag == "exit" and c["won"]:
                        capped += 1
                    elif flag == "no_exit":
                        noex += 1
                print(f"  {V:>5.2f} {T:>6} {cnt:>8} {wr:>7.1%} {wr - V:>+8.3f} "
                      f"{tp - hold:>+12,.0f} {capped:>11} {noex:>8}")

        # --- 3. BLANKET TP SWEEP (reconfirm the prior 'TP dead' result) -------
        print("\n[3] BLANKET TP SWEEP — sell first time value >= th (honest fill), over ALL trades:\n")
        print(f"  {'th':>5} {'exits':>6} {'no_exit':>8} {'won_capped':>11} "
              f"{'TP_net':>10} {'vs_hold':>10}")
        for th in TP_THS:
            tp = 0.0
            ex = noex = capped = 0
            for c in ctxs:
                pnl, flag = realize_tp(c, first_reach(c, th))
                tp += pnl
                if flag == "exit":
                    ex += 1
                    if c["won"]:
                        capped += 1
                elif flag == "no_exit":
                    noex += 1
            print(f"  {th:>5.2f} {ex:>6} {noex:>8} {capped:>11} {tp:>+10,.0f} {tp - hold_net:>+10,.0f}")

        print("\n" + "=" * 88)
        print("READ: TP only beats hold where realized WR < V (a real miscalibration), AND the")
        print("salvage survives the honest sell cost. Calibrated levels => TP is EV-neutral minus")
        print("spread (don't build). One ~2-day regime; throttled stream under-samples fast moves.")
        print("=" * 88)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
