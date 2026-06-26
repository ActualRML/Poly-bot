"""READ-ONLY: do CRYPTO CANDLE features filter contrarian trades EX-ANTE? (esp. efficient regime).

Research question: can pre-entry spot candle info separate contrarian WINNERS from
LOSERS, as a FILTER on top of contrarian — especially in the EFFICIENT regime?

FEATURES (12). Magnitude/extension cluster (expected to be ~one PRICE/VOL PROXY,
FINDINGS prior): ret_5m/15m/30m, vol_30m, dist_MA30, ema20d, bb_z. Path/structure
cluster (potentially INDEPENDENT of magnitude): wick_rej, concentr, atr_chg,
streak, bos. DROPPED: volume-spike + VWAP — binance volume is not persisted (parser
stores close price only). NOTE: bars are tick-derived (~10s ticks → 1m OHLC ≈ 6
ticks/bar) so wick / structure / BOS are COARSE approximations — read as direction,
not precision.

GUARDS against "search manufactures train-winners" (CLAUDE.md *Don't*), now harder
because 12 features = a wide search:
  * AUC (threshold-free → no knob to tune) as the primary metric;
  * per-regime PLACEBO floor (random features on the same labels; small efficient n
    ⇒ wide floor); PLUS a BEST-OF-N placebo — the bar the single BEST real feature
    must clear (the honest multiple-testing bar for a 12-feature search);
  * TRAIN/TEST chronological split — a real feature holds AUC direction OOS.
Labels = the LIVE contrarian ledger (clean, the authority). Features ex-ante (ticks
strictly < entry_ts; no opening/future info).

Run:  .venv/Scripts/python.exe research/probe_candle_filter.py
"""
import bisect
import random
import sqlite3
import statistics
import sys
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DB = REPO / "data" / "bot.db"
REVERT_LAST = "2026-06-15"
TRAIN_FRAC = 0.60
PLACEBO_N = 1000
BON_ITERS = 1000        # best-of-N placebo iterations
random.seed(7)

# feature families (for reading: magnitude = proxy cluster; path = maybe independent)
MAG = ["ret_5m", "ret_15m", "ret_30m", "vol_30m", "dist_MA30", "ema20d", "bb_z"]
PATH = ["wick_rej", "concentr", "atr_chg", "streak", "bos"]
FEATS = MAG + PATH


def _epoch(s):
    return datetime.fromisoformat(s).timestamp()


def auc(scores, labels):
    pairs = [(s, l) for s, l in zip(scores, labels) if s is not None]
    if not pairs:
        return None
    scores = [s for s, _ in pairs]
    labels = [l for _, l in pairs]
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    P = sum(labels)
    N = len(labels) - P
    if P == 0 or N == 0:
        return None
    sp = sum(ranks[i] for i in range(len(labels)) if labels[i])
    return (sp - P * (P + 1) / 2.0) / (P * N)


def placebo_floor(labels, n_feat=PLACEBO_N):
    devs = []
    m = len(labels)
    for _ in range(n_feat):
        a = auc([random.random() for _ in range(m)], labels)
        if a is not None:
            devs.append(abs(a - 0.5))
    devs.sort()
    return devs[int(0.95 * len(devs))]


def best_of_n_floor(labels, n_feat, iters=BON_ITERS):
    """p95 of the MAX |AUC-0.5| over n_feat random features — the bar the BEST of a
    n_feat-feature search must beat to not be explained by noise."""
    maxes = []
    m = len(labels)
    for _ in range(iters):
        best = 0.0
        for _ in range(n_feat):
            a = auc([random.random() for _ in range(m)], labels)
            if a is not None:
                best = max(best, abs(a - 0.5))
        maxes.append(best)
    maxes.sort()
    return maxes[int(0.95 * len(maxes))]


def _price_at(ts_arr, px_arr, t):
    i = bisect.bisect_right(ts_arr, t) - 1
    return px_arr[i] if i >= 0 else None


def _slice_before(ts_arr, px_arr, t, win_s):
    lo = bisect.bisect_left(ts_arr, t - win_s)
    hi = bisect.bisect_right(ts_arr, t)
    return ts_arr[lo:hi], px_arr[lo:hi]


def _bars(wt, wp, bar_s=60):
    """tick → 1-min OHLC bars (time-ordered list of [o,h,l,c])."""
    b = {}
    for t, p in zip(wt, wp):
        k = int(t // bar_s)
        if k not in b:
            b[k] = [p, p, p, p]
        else:
            b[k][1] = max(b[k][1], p)
            b[k][2] = min(b[k][2], p)
            b[k][3] = p
    return [b[k] for k in sorted(b)]


def _ema(vals, span):
    if not vals:
        return None
    k = 2.0 / (span + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def features(ts_arr, px_arr, entry_t, side):
    """All ex-ante; oriented by FADE dir (d=-1 YES faded a DROP, +1 NO faded a RISE)
    so positive = strength of the CONTINUATION/structure move (hypothesis: stronger
    ⇒ less reversion ⇒ loss), EXCEPT wick_rej (positive = reversal support ⇒ win)."""
    p0 = _price_at(ts_arr, px_arr, entry_t)
    if p0 is None or p0 <= 0:
        return None
    d = -1.0 if side == "YES" else 1.0

    def ret(w):
        pb = _price_at(ts_arr, px_arr, entry_t - w)
        return (p0 / pb - 1.0) * d if pb and pb > 0 else None

    wt, wp = _slice_before(ts_arr, px_arr, entry_t, 3600)   # 60m
    if len(wp) < 12:
        return None
    bars = _bars(wt, wp)
    if len(bars) < 10:
        return None
    closes = [b[3] for b in bars]

    # --- magnitude/extension cluster ---
    rec = closes[-30:]
    rets = [rec[i] / rec[i - 1] - 1.0 for i in range(1, len(rec)) if rec[i - 1]]
    vol30 = statistics.pstdev(rets) if len(rets) >= 2 else None
    ma30 = sum(rec) / len(rec)
    distma = (p0 / ma30 - 1.0) * d if ma30 else None
    ema20 = _ema(closes[-40:], 20)
    ema20d = (p0 / ema20 - 1.0) * d if ema20 else None
    last20 = closes[-20:]
    ma20 = sum(last20) / len(last20)
    sd20 = statistics.pstdev(last20) if len(last20) >= 2 else 0.0
    bb_z = ((p0 - ma20) / sd20) * d if sd20 > 0 else None

    # --- path/structure cluster ---
    # ATR expansion: range-vol now (last 30m) vs prior (30-60m ago)
    def rng_vol(bb):
        if len(bb) < 3:
            return None
        trs = [(x[1] - x[2]) / x[3] for x in bb if x[3]]
        return statistics.mean(trs) if trs else None
    atr_now, atr_prior = rng_vol(bars[-30:]), rng_vol(bars[-60:-30])
    atr_chg = (atr_now / atr_prior) if (atr_now and atr_prior) else None
    # concentration: biggest 1m bar move / total (high = one sudden dump, low = grind)
    br = [abs(rec[i] / rec[i - 1] - 1.0) for i in range(1, len(rec)) if rec[i - 1]]
    concentr = (max(br) / sum(br)) if br and sum(br) > 0 else None
    # streak: consecutive 1m bars in the continuation direction
    streak = 0
    for i in range(len(closes) - 1, 0, -1):
        s = (closes[i] - closes[i - 1])
        if s * d > 0:
            streak += 1
        else:
            break
    # wick rejection on the last completed bar (reversal support for the fade)
    o, h, l, c = bars[-1]
    rng = h - l
    if rng > 0:
        wick = (min(o, c) - l) / rng if side == "YES" else (h - max(o, c)) / rng
    else:
        wick = None
    # break of structure in the extreme direction (recent 20m vs prior swing)
    prior, recent = bars[:-20], bars[-20:]
    bos = 0.0
    if prior and recent:
        if side == "YES":
            ps, rs = min(x[2] for x in prior), min(x[2] for x in recent)
            bos = (ps - rs) / ps if (ps and rs < ps) else 0.0
        else:
            ps, rs = max(x[1] for x in prior), max(x[1] for x in recent)
            bos = (rs - ps) / ps if (ps and rs > ps) else 0.0

    return {"ret_5m": ret(300), "ret_15m": ret(900), "ret_30m": ret(1800),
            "vol_30m": vol30, "dist_MA30": distma, "ema20d": ema20d, "bb_z": bb_z,
            "wick_rej": wick, "concentr": concentr, "atr_chg": atr_chg,
            "streak": float(streak), "bos": bos}


def main():
    if not DB.exists():
        raise SystemExit(f"DB not found: {DB}")
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT ts, symbol, side, entry_price, pnl_usdc FROM positions "
        "WHERE strategy='contrarian' AND status='resolved' AND pnl_usdc IS NOT NULL ORDER BY ts"
    ).fetchall()
    ticks = {}
    for r in conn.execute("SELECT symbol, ts, price FROM snapshots "
                          "WHERE source='binance' AND price IS NOT NULL ORDER BY symbol, ts"):
        ticks.setdefault(r["symbol"], ([], []))
        ticks[r["symbol"]][0].append(_epoch(r["ts"]))
        ticks[r["symbol"]][1].append(float(r["price"]))
    conn.close()

    trades = []
    for r in rows:
        if r["symbol"] not in ticks:
            continue
        f = features(ticks[r["symbol"]][0], ticks[r["symbol"]][1], _epoch(r["ts"]), r["side"])
        if f is None:
            continue
        trades.append({"ts": _epoch(r["ts"]), "won": 1 if r["pnl_usdc"] > 0 else 0,
                       "entry": float(r["entry_price"]),
                       "regime": "revert" if r["ts"][:10] <= REVERT_LAST else "efficient",
                       "feat": f})
    trades.sort(key=lambda t: t["ts"])
    sub = lambda rg: [t for t in trades if rg is None or t["regime"] == rg]

    print("=" * 96)
    print("CANDLE FEATURES as EX-ANTE FILTER on contrarian — AUC (winner vs loser) | 12 feats")
    print("  mag = magnitude/extension (proxy cluster) ; path = path/structure (maybe independent)")
    print("  bars are TICK-DERIVED (~10s) → wick/bos COARSE ; volume+VWAP DROPPED (not logged)")
    print("=" * 96)
    for rg in (None, "revert", "efficient"):
        ts_ = sub(rg)
        labels = [t["won"] for t in ts_]
        W = sum(labels)
        pf = placebo_floor(labels)
        bon = best_of_n_floor(labels, len(FEATS))
        print(f"\n--- {rg or 'ALL':<9} n={len(ts_)} W={W} L={len(ts_)-W}   placebo p95(per-feat)={pf:.3f}  "
              f"BEST-OF-{len(FEATS)} p95={bon:.3f}  <- best real feat must beat THIS ---")
        scored = []
        for ft in FEATS:
            a = auc([t["feat"][ft] for t in ts_], labels)
            scored.append((ft, a))
        for ft, a in scored:
            if a is None:
                print(f"    {ft:<10} AUC=n/a"); continue
            dev = abs(a - 0.5)
            tag = "mag " if ft in MAG else "path"
            mark = " <<BEATS best-of-N" if dev > bon else (" <p95" if dev > pf else "")
            print(f"    [{tag}] {ft:<10} AUC={a:.3f}  |dev|={dev:.3f}{mark}")

    print("\n" + "=" * 96)
    print("TRAIN/TEST (chrono 60/40) — does AUC direction hold OOS?  (efficient is THIN, read as weak)")
    print("=" * 96)
    for rg in ("revert", "efficient"):
        ts_ = sub(rg)
        k = int(len(ts_) * TRAIN_FRAC)
        tr, te = ts_[:k], ts_[k:]
        print(f"\n--- {rg}  train n={len(tr)}(W{sum(t['won'] for t in tr)})  test n={len(te)}(W{sum(t['won'] for t in te)}) ---")
        for ft in FEATS:
            atr = auc([t["feat"][ft] for t in tr], [t["won"] for t in tr])
            ate = auc([t["feat"][ft] for t in te], [t["won"] for t in te])
            if atr is None or ate is None:
                continue
            held = "HOLDS" if (atr - 0.5) * (ate - 0.5) > 0 and abs(ate - 0.5) > 0.07 else ""
            print(f"    {ft:<10} train={atr:.3f}  test={ate:.3f}  {held}")

    print("\n" + "=" * 96)
    print("EFFICIENT regime — WR & edge by tercile, ONLY for feats beating per-feat p95 (focused)")
    print("=" * 96)
    eff = sub("efficient")
    pf_eff = placebo_floor([t["won"] for t in eff])
    shown = False
    for ft in FEATS:
        a = auc([t["feat"][ft] for t in eff], [t["won"] for t in eff])
        if a is None or abs(a - 0.5) <= pf_eff:
            continue
        shown = True
        vals = sorted(t["feat"][ft] for t in eff if t["feat"][ft] is not None)
        q1, q2 = vals[len(vals) // 3], vals[2 * len(vals) // 3]
        bk = {"low": [], "mid": [], "high": []}
        for t in eff:
            v = t["feat"][ft]
            if v is None:
                continue
            bk["low" if v <= q1 else ("high" if v > q2 else "mid")].append(t)
        print(f"  {ft} (AUC={a:.3f}):")
        for b in ("low", "mid", "high"):
            g = bk[b]
            if g:
                n = len(g); wr = sum(t["won"] for t in g) / n
                print(f"    {b:<5} n={n:<3} WR={wr*100:4.1f}%  edge={(wr-sum(t['entry'] for t in g)/n)*100:+5.1f}pp")
    if not shown:
        print("  (no efficient feature beats even per-feat p95 — nothing to show)")

    print("\nREAD: a feature is a real filter ONLY if it beats BEST-OF-N + holds train→test + is in the")
    print("PATH cluster (mag cluster = price/vol proxy, FINDINGS prior). Efficient cell is underpowered")
    print("(n≈92). Do NOT tune thresholds to a winner (manufactures an OOS-flip).")


if __name__ == "__main__":
    main()
