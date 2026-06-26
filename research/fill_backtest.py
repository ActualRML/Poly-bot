"""EXECUTION-COST fill backtest -- READ-ONLY, extends audit_contrarian.py Section 5
to ALL resolved contrarian trades and writes a markdown report.

NOT RUN BY CLAUDE (you run everything). Review, then run yourself:
    .venv/Scripts/python.exe research/fill_backtest.py
Stdlib-only (sqlite3 + math) so it runs under ANY python. Opens data/bot.db with
mode=ro and NEVER writes the DB. The ONLY file it writes is the report:
    research/diagnostics/fill_backtest.md

GOAL: "if these trades had paid REALISTIC entry prices instead of the stored YES
bid, are they net profit or loss?" -- using the ACTUAL per-trade book (spread +
depth) at entry, not a flat assumed entry.

Outcome source: the LIVE ledger is API-clean -- the resolver settles from the real
Polymarket resolution, so positions.pnl_usdc SIGN is ground truth (touch-only
canon). research/diagnostics/label_truth.csv is loaded only as an overlap
cross-check; disagreements are reported.

THREE MODELS, same winners/losers in every one (WR is a property of the RESOLUTION,
not the fill -- only the fill PRICE/SIZE moves):

  1 PAPER       stored pnl_usdc == what check_state shows. Stored entry = YES bid,
                infinite fill. (NB: the stored number already bakes in a FLAT 3c
                slippage buffer -- see resolver; models 2/3 replace that flat 3c
                with the REAL book, so a delta can go EITHER way.)
  2 SPREAD-TRUE YES taker pays the real ASK (Ya) at entry; NO taker's cost is
                already 1-Yb (taker-correct) so NO pays NO spread penalty. Uses the
                real per-trade spread from the captured book -- no flat assumption.
  3 SPREAD+WALK on top of spread, when the order's shares exceed the top-of-book
                size we WALK the captured depth. Book-shape ASSUMPTION (stated, since
                we capture only best price + size-at-best + TOTAL depth, no inner
                level prices): liquidity sits in chunks of `size_at_best` shares,
                each chunk one captured spread worse, capped at total depth. A
                fixed-dollar budget walks that ladder; if depth is exhausted before
                the stake is spent the position is PARTIAL (smaller stake at risk).
                Trades missing the needed depth fields are UNMODELED (excluded, counted).
"""
import csv
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

DB_PATH = REPO_ROOT / "data" / "bot.db"
LABEL_TRUTH = REPO_ROOT / "research" / "diagnostics" / "label_truth.csv"
OUT_PATH = REPO_ROOT / "research" / "diagnostics" / "fill_backtest.md"

STRATEGY = "contrarian"
SLIPPAGE_BUFFER = 0.03   # mirrors portfolio.py; the flat buffer baked into stored pnl


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _median(v):
    v = sorted(x for x in v if x is not None and x == x)
    n = len(v)
    if not n:
        return float("nan")
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def _pctile(v, q):
    v = sorted(x for x in v if x is not None and x == x)
    if not v:
        return float("nan")
    i = min(len(v) - 1, int(q * (len(v) - 1) + 0.5))
    return v[i]


def _book_at_entry(conn, market_id, entry_ts):
    return conn.execute(
        """
        SELECT best_bid, best_ask, bid_size, ask_size, bid_depth, ask_depth
          FROM snapshots
         WHERE market_id = ? AND event_type = 'book' AND ts <= ?
           AND best_bid IS NOT NULL
         ORDER BY ts DESC LIMIT 1
        """,
        (market_id, entry_ts),
    ).fetchone()


def _walk_fill(stake, p0, top, depth, spread):
    """Budget-walk one side of the book with `stake` dollars.

    Book-shape ASSUMPTION (we only capture best price + size-at-best + total depth):
    liquidity sits in chunks of `top` shares, each chunk one `spread` worse than the
    last, starting at p0, total capped at `depth` shares.

    Returns (shares_acquired, dollars_spent) or None if unmodelable.
    """
    if None in (p0, top, depth, spread) or top <= 0 or depth <= 0 or p0 <= 0:
        return None
    if spread <= 0:                       # locked/crossed book -> flat fill at p0
        shares = min(stake / p0, depth)
        return shares, shares * p0
    shares = dollars = 0.0
    remaining = depth
    k = 0
    while dollars < stake - 1e-9 and remaining > 1e-9:
        price = p0 + k * spread
        if price >= 1.0:                  # a 0/1 token never economically fills >= $1
            break
        lvl = min(top, remaining)
        cost = lvl * price
        if dollars + cost <= stake:
            shares += lvl
            dollars += cost
            remaining -= lvl
            k += 1
        else:                             # partial fill of this level with the last dollars
            buy = (stake - dollars) / price
            shares += buy
            dollars += buy * price
            remaining -= buy
            break
    return shares, dollars


# --------------------------------------------------------------------------- #
# per-trade model evaluation
# --------------------------------------------------------------------------- #
def eval_trades(conn):
    rows = conn.execute(
        """
        SELECT id, ts, market_id, symbol, side, entry_price, size_usdc, pnl_usdc
          FROM positions
         WHERE strategy = ? AND status = 'resolved' AND pnl_usdc IS NOT NULL
         ORDER BY ts
        """,
        (STRATEGY,),
    ).fetchall()

    trades = []
    for r in rows:
        stake = float(r["size_usdc"])
        entry = float(r["entry_price"])
        side = r["side"]
        paper = float(r["pnl_usdc"])
        won = paper > 0                   # RESOLUTION sign -- identical across models
        shares_nom = stake / entry if entry else 0.0

        t = dict(
            market_id=r["market_id"], symbol=r["symbol"] or "?", side=side,
            stake=stake, entry=entry, won=won, paper=paper,
            Yb=None, Ya=None, spread=None,
            st=None, st_covered=False,         # spread-true pnl
            walk=None, walk_modeled=False, walk_flag="unmodeled",
        )

        b = _book_at_entry(conn, r["market_id"], r["ts"])
        if b is not None:
            Yb, Ya = b["best_bid"], b["best_ask"]
            t["Yb"], t["Ya"] = Yb, Ya
            if Ya is not None and Yb is not None:
                t["spread"] = Ya - Yb

            # ---- model 2: SPREAD-TRUE (single-price fill at the real ask) ----
            if side == "YES":
                eff = Ya                                   # YES taker lifts the ask
                top, dep = b["ask_size"], b["ask_depth"]
            else:                                          # NO ask == 1 - Yb (taker-correct)
                eff = (1.0 - Yb) if Yb is not None else None
                top, dep = b["bid_size"], b["bid_depth"]
            if eff is not None and eff > 0:
                eff = min(eff, 1.0)
                t["st"] = (stake / eff - stake) if won else -stake
                t["st_covered"] = True

            # ---- model 3: SPREAD+WALK (budget-walk the captured depth) ----
            p0 = eff
            sp = t["spread"]
            wf = _walk_fill(stake, p0, top, dep, sp) if p0 is not None else None
            if wf is not None:
                sh, doll = wf
                t["walk"] = (sh - doll) if won else -doll
                t["walk_modeled"] = True
                if sh < 1e-9:
                    t["walk_flag"] = "nofill"
                elif doll < stake - 0.01:
                    t["walk_flag"] = "partial"
                elif top is not None and shares_nom > top + 1e-9:
                    t["walk_flag"] = "walk"
                else:
                    t["walk_flag"] = "ok"
        trades.append(t)
    return trades


# --------------------------------------------------------------------------- #
# aggregation
# --------------------------------------------------------------------------- #
def agg(trades):
    """Totals over a trade list. spread-true carries paper where no book;
    walk reported BOTH modeled-only and carry-paper-where-unmodeled."""
    n = len(trades)
    wins = sum(1 for t in trades if t["won"])
    paper = sum(t["paper"] for t in trades)
    st = sum((t["st"] if t["st_covered"] else t["paper"]) for t in trades)
    walk_modeled = [t for t in trades if t["walk_modeled"]]
    walk_only = sum(t["walk"] for t in walk_modeled)
    walk_carry = sum((t["walk"] if t["walk_modeled"] else t["paper"]) for t in trades)
    return dict(
        n=n, wins=wins, wr=(wins / n if n else float("nan")),
        paper=paper, st=st,
        walk_only=walk_only, walk_carry=walk_carry,
        n_walk=len(walk_modeled),
        st_covered=sum(1 for t in trades if t["st_covered"]),
        unmodeled=sum(1 for t in trades if not t["walk_modeled"]),
    )


def breakeven_spread(trades, no_pays_spread=False):
    """Total pnl when the YES taker pays Yb+s (and, if no_pays_spread, NO pays
    (1-Yb)+s too). Only trades with a captured Yb count. Returns (s_star, cov)."""
    cov = [t for t in trades if t["Yb"] is not None]

    def total(s):
        tot = 0.0
        for t in cov:
            stake, won = t["stake"], t["won"]
            if t["side"] == "YES":
                eff = min(t["Yb"] + s, 0.999999)
            else:
                eff = min((1.0 - t["Yb"]) + (s if no_pays_spread else 0.0), 0.999999)
            tot += (stake / eff - stake) if won else -stake
        return tot

    s = 0.0
    prev = total(0.0)
    star = None
    while s <= 0.90:
        cur = total(s)
        if prev > 0 >= cur:
            star = s
            break
        prev = cur
        s += 0.0025
    return star, len(cov), total


# --------------------------------------------------------------------------- #
# label-truth cross-check (overlap only)
# --------------------------------------------------------------------------- #
def truth_check(trades):
    truth = {}
    if LABEL_TRUTH.exists():
        with open(LABEL_TRUTH, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                tt = (row.get("truth") or "").strip().upper()
                if tt in ("YES", "NO"):
                    truth[row["market_id"]] = tt
    checked = disagree = 0
    for t in trades:
        if t["market_id"] in truth:
            checked += 1
            if (truth[t["market_id"]] == t["side"]) != t["won"]:
                disagree += 1
    return checked, disagree


# --------------------------------------------------------------------------- #
# markdown report
# --------------------------------------------------------------------------- #
def money(x):
    return f"{x:+,.2f}" if x == x else "n/a"


def verdict(x):
    return "**NET PROFIT**" if x > 0 else ("**NET LOSS**" if x < 0 else "**FLAT**")


def build_report(trades):
    L = []
    w = L.append
    a = agg(trades)
    checked, disagree = truth_check(trades)
    spreads = [t["spread"] for t in trades if t["spread"] is not None]

    w(f"# Contrarian execution-cost fill backtest")
    w("")
    w(f"_Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} by "
      f"`research/fill_backtest.py` — READ-ONLY on `data/bot.db`._")
    w("")
    w(f"Scope: **ALL {a['n']} resolved contrarian trades** (not just the last 30). "
      f"Outcome = live ledger (API-clean, touch-only canon). "
      f"label_truth overlap {checked}/{a['n']}, **{disagree} disagreements**.")
    w("")
    w("**WR is identical across all three models** — a trade's win/loss is fixed by the "
      f"market RESOLUTION, only the fill price/size moves. WR = **{0}/{1} = {2:.1%}** "
      "in PAPER, SPREAD-TRUE, and SPREAD+WALK alike (sanity confirmed)."
      .format(a["wins"], a["n"], a["wr"]))
    w("")

    # ---- spread distribution actually used ----
    w("## Spread actually used (captured book, not assumed)")
    w("")
    w(f"- Trades with a captured book near entry: **{a['st_covered']}/{a['n']}**.")
    w(f"- YES-side spread `Ya−Yb` over those: median **{_median(spreads)*100:.2f}c**, "
      f"p90 **{_pctile(spreads,0.90)*100:.2f}c**, max **{(max(spreads) if spreads else float('nan'))*100:.2f}c**.")
    w("- Per-symbol spread (median / p90, cents):")
    w("")
    w("| symbol | trades | median spread | p90 spread |")
    w("|---|--:|--:|--:|")
    by_sym = defaultdict(list)
    for t in trades:
        by_sym[t["symbol"]].append(t)
    for sym in sorted(by_sym, key=lambda s: -len(by_sym[s])):
        sp = [t["spread"] for t in by_sym[sym] if t["spread"] is not None]
        w(f"| {sym} | {len(by_sym[sym])} | "
          f"{_median(sp)*100:.2f}c | {_pctile(sp,0.90)*100:.2f}c |")
    w("")

    # ---- headline three models ----
    w("## Net PnL under each model")
    w("")
    w("All-resolved basis (spread-true & walk **carry paper** where the book/depth was "
      "not captured, so all three cover the same N):")
    w("")
    w("| model | net PnL | vs paper |")
    w("|---|--:|--:|")
    w(f"| 1 PAPER (stored, flat 3c buffer) | {money(a['paper'])} | — |")
    w(f"| 2 SPREAD-TRUE (real ask; NO=1−Yb) | {money(a['st'])} | {money(a['st']-a['paper'])} |")
    w(f"| 3 SPREAD+WALK (depth-walked) | {money(a['walk_carry'])} | {money(a['walk_carry']-a['paper'])} |")
    w("")
    w(f"Walk-modeled subset only (n={a['n_walk']}, UNMODELED excluded = {a['unmodeled']}):")
    w("")
    sub = [t for t in trades if t["walk_modeled"]]
    sa = agg(sub)
    w("| model (modeled subset) | net PnL |")
    w("|---|--:|")
    w(f"| 1 PAPER | {money(sa['paper'])} |")
    w(f"| 2 SPREAD-TRUE | {money(sa['st'])} |")
    w(f"| 3 SPREAD+WALK | {money(sa['walk_only'])} |")
    w("")
    flags = defaultdict(int)
    for t in sub:
        flags[t["walk_flag"]] += 1
    w(f"Walk fill outcomes (modeled subset): "
      + ", ".join(f"{k}={v}" for k, v in sorted(flags.items())) + ".")
    w("")

    # ---- per-symbol table ----
    w("## Per-symbol PnL (which symbol carries the result)")
    w("")
    w("Paper & spread-true over all that symbol's trades (carry); walk over its "
      "modeled subset.")
    w("")
    w("| symbol | trades | median spread | PAPER | SPREAD-TRUE | SPREAD+WALK (n) |")
    w("|---|--:|--:|--:|--:|--:|")
    for sym in sorted(by_sym, key=lambda s: -len(by_sym[s])):
        g = by_sym[sym]
        ga = agg(g)
        sp = [t["spread"] for t in g if t["spread"] is not None]
        w(f"| {sym} | {ga['n']} | {_median(sp)*100:.2f}c | "
          f"{money(ga['paper'])} | {money(ga['st'])} | "
          f"{money(ga['walk_only'])} (n={ga['n_walk']}) |")
    w("")
    # sign-flip levers
    w("Sign-flip levers (drop a symbol entirely):")
    w("")
    for drop in (["BNB"], ["BNB", "DOGE"], ["BNB", "DOGE", "XRP"]):
        keep = [t for t in trades if t["symbol"] not in drop]
        ka = agg(keep)
        w(f"- ex-{'/'.join(drop)} (n={ka['n']}): "
          f"PAPER {money(ka['paper'])} · SPREAD-TRUE {money(ka['st'])} · "
          f"SPREAD+WALK {money(ka['walk_carry'])}")
    w("")

    # ---- break-even spread ----
    w("## Break-even spread (how much headroom)")
    w("")
    star, cov, total_fn = breakeven_spread(trades, no_pays_spread=False)
    star2, _, _ = breakeven_spread(trades, no_pays_spread=True)
    med_sp = _median(spreads)
    w(f"Sweeping a uniform YES-entry spread `s` (NO stays taker-correct at 1−Yb), over "
      f"the {cov} book-covered trades:")
    w("")
    if star is None:
        w(f"- **No break-even within s ∈ [0, 0.90].** Total stays positive even if the "
      f"YES side pays a 90c spread — because the NO side pays no spread and is "
      f"independently profitable. PnL at s=0: {money(total_fn(0.0))}; at s=0.90: "
      f"{money(total_fn(0.90))}.")
    else:
        w(f"- **Break-even YES spread ≈ {star*100:.1f}c.** Actual median YES spread is "
          f"**{med_sp*100:.2f}c** → headroom ≈ **{(star-med_sp)*100:.1f}c**.")
    if star2 is None:
        w(f"- Pessimistic (NO ALSO pays the spread): still no break-even within [0,0.90].")
    else:
        w(f"- Pessimistic (NO ALSO pays the spread): break-even ≈ **{star2*100:.1f}c**.")
    w("")

    # ---- caveats ----
    w("## Honest caveats")
    w("")
    w("- **Book data is young.** Two-sided depth exists only POST-RESTART (~06:00 UTC "
      "2026-06-09); earlier trades may lack a captured book and fall back to PAPER carry.")
    w("- **The walk under-samples the true book.** We capture only best price, "
      "size-at-best, and TOTAL depth — never inner level prices. Model 3 ASSUMES a "
      "linear ladder (one spread per top-size chunk). The slope is an assumption, not "
      "data; treat the walk column as indicative, not exact.")
    w("- **Captured-depth snapshots are point-in-time** and ~throttled; the real book at "
      "the trade instant could be deeper (optimistic walk) or thinner (the book could "
      "have pulled — pessimistic). A snapshot is not a guaranteed resting fill.")
    w("- **The PAPER baseline already bakes in a flat 3c buffer**, so SPREAD-TRUE looking "
      "*better* than PAPER is mostly the real ~1c spread (and the NO side's 0c) being "
      "GENTLER than the flat 3c — it is not new alpha.")
    w("- **This is still ONE regime** (~bull/chop window, a few days). Even where "
      "spread-true is positive, the extreme_low/high corner is calibrated in aggregate "
      "(the sweep / loss-falsification say zero edge given price); any positive here is "
      "REGIME, not a durable execution-cost edge. Execution accounting cannot manufacture "
      "alpha — it can only tell you whether realistic cost ERASES the window's drift.")
    w("")

    # ---- one-line verdicts ----
    w("## Verdict (one line per model, all-resolved basis)")
    w("")
    w(f"- **PAPER** = {verdict(a['paper'])} ({money(a['paper'])})")
    w(f"- **SPREAD-TRUE** = {verdict(a['st'])} ({money(a['st'])})")
    w(f"- **SPREAD+WALK** = {verdict(a['walk_carry'])} ({money(a['walk_carry'])}) "
      f"[modeled-subset only: {verdict(sa['walk_only'])} {money(sa['walk_only'])}]")
    w("")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
def main():
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        return
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        trades = eval_trades(conn)
    finally:
        conn.close()

    if not trades:
        print("no resolved contrarian trades found")
        return

    report = build_report(trades)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(report, encoding="utf-8")

    a = agg(trades)
    print(f"wrote {OUT_PATH}")
    print(f"  resolved contrarian trades : {a['n']}  (WR {a['wr']:.1%}, identical across models)")
    print(f"  PAPER       net : {a['paper']:+,.2f}")
    print(f"  SPREAD-TRUE net : {a['st']:+,.2f}")
    print(f"  SPREAD+WALK net : {a['walk_carry']:+,.2f}  (modeled-only {a['walk_only']:+,.2f})")


if __name__ == "__main__":
    main()
