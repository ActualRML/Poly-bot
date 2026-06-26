"""ADVERSARIAL contrarian-run audit -- READ-ONLY ledger characterization.

NOT RUN BY CLAUDE (you run everything). Review, then run yourself:
    .venv/Scripts/python.exe research/audit_contrarian.py
Stdlib-only (sqlite3 + csv + math) on purpose, so it runs under ANY python --
no aiosqlite/venv dependency. Opens data/bot.db with mode=ro and NEVER writes.

Outcome source: the LIVE ledger is already API-clean -- the resolver settles
from the real Polymarket resolution, so positions.pnl_usdc SIGN is ground truth
(FINDINGS: 'the live resolver/ledger ... uses real API resolution; it was the
only clean dataset all along'). research/diagnostics/label_truth.csv is loaded
ONLY as a cross-check on the overlap (touch-only canon); disagreements print.

This CHARACTERIZES the run. It does NOT score the pre-registered test and does
NOT move the line. Section 4 reports a structural fact (NO-side actual vs
implied) -- that is gate (b)'s INPUT, not its pass/fail verdict.

Sections (each guarded -- one failure won't sink the rest):
  1  pre/post-line split (line = 2026-06-12 06:00 UTC, by positions.ts = OPEN time)
  2  time-concentration of realized PnL (resolve-hour + day Pareto)   [Q1]
  3  realized equity curve + max drawdown                            [Q1]
  4  side calibration on clean labels (YES vs NO actual-vs-implied)   [Q2 / gate-b INPUT]
  5  depth-at-entry + spread-true PnL for the last N trades           [Q4 fill reality]
  6  intra-hour realized spot vol, post-line vs pre-line              [Q1 chop test]
  7  loss-streak + correlated same-hour cluster model                 [Q5]
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

STRATEGY = "contrarian"
LINE = datetime(2026, 6, 12, 6, 0, 0, tzinfo=timezone.utc)  # pre-registered line
STARTING_BALANCE = 1000.0
SLIPPAGE_BUFFER = 0.03   # mirrors portfolio.py (hardcoded so this stays stdlib-only)
LAST_N = 30              # window for the fill-reality section


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _ts(s):
    return datetime.fromisoformat(s) if s else None


def _hour(dt):
    return dt.replace(minute=0, second=0, microsecond=0)


def _median(v):
    v = sorted(x for x in v if x == x)  # drop NaN
    n = len(v)
    if not n:
        return float("nan")
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def load_positions(conn):
    rows = conn.execute(
        """
        SELECT id, ts, market_id, symbol, side, entry_price, size_usdc,
               status, exit_price, pnl_usdc, resolved_ts, resolve_time, strategy
          FROM positions
         WHERE strategy = ?
         ORDER BY ts
        """,
        (STRATEGY,),
    ).fetchall()
    return [dict(r) for r in rows]


def resolved(rows):
    return [r for r in rows if r["status"] == "resolved" and r["pnl_usdc"] is not None]


def _resolve_hour(r):
    rt = _ts(r["resolve_time"]) or _ts(r["resolved_ts"])
    return _hour(rt) if rt else None


# --------------------------------------------------------------------------- #
# 1. pre/post-line split
# --------------------------------------------------------------------------- #
def section_split(rows):
    res = resolved(rows)
    pre = [r for r in res if _ts(r["ts"]) and _ts(r["ts"]) < LINE]
    post = [r for r in res if _ts(r["ts"]) and _ts(r["ts"]) >= LINE]
    print("=== 1. PRE/POST-LINE SPLIT (line 2026-06-12 06:00 UTC, by OPEN time) ===")

    def line(label, g):
        n = len(g)
        if not n:
            print(f"  {label:<10} (none)")
            return
        wins = sum(1 for r in g if r["pnl_usdc"] > 0)
        pnl = sum(r["pnl_usdc"] for r in g)
        avg_entry = sum(r["entry_price"] for r in g) / n
        be = min(avg_entry + SLIPPAGE_BUFFER, 1.0)  # breakeven WR == effective cost
        print(
            f"  {label:<10} n={n:<4} wins={wins:<4} WR={wins / n * 100:5.1f}%  "
            f"be@3c~{be * 100:4.1f}%  pnl={pnl:+10.2f}  avg_entry={avg_entry:.3f}"
        )

    line("ALL", res)
    line("pre-line", pre)
    line("post-line", post)
    print("  [characterization only -- NOT the pre-registered test score]\n")


# --------------------------------------------------------------------------- #
# 2. time-concentration of realized PnL
# --------------------------------------------------------------------------- #
def section_concentration(rows):
    res = resolved(rows)
    by_hour = defaultdict(float)
    by_day = defaultdict(float)
    for r in res:
        h = _resolve_hour(r)
        if h is None:
            continue
        by_hour[h] += r["pnl_usdc"]
        by_day[h.date()] += r["pnl_usdc"]

    total = sum(by_hour.values())
    pos_total = sum(v for v in by_hour.values() if v > 0)
    hours_sorted = sorted(by_hour.values(), reverse=True)
    top1 = hours_sorted[0] if hours_sorted else 0.0
    top5 = sum(hours_sorted[:5])

    print("=== 2. TIME-CONCENTRATION OF REALIZED PnL (Q1) ===")
    print(
        f"  resolved={len(res)}  distinct resolve-hours={len(by_hour)} "
        f"(effective n)  net pnl={total:+.2f}"
    )
    if total:
        print(f"  best hour      {top1:+10.2f}  ({top1 / total * 100:5.0f}% of NET)")
        print(f"  best 5 hours   {top5:+10.2f}  ({top5 / total * 100:5.0f}% of NET)")
    if pos_total:
        print(
            f"  best hour = {top1 / pos_total * 100:.0f}% of GROSS profit; "
            f"best 5 = {top5 / pos_total * 100:.0f}% of gross profit"
        )
    if by_day:
        bd = max(by_day.items(), key=lambda kv: kv[1])
        wd = min(by_day.items(), key=lambda kv: kv[1])
        print(f"  best day {bd[0]} {bd[1]:+.2f}    worst day {wd[0]} {wd[1]:+.2f}")
        print("  per-day net:")
        for d in sorted(by_day):
            print(f"    {d}  {by_day[d]:+10.2f}")
    print("  [few hours carrying most of NET = regime concentration, not breadth]\n")


# --------------------------------------------------------------------------- #
# 3. realized equity curve + max drawdown
# --------------------------------------------------------------------------- #
def section_equity(rows):
    res = sorted(resolved(rows), key=lambda r: r["resolved_ts"] or r["ts"])
    eq = peak = trough = STARTING_BALANCE
    maxdd = 0.0
    maxdd_at = None
    curve = []
    for r in res:
        eq += r["pnl_usdc"]
        peak = max(peak, eq)
        dd = (eq - peak) / peak if peak else 0.0
        if dd < maxdd:
            maxdd, maxdd_at, trough = dd, (r["resolved_ts"] or r["ts"]), eq
        curve.append((r["resolved_ts"] or r["ts"], eq))

    print("=== 3. REALIZED EQUITY CURVE + MAX DRAWDOWN (Q1) ===")
    print(f"  realized equity: start {STARTING_BALANCE:.2f} -> end {eq:.2f}")
    if maxdd_at:
        print(f"  MAX DRAWDOWN {maxdd * 100:.1f}%  trough~{trough:.2f}  at {maxdd_at}")
    if curve:
        step = max(1, len(curve) // 12)
        print(f"  waypoints (every ~{step}th close):")
        for tstr, e in curve[::step]:
            print(f"    {tstr}  {e:9.2f}")
    print("  [realized-only; open-position unrealized excluded]\n")


# --------------------------------------------------------------------------- #
# 4. side calibration on clean labels  (gate-b INPUT, not a score)
# --------------------------------------------------------------------------- #
def load_truth():
    truth = {}
    if not LABEL_TRUTH.exists():
        return truth
    with open(LABEL_TRUTH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            t = (row.get("truth") or "").strip().upper()
            if t in ("YES", "NO"):
                truth[row["market_id"]] = t
    return truth


def _calibration(label, g, truth):
    n = len(g)
    if not n:
        print(f"  [{label}] (no trades)")
        return
    checked = disagree = 0
    for r in g:
        if r["market_id"] in truth:
            checked += 1
            if (truth[r["market_id"]] == r["side"]) != (r["pnl_usdc"] > 0):
                disagree += 1
    print(
        f"  [{label}] label_truth overlap {checked}/{n}, {disagree} disagreements "
        f"(ledger is API-clean by construction)"
    )
    for side in ("YES", "NO"):
        s = [r for r in g if r["side"] == side]
        if not s:
            print(f"    {side}: (none)")
            continue
        m = len(s)
        wins = sum(1 for r in s if r["pnl_usdc"] > 0)
        implied = sum(r["entry_price"] for r in s) / m   # held-side price = implied prob
        actual = wins / m
        pnl = sum(r["pnl_usdc"] for r in s)
        print(
            f"    {side}: n={m:<3} actual={actual * 100:5.1f}%  implied={implied * 100:5.1f}%  "
            f"gap={(actual - implied) * 100:+5.1f}pt  pnl={pnl:+9.2f}"
        )


def section_calibration(rows):
    res = resolved(rows)
    truth = load_truth()
    post = [r for r in res if _ts(r["ts"]) and _ts(r["ts"]) >= LINE]
    print("=== 4. SIDE CALIBRATION ON CLEAN LABELS (Q2 / gate-b INPUT, NOT a score) ===")
    _calibration("ALL", res, truth)
    _calibration("POST-LINE", post, truth)
    print("  [gate (b) asks: is NO-side actual > NO-side implied? -- reported, NOT judged]")
    print("  [one-sided (YES-only) over-win = bull-drift fingerprint, not 2-sided reversion]\n")


# --------------------------------------------------------------------------- #
# 5. depth-at-entry + spread-true PnL (fill reality)
# --------------------------------------------------------------------------- #
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


def section_fill(conn, rows):
    res = resolved(rows)
    recent = sorted(res, key=lambda r: r["resolved_ts"] or r["ts"])[-LAST_N:]
    print(f"=== 5. FILL REALITY -- last {len(recent)} resolved (Q4) ===")
    print(
        "  Stored entry = YES BID. A YES taker pays the ASK (entry+spread); a NO taker's "
        "stored\n  cost already == NO ask (1-Yb). book-true models the SPREAD only; "
        "WALK/NO-FILL flag where\n  size exceeds captured liquidity (extra, unmodeled slippage on top).\n"
    )
    paper_sum = book_sum = 0.0
    have = walk = nofill = 0
    for r in recent:
        side, stake, entry, paper = r["side"], r["size_usdc"], r["entry_price"], r["pnl_usdc"]
        paper_sum += paper
        b = _book_at_entry(conn, r["market_id"], r["ts"])
        if b is None:
            book_sum += paper  # no book row -> can't adjust, carry paper
            print(f"  {side:<3} entry={entry:.3f}  (no book row captured near entry)")
            continue
        have += 1
        Yb, Ya = b["best_bid"], b["best_ask"]
        spread = (Ya - Yb) if (Ya is not None and Yb is not None) else None
        if side == "YES":
            eff = Ya if Ya is not None else min(entry + SLIPPAGE_BUFFER, 1.0)
            top, dep = b["ask_size"], b["ask_depth"]
        else:  # NO taker sells YES at the bid; NO ask == 1 - Yb
            eff = (1.0 - Yb) if Yb is not None else min(entry + SLIPPAGE_BUFFER, 1.0)
            top, dep = b["bid_size"], b["bid_depth"]
        eff = max(min(eff, 1.0), 1e-6)
        shares = stake / entry if entry else 0.0
        won = paper > 0
        book_pnl = (stake / eff - stake) if won else -stake
        book_sum += book_pnl
        fill = "ok"
        if top is not None and shares > top:
            fill, walk = "WALK", walk + 1
        if dep is not None and shares > dep:
            fill, nofill = "NO-FILL", nofill + 1
        sp = f"{spread * 100:.1f}c" if spread is not None else "-"
        ts_ = f"{top:.0f}" if top is not None else "-"
        dp_ = f"{dep:.0f}" if dep is not None else "-"
        print(
            f"  {side:<3} entry={entry:.3f} spread={sp:>5} shares={shares:6.0f} "
            f"top={ts_:>7} depth={dp_:>8} {fill:<7} {paper:+8.2f} -> {book_pnl:+8.2f}"
        )
    print(
        f"\n  TOTALS last {len(recent)}: paper={paper_sum:+.2f}  book-true(spread)={book_sum:+.2f}  "
        f"delta={book_sum - paper_sum:+.2f}"
    )
    print(f"  of {have} with captured book: WALK(top<size)={walk}  NO-FILL(depth<size)={nofill}")
    print("  [spread penalty falls on the YES side = where ALL the profit is]\n")


# --------------------------------------------------------------------------- #
# 6. intra-hour realized spot vol, post vs pre line
# --------------------------------------------------------------------------- #
def section_vol(conn):
    print("=== 6. INTRA-HOUR REALIZED SPOT VOL (post vs pre line) (Q1 chop test) ===")
    print("  realized vol = stdev of consecutive pct-changes within each UTC hour")
    print("  (binance stream is ~10s-throttled -> a RELATIVE comparison, not absolute)\n")
    for sym in ("BTC", "ETH", "BNB"):
        rows = conn.execute(
            """
            SELECT ts, price FROM snapshots
             WHERE source = 'binance' AND symbol = ? AND price IS NOT NULL
             ORDER BY ts
            """,
            (sym,),
        ).fetchall()
        buckets = defaultdict(list)
        for r in rows:
            dt = _ts(r["ts"])
            if dt:
                buckets[_hour(dt)].append((dt, float(r["price"])))
        pre_v, post_v = [], []
        for h, seq in buckets.items():
            if len(seq) < 6:
                continue
            seq.sort()
            rets = [(p1 - p0) / p0 for (_, p0), (_, p1) in zip(seq, seq[1:]) if p0]
            if len(rets) < 5:
                continue
            mu = sum(rets) / len(rets)
            rv = math.sqrt(sum((x - mu) ** 2 for x in rets) / (len(rets) - 1))
            (post_v if h >= LINE else pre_v).append(rv)
        print(
            f"  {sym}: pre-line  hours={len(pre_v):<4} med_rv={_median(pre_v):.2e}   "
            f"post-line hours={len(post_v):<4} med_rv={_median(post_v):.2e}"
        )
    print("  [post >> pre => 'choppier weather'; but check it against the DIRECTION in §4]\n")


# --------------------------------------------------------------------------- #
# 7. loss-streak + correlated same-hour cluster model
# --------------------------------------------------------------------------- #
def section_streaks(rows):
    res = sorted(resolved(rows), key=lambda r: r["resolved_ts"] or r["ts"])
    seq = [1 if r["pnl_usdc"] > 0 else 0 for r in res]
    n = len(seq)
    wins = sum(seq)
    wr = wins / n if n else 0.0
    q = 1 - wr
    longest = cur = 0
    for x in seq:
        cur = cur + 1 if x == 0 else 0
        longest = max(longest, cur)
    exp_run = math.log(n) / math.log(1 / q) if (n > 1 and 0 < q < 1) else float("nan")

    by_hour = defaultdict(lambda: [0, 0.0])
    for r in res:
        if r["pnl_usdc"] < 0:
            h = _resolve_hour(r)
            if h:
                by_hour[h][0] += 1
                by_hour[h][1] += r["pnl_usdc"]
    worst = min(by_hour.values(), key=lambda v: v[1]) if by_hour else [0, 0.0]

    print("=== 7. LOSS-STREAK + CORRELATED CLUSTER MODEL (Q5) ===")
    print(f"  n={n}  WR={wr * 100:.1f}%  (loss p={q:.3f})")
    print(f"  longest ACTUAL loss run = {longest}   expected longest run ~ {exp_run:.1f}")
    print(f"  worst same-hour loss CLUSTER: {worst[0]} losses in one resolve-hour, {worst[1]:+.2f}")
    print("  sequential 2%-rebet drawdown (independent-loss lower bound):")
    for N in (5, 8, 10, 15, 20):
        print(f"    {N:>2} straight full losses -> {(0.98 ** N - 1) * 100:+.1f}% of balance")
    print("  [positions OVERLAP in time -> correlated same-hour clusters, not")
    print("   sequences, are the real risk; they hit many open longshots at once]\n")


# --------------------------------------------------------------------------- #
def main():
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        return
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = load_positions(conn)
        print(
            f"contrarian positions: {len(rows)} total, {len(resolved(rows))} resolved  "
            f"(DB={DB_PATH})\n"
        )
        for fn, args in (
            (section_split, (rows,)),
            (section_concentration, (rows,)),
            (section_equity, (rows,)),
            (section_calibration, (rows,)),
            (section_fill, (conn, rows)),
            (section_vol, (conn,)),
            (section_streaks, (rows,)),
        ):
            try:
                fn(*args)
            except Exception as e:  # fail soft per section -- one bug won't sink the rest
                print(f"  [section {fn.__name__} error: {e!r}]\n")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
