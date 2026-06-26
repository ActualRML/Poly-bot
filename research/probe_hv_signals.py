"""READ-ONLY one-pass SIGNAL SEARCH for contrarian_hv (high-vol fade): of many
mechanistic entry signals, does ANY lift the fade's edge OUT OF SAMPLE? Disciplined,
not an infinite loop (that = curve-fitting): each signal gets a TRAIN/TEST split — the
best split is chosen on TRAIN, scored ONCE on TEST. A signal is "found" only if it
lifts the held-out TEST avg-ROI clearly above baseline AND makes mechanistic sense.
Testing ~7 signals inflates false positives, so the bar is OOS survival, not in-sample.

Metric = fade ROI per trade AT SIGNAL price ((1/entry−1) win, −1 loss) — accounts for
how cheap the entry is (raw WR doesn't). Fill cost is a separate later gate. Selection:
first extreme `book` event in high_vol, with the live floor (signal≥0.15) + 120s time
gate for fidelity. Outcome = official recovery.

Run on the indexed copy:  .venv/Scripts/python.exe research/probe_hv_signals.py
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO)); sys.path.insert(0, str(REPO / "research"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from src.backtest.recovery import recover_resolutions

DB = REPO / "data" / "bot_bt.db"
FLOOR, MIN_TTR, TRAIN_FRAC = 0.15, 120, 0.60


def _parse(s):
    return datetime.fromisoformat(s)


def main():
    VOL = sys.argv[1] if len(sys.argv) > 1 else "high_vol"   # 'high_vol' | 'mid_vol' | 'low_vol'
    print(f"=== SIGNAL SEARCH for vol_regime={VOL} (fade) ===")
    print("recovering...", flush=True)
    rec = recover_resolutions(DB).usable
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True); conn.row_factory = sqlite3.Row

    # preload binance prices per symbol once (avoid per-market queries)
    spot = {}
    for r in conn.execute("""SELECT symbol s, ts, price p FROM snapshots WHERE source='binance'
                              AND price IS NOT NULL ORDER BY ts"""):
        spot.setdefault(r["s"], []).append((r["ts"], float(r["p"])))

    def hour_stats(sym, lo_iso, hi_iso):
        arr = spot.get(sym)
        if not arr:
            return None
        ps = [p for t, p in arr if lo_iso <= t <= hi_iso]
        if len(ps) < 3:
            return None
        op = ps[0]
        crosses = sum(1 for i in range(1, len(ps)) if (ps[i-1]-op)*(ps[i]-op) < 0)
        path = sum(abs(ps[i]-ps[i-1]) for i in range(1, len(ps)))
        eff = abs(ps[-1]-op)/path if path > 0 else 0.0
        ret = (ps[-1]-op)/op if op else 0.0          # spot return since hour open
        return dict(crosses=crosses, eff=eff, ret=ret, absret=abs(ret))

    rows = conn.execute(
        """SELECT market_id m, ts, price p, symbol sym, price_zone z FROM snapshots
            WHERE source='polymarket' AND event_type='book' AND vol_regime=?
              AND price_zone IN ('extreme_low','extreme_high') AND price IS NOT NULL ORDER BY ts""",
        (VOL,)).fetchall()
    seen, data = set(), []
    for r in rows:
        if r["m"] in seen:
            continue
        res = rec.get(r["m"])
        if res is None or r["sym"] is None:
            continue
        seen.add(r["m"])
        side = "YES" if r["z"] == "extreme_low" else "NO"
        entry = float(r["p"]) if side == "YES" else 1.0 - float(r["p"])
        if entry < FLOOR or (res.resolve_ts - _parse(r["ts"])).total_seconds() < MIN_TTR:
            continue
        hs = hour_stats(r["sym"], (res.resolve_ts - timedelta(hours=1)).isoformat(), r["ts"])
        if hs is None:
            continue
        won = res.outcome == side
        roi = (1.0/entry - 1.0) if won else -1.0
        data.append(dict(ts=r["ts"], roi=roi, won=won, sym=r["sym"], side=side,
                         entry=entry, ttr=(res.resolve_ts-_parse(r["ts"])).total_seconds()/60,
                         **hs))
    data.sort(key=lambda d: d["ts"])
    n = len(data); k = int(n*TRAIN_FRAC); tr, te = data[:k], data[k:]
    base_tr = sum(d["roi"] for d in tr)/len(tr)
    base_te = sum(d["roi"] for d in te)/len(te)
    print(f"\nn={n} (train {len(tr)} / test {len(te)})  baseline avg-ROI: train {base_tr:+.3f}  TEST {base_te:+.3f}")
    print("(positive avg-ROI = fade has edge at signal price; fills erode it later)\n")

    # each signal: candidate binary splits; pick best-train subset, score its TEST avg-ROI
    def num_splits(key, cuts):
        return [(f"{key}<={c}", (lambda d, c=c: d[key] <= c)) for c in cuts] + \
               [(f"{key}>{c}",  (lambda d, c=c: d[key] >  c)) for c in cuts]
    SIGS = {
        "symbol":     [(f"sym={s}", (lambda d, s=s: d["sym"] == s)) for s in ("BTC","ETH","BNB","SOL","XRP","DOGE")],
        "side":       [("side=YES", lambda d: d["side"]=="YES"), ("side=NO", lambda d: d["side"]=="NO")],
        "entry":      num_splits("entry", [0.16, 0.18, 0.20, 0.25]),
        "ttr_min":    num_splits("ttr", [10, 20, 30, 45]),
        "crosses":    num_splits("crosses", [1, 3, 5, 8]),
        "trend_eff":  num_splits("eff", [0.05, 0.10, 0.20]),
        "spot_absret":num_splits("absret", [0.001, 0.002, 0.004]),
    }
    print(f"  {'signal':>12} {'best-train split':>22} {'train_ROI':>10} {'TEST_ROI':>9} {'test_n':>7}")
    flags = []
    for name, splits in SIGS.items():
        best = None
        for lbl, fn in splits:
            sub = [d["roi"] for d in tr if fn(d)]
            if len(sub) < 15:
                continue
            m = sum(sub)/len(sub)
            if best is None or m > best[1]:
                best = (lbl, m, fn)
        if best is None:
            continue
        lbl, m_tr, fn = best
        sub_te = [d["roi"] for d in te if fn(d)]
        m_te = sum(sub_te)/len(sub_te) if sub_te else 0.0
        lift = m_te - base_te
        tag = "  <== beats baseline OOS" if (lift > 0.05 and len(sub_te) >= 15) else ""
        print(f"  {name:>12} {lbl:>22} {m_tr:>+10.3f} {m_te:>+9.3f} {len(sub_te):>7}{tag}")
        if tag:
            flags.append((name, lbl, m_te, len(sub_te)))
    print("\n" + "="*84)
    if flags:
        print("OOS SURVIVORS (re-check for mechanism + n before believing; many tested = false-pos risk):")
        for name, lbl, m, nn in flags:
            print(f"  {name}: {lbl}  TEST avg-ROI {m:+.3f}  n={nn}")
    else:
        print("NO signal beats baseline OOS. hv fade has no findable entry edge — looping more = noise.")
    print("="*84)
    conn.close()


if __name__ == "__main__":
    main()
