"""READ-ONLY diagnostic: are one-sided depth rows REAL (empty book side) or a CAPTURE BUG?

check_depth_capture.py flagged book/depth rows where only ONE side's size+depth is
populated (the other side's two columns NULL), and that share is growing today (the
fully-populated share fell from ~100% to ~71%). This script decides whether that is
genuine -- a book side that is really empty at that instant -- or a capture bug -- a
non-book event leaking values into the depth columns, or a book-path bug dropping one side.

READ-ONLY: opens data/bot.db with mode=ro via stdlib sqlite3, issues only SELECTs (no temp
tables, no writes), prints to the console. Repo root is the working dir.

    python research/diagnose_oneside_book.py

WHY event_type is the discriminator (verified against src/data/parsers.py +
src/data/schema.py -- NOT guessed):

    snapshots.event_type (TEXT NOT NULL) is the per-row event discriminator. In
    parse_polymarket():
        event_type = raw.get("event_type") or raw.get("type") or "unknown"
        ...
        if event_type == "book":
            best_bid, bid_size, bid_depth = _book_side(raw.get("bids"), is_bid=True)
            best_ask, ask_size, ask_depth = _book_side(raw.get("asks"), is_bid=False)

    The four depth columns (bid_size, ask_size, bid_depth, ask_depth) are populated in the
    `event_type == "book"` branch ONLY. price_change / last_trade_price / unknown
    (polymarket) and ticker (parse_binance) never touch them; the writer (_to_row) maps
    fields straight to columns with no carry-forward state. THEREFORE any non-book row with
    a non-NULL depth column is impossible under the parser => a capture leak. That is THE
    decisive test (BY EVENT TYPE below). `source='polymarket' AND event_type='book'` is the
    only path that may legitimately produce a one-sided row: _book_side returns
    (None, None, None) for an empty bids/asks array, so a genuinely empty side yields its
    two columns NULL while the other side is set.

Other exact names used: timestamp = ts (ISO-8601 UTC, '...+00:00'); coin = symbol
(BTC/ETH/SOL/XRP/DOGE/BNB or NULL); YES-perspective price = price; feed = source.

Resolution time for the time-to-resolution split is recovered offline EXACTLY like
src/backtest/recovery.py: round_to_hour(MAX(ts) per market_id). No network, no writes.
"""
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DB = REPO_ROOT / "data" / "bot.db"

DEPTH_COLS = ("bid_size", "ask_size", "bid_depth", "ask_depth")
CLASSES = ("two_sided", "one_sided", "no_depth", "malformed")
BOOK = "source = 'polymarket' AND event_type = 'book'"   # the only legit depth path

DEEP = ("BTC", "ETH", "SOL")     # liquid books -- an empty side is implausible -> leans BUG
THIN = ("DOGE", "BNB")           # illiquid books -- a real empty side is plausible -> leans REAL
SYMBOL_ORDER = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB")

# Single source of truth for the 4-way classification, expressed in SQL so EVERY breakdown
# and both sample queries share ONE definition (no Python re-implementation to drift).
# Mirrors the spec exactly:
#   two_sided : all 4 depth cols non-null
#   one_sided : exactly one side's (size, depth) set, the OTHER side's two cols both null
#   no_depth  : all 4 null
#   malformed : any other null pattern (e.g. size set but its matching depth null)
CLASS_CASE = """
CASE
  WHEN bid_size IS NOT NULL AND bid_depth IS NOT NULL
   AND ask_size IS NOT NULL AND ask_depth IS NOT NULL THEN 'two_sided'
  WHEN bid_size IS NULL AND bid_depth IS NULL
   AND ask_size IS NULL AND ask_depth IS NULL THEN 'no_depth'
  WHEN (bid_size IS NOT NULL AND bid_depth IS NOT NULL
        AND ask_size IS NULL AND ask_depth IS NULL)
    OR (ask_size IS NOT NULL AND ask_depth IS NOT NULL
        AND bid_size IS NULL AND bid_depth IS NULL) THEN 'one_sided'
  ELSE 'malformed'
END
""".strip()

TTR_ORDER = ("<=0 (post)", "0-5m", "5-15m", "15-30m", "30-60m", ">60m", "unknown")
TTR_NEAR = ("<=0 (post)", "0-5m", "5-15m")          # legit book thinning sits here if REAL
TTR_FAR = ("15-30m", "30-60m", ">60m")


# --- offline resolution recovery (identical method to src/backtest/recovery.py) ----------
def round_to_hour(ts: datetime) -> datetime:
    """Nearest hour boundary (>=30 min ceils, else floors) -- the :00 a market settled on."""
    floored = ts.replace(minute=0, second=0, microsecond=0)
    return floored + timedelta(hours=1) if ts.minute >= 30 else floored


def parse_ts(s):
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def ttr_bucket(minutes: float) -> str:
    if minutes <= 0:
        return "<=0 (post)"
    if minutes <= 5:
        return "0-5m"
    if minutes <= 15:
        return "5-15m"
    if minutes <= 30:
        return "15-30m"
    if minutes <= 60:
        return "30-60m"
    return ">60m"


# --- query + formatting helpers -----------------------------------------------------------
def breakdown(conn, group_expr, where=None):
    """{group_value: {class: count}} for the 4-way split, grouped by `group_expr`."""
    sql = f"SELECT {group_expr} AS g, {CLASS_CASE} AS cls, COUNT(*) AS n FROM snapshots"
    if where:
        sql += f" WHERE {where}"
    sql += " GROUP BY g, cls"
    data = defaultdict(lambda: defaultdict(int))
    for g, cls, n in conn.execute(sql):
        data[g][cls] += n
    return data


def total(row):
    return sum(row.values())


def fmt_table(out, data, key_order, key_label):
    """Append a fixed-width table: one row per key, four class cells as 'count pct%'."""
    out.append(f"  {key_label:<22}{'N':>9}   " + "  ".join(f"{c:>12}" for c in CLASSES))
    if not key_order:
        out.append("  (no rows)")
        return
    for k in key_order:
        row = data.get(k, {})
        n_tot = total(row)
        cells = []
        for c in CLASSES:
            n = row.get(c, 0)
            pct = (n / n_tot * 100) if n_tot else 0.0
            cells.append(f"{n:>6} {pct:4.0f}%")
        label = "(none)" if k is None else str(k)
        out.append(f"  {label:<22}{n_tot:>9}   " + "  ".join(c for c in cells))


def fnum(v, prec=None):
    if v is None:
        return "NULL"
    return f"{v:.{prec}f}" if prec is not None else f"{v:g}"


# --- main ---------------------------------------------------------------------------------
def main() -> None:
    if not DB.exists():
        print(f"VERDICT: INCONCLUSIVE -- DB not found at {DB} (nothing to diagnose).")
        return

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    out: list[str] = []
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "snapshots" not in tables:
            print("VERDICT: INCONCLUSIVE -- no `snapshots` table in this DB.")
            return
        cols = {r[1] for r in conn.execute("PRAGMA table_info(snapshots)")}
        missing = [c for c in DEPTH_COLS if c not in cols]
        if missing:
            print(f"VERDICT: INCONCLUSIVE -- depth columns missing {missing}; the "
                  f"capture migration has not run on this DB, so there is no depth to "
                  f"classify. Restart the bot (schema.py ALTER-adds them on startup).")
            return

        # ----- GLOBAL (every row, then the book-only subset that can carry depth) ---------
        g_all = breakdown(conn, "'ALL'")
        g_book = breakdown(conn, "'BOOK'", where=BOOK)
        out.append("1. GLOBAL CLASSIFICATION")
        out.append("   every snapshot row:")
        fmt_table(out, g_all, ["ALL"], "scope")
        out.append("   book rows only (the ONLY rows the parser ever writes depth into):")
        fmt_table(out, g_book, ["BOOK"], "scope")
        malformed_total = g_all.get("ALL", {}).get("malformed", 0)

        # ----- BY EVENT TYPE -- THE decisive test ----------------------------------------
        evt = breakdown(conn, "source || '/' || event_type")
        evt_keys = sorted(
            evt, key=lambda k: (0 if k == "polymarket/book"
                                else 1 if str(k).startswith("polymarket/") else 2, str(k)))
        out.append("")
        out.append("2. BY EVENT TYPE  <-- decisive: depth can ONLY come from polymarket/book.")
        out.append("   Any one_sided (or any depth) on a NON-book row is impossible per the")
        out.append("   parser => a capture leak.")
        fmt_table(out, evt, evt_keys, "source/event_type")

        # ----- OVER TIME (book-only, hourly) ---------------------------------------------
        over = breakdown(conn, "substr(ts,1,13)", where=BOOK)
        over_keys = sorted(over)
        out.append("")
        out.append("3. OVER TIME (book rows only, by hour UTC) -- when did one_sided start "
                   "/ is it growing?")
        fmt_table(out, over, over_keys, "hour")

        # ----- BY SYMBOL (book-only) -----------------------------------------------------
        sym = breakdown(conn, "symbol", where=BOOK)
        present = [s for s in SYMBOL_ORDER if s in sym]
        extras = sorted((k for k in sym if k not in SYMBOL_ORDER), key=lambda x: (x is None, str(x)))
        sym_keys = present + extras
        out.append("")
        out.append("4. BY SYMBOL (book rows only) -- deep BTC/ETH/SOL vs thin DOGE/BNB.")
        out.append("   one_sided concentrated in thin coins => plausibly REAL; uniform across")
        out.append("   deep coins too => implausible empty side => leans BUG.")
        fmt_table(out, sym, sym_keys, "symbol")

        # ----- BY TIME-TO-RESOLUTION (book-only) -----------------------------------------
        resolve = {}
        for mid, last in conn.execute(
            "SELECT market_id, MAX(ts) AS m FROM snapshots "
            "WHERE source='polymarket' AND market_id IS NOT NULL GROUP BY market_id"):
            dt = parse_ts(last)
            if dt is not None:
                resolve[mid] = round_to_hour(dt)
        ttr = defaultdict(lambda: defaultdict(int))
        for mid, ts, cls in conn.execute(
                f"SELECT market_id, ts, {CLASS_CASE} AS cls FROM snapshots WHERE {BOOK}"):
            r = resolve.get(mid)
            t = parse_ts(ts)
            if r is None or t is None:
                bucket = "unknown"
            else:
                bucket = ttr_bucket((r - t).total_seconds() / 60.0)
            ttr[bucket][cls] += 1
        ttr_keys = [b for b in TTR_ORDER if b in ttr]
        out.append("")
        out.append("5. BY TIME-TO-RESOLUTION (book rows only; resolve_ts = round-to-hour of")
        out.append("   last ts per market, same recovery as src/backtest/recovery.py).")
        out.append("   one_sided clustered near resolution => legit thinning (REAL); spread")
        out.append("   across all phases => leans BUG.")
        fmt_table(out, ttr, ttr_keys, "time-to-resolve")

        # ----- SAMPLES (across ALL rows, newest first, so a non-book leak is visible) -----
        def sample(cls, limit):
            return conn.execute(
                f"SELECT ts, source, event_type, symbol, price, "
                f"bid_size, ask_size, bid_depth, ask_depth FROM snapshots "
                f"WHERE ({CLASS_CASE}) = ? ORDER BY ts DESC LIMIT ?", (cls, limit)).fetchall()

        def render_samples(title, rows):
            out.append(title)
            out.append(f"     {'ts':<32} {'src/event':<26} {'sym':<5} {'yes':>6}  "
                       f"{'bidsz':>8} {'asksz':>8} {'biddp':>8} {'askdp':>8}")
            if not rows:
                out.append("     (none)")
                return
            for ts, src, et, symbol, price, bs, asz, bd, ad in rows:
                out.append(
                    f"     {str(ts):<32} {f'{src}/{et}':<26} {str(symbol or '-'):<5} "
                    f"{fnum(price, 3):>6}  {fnum(bs):>8} {fnum(asz):>8} "
                    f"{fnum(bd):>8} {fnum(ad):>8}")

        out.append("")
        out.append("6. SAMPLES")
        render_samples("   8 newest one_sided rows:", sample("one_sided", 8))
        render_samples("   4 newest malformed rows:", sample("malformed", 4))

        # ----- VERDICT SIGNALS -----------------------------------------------------------
        book_os = evt.get("polymarket/book", {}).get("one_sided", 0)
        nonbook_os = sum(v.get("one_sided", 0) for k, v in evt.items() if k != "polymarket/book")
        os_total = book_os + nonbook_os
        nonbook_share = nonbook_os / os_total if os_total else 0.0
        nonbook_types = sorted(
            ((k, v.get("one_sided", 0)) for k, v in evt.items()
             if k != "polymarket/book" and v.get("one_sided", 0) > 0),
            key=lambda x: -x[1])

        def rate(symbols):
            os_ = sum(sym.get(s, {}).get("one_sided", 0) for s in symbols)
            tot = sum(total(sym.get(s, {})) for s in symbols)
            return os_, tot, (os_ / tot if tot else 0.0)

        deep_os, deep_tot, deep_rate = rate(DEEP)
        thin_os, thin_tot, thin_rate = rate(THIN)
        near = sum(ttr.get(b, {}).get("one_sided", 0) for b in TTR_NEAR)
        far = sum(ttr.get(b, {}).get("one_sided", 0) for b in TTR_FAR)
        near_share = near / (near + far) if (near + far) else 0.0

        # ----- DECIDE --------------------------------------------------------------------
        if os_total == 0:
            verdict, why = "INCONCLUSIVE", (
                "no one_sided rows found. Either the bot has not written book rows since the "
                "change, or this is the wrong DB -- nothing to diagnose yet.")
        elif nonbook_os > 0:
            # Decisive: depth on a non-book row cannot be produced by the parser.
            types = ", ".join(f"{k}({n})" for k, n in nonbook_types[:4])
            verdict, why = "LIKELY_BUG", (
                f"{nonbook_os} of {os_total} one_sided rows ({nonbook_share:.0%}) carry a "
                f"NON-book event type [{types}], which the parser can NEVER populate with "
                f"depth -- proof of a capture leak. Depth captured since the change is "
                f"partly corrupt; fix the capture path. (Book one_sided rows may ALSO include "
                f"real empty sides, but the leak is the headline.)")
        elif os_total < 10:
            verdict, why = "INCONCLUSIVE", (
                f"all {os_total} one_sided rows are book events (not a non-book leak), but "
                f"that is too few to judge real-vs-bug. Keep logging and re-run.")
        else:
            deep_meaningful = deep_os >= 20 and (deep_os / os_total) >= 0.20
            deep_like_thin = thin_rate == 0 or deep_rate >= 0.5 * thin_rate
            thin_concentrated = thin_os >= 20 and thin_rate >= 2 * max(deep_rate, 1e-9)
            if deep_meaningful and deep_like_thin and near_share < 0.60:
                verdict, why = "LIKELY_BUG", (
                    f"one_sided rows are book events but appear in DEEP books "
                    f"(BTC/ETH/SOL: {deep_os} rows = {deep_rate:.1%} of their book rows, vs "
                    f"thin {thin_rate:.1%}) and across all phases (only {near_share:.0%} near "
                    f"resolution). Deep books don't legitimately go one-sided, so a book-path "
                    f"bug is intermittently dropping one side -- depth since the change is "
                    f"partly corrupt.")
            elif thin_concentrated or near_share >= 0.70:
                bits = []
                if thin_concentrated:
                    bits.append(f"concentrated in thin coins (DOGE/BNB {thin_rate:.1%} vs "
                                f"deep {deep_rate:.1%})")
                if near_share >= 0.70:
                    bits.append(f"{near_share:.0%} within 15m of resolution")
                verdict, why = "LIKELY_REAL", (
                    "all one_sided rows are book events and " + " and ".join(bits) +
                    " -- a book side that genuinely empties. Safe to handle by FILTERING "
                    "these rows (treat the missing side as no-quote), not by repairing data.")
            else:
                verdict, why = "INCONCLUSIVE", (
                    f"all one_sided rows are book events (good -- not a non-book leak), but "
                    f"symbol/TTR signals are mixed: deep {deep_os} ({deep_rate:.1%}) vs thin "
                    f"{thin_os} ({thin_rate:.1%}), {near_share:.0%} near resolution. To "
                    f"resolve, pull the raw `bids`/`asks` arrays for a few deep-symbol "
                    f"one_sided rows: if the array is truly [] it's REAL; if it has levels "
                    f"but parsing dropped them it's a BUG.")

        signals = [
            "",
            "VERDICT SIGNALS (inputs to the call above)",
            f"   one_sided total ......... {os_total}  (book {book_os} / non-book {nonbook_os})",
            f"   non-book one_sided ...... {nonbook_share:.0%}  <-- >0 is decisive proof of a leak",
            f"   deep BTC/ETH/SOL ........ {deep_os} one_sided  ({deep_rate:.2%} of deep book rows)",
            f"   thin DOGE/BNB ........... {thin_os} one_sided  ({thin_rate:.2%} of thin book rows)",
            f"   near resolution (<=15m).. {near_share:.0%} of one_sided  (near {near} / far {far})",
            f"   malformed rows (any) .... {malformed_total}",
        ]
    finally:
        conn.close()

    print(f"VERDICT: {verdict}")
    print(f"  {why}")
    print("=" * 92)
    for line in out:
        print(line)
    for line in signals:
        print(line)


if __name__ == "__main__":
    main()
