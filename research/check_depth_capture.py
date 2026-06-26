"""READ-ONLY check that the depth-capture migration activated after the bot restart.

Confirms the snapshots table gained bid_size/ask_size/bid_depth/ask_depth AND that fresh
`book` events are actually being populated. Stdlib sqlite3 only (no venv/aiosqlite needed);
opens data/bot.db with mode=ro and NEVER writes. Console output only.

    python research/check_depth_capture.py

Exact names verified against src/data/schema.py + src/data/parsers.py:
  table=snapshots, discriminator=event_type (book => event_type='book', source='polymarket'),
  timestamp=ts (ISO-8601 UTC), depth cols = bid_size, ask_size, bid_depth, ask_depth.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DB = REPO_ROOT / "data" / "bot.db"
DEPTH_COLS = ("bid_size", "ask_size", "bid_depth", "ask_depth")
BOOK = "source = 'polymarket' AND event_type = 'book'"
LIVENESS_WARN_S = 300          # newest book row older than 5 min -> WARN
POP_SAMPLE = 200               # how many recent book rows to inspect
POP_FAIL_PCT = 10.0            # below this populated -> FAIL (parser not writing depth)
POP_WARN_PCT = 90.0            # below this populated -> WARN (stragglers / partial)

_ORDER = {"PASS": 0, "WARN": 1, "FAIL": 2}


def _fmt(v) -> str:
    return "NULL" if v is None else f"{v:.2f}"


def _age(iso: str) -> tuple[float, str]:
    ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    secs = (datetime.now(timezone.utc) - ts).total_seconds()
    return secs, (f"{secs:.0f}s" if secs < 120 else f"{secs / 60:.1f} min")


def main() -> None:
    detail: list[str] = []
    verdict = "PASS"

    def esc(level: str) -> None:
        nonlocal verdict
        if _ORDER[level] > _ORDER[verdict]:
            verdict = level

    if not DB.exists():
        print(f"VERDICT: FAIL -- depth-capture check: DB not found at {DB}")
        return

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # 1. SCHEMA -------------------------------------------------------------
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(snapshots)")]
        missing = [c for c in DEPTH_COLS if c not in cols]
        have_cols = not missing
        if missing:
            esc("FAIL")
            detail.append(f"1. SCHEMA    : FAIL -- missing depth columns {missing} "
                          f"(migration did not run; restart didn't take / schema path broken)")
        else:
            detail.append(f"1. SCHEMA    : OK -- all 4 depth columns present {list(DEPTH_COLS)}")

        # 2. LIVENESS -----------------------------------------------------------
        newest = conn.execute(f"SELECT MAX(ts) AS m FROM snapshots WHERE {BOOK}").fetchone()["m"]
        if newest is None:
            esc("WARN")
            detail.append("2. LIVENESS  : WARN -- no book rows at all (bot not writing book events?)")
        else:
            secs, human = _age(newest)
            if secs > LIVENESS_WARN_S:
                esc("WARN")
                detail.append(f"2. LIVENESS  : WARN -- newest book row is {human} old "
                              f"(> {LIVENESS_WARN_S / 60:.0f} min; bot may have stopped writing)")
            else:
                detail.append(f"2. LIVENESS  : OK -- newest book row {human} old (ts={newest})")

        # 3. POPULATION (book only, most recent ~200) --------------------------
        pop_rows = []
        if have_cols:
            pop_rows = conn.execute(
                f"SELECT {', '.join(DEPTH_COLS)} FROM snapshots WHERE {BOOK} "
                f"ORDER BY ts DESC LIMIT {POP_SAMPLE}"
            ).fetchall()
        if not have_cols:
            detail.append("3. POPULATION: SKIP -- depth columns missing (see SCHEMA)")
        elif not pop_rows:
            esc("WARN")
            detail.append("3. POPULATION: WARN -- no book rows to sample")
        else:
            full = sum(1 for r in pop_rows if all(r[c] is not None for c in DEPTH_COLS))
            pct = full / len(pop_rows) * 100
            if pct < POP_FAIL_PCT:
                esc("FAIL")
                tag, note = "FAIL", " -- columns exist but parser is NOT populating depth"
            elif pct < POP_WARN_PCT:
                esc("WARN")
                tag, note = "WARN", " -- partial (ok only if pre-restart rows are still in the window)"
            else:
                tag, note = "OK", ""
            detail.append(f"3. POPULATION: {tag} -- {full}/{len(pop_rows)} recent book rows fully "
                          f"populated ({pct:.1f}%){note}")

        # 4. SAMPLE -------------------------------------------------------------
        if have_cols:
            sample = conn.execute(
                f"SELECT ts, {', '.join(DEPTH_COLS)} FROM snapshots WHERE {BOOK} "
                f"ORDER BY ts DESC LIMIT 5"
            ).fetchall()
            detail.append("4. SAMPLE    : 5 newest book rows  [ts | bid_size ask_size bid_depth ask_depth]")
            if not sample:
                detail.append("               (none)")
            for r in sample:
                detail.append(f"               {r['ts']} | {_fmt(r['bid_size'])} {_fmt(r['ask_size'])} "
                              f"{_fmt(r['bid_depth'])} {_fmt(r['ask_depth'])}")
        else:
            detail.append("4. SAMPLE    : SKIP -- depth columns missing")

        # 5. SANITY (soft -- flag, never crash) --------------------------------
        if have_cols and pop_rows:
            viol = {"bid_size<=0": 0, "ask_size<=0": 0,
                    "bid_depth<bid_size": 0, "ask_depth<ask_size": 0}
            checked = 0
            for r in pop_rows:
                if any(r[c] is None for c in DEPTH_COLS):
                    continue
                checked += 1
                if r["bid_size"] <= 0:
                    viol["bid_size<=0"] += 1
                if r["ask_size"] <= 0:
                    viol["ask_size<=0"] += 1
                if r["bid_depth"] < r["bid_size"]:
                    viol["bid_depth<bid_size"] += 1
                if r["ask_depth"] < r["ask_size"]:
                    viol["ask_depth<ask_size"] += 1
            total = sum(viol.values())
            if total:
                esc("WARN")
                hits = ", ".join(f"{k}={v}" for k, v in viol.items() if v)
                detail.append(f"5. SANITY    : WARN -- {total} violations across {checked} populated rows ({hits})")
            else:
                detail.append(f"5. SANITY    : OK -- {checked} populated rows, no violations "
                              f"(sizes > 0, per-side depth >= top-of-book size)")
        else:
            detail.append("5. SANITY    : SKIP -- no populated rows to check")
    finally:
        conn.close()

    print(f"VERDICT: {verdict} -- depth-capture verification (data/bot.db)")
    print("-" * 78)
    for line in detail:
        print(line)


if __name__ == "__main__":
    main()
