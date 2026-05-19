"""
Phase 3 Thesis Validation: Hourly Contrarian
Read-only. DB: data/bot_database.db, table: positions.
"""

import csv
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.utils.parsing import detect_symbol_from_question

DB_PATH = ROOT / "data" / "bot_database.db"
CSV_OUT = Path(__file__).parent / "output" / "phase3_thesis_analysis.csv"

SQL = """
SELECT id, question, outcome, entry_price, exit_price, pnl_usdc, exit_reason,
       strategy_mode, sym_m15m, sym_m5m, sym_m30m, vol_ratio, btc_m15m,
       scout_score, mtf_aligned, entry_time, exit_time, kelly_fraction
FROM positions
WHERE strategy_mode LIKE 'updown_hourly%'
  AND status='closed'
ORDER BY entry_time ASC
"""


def _safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def classify(row):
    m = _safe_float(row["sym_m15m"])
    if m is None:
        sign = "NULL"
    elif m > 0:
        sign = "+"
    elif m < 0:
        sign = "-"
    else:
        sign = "0"

    pnl = _safe_float(row["pnl_usdc"])
    is_win = (pnl is not None) and (pnl > 0)
    outcome = row["outcome"] or ""

    if sign in ("+", "-"):
        contrarian = (sign == "+" and outcome == "Down") or (sign == "-" and outcome == "Up")
        thesis_type = "contrarian" if contrarian else "momentum_follow"
    else:
        thesis_type = "neutral"

    if outcome == "Up":
        actual_candle = "Up" if is_win else "Down"
    else:
        actual_candle = "Down" if is_win else "Up"

    hyp_flip_win = not is_win

    return {
        "symbol": detect_symbol_from_question(row["question"] or ""),
        "momentum_sign": sign,
        "thesis_type": thesis_type,
        "actual_candle_outcome": actual_candle,
        "hyp_flip_win": hyp_flip_win,
        "is_win": is_win,
    }


def wr(wins, total):
    if total == 0:
        return "N/A"
    return f"{wins / total * 100:.1f}%"


def bucket_vol(v):
    if v is None:
        return "NULL"
    v = float(v)
    if v < 0.8:
        return "low(<0.8)"
    if v <= 1.2:
        return "med(0.8-1.2)"
    return "high(>1.2)"


def bucket_regime(v):
    if v is None:
        return "NULL"
    v = int(v)
    if v >= 3:
        return "3+"
    return str(v)


def main():
    assert DB_PATH.exists(), f"DB not found: {DB_PATH}"
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute(SQL)
    raw_rows = cur.fetchall()
    con.close()

    rows = []
    for r in raw_rows:
        d = dict(r)
        d.update(classify(d))
        rows.append(d)

    total = len(rows)
    wins_total = sum(1 for r in rows if r["is_win"])

    print("=" * 55)
    print("  PHASE 3: HOURLY CONTRARIAN THESIS VALIDATION")
    print("=" * 55)

    if total < 30:
        print(f"  WARNING: only {total} trades (need >=30 for significance)")
    print()

    print(f"[OVERALL]")
    print(f"  Total: {total} | Wins: {wins_total} | Losses: {total - wins_total} | WR: {wr(wins_total, total)}")
    print()

    print("[BY MOMENTUM SIGN (sym_m15m)]")
    for sign_label, sign_key in [("momentum > 0", "+"), ("momentum < 0", "-"), ("zero/NULL", ("0", "NULL"))]:
        if isinstance(sign_key, tuple):
            sub = [r for r in rows if r["momentum_sign"] in sign_key]
        else:
            sub = [r for r in rows if r["momentum_sign"] == sign_key]
        w = sum(1 for r in sub if r["is_win"])
        print(f"  {sign_label:18s}: {len(sub):3d} trades | {w} wins | WR {wr(w, len(sub))}")
    print()

    print("[BY BOT DIRECTION]")
    for direction in ("Up", "Down"):
        sub = [r for r in rows if r["outcome"] == direction]
        w = sum(1 for r in sub if r["is_win"])
        print(f"  {direction} picks: {len(sub):3d} | {w} wins | WR {wr(w, len(sub))}")
    print()

    contrarian_rows = [r for r in rows if r["thesis_type"] == "contrarian"]
    momfollow_rows = [r for r in rows if r["thesis_type"] == "momentum_follow"]
    c_wins = sum(1 for r in contrarian_rows if r["is_win"])
    c_flip_wins = sum(1 for r in contrarian_rows if r["hyp_flip_win"])

    print("[COUNTER-THESIS CHECK]")
    print(f"  Contrarian trades:              {len(contrarian_rows)}")
    print(f"  Actual contrarian WR:           {c_wins} / {len(contrarian_rows)} = {wr(c_wins, len(contrarian_rows))}")
    print(f"  Hypothetical momentum-follow WR: {c_flip_wins} / {len(contrarian_rows)} = {wr(c_flip_wins, len(contrarian_rows))}")
    if len(contrarian_rows) > 0:
        flip_pct = c_flip_wins / len(contrarian_rows)
        actual_pct = c_wins / len(contrarian_rows)
        if flip_pct > actual_pct + 0.10:
            verdict = "THESIS FAILS: flipping direction would outperform by {:.0f}pp".format((flip_pct - actual_pct) * 100)
        elif actual_pct > flip_pct + 0.10:
            verdict = "THESIS HOLDS: contrarian outperforms flip by {:.0f}pp".format((actual_pct - flip_pct) * 100)
        else:
            verdict = "INCONCLUSIVE: difference < 10pp (small sample or no edge either way)"
        print(f"  -> {verdict}")
    if momfollow_rows:
        mf_wins = sum(1 for r in momfollow_rows if r["is_win"])
        print(f"  Momentum-follow trades (non-contrarian): {len(momfollow_rows)} | WR {wr(mf_wins, len(momfollow_rows))}")
    print()

    print("[BY SYMBOL]")
    sym_map = defaultdict(list)
    for r in rows:
        sym_map[r["symbol"]].append(r)
    for sym in sorted(sym_map):
        sub = sym_map[sym]
        w = sum(1 for r in sub if r["is_win"])
        print(f"  {sym:6s}: {len(sub):3d} | {w} wins | WR {wr(w, len(sub))}")
    print()

    print("[BY VOL_RATIO BUCKET]")
    vol_map = defaultdict(list)
    for r in rows:
        vol_map[bucket_vol(r["vol_ratio"])].append(r)
    for bkt in ["low(<0.8)", "med(0.8-1.2)", "high(>1.2)", "NULL"]:
        sub = vol_map.get(bkt, [])
        if not sub:
            continue
        w = sum(1 for r in sub if r["is_win"])
        print(f"  {bkt:16s}: {len(sub):3d} | {w} wins | WR {wr(w, len(sub))}")
    print()

    print("[BY REGIME_SCORE]")
    reg_map = defaultdict(list)
    for r in rows:
        reg_map[bucket_regime(r["scout_score"])].append(r)
    for bkt in ["0", "1", "2", "3+", "NULL"]:
        sub = reg_map.get(bkt, [])
        if not sub:
            continue
        w = sum(1 for r in sub if r["is_win"])
        print(f"  score {bkt}: {len(sub):3d} | {w} wins | WR {wr(w, len(sub))}")
    print()

    print("[EXIT REASONS]")
    exit_map = defaultdict(int)
    for r in rows:
        exit_map[r["exit_reason"] or "NULL"] += 1
    for reason, count in sorted(exit_map.items(), key=lambda x: -x[1]):
        print(f"  {reason}: {count}")
    print()

    CSV_OUT.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "id", "question", "outcome", "entry_price", "exit_price", "pnl_usdc",
        "exit_reason", "strategy_mode", "sym_m15m", "sym_m5m", "sym_m30m",
        "vol_ratio", "btc_m15m", "scout_score", "mtf_aligned", "entry_time",
        "exit_time", "kelly_fraction",
        "symbol", "momentum_sign", "thesis_type", "actual_candle_outcome",
        "hyp_flip_win", "is_win",
    ]
    with open(CSV_OUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"CSV -> {CSV_OUT}")


if __name__ == "__main__":
    main()
