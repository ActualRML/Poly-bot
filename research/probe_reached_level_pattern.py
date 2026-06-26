"""READ-ONLY pattern hunt: a trade that ROSE to level V (e.g. 0.2 -> 0.5) then went
to 0 — is there ANY feature, AT the moment it sits at V, that distinguishes the ones
that will DIE from the ones that will WIN? If yes, we can sell the dying ones at V.

At a calibrated market, value==P(win), so AT 0.5 it's a coin flip and you CAN'T tell
(reaching 0.5 won 54.6% in this ledger — slightly favourable, so blanket TP loses).
This probe loops over candidate conditioning features and asks: does any SUBSET of
'reached V' trades win MATERIALLY BELOW V (a real sell signal), with enough n, and
does it survive a temporal TRAIN/TEST split?

Features tested at first-reach of V (all causally usable by a live rule):
  * minutes LEFT to resolve when it reached V (late vs early — phase effect)
  * minutes SINCE entry to reach V (fast spike vs slow grind)
  * MOMENTUM after reaching V: within OBS min, did it make a new high (V+band) =
    momentum continues, vs fail/roll-over = exhausted?
  * STALLED: stayed flat in [V+/-band] for OBS min (the user's 'stuck at 0.5')
  * symbol

Then the most promising 'sell-at-V' rule is scored OUT OF SAMPLE (honest sell fill).

METHOD (canon): TOUCH-ONLY `book` path; value = price if YES else 1-price; outcome =
ledger sign (API-clean). data/bot.db mode=ro, never writes. Heavy multiple-comparison
risk (many features x bins x levels) => trust ONLY n>=~20 AND OOS-positive cells.

Run:  .venv/Scripts/python.exe research/probe_reached_level_pattern.py
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
LEVELS = (0.40, 0.50, 0.60)
BAND = 0.05
OBS_MIN = 5                 # observe this long after reaching V before deciding
TRAIN_FRAC = 0.60


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


def feats_at_reach(ctx, V):
    """Features at the first event where value >= V (a rise from entry < V), plus the
    sell index OBS_MIN later. None if the trade never reached V."""
    if ctx["entry"] >= V:
        return None
    ev = ctx["ev"]
    i0 = next((i for i, (ts, v, yb) in enumerate(ev) if v >= V), None)
    if i0 is None:
        return None
    ts0 = ev[i0][0]
    win = [(ts, v) for ts, v, _ in ev[i0:] if (ts - ts0).total_seconds() <= OBS_MIN * 60]
    max_after = max(v for ts, v in win)
    min_after = min(v for ts, v in win)
    dur = (win[-1][0] - ts0).total_seconds() / 60.0
    jsell = i0
    for j in range(i0, len(ev)):
        if (ev[j][0] - ts0).total_seconds() <= OBS_MIN * 60:
            jsell = j
        else:
            break
    return dict(
        minutes_left=(ctx["rt"] - ts0).total_seconds() / 60.0,
        minutes_since_entry=(ts0 - ctx["ts_open"]).total_seconds() / 60.0,
        new_high=max_after >= V + BAND,                       # momentum continued up
        broke_down=min_after <= V - BAND,                     # dipped out the bottom
        stalled=(dur >= OBS_MIN - 0.5 and max_after <= V + BAND and min_after >= V - BAND),
        jsell=jsell,
        symbol=ctx["symbol"],
    )


def realize(ctx, idx):
    if idx is None:
        return ctx["hold"], "hold"
    _, _, yb = ctx["ev"][idx]
    proceeds, sold, top = sell_fill(ctx["side"], ctx["shares"], yb)
    if top is None:
        return ctx["hold"], "no_exit"
    settle = 1.0 if ctx["won"] else 0.0
    return proceeds + (ctx["shares"] - sold) * settle - ctx["stake"], "exit"


def _wr(rows):
    n = len(rows)
    w = sum(c["won"] for c in rows)
    return n, w, (w / n if n else 0.0)


def cell(label, rows, V):
    n, w, wr = _wr(rows)
    if n == 0:
        print(f"      {label:>22}: (none)")
        return
    flag = "  <-- WR<<V" if (wr < V - 0.08 and n >= 15) else ("  [thin]" if n < 15 else "")
    print(f"      {label:>22}: n={n:>3}  WR={wr:>5.1%}  resid(WR-V)={wr - V:>+.3f}{flag}")


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        ctxs = load_ctxs(conn)
        print("=" * 92)
        print(f"REACHED-LEVEL PATTERN HUNT  strategy={STRATEGY}  trades={len(ctxs)}  "
              f"(OBS window = {OBS_MIN} min)")
        print("  Q: among trades that rose to V, does any feature mark the ones that then DIE?")
        print("  A real sell-signal = a subset with WR materially BELOW V, n>=~20, OOS-positive.")
        print("=" * 92)

        for V in LEVELS:
            pop = [(c, f) for c in ctxs if (f := feats_at_reach(c, V))]
            base = [c for c, f in pop]
            n, w, wr = _wr(base)
            print(f"\n  V={V:.2f}  reached by {n} trades, baseline WR@reach = {wr:.1%} "
                  f"(resid {wr - V:+.3f})  [hold beats sell when WR>V]")
            if n < 10:
                continue
            print("    split by MOMENTUM after reaching V (within %d min):" % OBS_MIN)
            cell("new high (V+band)", [c for c, f in pop if f["new_high"]], V)
            cell("NO new high", [c for c, f in pop if not f["new_high"]], V)
            cell("stalled (flat)", [c for c, f in pop if f["stalled"]], V)
            cell("broke down (V-band)", [c for c, f in pop if f["broke_down"]], V)
            print("    split by TIME LEFT when it reached V:")
            cell("<=15 min left", [c for c, f in pop if f["minutes_left"] <= 15], V)
            cell("15-30 min left", [c for c, f in pop if 15 < f["minutes_left"] <= 30], V)
            cell(">30 min left", [c for c, f in pop if f["minutes_left"] > 30], V)
            print("    split by SPEED of the rise (entry->V):")
            cell("fast (<=10 min)", [c for c, f in pop if f["minutes_since_entry"] <= 10], V)
            cell("slow (>10 min)", [c for c, f in pop if f["minutes_since_entry"] > 10], V)
            print("    split by SYMBOL:")
            for sym in ("BTC", "ETH", "BNB"):
                cell(sym, [c for c, f in pop if f["symbol"] == sym], V)

        # --- OOS test of the most intuitive rule: "reached V, momentum FAILED -> sell"
        V = 0.50
        print("\n" + "=" * 92)
        print(f"OOS RULE TEST @ V={V:.2f}: 'reached {V:.2f} but made NO new high within "
              f"{OBS_MIN} min -> SELL'")
        print("=" * 92)
        tagged = []
        for c in ctxs:
            f = feats_at_reach(c, V)
            tagged.append((c, f))
        k = int(len(tagged) * TRAIN_FRAC)
        for split_name, part in (("TRAIN", tagged[:k]), ("TEST", tagged[k:])):
            sell = [(c, f) for c, f in part if f and not f["new_high"]]
            holdall = sum(c["hold"] for c, f in part)
            net = 0.0
            ex = noex = wk = 0
            for c, f in part:
                if f and not f["new_high"]:
                    pnl, flag = realize(c, f["jsell"])
                    if flag == "exit":
                        ex += 1
                        wk += c["won"]
                    elif flag == "no_exit":
                        noex += 1
                else:
                    pnl, flag = realize(c, None)
                net += pnl
            n_sell, w_sell, wr_sell = _wr([c for c, f in sell])
            print(f"  {split_name}: rule sells {n_sell} (WR {wr_sell:.0%}), "
                  f"net {net:+,.0f} vs hold {holdall:+,.0f}  => effect {net - holdall:+,.0f}  "
                  f"(exits={ex}, no_exit={noex}, winners_killed={wk})")
        print("\n  Read: only believe it if the TEST effect is clearly + AND the sold subset's WR")
        print("  is well below 0.50 on a non-tiny n. Otherwise it's noise (market efficient at 0.5).")
        print("=" * 92)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
