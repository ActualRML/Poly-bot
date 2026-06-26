"""READ-ONLY: CONTRARIAN WINNER-vs-LOSER MICROSTRUCTURE AUTOPSY.

THE QUESTION (2026-06-19): does the book STRUCTURE at entry separate contrarian
winners from losers? If yes, "trade only when structure is healthy" could be the
filter we've been hunting. Run with the SAME rigor as every other probe: AUC +
permutation + a FAMILY-WISE guard (9 features = 9 chances at a false positive),
then orthogonalize any survivor against the known confounds (runway, regime), then
an honest train/test PnL-lift test (vs a random-cull placebo).

VERDICT FROM THE FIRST RUN (see FINDINGS *Session 2026-06-19*):
  * depth imbalance (held-side bid_depth/ask_depth at entry) is the ONLY signal that
    clears the family-wise null. Winners bought a longshot with DEEPER resting bid
    support (it has real bids under it -> reverts up more).
  * Every dynamic candidate (refill/resiliency, spread expansion, one-sided
    persistence, activity) is DEAD (AUC ~0.5). There is ONE lever, not a stack.
  * PnL-lift test PASSES out-of-sample (drop the bottom depth_ratio third = real,
    non-overfit lift) BUT it is reverting-regime-dominated; efficient-regime transfer
    is UNRESOLVED (W=15 too small) -> RESEARCH CANDIDATE, not production.

METHOD (project canon): held-side book reflected to the bought side via
`yes_book_from_token` + `infer_outcome` (the same polarity logic the fill model
uses); entry book = latest `book` snapshot with ts <= position.ts (what the live
`latest_book` cache held); outcome = live ledger sign (pnl_usdc), API-clean.
Dynamic features use ONLY the pre-entry window [ts-W, ts] so they stay EX-ANTE
(a usable filter cannot peek past the decision). Opens data/bot.db mode=ro,
stdlib only, console output, never writes.

Re-run:  .venv/Scripts/python.exe research/audit_microstructure.py
When efficient n grows (live run), re-read section B's effic AUC at W>=~40 — that
is the binding test for whether the depth signal transfers across regimes.
"""
import math
import random
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "research"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from probe_lastmin_exit import infer_outcome  # noqa: E402  (raw-token -> outcome)
from src.execute.fill import yes_book_from_token  # noqa: E402  (reflect to YES book)

DB = REPO / "data" / "bot.db"
STRATEGY = "contrarian"
# Regime is labeled PER DAY from realized calibration (edge = WR - avg_entry), NOT a raw date
# cut. A date boundary mislabels TRANSITION days: 2026-06-19 looked "efficient" by date but its
# WR spiked to 32% (edge +8pp) on EXPENSIVE longshots (avg entry .24) and still LOST -$916 — a
# fake-reverting day that would contaminate a clean efficient sample (it alone supplied 12 of the
# 27 date-defined efficient wins). Post-hoc regime LABELING from resolutions is legitimate
# (FINDINGS: you can LABEL the regime, you just cannot PREDICT it ex-ante).
REVERT_EDGE, REVERT_Z = 0.06, 1.3   # "revert" day: longshots paid off well above their price (z-sig)
EFFIC_EDGE = 0.03                   # "effic" day: calibrated (edge ~0 / negative); between = ambiguous
MIN_DAY_N = 8                       # fewer resolved trades than this -> too thin to label (ambiguous)
WINDOW_MIN = 10                     # pre-entry window for dynamic features
NPERM = 2000                     # permutation draws (AUC nulls)
NSPLIT = 300                     # train/test repeats for the PnL-lift test

ENTRY_FEATS = ["spread", "bid_size", "ask_size", "bid_depth", "ask_depth",
               "depth_ratio", "book_age_s", "one_sided", "mins_to_res"]
DYN_FEATS = ["spread_mean", "spread_expand", "depth_mean", "depth_refill",
             "ratio_mean", "onesided_frac", "activity"]


def _parse(s):
    return datetime.fromisoformat(s)


def held_side(side, row):
    """Reflect a raw book row onto the side the contrarian BOUGHT. Returns the
    held-side ask/bid + sizes/depths (the ask is the level a buy lifts)."""
    oc = infer_outcome(row["best_bid"], row["best_ask"], row["price"])
    yb = yes_book_from_token(oc, row["best_bid"], row["best_ask"], row["bid_size"],
                             row["ask_size"], row["bid_depth"], row["ask_depth"])
    if side == "YES":
        return dict(ask=yb.yes_ask, bid=yb.yes_bid, ask_size=yb.yes_ask_size,
                    bid_size=yb.yes_bid_size, ask_depth=yb.yes_ask_depth, bid_depth=yb.yes_bid_depth)
    # NO buy: no_ask = 1 - yes_bid, sizes/depths swap sides with their level.
    return dict(
        ask=(1.0 - yb.yes_bid) if yb.yes_bid is not None else None,
        bid=(1.0 - yb.yes_ask) if yb.yes_ask is not None else None,
        ask_size=yb.yes_bid_size, bid_size=yb.yes_ask_size,
        ask_depth=yb.yes_bid_depth, bid_depth=yb.yes_ask_depth,
    )


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def compute_day_labels(conn):
    """Label each DAY's regime from its realized calibration edge (WR - avg_entry).
    Returns {day: dict(label, n, wr, edge, z)}. Post-hoc LABELING only (allowed) — used
    to SEGMENT the depth transfer test by clean regime, NOT as an ex-ante predictor.
    Null: a calibrated (efficient) market has WR == avg_entry, so z tests edge against
    binomial SE at p=avg_entry. revert = edge big AND z-significant; effic = edge <= ~0."""
    rows = conn.execute(
        f"""SELECT substr(ts,1,10) d, count(*) n,
                   sum(CASE WHEN pnl_usdc>0 THEN 1 ELSE 0 END) w, avg(entry_price) ae
              FROM positions WHERE strategy='{STRATEGY}' AND status IN('resolved','closed')
               AND pnl_usdc IS NOT NULL AND resolve_time IS NOT NULL
             GROUP BY d ORDER BY d""").fetchall()
    out = {}
    for r in rows:
        n, w, ae = r["n"], r["w"], r["ae"]
        wr = w / n
        edge = wr - ae
        se = math.sqrt(ae * (1 - ae) / n) if (0 < ae < 1 and n) else 0.0
        z = edge / se if se > 0 else 0.0
        if n < MIN_DAY_N:
            lab = "ambiguous"
        elif edge >= REVERT_EDGE and z >= REVERT_Z:
            lab = "revert"
        elif edge <= EFFIC_EDGE:
            lab = "effic"
        else:
            lab = "ambiguous"          # transition (e.g. 06-19): edge up but not z-sig
        out[r["d"]] = dict(label=lab, n=n, wr=wr, edge=edge, z=z)
    return out


def load(conn, day_labels):
    """Per contrarian trade: outcome + entry-book features + pre-entry-window
    dynamic features + (depth_ratio, roi) for the PnL test. `era` is the trade's
    DAY regime label (calibration-based), so transition days are excluded from the
    regime split rather than mislabeled by date."""
    pos = conn.execute(
        f"""SELECT market_id, side, entry_price, pnl_usdc, size_usdc, ts, resolve_time
              FROM positions WHERE strategy='{STRATEGY}' AND status IN('resolved','closed')
               AND pnl_usdc IS NOT NULL AND resolve_time IS NOT NULL ORDER BY ts""").fetchall()
    rows = []
    for t in pos:
        entry_book = conn.execute(
            """SELECT ts, price, best_bid, best_ask, bid_size, ask_size, bid_depth, ask_depth
                 FROM snapshots WHERE market_id=? AND event_type='book' AND ts<=?
                 ORDER BY ts DESC LIMIT 1""", (t["market_id"], t["ts"])).fetchone()
        if entry_book is None:
            continue
        h = held_side(t["side"], entry_book)
        spread = (h["ask"] - h["bid"]) if (h["ask"] is not None and h["bid"] is not None) else None
        ratio = (h["bid_depth"] / h["ask_depth"]) if (h["bid_depth"] and h["ask_depth"]) else None
        won = t["pnl_usdc"] > 0
        mins = (_parse(t["resolve_time"]) - _parse(t["ts"])).total_seconds() / 60.0
        entry = {
            "spread": spread, "bid_size": h["bid_size"], "ask_size": h["ask_size"],
            "bid_depth": h["bid_depth"], "ask_depth": h["ask_depth"], "depth_ratio": ratio,
            "book_age_s": (_parse(t["ts"]) - _parse(entry_book["ts"])).total_seconds(),
            "one_sided": 1.0 if (entry_book["best_bid"] is None or entry_book["best_ask"] is None) else 0.0,
            "mins_to_res": mins,
        }
        # --- dynamic pre-entry window [ts-W, ts] (ex-ante) ---
        win = conn.execute(
            """SELECT ts, price, best_bid, best_ask, bid_size, ask_size, bid_depth, ask_depth
                 FROM snapshots WHERE market_id=? AND event_type='book' AND ts<=? AND ts>=?
                 ORDER BY ts""",
            (t["market_id"], t["ts"], (_parse(t["ts"]) - timedelta(minutes=WINDOW_MIN)).isoformat())
        ).fetchall()
        dyn = {f: None for f in DYN_FEATS}
        if len(win) >= 4:
            hs = [held_side(t["side"], b) for b in win]
            sprs = [(x["ask"] - x["bid"]) if (x["ask"] is not None and x["bid"] is not None) else None for x in hs]
            tots = [(x["bid_depth"] + x["ask_depth"]) if (x["bid_depth"] is not None and x["ask_depth"] is not None) else None for x in hs]
            rats = [(x["bid_depth"] / x["ask_depth"]) if (x["bid_depth"] and x["ask_depth"]) else None for x in hs]
            ones = [1.0 if (b["best_bid"] is None or b["best_ask"] is None) else 0.0 for b in win]
            k = max(1, len(win) // 3)
            dyn = {
                "spread_mean": _mean(sprs),
                "spread_expand": (_mean(sprs[-k:]) or 0) - (_mean(sprs[:k]) or 0),
                "depth_mean": _mean(tots),
                "depth_refill": (_mean(tots[-k:]) or 0) - (_mean(tots[:k]) or 0),
                "ratio_mean": _mean(rats),
                "onesided_frac": _mean(ones),
                "activity": float(len(win)),
            }
        roi = (t["pnl_usdc"] / t["size_usdc"]) if t["size_usdc"] else None
        era = day_labels.get(t["ts"][:10], {}).get("label", "ambiguous")
        rows.append(dict(won=won, era=era, entry=entry, dyn=dyn,
                         depth_ratio=ratio, roi=roi, entry_price=float(t["entry_price"])))
    return rows


# ---------------------------------------------------------------- statistics --
def ranks(vals):
    """Average ranks (ties shared) — stable across label permutations."""
    idx = sorted(range(len(vals)), key=lambda i: vals[i])
    r = [0.0] * len(vals)
    i = 0
    while i < len(vals):
        j = i
        while j + 1 < len(vals) and vals[idx[j + 1]] == vals[idx[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            r[idx[k]] = avg
        i = j + 1
    return r


def auc_from(pairs):
    """AUC = P(winner value > loser value); 0.5 = no separation. pairs=[(won,val)]."""
    d = [(w, v) for w, v in pairs if v is not None]
    nw = sum(1 for w, _ in d if w)
    nl = len(d) - nw
    if nw < 5 or nl < 5:
        return None, nw, nl
    rk = ranks([v for _, v in d])
    sw = sum(rk[i] for i in range(len(d)) if d[i][0])
    return (sw - nw * (nw + 1) / 2) / (nw * nl), nw, nl


def perm_p(pairs, obs_dev, n=NPERM):
    """Two-sided permutation p for |AUC-0.5| via label shuffle (ranks fixed)."""
    d = [(w, v) for w, v in pairs if v is not None]
    rk = ranks([v for _, v in d])
    nw = sum(1 for w, _ in d if w)
    nl = len(d) - nw
    if nw < 5 or nl < 5:
        return None
    ge = 0
    for _ in range(n):
        s = random.sample(range(len(d)), nw)
        a = (sum(rk[i] for i in s) - nw * (nw + 1) / 2) / (nw * nl)
        if abs(a - 0.5) >= obs_dev:
            ge += 1
    return ge / n


def _spearman(xs, ys):
    pairs = [(a, b) for a, b in zip(xs, ys) if a is not None and b is not None]
    if len(pairs) < 5:
        return None
    r1 = ranks([a for a, _ in pairs])
    r2 = ranks([b for _, b in pairs])
    n = len(pairs)
    mr = (n + 1) / 2
    num = sum((r1[i] - mr) * (r2[i] - mr) for i in range(n))
    den = (sum((r - mr) ** 2 for r in r1) * sum((r - mr) ** 2 for r in r2)) ** 0.5
    return num / den if den else 0.0


def _med(xs):
    xs = sorted(x for x in xs if x is not None)
    n = len(xs)
    return None if n == 0 else (xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2)


# ----------------------------------------------------------------- sections --
def section_regime_calendar(day_labels):
    print("=" * 84)
    print("[0] PER-DAY REGIME LABEL — post-hoc calibration (edge = WR - avg_entry, z-tested)")
    print("=" * 84)
    print(f"  {'day':>12} {'n':>4} {'WR':>6} {'edge':>9} {'z':>6}  regime")
    agg = {}
    for d in sorted(day_labels):
        x = day_labels[d]
        print(f"  {d:>12} {x['n']:>4} {x['wr']:>6.0%} {x['edge']*100:>+7.0f}pp {x['z']:>+6.1f}  {x['label']}")
        agg[x["label"]] = agg.get(x["label"], 0) + x["n"]
    print("  trades by regime:  " + "   ".join(f"{k}={v}" for k, v in sorted(agg.items())))
    print("  (ambiguous/transition days are EXCLUDED from the regime split in [B]/[C])")


def section_entry_autopsy(rows):
    nW = sum(1 for r in rows if r["won"])
    print("=" * 84)
    print(f"[A] ENTRY-BOOK AUTOPSY — held-side book at entry. n={len(rows)} "
          f"(W={nW}, L={len(rows) - nW})")
    print("=" * 84)
    print(f"{'feature':>12} {'n':>4} {'med_WIN':>11} {'med_LOSE':>11} {'AUC':>6} {'perm_p':>7}")
    dev = {}
    for f in ENTRY_FEATS:
        pairs = [(r["won"], r["entry"][f]) for r in rows]
        a, nw, nl = auc_from(pairs)
        if a is None:
            print(f"{f:>12} {'--':>4}  (degenerate)")
            continue
        dev[f] = abs(a - 0.5)
        p = perm_p(pairs, dev[f])
        mw = _med([r["entry"][f] for r in rows if r["won"]])
        ml = _med([r["entry"][f] for r in rows if not r["won"]])
        g = lambda x: f"{x:>11.4f}" if x is not None else f"{'NA':>11}"
        print(f"{f:>12} {nw + nl:>4} {g(mw)} {g(ml)} {a:>6.3f} {p:>7.3f}")

    # FAMILY-WISE guard: max |AUC-0.5| across features under ONE shared shuffle.
    common = [r for r in rows if all(r["entry"][f] is not None for f in ENTRY_FEATS)]
    rk = {f: ranks([r["entry"][f] for r in common]) for f in ENTRY_FEATS}
    wmask = [r["won"] for r in common]
    N = len(common)
    nw = sum(wmask)

    def dev_of(rkf, sel):
        return abs((sum(rkf[i] for i in sel) - nw * (nw + 1) / 2) / (nw * (N - nw)) - 0.5)

    real = max(dev_of(rk[f], [i for i in range(N) if wmask[i]]) for f in ENTRY_FEATS)
    nul = []
    for _ in range(NPERM):
        s = random.sample(range(N), nw)
        nul.append(max(dev_of(rk[f], s) for f in ENTRY_FEATS))
    nul.sort()
    p95 = nul[int(0.95 * len(nul))]
    print(f"\n  FAMILY-WISE guard (complete-case n={N}, W={nw}): real best |AUC-.5|={real:.3f}")
    print(f"    shuffled max |AUC-.5|: mean={sum(nul)/len(nul):.3f} p95={p95:.3f} max={nul[-1]:.3f}")
    print(f"    >>> {'A FEATURE CLEARS THE NULL (real)' if real > p95 else 'within noise = NO real separation'}")


def section_orthogonalize(rows):
    print("\n" + "=" * 84)
    print("[B] ORTHOGONALIZE depth_ratio vs the known confounds (runway, regime)")
    print("=" * 84)
    dr = [r["entry"]["depth_ratio"] for r in rows]
    mn = [r["entry"]["mins_to_res"] for r in rows]
    ep = [r["entry_price"] for r in rows]
    print(f"  corr(depth_ratio, mins_to_res) = {_spearman(dr, mn):+.3f}   "
          f"corr(depth_ratio, entry_price) = {_spearman(dr, ep):+.3f}")

    print("\n  depth_ratio AUC WITHIN runway buckets (control for the clock):")
    def bucket(r):
        m = r["entry"]["mins_to_res"]
        return "<35m" if m < 35 else "35-50m" if m < 50 else ">50m"
    for bn in ("<35m", "35-50m", ">50m"):
        sub = [r for r in rows if bucket(r) == bn]
        a, nw, nl = auc_from([(r["won"], r["entry"]["depth_ratio"]) for r in sub])
        print(f"    {bn:>7}: AUC={a:.3f} (W={nw},L={nl})" if a is not None else f"    {bn:>7}: degenerate")

    print("\n  AUC WITHIN each clean regime (calibration-labeled; efficient W is the bottleneck):")
    n_amb = sum(1 for r in rows if r["era"] == "ambiguous")
    for e in ("revert", "effic"):
        sub = [r for r in rows if r["era"] == e]
        ar, nw, nl = auc_from([(r["won"], r["entry"]["depth_ratio"]) for r in sub])
        ad, _, _ = auc_from([(r["won"], r["entry"]["bid_depth"]) for r in sub])
        am, _, _ = auc_from([(r["won"], r["entry"]["mins_to_res"]) for r in sub])
        f = lambda x: f"{x:.3f}" if x is not None else "NA"
        print(f"    {e:>6}: depth_ratio={f(ar)}  bid_depth={f(ad)}  mins={f(am)}  (W={nw},L={nl})")
    print(f"    [excluded {n_amb} trades on ambiguous/transition days — not date-mislabeled]")
    we = sum(1 for r in rows if r["era"] == "effic" and r["won"])
    print(f"    >>> CLEAN-efficient wins so far: W={we} (transfer test wants W>=~40; grow via live run)")


def section_second_signal(rows):
    print("\n" + "=" * 84)
    print(f"[C] SECOND-SIGNAL SEARCH — dynamic {WINDOW_MIN}min PRE-entry features (ex-ante)")
    print("=" * 84)
    have = [r for r in rows if r["dyn"]["ratio_mean"] is not None]
    nW = sum(1 for r in have if r["won"])
    print(f"  n with window (>=4 book ev) = {len(have)} (W={nW},L={len(have)-nW})\n")
    print(f"{'feature':>14} {'AUC_all':>8} {'AUC_revert':>11} {'AUC_effic':>10} {'corr->ratio':>12}")
    for f in DYN_FEATS:
        a, _, _ = auc_from([(r["won"], r["dyn"][f]) for r in have])
        ar, _, _ = auc_from([(r["won"], r["dyn"][f]) for r in have if r["era"] == "revert"])
        ae, _, _ = auc_from([(r["won"], r["dyn"][f]) for r in have if r["era"] == "effic"])
        c = _spearman([r["dyn"][f] for r in have], [r["dyn"]["ratio_mean"] for r in have])
        g = lambda x: f"{x:>8.3f}" if x is not None else f"{'NA':>8}"
        print(f"{f:>14} {g(a)} {g(ar):>11} {g(ae):>10} {('%+.3f' % c) if c is not None else 'NA':>12}")
    print("  (only depth_mean/ratio_mean carry signal = the SAME depth feature; rest DEAD)")


def section_pnl_lift(rows):
    print("\n" + "=" * 84)
    print("[D] PnL-LIFT TEST — depth_ratio entry filter, train/test (theta from TRAIN only)")
    print("=" * 84)
    data = [(r["depth_ratio"], r["roi"]) for r in rows if r["depth_ratio"] is not None and r["roi"] is not None]
    n = len(data)
    roi_all = sum(r for _, r in data) / n
    print(f"  n={n}  baseline avg_roi(all)={roi_all:+.4f}  (note: era-1 mid-fill inflates the LEVEL)")

    def avg_roi(sub):
        return sum(r for _, r in sub) / len(sub) if sub else 0.0

    def pick_theta(train):
        rs = sorted(x for x, _ in train)
        best = (-9.0, 0.0)
        for q in range(0, 65, 5):
            th = rs[int(q / 100 * len(rs))]
            kept = [x for x in train if x[0] >= th]
            if len(kept) < 0.40 * len(train):
                continue
            a = avg_roi(kept)
            if a > best[0]:
                best = (a, th)
        return best[1]

    real, plac, kfracs = [], [], []
    for _ in range(NSPLIT):
        idx = list(range(n))
        random.shuffle(idx)
        cut = int(0.7 * n)
        tr = [data[i] for i in idx[:cut]]
        te = [data[i] for i in idx[cut:]]
        th = pick_theta(tr)
        kept = [x for x in te if x[0] >= th]
        if not kept:
            continue
        kf = len(kept) / len(te)
        kfracs.append(kf)
        real.append(avg_roi(kept) - avg_roi(te))
        m = max(1, int(round(kf * len(te))))
        plac.append(sum(avg_roi(random.sample(te, m)) - avg_roi(te) for _ in range(40)) / 40)

    def stat(xs):
        return sum(xs) / len(xs), sum(1 for x in xs if x > 0) / len(xs)
    rm, rp = stat(real)
    pm, pp = stat(plac)
    print(f"  avg kept fraction on test = {sum(kfracs)/len(kfracs):.0%}\n")
    print(f"  {'':22}{'mean lift':>11}{'% splits +':>12}")
    print(f"  REAL depth filter     {rm:>+11.4f}{rp:>11.0%}")
    print(f"  PLACEBO random cull   {pm:>+11.4f}{pp:>11.0%}")
    print(f"  real - placebo = {rm - pm:+.4f}  -> "
          f"{'DEPTH ADDS real OOS lift' if (rm > pm + 0.005 and rp > 0.6) else 'no lift beyond random'}")

    print("\n  avg_roi by depth_ratio TERCILE (all data — monotonic?):")
    ds = sorted(data)
    t3 = len(ds) // 3
    for nm, sub in (("low", ds[:t3]), ("mid", ds[t3:2 * t3]), ("high", ds[2 * t3:])):
        print(f"    {nm:>4}: avg_roi={avg_roi(sub):+.4f}  (n={len(sub)}, median_ratio={sub[len(sub)//2][0]:.2f})")


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    random.seed(0)
    try:
        day_labels = compute_day_labels(conn)
        rows = load(conn, day_labels)
        if not rows:
            print("no contrarian trades with an entry book — nothing to audit")
            return
        section_regime_calendar(day_labels)
        print()
        section_entry_autopsy(rows)
        section_orthogonalize(rows)
        section_second_signal(rows)
        section_pnl_lift(rows)
        print("\n" + "=" * 84)
        print("READ: depth imbalance = the one signal (passes family-wise + OOS PnL lift), BUT")
        print("reverting-dominated; efficient transfer UNRESOLVED. Regime is now CALIBRATION-labeled")
        print("per day (section [0]) so transition days (06-19) don't inflate the clean-efficient W.")
        print("Grow clean-efficient W to ~40 via the live run, then trust [B]'s effic AUC. RESEARCH")
        print("CANDIDATE, not production. FINDINGS *Session 2026-06-19*.")
        print("=" * 84)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
