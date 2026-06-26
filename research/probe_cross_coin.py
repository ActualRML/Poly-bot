"""READ-ONLY probe: does ANOTHER coin's spot movement predict a market's RESOLUTION
*beyond what that market's OWN YES price already implies*?

Own-coin spot is established as priced-in (lead-lag null at tradeable scale -- FINDINGS.md).
The UNTESTED part is OTHER coins' spot: when BTC moves, do the alt "Up/Down" markets
over-/under-win versus their own price (crypto beta leaking into resolution)? The honest
statistic is corr(LEADER spot return, RESIDUAL) where residual = outcome(1/0) - yes_price;
we report it next to the raw corr(leader_return, outcome) so the price confound is explicit.
Expected informative sign is POSITIVE (BTC up -> alt "Up" over-wins vs its price). Default
expectation: NO_SIGNAL (efficient / already priced).

LEADER = BTC. Primary pairs BTC->{ETH,SOL,XRP,DOGE,BNB} + ONE pooled test (BTC vs all alt
residuals). We do NOT mine all 30 ordered pairs (multiple-comparison explosion).

NULL CANARY (mandatory): for every coin we ALSO compute corr(OWN-coin return, own residual).
The established own-coin lead-lag null says this must be ~0. If a canary cell comes back
significant, the machinery (stale prices / alignment), not the market, produced the signal,
and we print METHODOLOGY_SUSPECT prominently.

STANDALONE: stdlib sqlite3 only, opens data/bot.db mode=ro, never writes the DB. Runnable
with ANY python (no venv/aiosqlite) -- so the resolution rule and the Bernoulli-null
machinery are REPLICATED here rather than imported (importing src.backtest.* would pull
aiosqlite, defeating "any python"). Mirrors the conventions of
research/probe_orderbook_imbalance.py and probe_loss_structure.py (per-cell + global-max
Bernoulli(price) null, seeded; CSV/markdown into research/diagnostics/). ASCII output only
(Windows console safe).

HOW THE YES PRICE IS READ (verified in source, not guessed):
  parsers.parse_polymarket sets the ticking TOKEN's own raw price (book -> best_bid;
  price_change -> the entry's price). main.py on_poly_event THEN normalizes to YES:
      snapshot.outcome = outcome_lookup.get(snapshot.asset_id or "")
      if snapshot.outcome == "NO" and snapshot.price is not None:
          snapshot.price = 1.0 - snapshot.price
      ...
      writer.add(snapshot)              # writer._to_row stores s.price AFTER the flip
  So the stored `snapshots.price` column is ALREADY YES-perspective for every polymarket
  row (book / price_change / last_trade_price), regardless of which token ticked. We read it
  directly as P(YES). (best_bid/best_ask + the 4 size/depth cols are NEVER flipped -- raw
  token -- so the imbalance side-channel re-identifies the YES token via price == best_bid,
  exactly as probe_orderbook_imbalance.py does.)

LOOK-AHEAD + FRESHNESS GUARD: anchors are T-45m/T-30m/T-15m BEFORE resolve_ts. Every value
(YES price, both spot-return endpoints, book imbalance) is the LAST sample AT-OR-BEFORE its
nominal time AND no older than STALENESS_MAX_SEC (default 30s). "At-or-before" IS the look-ahead
guard (never a sample after the nominal time); the staleness cap removes the stale-pairing
artifact -- a minutes-old YES price paired with a fresh spot move mechanically yields negative
corr(return, residual), the same disease family as the harness stale-entry artifact in FINDINGS.
An anchor with no fresh-enough YES snapshot is SKIPPED (counted "stale-yes"), never built on a
stale price; the report prints the per-symbol cost of the gate.
THROTTLE NOTE: price_change/ticker are stored ~10s-throttled; book/last_trade are kept full, so a
30s gate usually has a sample -- but sparse alt books may not, so if alts thin out the report says
so and suggests --staleness 60. Timestamps are capture-time -- fine at this granularity.
"""
import argparse
import bisect
import csv
import io
import math
import random
import statistics
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DB = REPO_ROOT / "data" / "bot.db"

EPS = 1e-6
YES_ABOVE, NO_BELOW = 0.9, 0.1            # recovery.py decisive-resolution rule
MIN_OBS = 10                              # cells thinner than this are [thin] / not verdict-eligible
DOMINANCE = 0.40                          # one market may be at most 40% of a cell's covariance
STALENESS_MAX_SEC = 30.0                  # YES/spot/book sample must be <= this old, AT-OR-BEFORE its nominal time
BASELINE_TOL_SEC = 120.0                  # OLD +/-2min loose match -- kept ONLY to report what the gate costs in n

LEADER = "BTC"
ALL_SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"]
ALT_SYMBOLS = ["ETH", "SOL", "XRP", "DOGE", "BNB"]
ANCHORS = [("T-45", 45 * 60), ("T-30", 30 * 60), ("T-15", 15 * 60)]
WINDOWS = [("ret5", 5 * 60), ("ret15", 15 * 60)]
# Depth was first captured at the ~06:00 UTC 2026-06-09 restart (FINDINGS.md). The imbalance
# side-channel is post-restart-only and therefore YOUNG -- flagged as such in the report.
POST_RESTART = datetime(2026, 6, 9, 6, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# small stats / time helpers (mirrors probe_orderbook_imbalance.py)
# --------------------------------------------------------------------------- #
def _parse_ts(s: str) -> datetime:
    ts = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def round_to_hour(ts: datetime) -> datetime:
    """Nearest hour boundary -- replicated from src/backtest/recovery.round_to_hour."""
    floored = ts.replace(minute=0, second=0, microsecond=0)
    return floored + timedelta(hours=1) if ts.minute >= 30 else floored


def _clip01(p: float) -> float:
    return 0.0 if p < 0.0 else (1.0 if p > 1.0 else p)


def _pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sx = sum((x - mx) ** 2 for x in xs)
    sy = sum((y - my) ** 2 for y in ys)
    if sx <= 0 or sy <= 0:
        return None
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return cov / math.sqrt(sx * sy)


def _dominance(xs, rs):
    """Max single-point share of the |covariance| -- 'no one market drives it'."""
    n = len(xs)
    if n < 2:
        return None
    mx, mr = statistics.fmean(xs), statistics.fmean(rs)
    contribs = [abs((x - mx) * (r - mr)) for x, r in zip(xs, rs)]
    tot = sum(contribs)
    return (max(contribs) / tot) if tot > 0 else 0.0


def _to_csv(header, rows) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# sample lookups over a per-key sorted (epoch, value) index
# --------------------------------------------------------------------------- #
def _at_or_before(index, key, t_epoch, max_stale):
    """Value of the LAST sample AT-OR-BEFORE t_epoch, but only if it is no older than max_stale
    seconds (t_epoch - te <= max_stale); else None. 'At-or-before' IS the look-ahead guard (never
    a sample after the nominal time), and the staleness cap removes the stale-pairing artifact
    (a minutes-old price paired with a fresh return). Index: key -> (ts_list, val_list)."""
    arr = index.get(key)
    if not arr:
        return None
    ts_list, val_list = arr
    j = bisect.bisect_right(ts_list, t_epoch) - 1     # last index with te <= t_epoch
    if j < 0:
        return None
    if t_epoch - ts_list[j] > max_stale:
        return None                                   # too stale -> caller skips / signal is None
    return val_list[j]


def _nearest_within(index, key, t_epoch, tol, cutoff_epoch):
    """BASELINE ONLY (cost report): value of the sample nearest t_epoch within +/-tol whose epoch
    < cutoff_epoch. This is the OLD loose match -- used solely to count what the freshness gate
    costs in n, NEVER to build an observation."""
    arr = index.get(key)
    if not arr:
        return None
    ts_list, val_list = arr
    pos = bisect.bisect_left(ts_list, t_epoch)
    best, best_d = None, None
    for j in (pos - 1, pos, pos + 1):
        if 0 <= j < len(ts_list):
            te = ts_list[j]
            if cutoff_epoch is not None and te >= cutoff_epoch:
                continue                     # look-ahead: never use a sample at/after resolve
            d = abs(te - t_epoch)
            if d <= tol and (best_d is None or d < best_d):
                best_d, best = d, val_list[j]
    return best


def _trailing_return(spot_index, symbol, anchor_epoch, window_sec, max_stale):
    """price_now/price_then - 1. BOTH endpoints are the last spot AT-OR-BEFORE their nominal times
    under the SAME staleness gate, so the return and the YES price are measured on one clock."""
    p_now = _at_or_before(spot_index, symbol, anchor_epoch, max_stale)
    p_then = _at_or_before(spot_index, symbol, anchor_epoch - window_sec, max_stale)
    if p_now is None or p_then is None or p_then <= 0:
        return None
    return p_now / p_then - 1.0


# --------------------------------------------------------------------------- #
# resolution recovery (replicated from src/backtest/recovery.py, sync stdlib)
# --------------------------------------------------------------------------- #
def _decisive(price: float):
    """1 = YES won, 0 = NO won, None = not decisive."""
    if price > YES_ABOVE:
        return 1
    if price < NO_BELOW:
        return 0
    return None


def recover_resolutions(conn) -> dict[str, tuple[datetime, int]]:
    """market_id -> (resolve_ts, outcome) where outcome = 1 (YES won) / 0 (NO won).
    RULE (FIXED 2026-06-11, mirrors src/backtest/recovery.py): resolve_ts AND the
    decisive last YES-price (>0.9 YES, <0.1 NO) come ONLY from touch-bearing rows --
    event_type IN ('book','last_trade_price') -- NEVER price_change. price_change
    rows store the changed LEVEL's price (audit: ~30% land >10c off-touch), so a
    deep 0.0x bid / 0.9x ask level at stream end forged "decisive" labels under the
    old any-event rule (ground truth: research/verify_labels.py). Ambiguous /
    still-trading-at-data-end markets stay excluded. Prints a one-line
    old-rule -> new-rule label-change report."""
    gmax_row = conn.execute("SELECT MAX(ts) AS m FROM snapshots").fetchone()
    if gmax_row["m"] is None:
        return {}
    gmax = _parse_ts(gmax_row["m"])

    def _labels(touch_only: bool) -> dict[str, tuple[datetime, int | None]]:
        et = " AND event_type IN ('book','last_trade_price')" if touch_only else ""
        out: dict[str, tuple[datetime, int | None]] = {}
        rows = conn.execute(
            "SELECT market_id, MAX(ts) AS last_ts FROM snapshots "
            "WHERE source='polymarket' AND market_id IS NOT NULL AND price IS NOT NULL"
            + et + " GROUP BY market_id"
        ).fetchall()
        for r in rows:
            lp = conn.execute(
                "SELECT price FROM snapshots WHERE market_id=? AND price IS NOT NULL"
                + et + " ORDER BY ts DESC LIMIT 1", (r["market_id"],)
            ).fetchone()
            if lp is None:
                continue
            resolve_ts = round_to_hour(_parse_ts(r["last_ts"]))
            outcome = None if resolve_ts > gmax else _decisive(float(lp["price"]))
            out[r["market_id"]] = (resolve_ts, outcome)
        return out

    new = _labels(touch_only=True)
    legacy = _labels(touch_only=False)
    usable = {m: (rt, oc) for m, (rt, oc) in new.items() if oc is not None}
    changed = sum(1 for m, (_, oc) in usable.items()
                  if legacy.get(m, (None, None))[1] is not None and legacy[m][1] != oc)
    dropped = sum(1 for m, (_, oc) in legacy.items()
                  if oc is not None and new.get(m, (None, None))[1] is None)
    added = sum(1 for m in usable if legacy.get(m, (None, None))[1] is None)
    print(f"[label-rule fix] touch-only decisive price: {len(usable)} usable | vs "
          f"legacy any-event rule: {changed} label(s) changed, {dropped} legacy-only "
          f"dropped, {added} newly usable")
    return usable


def apply_label_overrides(conn, resolutions, labels_path: Path) -> dict[str, tuple[datetime, int]]:
    """--labels override: API ground truth from research/verify_labels.py REPLACES
    every recovered label (truth-only mode -- markets without a cached truth row are
    DROPPED, never mixed). resolve_ts prefers the API end time when cached; markets
    ending past the stored stream are still excluded (never settled in-sample)."""
    gmax_row = conn.execute("SELECT MAX(ts) AS m FROM snapshots").fetchone()
    gmax = _parse_ts(gmax_row["m"]) if gmax_row["m"] else None
    out: dict[str, tuple[datetime, int]] = {}
    n_truth = 0
    with open(labels_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            mid = (row.get("market_id") or "").strip()
            t = (row.get("truth") or "").strip().upper()
            if not mid or t not in ("YES", "NO"):
                continue
            n_truth += 1
            rt = None
            end_iso = (row.get("end_date_iso") or "").strip()
            if end_iso:
                try:
                    rt = _parse_ts(end_iso)
                except ValueError:
                    rt = None
            if rt is None and mid in resolutions:
                rt = resolutions[mid][0]
            if rt is None or (gmax is not None and rt > gmax):
                continue
            out[mid] = (rt, 1 if t == "YES" else 0)
    print(f"[labels override] {labels_path}: {n_truth} truth rows -> {len(out)} usable "
          f"markets (recovery rule BYPASSED; using API ground truth)")
    return out


def label_yes_assets(conn, usable: set[str]) -> set[str]:
    """asset_ids whose book rows are predominantly the YES token (stored price == own
    best_bid, i.e. price was NOT flipped). Majority vote is robust to the price==0.5 tie.
    Replicated from probe_orderbook_imbalance.label_yes_assets."""
    tally: dict[str, list[int]] = {}
    rows = conn.execute(
        "SELECT market_id, asset_id, price, best_bid, best_ask FROM snapshots "
        "WHERE source='polymarket' AND event_type='book' AND price IS NOT NULL "
        "AND asset_id IS NOT NULL"
    ).fetchall()
    for r in rows:
        if r["market_id"] not in usable:
            continue
        ref = r["best_bid"] if r["best_bid"] is not None else r["best_ask"]
        if ref is None:
            continue
        p = r["price"]
        yes_like = abs(p - ref) < EPS
        no_like = abs(p - (1.0 - ref)) < EPS
        if yes_like == no_like:               # tie (p==ref==0.5) or neither -> skip
            continue
        t = tally.setdefault(r["asset_id"], [0, 0])
        t[0 if yes_like else 1] += 1
    return {aid for aid, (y, n) in tally.items() if y > n}


# --------------------------------------------------------------------------- #
# index builders (one read-only pass each)
# --------------------------------------------------------------------------- #
def build_spot_index(conn):
    """symbol -> (sorted epoch list, price list) from binance ticker rows."""
    raw: dict[str, list[tuple[float, float]]] = {}
    rows = conn.execute(
        "SELECT ts, symbol, price FROM snapshots "
        "WHERE source='binance' AND price IS NOT NULL AND symbol IS NOT NULL ORDER BY ts"
    ).fetchall()
    for r in rows:
        raw.setdefault(r["symbol"], []).append((_parse_ts(r["ts"]).timestamp(), float(r["price"])))
    return {sym: ([t for t, _ in seq], [p for _, p in seq]) for sym, seq in raw.items()}


def build_price_index(conn):
    """(market_id -> (epoch list, YES-price list)) and (market_id -> symbol by majority).
    YES-price is the stored `price` column (already YES-normalized -- see module docstring)."""
    raw: dict[str, list[tuple[float, float]]] = {}
    sym_tally: dict[str, dict[str, int]] = {}
    rows = conn.execute(
        "SELECT ts, market_id, symbol, price FROM snapshots "
        "WHERE source='polymarket' AND market_id IS NOT NULL AND price IS NOT NULL ORDER BY ts"
    ).fetchall()
    for r in rows:
        raw.setdefault(r["market_id"], []).append((_parse_ts(r["ts"]).timestamp(), float(r["price"])))
        if r["symbol"]:
            t = sym_tally.setdefault(r["market_id"], {})
            t[r["symbol"]] = t.get(r["symbol"], 0) + 1
    price_index = {mid: ([t for t, _ in seq], [p for _, p in seq]) for mid, seq in raw.items()}
    market_symbol = {mid: max(t, key=t.get) for mid, t in sym_tally.items()}
    return price_index, market_symbol


def build_imbalance_index(conn, yes_assets: set[str]):
    """market_id -> (epoch list, top-of-book imbalance list) for POST-RESTART two-sided YES
    book rows only (both sizes present, bid+ask>0). One-sided rows are filtered (no-quote)."""
    raw: dict[str, list[tuple[float, float]]] = {}
    rows = conn.execute(
        "SELECT ts, market_id, asset_id, bid_size, ask_size FROM snapshots "
        "WHERE source='polymarket' AND event_type='book' AND asset_id IS NOT NULL "
        "AND bid_size IS NOT NULL AND ask_size IS NOT NULL ORDER BY ts"
    ).fetchall()
    for r in rows:
        if r["asset_id"] not in yes_assets:
            continue
        ts = _parse_ts(r["ts"])
        if ts < POST_RESTART:
            continue
        bs, ask = float(r["bid_size"]), float(r["ask_size"])
        if bs + ask <= 0:
            continue
        raw.setdefault(r["market_id"], []).append((ts.timestamp(), (bs - ask) / (bs + ask)))
    return {mid: ([t for t, _ in seq], [v for _, v in seq]) for mid, seq in raw.items()}


# --------------------------------------------------------------------------- #
# Bernoulli(price) null over heterogeneous-signal cells
# --------------------------------------------------------------------------- #
def mc_corr_pvalues(observations, cells, obs_corr_res, *, iters, seed):
    """Seeded one-pass null. cells: cell_id -> (signal_values, idx_list) where signal_values
    is aligned to idx_list (each obs's FIXED signal: a leader or own-coin return). Under H0,
    outcome ~ Bernoulli(price) independent of the signal, so residual = outcome - price carries
    no signal. Returns (p_cell, p_global) for |corr(signal, residual)| with a multiple-
    comparison-aware global max across exactly these cells. Mirrors
    probe_orderbook_imbalance.mc_corr_pvalues, generalized to a per-cell signal array."""
    prices = [o["price"] for o in observations]
    n = len(observations)
    obs_max = max((abs(v) for v in obs_corr_res.values() if v is not None), default=0.0)
    rng = random.Random(seed)
    ge = {c: 0 for c in cells}
    gmax_ge = 0
    for _ in range(iters):
        sim_res = [(1 if rng.random() < prices[i] else 0) - prices[i] for i in range(n)]
        smax = 0.0
        for c, (sig, idx) in cells.items():
            cc = _pearson(sig, [sim_res[i] for i in idx])
            if cc is None:
                continue
            oc = obs_corr_res[c]
            if oc is not None and abs(cc) >= abs(oc) - 1e-12:
                ge[c] += 1
            if abs(cc) > smax:
                smax = abs(cc)
        if smax >= obs_max - 1e-12:
            gmax_ge += 1
    p_cell = {c: (ge[c] / iters if obs_corr_res[c] is not None else 1.0) for c in cells}
    return p_cell, (gmax_ge / iters if cells else 1.0)


# --------------------------------------------------------------------------- #
# cell construction + per-cell stats
# --------------------------------------------------------------------------- #
def _cell_stat(observations, sig, idx):
    outc = [observations[i]["outcome"] for i in idx]
    res = [observations[i]["outcome"] - observations[i]["price"] for i in idx]
    cr_raw = _pearson(sig, outc)
    cr_res = _pearson(sig, res)
    return {
        "n": len(idx),
        "corr_outcome": cr_raw,
        "corr_residual": cr_res,
        "gap": (None if cr_raw is None or cr_res is None else cr_raw - cr_res),
        "dominance": _dominance(sig, res),
    }


def build_primary_cells(observations):
    """Leader (BTC) return vs alt residual, over (pair x window x anchor). Each cell holds at
    most ONE observation per market (the clean, non-pseudo-replicated unit). pair in
    {pooled-alts} U ALT_SYMBOLS. cell_id -> (signal_values, idx_list)."""
    cells: dict[str, tuple[list, list]] = {}
    meta: dict[str, tuple[str, str, str]] = {}      # cell_id -> (pair, window, anchor)
    for wlabel, _ in WINDOWS:
        skey = f"leader_{wlabel}"
        for anc, _ in ANCHORS:
            base = [i for i, o in enumerate(observations)
                    if o["symbol"] in ALT_SYMBOLS and o["anchor"] == anc and o[skey] is not None]
            if base:
                cid = f"leader|{wlabel}|{anc}|pooled-alts"
                cells[cid] = ([observations[i][skey] for i in base], base)
                meta[cid] = ("pooled-alts", wlabel, anc)
            for sym in ALT_SYMBOLS:
                idx = [i for i in base if observations[i]["symbol"] == sym]
                if idx:
                    cid = f"leader|{wlabel}|{anc}|{sym}"
                    cells[cid] = ([observations[i][skey] for i in idx], idx)
                    meta[cid] = (sym, wlabel, anc)
    return cells, meta


def build_canary_cells(observations):
    """Own-coin return vs own residual, over (coin x window x anchor) + pooled-all. Same clean
    one-obs-per-market unit. This MUST come back ~0 (the established own-coin null)."""
    cells: dict[str, tuple[list, list]] = {}
    meta: dict[str, tuple[str, str, str]] = {}
    for wlabel, _ in WINDOWS:
        skey = f"own_{wlabel}"
        for anc, _ in ANCHORS:
            base = [i for i, o in enumerate(observations)
                    if o["anchor"] == anc and o[skey] is not None]
            if base:
                cid = f"canary|{wlabel}|{anc}|pooled-all"
                cells[cid] = ([observations[i][skey] for i in base], base)
                meta[cid] = ("pooled-all", wlabel, anc)
            for sym in ALL_SYMBOLS:
                idx = [i for i in base if observations[i]["symbol"] == sym]
                if idx:
                    cid = f"canary|{wlabel}|{anc}|{sym}"
                    cells[cid] = ([observations[i][skey] for i in idx], idx)
                    meta[cid] = (sym, wlabel, anc)
    return cells, meta


def classify(stat, p_global):
    """Per-cell verdict. Expected informative sign is POSITIVE (crypto beta)."""
    if stat["n"] < MIN_OBS or stat["corr_residual"] is None:
        return "thin"
    sig = stat["p"] < 0.05
    pos = stat["corr_residual"] > 0
    dom_ok = stat["dominance"] is None or stat["dominance"] <= DOMINANCE
    if sig and pos and dom_ok and p_global < 0.05:
        return "ROBUST"
    if sig and pos:
        return "WEAK"
    return "NO_SIGNAL"


# --------------------------------------------------------------------------- #
def _fmt(v, d=3):
    return "n/a" if v is None else f"{v:+.{d}f}"


def _r(v):
    return "" if v is None else round(v, 4)


def _cell_table(meta, stat, flagged):
    lines = ["| pair | window | anchor | n | corr(lead,outcome) | corr(lead,residual) | "
             "confound gap | p | max-dom | verdict |",
             "|---|---|---|--:|--:|--:|--:|--:|--:|---|"]
    # stable order: pair (pooled first), window, anchor
    pair_order = {"pooled-alts": 0, "pooled-all": 0}
    for k, s in enumerate(ALL_SYMBOLS, start=1):
        pair_order[s] = k
    anc_order = {a: i for i, (a, _) in enumerate(ANCHORS)}
    win_order = {w: i for i, (w, _) in enumerate(WINDOWS)}
    for cid in sorted(meta, key=lambda c: (pair_order.get(meta[c][0], 9),
                                           win_order.get(meta[c][1], 9),
                                           anc_order.get(meta[c][2], 9))):
        pair, win, anc = meta[cid]
        s = stat[cid]
        thin = "" if s["n"] >= MIN_OBS else " [thin]"
        dom = "n/a" if s["dominance"] is None else f"{s['dominance'] * 100:.0f}%"
        flag = "  <-" if cid in flagged else ""
        lines.append(f"| {pair}{thin} | {win} | {anc} | {s['n']} | {_fmt(s['corr_outcome'])} | "
                     f"{_fmt(s['corr_residual'])} | {_fmt(s['gap'])} | {s['p']:.3f} | {dom} | "
                     f"{s['verdict']}{flag} |")
    return lines


# --------------------------------------------------------------------------- #
def main() -> None:
    global MIN_OBS  # declared up-front: main() overrides it from --min-obs (read below as the default)
    ap = argparse.ArgumentParser(prog="python research/probe_cross_coin.py", description=__doc__)
    ap.add_argument("--db", type=Path, default=DB)
    ap.add_argument("--iters", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--min-obs", type=int, default=MIN_OBS)
    ap.add_argument("--staleness", type=float, default=STALENESS_MAX_SEC,
                    help="max age (sec) of the YES/spot/book sample at-or-before each nominal time "
                         "(default 30; try 60 if alt markets thin out)")
    ap.add_argument("--labels", type=Path, default=None,
                    help="optional label_truth.csv from research/verify_labels.py; when "
                         "given, API ground truth REPLACES the recovery rule")
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "research" / "diagnostics")
    args = ap.parse_args()
    MIN_OBS = args.min_obs
    stale_max = args.staleness

    if not args.db.exists():
        print("VERDICT: cross-coin=NO_SIGNAL (DB not found)")
        print("CANARY: n/a")
        return

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        resolutions = recover_resolutions(conn)
        if args.labels:
            resolutions = apply_label_overrides(conn, resolutions, args.labels)
        yes_assets = label_yes_assets(conn, set(resolutions))
        spot_index = build_spot_index(conn)
        price_index, market_symbol = build_price_index(conn)
        imb_index = build_imbalance_index(conn, yes_assets)
    finally:
        conn.close()

    # ----- build observations: one per (resolved market, anchor) -----
    # Each value is the LAST sample at-or-before its nominal time, fresh within stale_max, so the
    # YES price and the spot-return endpoints share one clock (kills the stale-pairing artifact).
    # before_cnt/after_cnt measure the gate's cost in n (old +/-2min match vs the fresh gate).
    observations = []
    skip_no_symbol = skip_stale_yes = 0
    before_cnt = {s: 0 for s in ALL_SYMBOLS}
    after_cnt = {s: 0 for s in ALL_SYMBOLS}
    for mid, (resolve_ts, outcome) in resolutions.items():
        symbol = market_symbol.get(mid)
        if symbol not in ALL_SYMBOLS:
            skip_no_symbol += 1
            continue
        cutoff = resolve_ts.timestamp()
        for anc_label, anc_off in ANCHORS:
            anchor_epoch = (resolve_ts - timedelta(seconds=anc_off)).timestamp()
            # baseline (old loose match) -- counted ONLY to report what the freshness gate costs
            if _nearest_within(price_index, mid, anchor_epoch, BASELINE_TOL_SEC, cutoff) is not None:
                before_cnt[symbol] += 1
            yes_price = _at_or_before(price_index, mid, anchor_epoch, stale_max)
            if yes_price is None:
                skip_stale_yes += 1
                continue                       # no FRESH YES snapshot at-or-before -> skip this cell
            after_cnt[symbol] += 1
            obs = {
                "market_id": mid, "symbol": symbol, "anchor": anc_label,
                "price": _clip01(yes_price), "outcome": outcome,
                "imb_top": _at_or_before(imb_index, mid, anchor_epoch, stale_max),
            }
            for wlabel, wsec in WINDOWS:
                obs[f"leader_{wlabel}"] = _trailing_return(spot_index, LEADER, anchor_epoch, wsec, stale_max)
                obs[f"own_{wlabel}"] = _trailing_return(spot_index, symbol, anchor_epoch, wsec, stale_max)
            observations.append(obs)

    # ----- summary counters -----
    per_symbol = {s: sum(1 for o in observations if o["symbol"] == s) for s in ALL_SYMBOLS}
    n_leader_ok = sum(1 for o in observations if o["leader_ret5"] is not None or o["leader_ret15"] is not None)
    n_imb_ok = sum(1 for o in observations if o["imb_top"] is not None)

    # ----- freshness cost: per-symbol observations surviving the gate vs the old loose match -----
    alt_before = sum(before_cnt[s] for s in ALT_SYMBOLS)
    alt_after = sum(after_cnt[s] for s in ALT_SYMBOLS)
    alt_retain = (alt_after / alt_before) if alt_before else 0.0
    alt_sparse = alt_before > 0 and alt_retain < 0.5
    cost_lines = [
        f"## Freshness cost (staleness gate = {stale_max:.0f}s, at-or-before anchor)", "",
        f"- skipped: stale-yes (no YES snapshot within {stale_max:.0f}s at-or-before the anchor): "
        f"{skip_stale_yes}",
        "", "| symbol | obs before (<=2min nearest) | obs after (fresh gate) | retained |",
        "|---|--:|--:|--:|",
    ]
    for s in ALL_SYMBOLS:
        b, a = before_cnt[s], after_cnt[s]
        cost_lines.append(f"| {s} | {b} | {a} | {(a / b * 100):.0f}% |" if b else f"| {s} | 0 | {a} | n/a |")
    cost_lines.append(f"| **ALT total** | {alt_before} | {alt_after} | {(alt_retain * 100):.0f}% |")
    cost_lines.append("")
    if alt_sparse:
        cost_lines += [
            f"> **FRESHNESS vs n NOTE:** alt markets retained only {alt_retain * 100:.0f}% of "
            f"observations at {stale_max:.0f}s -- their stored YES stream is too sparse at this "
            "freshness. Prefer RE-RUNNING with `--staleness 60` and checking the canary STAYS clean, "
            "rather than trusting thin alt cells at 30s. Do NOT loosen further without re-checking the "
            "canary -- the gate, not n, is what removes the stale-pairing artifact.", "",
        ]

    header = [
        "# Cross-coin spot vs resolution (price-controlled)", "",
        f"- usable resolved markets: {len(resolutions)} (skipped: no-symbol {skip_no_symbol}); "
        f"YES-token asset_ids: {len(yes_assets)}",
        f"- observations (market x anchor): {len(observations)} "
        f"(skipped: stale-yes {skip_stale_yes} -- see Freshness cost)",
        f"- per-symbol observations: " + ", ".join(f"{s}={per_symbol[s]}" for s in ALL_SYMBOLS),
        f"- obs with a leader (BTC) return: {n_leader_ok}; with a post-restart two-sided book: {n_imb_ok}",
        f"- null iters={args.iters}, seed={args.seed}, min-obs={MIN_OBS}, staleness={stale_max:.0f}s; "
        "signal = LEADER spot trailing return (5m & 15m); residual = outcome - yes_price.",
        "- corr(leader,outcome) is price-confounded; **corr(leader,residual)** is the edge signal. "
        "Expected informative sign: POSITIVE (crypto beta).",
        "",
    ]

    if len(observations) < 2:
        line = "VERDICT: cross-coin=NO_SIGNAL (insufficient observations)"
        canary = "CANARY: n/a (insufficient observations)"
        md = line + "\n" + canary + "\n\n" + "\n".join(header + cost_lines) + \
            "\n_Not enough FRESH-priced resolved markets yet -- the staleness gate may be biting; see " \
            "Freshness cost above and consider --staleness 60, or re-run as data grows._\n"
        _emit(args.out, md, [])
        print(line)
        print(canary)
        if alt_sparse:
            print(f">>> FRESHNESS NOTE: alts kept {alt_retain * 100:.0f}% of obs at {stale_max:.0f}s; "
                  "try --staleness 60 (see report). <<<")
        return

    # ----- primary (leader) family -----
    pcells, pmeta = build_primary_cells(observations)
    pstat = {c: _cell_stat(observations, sig, idx) for c, (sig, idx) in pcells.items()}
    pobs_corr = {c: pstat[c]["corr_residual"] for c in pcells}
    pp_cell, pp_global = mc_corr_pvalues(observations, pcells, pobs_corr, iters=args.iters, seed=args.seed)
    for c in pcells:
        pstat[c]["p"] = pp_cell[c]
        pstat[c]["verdict"] = classify(pstat[c], pp_global)
    p_flagged = {c for c in pcells if pstat[c]["verdict"] in ("ROBUST", "WEAK")}

    # ----- canary (own-coin) family: separate global family -----
    ccells, cmeta = build_canary_cells(observations)
    cstat = {c: _cell_stat(observations, sig, idx) for c, (sig, idx) in ccells.items()}
    cobs_corr = {c: cstat[c]["corr_residual"] for c in ccells}
    cp_cell, cp_global = mc_corr_pvalues(observations, ccells, cobs_corr, iters=args.iters, seed=args.seed)
    for c in ccells:
        cstat[c]["p"] = cp_cell[c]
        # canary verdict is two-sided + uncorrected (a SENSITIVE tripwire by design)
        cstat[c]["verdict"] = ("SUSPECT" if (cstat[c]["n"] >= MIN_OBS and cstat[c]["corr_residual"] is not None
                                             and cstat[c]["p"] < 0.05) else "ok")
    canary_hits = sorted(c for c in ccells if cstat[c]["verdict"] == "SUSPECT")

    # ----- overall verdict (per-ALT cleanliness gates ROBUST; pooled-only -> at most WEAK) -----
    alt_robust = [c for c in pcells if pmeta[c][0] in ALT_SYMBOLS and pstat[c]["verdict"] == "ROBUST"]
    any_weak = [c for c in pcells if pstat[c]["verdict"] in ("WEAK", "ROBUST")]
    if alt_robust:
        overall = "ROBUST_SIGNAL"
    elif any_weak:
        overall = "WEAK_SIGNAL"
    else:
        overall = "NO_SIGNAL"

    # per-pair roll-up (best cell verdict across that pair's window x anchor cells)
    rank = {"ROBUST": 3, "WEAK": 2, "NO_SIGNAL": 1, "thin": 0}
    pair_verdict: dict[str, str] = {}
    for c in pcells:
        pair = pmeta[c][0]
        v = pstat[c]["verdict"]
        if pair not in pair_verdict or rank[v] > rank[pair_verdict[pair]]:
            pair_verdict[pair] = v

    canary_line = ("CANARY: METHODOLOGY_SUSPECT -- own-coin return significant in "
                   f"{canary_hits} (the probe, not the market, made a signal)"
                   if canary_hits else
                   "CANARY: own-coin null HOLDS (clean) -- no own-coin cell beats chance")
    verdict_line = (f"VERDICT: cross-coin(BTC->alts)={overall} | per-alt verdicts: "
                    + ", ".join(f"{s}={pair_verdict.get(s, 'n/a')}" for s in ALT_SYMBOLS)
                    + f" | pooled-alts={pair_verdict.get('pooled-alts', 'n/a')} "
                    f"(global p={pp_global:.3f})")

    # ----- imbalance redundancy (post-restart, young) -----
    imb_rows = []
    imb_lines = ["## Imbalance cross-correlation (secondary; POST-RESTART only -- YOUNG data)", "",
                 "_Do leader return and YES top-of-book imbalance carry the SAME information? If highly "
                 "correlated, combining them later adds no independence. Pure Pearson, no null (a "
                 "redundancy check, not an edge test)._", "",
                 "| signal pair | n | corr |", "|---|--:|--:|"]
    for wlabel, _ in WINDOWS:
        xs, ys = [], []
        for o in observations:
            if o["imb_top"] is not None and o[f"leader_{wlabel}"] is not None:
                xs.append(o[f"leader_{wlabel}"])
                ys.append(o["imb_top"])
        cc = _pearson(xs, ys)
        thin = " [thin]" if len(xs) < MIN_OBS else ""
        imb_lines.append(f"| leader_{wlabel} vs imb_top{thin} | {len(xs)} | {_fmt(cc)} |")
        imb_rows.append(["imbalance_redundancy", "leader_vs_imb", wlabel, "post-restart",
                         len(xs), "", _r(cc), "", "", "", "", ""])
    imb_lines.append("")

    # ----- markdown -----
    md_lines = [verdict_line, canary_line, ""] + header + cost_lines
    md_lines += ["## Primary: cross-coin leader (BTC) -> alt resolution", "",
                 f"- family global p (multiple-comparison max-stat across pairs x windows x anchors): "
                 f"**{pp_global:.3f}**",
                 "- ROBUST requires a PER-ALT cell: corr(lead,residual)>0, p<0.05, max-dom<=40%, "
                 "n>=min, AND global p<0.05. A pooled-alts-only hit is downgraded to WEAK (within-hour "
                 "crypto beta makes pooled observations non-independent -- see caveats).", ""]
    md_lines += _cell_table(pmeta, pstat, p_flagged)
    md_lines += ["", "## Null canary: own-coin return -> own resolution (MUST be ~0)", "",
                 f"- canary family global p: {cp_global:.3f}; tripwire = ANY own-coin cell with "
                 "n>=min and uncorrected p<0.05 (deliberately sensitive).",
                 f"- **status: {'METHODOLOGY_SUSPECT ' + str(canary_hits) if canary_hits else 'CLEAN'}**", ""]
    md_lines += _canary_table(cmeta, cstat)
    md_lines += [""] + imb_lines
    md_lines += [
        "## Reading guide + caveats", "",
        "- **corr(leader,residual) > 0** is the only tradeable direction (BTC up -> alt over-wins "
        "vs price). The confound gap = corr(outcome) - corr(residual) shows how much of the raw "
        "correlation was just price moving with BTC.",
        "- **Per-alt cells are the clean unit** (one market per cell, distinct hours -> ~independent). "
        "**Pooled-alts is CAVEATED**: within one resolve-hour the 5 alts share THE SAME BTC return and "
        "are positively correlated via crypto beta, which the Bernoulli-independent null does NOT "
        "model -> pooled per-cell p can be anti-conservative. Hence pooled-only significance is WEAK.",
        "- The null canary is the methodology check: own-coin spot leaking into own residual is the "
        "established null, so a significant canary means stale-price/alignment artifact, not edge.",
        "- Regime/sample-limited (one ~24h window); imbalance side-channel is post-restart YOUNG. "
        "Read aggregate direction, not [thin] cells. Look-ahead + freshness guarded (YES/spot "
        f"at-or-before, <= {stale_max:.0f}s old); ~10s throttle << 5-15min windows.",
        "",
    ]
    md = "\n".join(md_lines) + "\n"

    # ----- csv -----
    csv_rows = []
    for fam, meta, stat in (("primary", pmeta, pstat), ("canary", cmeta, cstat)):
        pg = pp_global if fam == "primary" else cp_global
        for cid, (pair, win, anc) in meta.items():
            s = stat[cid]
            csv_rows.append([fam, pair, win, anc, s["n"], _r(s["corr_outcome"]),
                             _r(s["corr_residual"]), _r(s["gap"]), round(s["p"], 4), round(pg, 4),
                             _r(s["dominance"]), s["verdict"]])
    csv_rows += imb_rows

    _emit(args.out, md, csv_rows)
    print(verdict_line)
    print(canary_line)
    if alt_sparse:
        print(f">>> FRESHNESS NOTE: alts kept {alt_retain * 100:.0f}% of obs at {stale_max:.0f}s; "
              "try --staleness 60 (see Freshness cost in report). <<<")
    print()
    print(md)
    print(f"wrote: {args.out / 'cross_coin.md'}")
    print(f"wrote: {args.out / 'cross_coin.csv'}")


def _canary_table(meta, stat):
    lines = ["| coin | window | anchor | n | corr(own,outcome) | corr(own,residual) | p | status |",
             "|---|---|---|--:|--:|--:|--:|---|"]
    pair_order = {"pooled-all": 0}
    for k, s in enumerate(ALL_SYMBOLS, start=1):
        pair_order[s] = k
    anc_order = {a: i for i, (a, _) in enumerate(ANCHORS)}
    win_order = {w: i for i, (w, _) in enumerate(WINDOWS)}
    for cid in sorted(meta, key=lambda c: (pair_order.get(meta[c][0], 9),
                                           win_order.get(meta[c][1], 9),
                                           anc_order.get(meta[c][2], 9))):
        pair, win, anc = meta[cid]
        s = stat[cid]
        thin = "" if s["n"] >= MIN_OBS else " [thin]"
        flag = "  <-" if s["verdict"] == "SUSPECT" else ""
        lines.append(f"| {pair}{thin} | {win} | {anc} | {s['n']} | {_fmt(s['corr_outcome'])} | "
                     f"{_fmt(s['corr_residual'])} | {s['p']:.3f} | {s['verdict']}{flag} |")
    return lines


def _emit(out: Path, md: str, csv_rows: list) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "cross_coin.md").write_text(md, encoding="utf-8")
    header = ["family", "pair", "window", "anchor", "n", "corr_outcome", "corr_residual",
              "gap", "p_value", "p_global", "max_dominance", "verdict"]
    (out / "cross_coin.csv").write_text(_to_csv(header, csv_rows), encoding="utf-8")


if __name__ == "__main__":
    main()
