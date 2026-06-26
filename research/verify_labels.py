"""GROUND-TRUTH label verification against the Polymarket resolution API (TODO #5).

WHY (2026-06-11 audit): audit_checks check 7 found recovered-label vs spot-move
agreement of ~45.6%, FLAT across move size, and check 5c found ~30% of
price_change rows sit >10c off-touch (p90=0.79). The offline recovery rule took
the LAST stored row's price of ANY event type as the decisive price -- but
price_change rows carry the changed LEVEL's price, and a deep 0.0x bid / 0.9x
ask level always looks "decisive", so the old rule could be poisoned wholesale.
Two confounds must be separated:
  (a) POLARITY-BLINDNESS in check 7 itself (a "Down"-polarity market's YES token
      means the price went DOWN, so spot-up != YES); vs
  (b) GENUINE label corruption from level prices.
This script measures both with ground truth:

  1. POLARITY CENSUS -- from the API market metadata (token outcome strings +
     question text), classify each market up/down/unknown polarity. A large
     "down" share explains check 7's coin-flip agreement without any corruption.
  2. AGREEMENT MATRIX -- OLD rule (any-event decisive price, frozen here for
     measurement) vs API truth: overall + by the final row's event type + by
     symbol. This is the actual label-corruption rate f of everything the
     probes consumed so far.
  3. NEW-RULE CHECK -- the FIXED touch-only rule (book/last_trade_price rows
     only, mirrors src/backtest/recovery.py as of 2026-06-11) vs API truth:
     the residual error after the fix, plus, among markets whose label CHANGED
     old->new, how many moved TO the truth (fix validation).
  4. RESOLVE-TIME CHECK -- recovered resolve_ts vs the API end_date_iso
     (quantifies the early-death/linger mislabeling directly).

OUTPUT CACHE: research/diagnostics/label_truth.csv -- consumed by the probes'
`--labels` flag (columns: market_id / truth / end_date_iso) and by audit_checks
check 7 (column: polarity). Truth convention matches the stored stream: YES =
the token whose API outcome string normalizes to YES ("Yes"/"Up"), i.e. the
same mapping main.py used at capture time, so `truth` is directly comparable
to recovered labels REGARDLESS of polarity.

NETWORK: GET {clob}/markets/{condition_id} (replicates
src/api/polymarket.py::get_market_resolution with sync stdlib urllib -- no
aiohttp, runnable with ANY python). Gentle by design: sequential, one request
per market, `--sleep` (default 0.25s) between calls, resumable via the CSV
cache (already-fetched truths are NOT refetched unless --refresh).

READ-ONLY on the DB (mode=ro). Writes ONLY the CSV cache + console report.
The live resolver/ledger path is untouched and was never affected (it reads
the resolution API directly).

    python research/verify_labels.py                # default: decisive markets
    python research/verify_labels.py --all-markets  # + ambiguous/truncated
    python research/verify_labels.py --limit 20     # smoke run
"""
import argparse
import csv
import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

REPO_ROOT = Path(__file__).resolve().parents[1]
DB = REPO_ROOT / "data" / "bot.db"
OUT_CSV = REPO_ROOT / "research" / "diagnostics" / "label_truth.csv"
CLOB_URL = "https://clob.polymarket.com"

YES_ABOVE, NO_BELOW = 0.9, 0.1                  # decisive thresholds (unchanged)
TOUCH_EVENTS = ("book", "last_trade_price")     # the FIXED rule's row filter

CSV_FIELDS = [
    "market_id", "symbol", "question", "polarity", "truth", "winner_raw",
    "closed", "end_date_iso",
    "old_label", "old_final_event", "old_last_price", "old_resolve_ts",
    "new_label", "new_last_price", "new_resolve_ts", "fetch_error",
]


# --------------------------------------------------------------------------- #
# shared rule helpers (must mirror src/backtest/recovery.py semantics)
# --------------------------------------------------------------------------- #
def _parse_ts(s: str) -> datetime:
    ts = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def round_to_hour(ts: datetime) -> datetime:
    floored = ts.replace(minute=0, second=0, microsecond=0)
    return floored + timedelta(hours=1) if ts.minute >= 30 else floored


def _decisive_label(price: float) -> str:
    if price > YES_ABOVE:
        return "YES"
    if price < NO_BELOW:
        return "NO"
    return "ambiguous"


def _normalize_outcome(label: str) -> str | None:
    """Replicated from src/api/polymarket.py -- the SAME mapping capture used,
    so `truth` shares the stored stream's YES convention."""
    s = (label or "").strip().lower()
    if s in ("yes", "up"):
        return "YES"
    if s in ("no", "down"):
        return "NO"
    return None


# --------------------------------------------------------------------------- #
# DB side: both rules per market (OLD frozen for measurement, NEW = the fix)
# --------------------------------------------------------------------------- #
def db_market_records(conn) -> tuple[dict[str, dict], datetime | None]:
    """market_id -> record with old_*/new_* labels. OLD = legacy any-event rule
    (deliberately frozen pre-fix behaviour); NEW = touch-only rule, identical to
    the fixed src/backtest/recovery.py / probe replicas."""
    gmax_row = conn.execute("SELECT MAX(ts) AS m FROM snapshots").fetchone()
    if gmax_row["m"] is None:
        return {}, None
    gmax = _parse_ts(gmax_row["m"])

    recs: dict[str, dict] = {}
    for touch_only, prefix in ((False, "old"), (True, "new")):
        et = " AND event_type IN ('book','last_trade_price')" if touch_only else ""
        rows = conn.execute(
            "SELECT market_id, MAX(ts) AS last_ts FROM snapshots "
            "WHERE source='polymarket' AND market_id IS NOT NULL AND price IS NOT NULL"
            + et + " GROUP BY market_id"
        ).fetchall()
        for r in rows:
            lp = conn.execute(
                "SELECT ts, price, event_type FROM snapshots "
                "WHERE market_id=? AND price IS NOT NULL"
                + et + " ORDER BY ts DESC LIMIT 1", (r["market_id"],)
            ).fetchone()
            if lp is None:
                continue
            resolve_ts = round_to_hour(_parse_ts(r["last_ts"]))
            label = ("truncated" if resolve_ts > gmax
                     else _decisive_label(float(lp["price"])))
            rec = recs.setdefault(r["market_id"], {})
            rec[f"{prefix}_label"] = label
            rec[f"{prefix}_last_price"] = f"{float(lp['price']):.4f}"
            rec[f"{prefix}_resolve_ts"] = resolve_ts.isoformat()
            if not touch_only:
                rec["old_final_event"] = lp["event_type"]
    for rec in recs.values():
        rec.setdefault("old_label", "no-rows")
        rec.setdefault("old_final_event", "")
        rec.setdefault("old_last_price", "")
        rec.setdefault("old_resolve_ts", "")
        rec.setdefault("new_label", "no-touch-rows")
        rec.setdefault("new_last_price", "")
        rec.setdefault("new_resolve_ts", "")
    return recs, gmax


def majority_symbols(conn) -> dict[str, str]:
    rows = conn.execute(
        "SELECT market_id, symbol, COUNT(*) AS n FROM snapshots "
        "WHERE source='polymarket' AND market_id IS NOT NULL AND symbol IS NOT NULL "
        "GROUP BY market_id, symbol"
    ).fetchall()
    best: dict[str, tuple[str, int]] = {}
    for r in rows:
        cur = best.get(r["market_id"])
        if cur is None or r["n"] > cur[1]:
            best[r["market_id"]] = (r["symbol"], r["n"])
    return {m: s for m, (s, _) in best.items()}


# --------------------------------------------------------------------------- #
# API side
# --------------------------------------------------------------------------- #
def fetch_market(clob_url: str, market_id: str, timeout: float) -> dict:
    url = f"{clob_url.rstrip('/')}/markets/{market_id}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "polymarket-bot-label-audit/1.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def classify_polarity(question: str, outcomes_lower: set[str]) -> str:
    """up = the YES-normalized token means 'price went UP'; down = it means
    'price went DOWN'. Token outcome strings rule; Yes/No token pairs fall back
    to the question text (word-boundary match)."""
    if "up" in outcomes_lower:
        return "up"            # Up/Down pair: _normalize_outcome maps Up -> YES
    if "down" in outcomes_lower:
        return "down"          # defensive: Down present without Up
    q = (question or "").lower()
    has_up = re.search(r"\bup\b", q) is not None
    has_down = re.search(r"\bdown\b", q) is not None
    if has_down and not has_up:
        return "down"
    if has_up and not has_down:
        return "up"
    return "unknown"


def extract_truth(m: dict) -> dict:
    """Network-derived CSV fields from one CLOB /markets response."""
    question = str(m.get("question") or "")
    end_iso = str(m.get("end_date_iso") or m.get("endDateIso") or m.get("end_date") or "")
    closed = bool(m.get("closed"))
    tokens = [t for t in (m.get("tokens") or []) if isinstance(t, dict)]
    outcomes_lower = {str(t.get("outcome") or "").strip().lower() for t in tokens}
    outcomes_lower.discard("")
    winner = next((t for t in tokens if t.get("winner")), None)
    winner_raw = str(winner.get("outcome") or "") if winner else ""
    truth = _normalize_outcome(winner_raw) if (closed and winner) else None
    return {
        "question": question,
        "polarity": classify_polarity(question, outcomes_lower),
        "truth": truth or "",
        "winner_raw": winner_raw,
        "closed": "true" if closed else "false",
        "end_date_iso": end_iso,
        "fetch_error": "",
    }


# --------------------------------------------------------------------------- #
# reporting helpers
# --------------------------------------------------------------------------- #
def _agree_line(name: str, pairs: list[tuple[str, str]]) -> str:
    n = len(pairs)
    if n == 0:
        return f"  {name:<28} n=0"
    a = sum(1 for lab, tr in pairs if lab == tr)
    return f"  {name:<28} n={n:>4}  agree={a:>4} ({a / n * 100:5.1f}%)  corrupt f={(n - a) / n * 100:5.1f}%"


def main() -> None:
    ap = argparse.ArgumentParser(prog="python research/verify_labels.py",
                                 description=__doc__)
    ap.add_argument("--db", type=Path, default=DB)
    ap.add_argument("--out", type=Path, default=OUT_CSV)
    ap.add_argument("--clob-url", type=str, default=CLOB_URL)
    ap.add_argument("--sleep", type=float, default=0.25,
                    help="seconds between API calls (be gentle; sequential)")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--refresh", action="store_true",
                    help="refetch ALL markets, ignoring the CSV cache")
    ap.add_argument("--all-markets", action="store_true",
                    help="also fetch ambiguous/truncated markets (recovers the "
                         "decisive-rule exclusion set), not just decisive ones")
    ap.add_argument("--limit", type=int, default=0,
                    help="fetch at most N uncached markets (0 = no limit)")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"DB not found: {args.db}")
        return
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        recs, gmax = db_market_records(conn)
        symbols = majority_symbols(conn)
    finally:
        conn.close()
    if not recs:
        print("no polymarket markets in the DB")
        return
    for mid, rec in recs.items():
        rec["market_id"] = mid
        rec["symbol"] = symbols.get(mid, "")

    # ----- cache: reuse previously fetched truths (network fields only) -----
    cached: dict[str, dict] = {}
    if args.out.exists() and not args.refresh:
        with open(args.out, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                mid = (row.get("market_id") or "").strip()
                if mid and (row.get("truth") or "").strip() in ("YES", "NO"):
                    cached[mid] = row

    decisive = {m for m, r in recs.items()
                if r["old_label"] in ("YES", "NO") or r["new_label"] in ("YES", "NO")}
    targets = sorted(recs) if args.all_markets else sorted(decisive)

    net_fields = ("question", "polarity", "truth", "winner_raw", "closed",
                  "end_date_iso", "fetch_error")
    fetched = cache_hits = errors = 0
    for i, mid in enumerate(targets):
        rec = recs[mid]
        hit = cached.get(mid)
        if hit is not None:
            for k in net_fields:
                rec[k] = (hit.get(k) or "")
            cache_hits += 1
            continue
        if args.limit and fetched >= args.limit:
            continue
        try:
            data = fetch_market(args.clob_url, mid, args.timeout)
            rec.update(extract_truth(data))
        except Exception as e:  # HTTPError / URLError / timeout / bad JSON
            rec.update({k: rec.get(k, "") for k in net_fields})
            rec["fetch_error"] = str(e)[:120]
            errors += 1
        fetched += 1
        if fetched % 25 == 0:
            print(f"  ..fetched {fetched} (cache hits {cache_hits}, errors {errors})")
        time.sleep(max(0.0, args.sleep))
    for rec in recs.values():            # markets never targeted: blank net fields
        for k in net_fields:
            rec.setdefault(k, "")

    # ----- write the cache CSV (the probes' --labels input) -----
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore",
                           lineterminator="\n")
        w.writeheader()
        for mid in sorted(recs):
            w.writerow(recs[mid])

    # ----- report -----
    truth_recs = {m: r for m, r in recs.items() if r["truth"] in ("YES", "NO")}
    print()
    print("=" * 78)
    print("GROUND-TRUTH LABEL VERIFICATION (Polymarket resolution API)")
    print("=" * 78)
    print(f"DB markets={len(recs)}  targets={len(targets)}  fetched-now={fetched}  "
          f"cache-hits={cache_hits}  fetch-errors={errors}  with-truth={len(truth_recs)}")
    print(f"data end (gmax) = {gmax.isoformat() if gmax else 'n/a'}")

    # 1) polarity census
    pol_cnt: dict[str, int] = {}
    pol_sym: dict[tuple[str, str], int] = {}
    for r in recs.values():
        p = r["polarity"] or ""
        if not p:
            continue
        pol_cnt[p] = pol_cnt.get(p, 0) + 1
        pol_sym[(r["symbol"] or "?", p)] = pol_sym.get((r["symbol"] or "?", p), 0) + 1
    print("\n-- 1) POLARITY CENSUS (YES token means price went up/down) --")
    print("  " + (", ".join(f"{k}={v}" for k, v in sorted(pol_cnt.items())) or "(no metadata fetched)"))
    syms = sorted({s for s, _ in pol_sym})
    for s in syms:
        parts = ", ".join(f"{p}={pol_sym.get((s, p), 0)}" for p in ("up", "down", "unknown")
                          if pol_sym.get((s, p), 0))
        print(f"    {s:<5} {parts}")
    n_down = pol_cnt.get("down", 0)
    if n_down:
        print(f"  >> {n_down} down-polarity market(s): check 7's polarity-BLIND 45.6% was "
              f"(at least partly) polarity-blindness. Re-run audit_checks check 7.")
    else:
        print("  >> no down-polarity markets found: check 7's 45.6% agreement was NOT "
              "polarity-blindness -> it measured genuine label corruption.")

    # 2) old-rule agreement (the corruption rate f)
    old_pairs = [(r["old_label"], r["truth"]) for r in truth_recs.values()
                 if r["old_label"] in ("YES", "NO")]
    print("\n-- 2) OLD rule (any-event decisive price) vs API truth --")
    print(_agree_line("overall", old_pairs))
    by_et: dict[str, list] = {}
    for r in truth_recs.values():
        if r["old_label"] in ("YES", "NO"):
            by_et.setdefault(r["old_final_event"] or "?", []).append((r["old_label"], r["truth"]))
    for et in sorted(by_et):
        print(_agree_line(f"final row = {et}", by_et[et]))
    by_sym: dict[str, list] = {}
    for r in truth_recs.values():
        if r["old_label"] in ("YES", "NO"):
            by_sym.setdefault(r["symbol"] or "?", []).append((r["old_label"], r["truth"]))
    for s in sorted(by_sym):
        print(_agree_line(f"symbol = {s}", by_sym[s]))

    # 3) new-rule agreement + fix validation
    new_pairs = [(r["new_label"], r["truth"]) for r in truth_recs.values()
                 if r["new_label"] in ("YES", "NO")]
    print("\n-- 3) NEW rule (touch-only: book/last_trade_price) vs API truth --")
    print(_agree_line("overall", new_pairs))
    changed = [r for r in truth_recs.values()
               if r["old_label"] in ("YES", "NO") and r["new_label"] in ("YES", "NO")
               and r["old_label"] != r["new_label"]]
    to_truth = sum(1 for r in changed if r["new_label"] == r["truth"])
    print(f"  labels CHANGED old->new (with truth): {len(changed)}; "
          f"moved TO truth: {to_truth}; moved AWAY: {len(changed) - to_truth}")
    residual = [r for r in truth_recs.values()
                if r["new_label"] in ("YES", "NO") and r["new_label"] != r["truth"]]
    if residual:
        print(f"  RESIDUAL new-rule mismatches ({len(residual)}; label suspects):")
        print(f"    {'market':<14} {'sym':<5} {'new':<4} {'truth':<5} "
              f"{'new_last_px':>11} {'old_final_event':<16}")
        for r in sorted(residual, key=lambda x: x['market_id'])[:15]:
            print(f"    {r['market_id'][:12] + '..':<14} {r['symbol']:<5} "
                  f"{r['new_label']:<4} {r['truth']:<5} {r['new_last_price']:>11} "
                  f"{r['old_final_event']:<16}")

    # 4) resolve-time check: recovered resolve_ts vs API end time
    diffs = []
    for r in truth_recs.values():
        if r["new_resolve_ts"] and r["end_date_iso"]:
            try:
                d = abs((_parse_ts(r["new_resolve_ts"]) - _parse_ts(r["end_date_iso"]))
                        .total_seconds()) / 60.0
                diffs.append(d)
            except ValueError:
                pass
    print("\n-- 4) recovered resolve_ts vs API end_date_iso --")
    if diffs:
        off = sum(1 for d in diffs if d > 0.5)
        print(f"  compared={len(diffs)}  exact(<=0.5min)={len(diffs) - off}  "
              f"off={off}  median={median(diffs):.1f}min  max={max(diffs):.1f}min")
        print("  (any 'off' market had its anchors/ttr measured against a wrong hour "
              "under recovery; --labels uses the API end time instead)")
    else:
        print("  (no comparable end times fetched)")

    print(f"\nwrote: {args.out}")
    print("next: re-run the probes with  --labels " + str(args.out))
    print("(read-only on the DB; live resolver/ledger untouched)")


if __name__ == "__main__":
    main()
