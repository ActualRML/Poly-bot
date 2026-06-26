"""ADVERSARIAL AUDIT CHECKS -- empirical questions code-reading cannot settle.

Written by the 2026-06-10 methodology audit of probe_orderbook_imbalance.py /
probe_return_calibration.py / probe_cross_coin.py. READ-ONLY: opens data/bot.db
mode=ro, never writes the DB, never writes any file (console report only).
Stdlib only -- runnable with ANY python. ASCII output (Windows console safe).

    python research/audit_checks.py                 # all checks
    python research/audit_checks.py --checks 2,3    # just the decision-critical ones
    python research/audit_checks.py --quick         # fewer MC iters (fast pass)

CHECKS
  1  Hour-clustering severity census: n vs distinct markets vs distinct resolve-
     HOURS for the key cells of the imbalance + return-calibration probes, plus
     the observed within-hour residual correlation rho_z and a design-effect hint.
     (The probes' Bernoulli nulls treat all observations as independent; the 6
     coins resolve on the same wall-clock hour with beta-correlated outcomes.)
  2  Hour-clustered null re-test -- THE decision number. Rebuilds both probes'
     observations and cells exactly, then recomputes per-cell and family-global
     p-values under (a) a per-MARKET independent null (one outcome draw per
     market, shared across its ttr-buckets/anchors -- fixes the per-row
     pseudo-replication in the probes' nulls) and (b) an hour-clustered null
     (Gaussian-copula common factor per resolve hour, latent rho calibrated so
     simulated within-hour z-correlation matches the observed rho_z).
     Survival rule printed with the table.
  3  Bid-vs-mid mirror: the stored book price is the YES best_BID; recomputes the
     overreaction calibration mirror with implied = bid / ask / mid from
     YES-token book rows at the same anchors (common-subset, apples-to-apples).
     If the up/down mirror collapses on mids, it is a QUOTE artifact, not
     miscalibration.
  4  Exclusion census: markets dropped by the decisive-price rule (ambiguous) +
     truncated, their characteristics; usable markets whose stream died BEFORE
     the recovered resolve hour ("ceiled" -- their outcome label is a
     pre-resolution price and their anchors shift); lingering stats.
  5  Price-semantics census: which event types / token sides actually feed the
     calibration probe's anchor joins (book row = bid if YES-token but ~ask if
     NO-token; price_change stores the changed LEVEL's price, not necessarily
     the touch); empirical |price_change - nearest fresh YES book bid| deltas vs
     a book-vs-book drift baseline; per-market flip sanity (both tokens of one
     market voting the same side = unflipped-NO disease).
  6  Momentum-ledger synthesis check: directly tests the FINDINGS claim that the
     momentum ledger loss is "exactly what overreaction predicts" -- joins each
     resolved momentum position to its own-coin trailing 15m spot return at
     entry, splits same-direction vs counter-direction entries, and compares
     actual win rate vs entry-implied per group.
  7  Outcome-label cross-check (POLARITY-AWARE since 2026-06-11): recovered
     outcome vs the sign of the Binance spot move over the market's hour, with
     the prediction FLIPPED for down-polarity markets (polarity read from
     research/diagnostics/label_truth.csv -- run research/verify_labels.py
     first). Residual large-|move| disagreements indicate label errors
     (near-zero moves can genuinely differ -- the official oracle is not
     Binance).

NOTE (2026-06-11 label-integrity fix): the recovery replica here uses the FIXED
touch-only decisive-price rule (book/last_trade_price rows only -- price_change
LEVEL prices poisoned the old rule; see src/backtest/recovery.py). `--labels
research/diagnostics/label_truth.csv` replaces recovered labels with API ground
truth from research/verify_labels.py (truth-only mode; ambiguous markets with a
truth row are promoted into the usable set).
"""
import argparse
import bisect
import csv
import math
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import fmean, median, quantiles

REPO_ROOT = Path(__file__).resolve().parents[1]
DB = REPO_ROOT / "data" / "bot.db"
LABELS_CSV = REPO_ROOT / "research" / "diagnostics" / "label_truth.csv"

EPS = 1e-6
YES_ABOVE, NO_BELOW = 0.9, 0.1               # recovery decisive rule (replicated)
LATE_MIN, MID_MIN = 10.0, 30.0               # imbalance ttr buckets
RET_WINDOW_SEC = 15 * 60
PRICE_BANDS = [("0.2-0.4", 0.2, 0.4), ("0.4-0.6", 0.4, 0.6), ("0.6-0.8", 0.6, 0.8)]
ANCHORS = [("T-45", 45 * 60), ("T-30", 30 * 60), ("T-15", 15 * 60)]
SQRT2 = math.sqrt(2.0)
P_CLAMP_LO, P_CLAMP_HI = 0.02, 0.98          # clamp for z-scores + copula draws


# --------------------------------------------------------------------------- #
# helpers (replicated from the probes -- semantics must match exactly)
# --------------------------------------------------------------------------- #
def _parse_ts(s: str) -> datetime:
    ts = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def round_to_hour(ts: datetime) -> datetime:
    floored = ts.replace(minute=0, second=0, microsecond=0)
    return floored + timedelta(hours=1) if ts.minute >= 30 else floored


def _clip01(p: float) -> float:
    return 0.0 if p < 0.0 else (1.0 if p > 1.0 else p)


def _pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx, my = fmean(xs), fmean(ys)
    sx = sum((x - mx) ** 2 for x in xs)
    sy = sum((y - my) ** 2 for y in ys)
    if sx <= 0 or sy <= 0:
        return None
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return cov / math.sqrt(sx * sy)


def _j_at_or_before(ts_list, t_epoch, max_stale):
    """Index of the LAST sample at-or-before t_epoch within max_stale, else None."""
    j = bisect.bisect_right(ts_list, t_epoch) - 1
    if j < 0:
        return None
    if t_epoch - ts_list[j] > max_stale:
        return None
    return j


def _at_or_before(index, key, t_epoch, max_stale):
    arr = index.get(key)
    if not arr:
        return None
    j = _j_at_or_before(arr[0], t_epoch, max_stale)
    return None if j is None else arr[1][j]


def _trailing_return(spot_index, symbol, anchor_epoch, window_sec, max_stale):
    p_now = _at_or_before(spot_index, symbol, anchor_epoch, max_stale)
    p_then = _at_or_before(spot_index, symbol, anchor_epoch - window_sec, max_stale)
    if p_now is None or p_then is None or p_then <= 0:
        return None
    return p_now / p_then - 1.0


def _band(p):
    for label, lo, hi in PRICE_BANDS:
        if lo <= p < hi:
            return label
    return None


def _ttr_bucket(ttr_secs):
    m = ttr_secs / 60.0
    if m <= LATE_MIN:
        return "late"
    if m <= MID_MIN:
        return "mid"
    return "early"


def _phi(x):
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def _pct(v):
    return "n/a" if v is None else f"{v * 100:.1f}"


def _pts(v):
    return "n/a" if v is None else f"{v * 100:+.1f}"


def _fmt(v, d=3):
    return "n/a" if v is None else f"{v:+.{d}f}"


# --------------------------------------------------------------------------- #
# loaders
# --------------------------------------------------------------------------- #
def recover_with_census(conn):
    """Replica of the FIXED recover_resolutions rule (2026-06-11, mirrors
    src/backtest/recovery.py): resolve_ts AND the decisive last price come ONLY
    from touch-bearing rows -- event_type IN ('book','last_trade_price') --
    NEVER price_change (level prices forged decisive labels under the old
    any-event rule). Keeps the EXCLUDED markets and per-market first/last tick
    for the census. usable[mid] = dict(...)."""
    gmax_row = conn.execute("SELECT MAX(ts) AS m FROM snapshots").fetchone()
    if gmax_row["m"] is None:
        return {}, {}, {}, None
    gmax = _parse_ts(gmax_row["m"])
    usable, ambiguous, truncated = {}, {}, {}
    rows = conn.execute(
        "SELECT market_id, MIN(ts) AS first_ts, MAX(ts) AS last_ts, COUNT(*) AS n "
        "FROM snapshots WHERE source='polymarket' AND market_id IS NOT NULL "
        "AND price IS NOT NULL AND event_type IN ('book','last_trade_price') "
        "GROUP BY market_id"
    ).fetchall()
    for r in rows:
        mid = r["market_id"]
        last_ts = _parse_ts(r["last_ts"])
        lp = conn.execute(
            "SELECT price FROM snapshots WHERE market_id=? AND price IS NOT NULL "
            "AND event_type IN ('book','last_trade_price') "
            "ORDER BY ts DESC LIMIT 1", (mid,)
        ).fetchone()
        if lp is None:
            continue
        info = {
            "first_ts": _parse_ts(r["first_ts"]), "last_ts": last_ts,
            "last_price": float(lp["price"]), "resolve_ts": round_to_hour(last_ts),
            "n": r["n"],
        }
        if info["resolve_ts"] > gmax:
            truncated[mid] = info
        elif info["last_price"] > YES_ABOVE:
            info["outcome"] = 1
            usable[mid] = info
        elif info["last_price"] < NO_BELOW:
            info["outcome"] = 0
            usable[mid] = info
        else:
            ambiguous[mid] = info
    return usable, ambiguous, truncated, gmax


def legacy_change_report(conn, usable, gmax):
    """One-line diff: legacy any-event decisive rule vs the FIXED touch-only rule
    (report-only; quantifies how many labels the 2026-06-11 fix changed)."""
    changed = dropped = added = 0
    rows = conn.execute(
        "SELECT market_id, MAX(ts) AS last_ts FROM snapshots "
        "WHERE source='polymarket' AND market_id IS NOT NULL AND price IS NOT NULL "
        "GROUP BY market_id"
    ).fetchall()
    for r in rows:
        mid = r["market_id"]
        lp = conn.execute(
            "SELECT price FROM snapshots WHERE market_id=? AND price IS NOT NULL "
            "ORDER BY ts DESC LIMIT 1", (mid,)
        ).fetchone()
        legacy = None
        if lp is not None and round_to_hour(_parse_ts(r["last_ts"])) <= gmax:
            p = float(lp["price"])
            legacy = 1 if p > YES_ABOVE else (0 if p < NO_BELOW else None)
        new = usable.get(mid)
        if legacy is not None and new is None:
            dropped += 1
        elif legacy is None and new is not None:
            added += 1
        elif legacy is not None and new is not None and new["outcome"] != legacy:
            changed += 1
    return (f"[label-rule fix] vs legacy any-event rule: {changed} label(s) changed, "
            f"{dropped} legacy-only dropped, {added} newly usable")


def apply_label_overrides_census(usable, ambiguous, labels_path, gmax):
    """--labels override: API ground truth from research/verify_labels.py REPLACES
    every recovered label (truth-only mode -- usable markets without a truth row are
    DROPPED, never mixed). Ambiguous markets carrying a truth are PROMOTED into the
    usable set (removes the decisive-rule exclusion bias); resolve_ts prefers the
    API end time when cached; markets ending past the stored stream stay excluded."""
    truth_rows = {}
    with open(labels_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            mid = (row.get("market_id") or "").strip()
            t = (row.get("truth") or "").strip().upper()
            if mid and t in ("YES", "NO"):
                truth_rows[mid] = (1 if t == "YES" else 0,
                                   (row.get("end_date_iso") or "").strip())
    out = {}
    promoted = 0
    for mid, (oc, end_iso) in truth_rows.items():
        src = usable.get(mid)
        if src is None:
            src = ambiguous.get(mid)
            if src is not None:
                promoted += 1
        if src is None:
            continue
        info = dict(src)
        info["outcome"] = oc
        if end_iso:
            try:
                rt = _parse_ts(end_iso)
            except ValueError:
                rt = None
            if rt is not None:
                if gmax is not None and rt > gmax:
                    continue
                info["resolve_ts"] = rt
        out[mid] = info
    print(f"[labels override] {labels_path}: {len(out)} usable from API truth "
          f"({promoted} promoted from ambiguous; recovery labels bypassed)")
    return out


def load_polarity_map(labels_path):
    """market_id -> 'up'/'down' from verify_labels.py's cache (check 7 needs it:
    a Down-polarity market's YES token means the price went DOWN)."""
    pol = {}
    try:
        with open(labels_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                mid = (row.get("market_id") or "").strip()
                p = (row.get("polarity") or "").strip().lower()
                if mid and p in ("up", "down"):
                    pol[mid] = p
    except OSError:
        return {}
    return pol


def yes_vote_detail(conn, usable):
    """Replica of label_yes_assets, but keeping the per-(market, asset) vote
    tallies for the flip-sanity census. Returns (yes_assets, detail)."""
    detail = defaultdict(lambda: defaultdict(lambda: [0, 0]))   # mid -> aid -> [yes, no]
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
        if yes_like == no_like:
            continue
        detail[r["market_id"]][r["asset_id"]][0 if yes_like else 1] += 1
    yes_assets = set()
    for mid, assets in detail.items():
        for aid, (y, n) in assets.items():
            if y > n:
                yes_assets.add(aid)
    return yes_assets, detail


def build_spot_index(conn):
    raw = defaultdict(list)
    rows = conn.execute(
        "SELECT ts, symbol, price FROM snapshots "
        "WHERE source='binance' AND price IS NOT NULL AND symbol IS NOT NULL ORDER BY ts"
    ).fetchall()
    for r in rows:
        raw[r["symbol"]].append((_parse_ts(r["ts"]).timestamp(), float(r["price"])))
    return {s: ([t for t, _ in seq], [p for _, p in seq]) for s, seq in raw.items()}


def build_price_index_typed(conn, usable, yes_assets, symbol_for):
    """Per-USABLE-market price stream with event_type + token-side tags, plus the
    majority symbol per market (tallied over ALL markets in symbol_for, so the
    exclusion census can label ambiguous markets too)."""
    idx = {}
    rows = conn.execute(
        "SELECT ts, market_id, symbol, price, event_type, asset_id FROM snapshots "
        "WHERE source='polymarket' AND market_id IS NOT NULL AND price IS NOT NULL "
        "ORDER BY ts"
    ).fetchall()
    for r in rows:
        mid = r["market_id"]
        if r["symbol"]:
            t = symbol_for.setdefault(mid, {})
            t[r["symbol"]] = t.get(r["symbol"], 0) + 1
        if mid not in usable:
            continue
        d = idx.get(mid)
        if d is None:
            d = idx[mid] = {"ts": [], "p": [], "et": [], "sd": []}
        d["ts"].append(_parse_ts(r["ts"]).timestamp())
        d["p"].append(float(r["price"]))
        d["et"].append(sys.intern(r["event_type"]))
        aid = r["asset_id"]
        d["sd"].append(0 if aid is None else (1 if aid in yes_assets else 2))
    return idx


def build_book_index(conn, yes_assets):
    """YES-token book rows only: market_id -> (ts, bid, ask) parallel lists.
    bid/ask may be None on one-sided rows."""
    idx = {}
    rows = conn.execute(
        "SELECT ts, market_id, asset_id, best_bid, best_ask FROM snapshots "
        "WHERE source='polymarket' AND event_type='book' AND asset_id IS NOT NULL "
        "ORDER BY ts"
    ).fetchall()
    for r in rows:
        if r["asset_id"] not in yes_assets:
            continue
        d = idx.get(r["market_id"])
        if d is None:
            d = idx[r["market_id"]] = {"ts": [], "bid": [], "ask": []}
        d["ts"].append(_parse_ts(r["ts"]).timestamp())
        d["bid"].append(r["best_bid"])
        d["ask"].append(r["best_ask"])
    return idx


# --------------------------------------------------------------------------- #
# observation builders (exact replicas of the probes' construction)
# --------------------------------------------------------------------------- #
def build_imb_obs(conn, usable, yes_assets):
    """Replica of probe_orderbook_imbalance: mean top-of-book imbalance + mean
    price per (market x ttr-bucket) over two-sided YES book rows, ttr>0."""
    agg = {}
    rows = conn.execute(
        "SELECT ts, market_id, asset_id, symbol, price, bid_size, ask_size, "
        "bid_depth, ask_depth FROM snapshots "
        "WHERE source='polymarket' AND event_type='book' AND asset_id IS NOT NULL "
        "AND price IS NOT NULL"
    ).fetchall()
    for r in rows:
        if r["asset_id"] not in yes_assets:
            continue
        info = usable.get(r["market_id"])
        if info is None:
            continue
        ttr = (info["resolve_ts"] - _parse_ts(r["ts"])).total_seconds()
        if ttr <= 0:
            continue
        bs, ask, bd, ad = r["bid_size"], r["ask_size"], r["bid_depth"], r["ask_depth"]
        if None in (bs, ask, bd, ad) or (bs + ask) <= 0 or (bd + ad) <= 0:
            continue
        key = (r["market_id"], _ttr_bucket(ttr))
        a = agg.setdefault(key, {"top": [], "px": [], "symbol": r["symbol"],
                                 "outcome": info["outcome"],
                                 "hour": info["resolve_ts"].isoformat()})
        a["top"].append((bs - ask) / (bs + ask))
        a["px"].append(r["price"])
    return [{
        "market_id": mid, "bucket": bucket, "symbol": a["symbol"],
        "sig": fmean(a["top"]), "price": fmean(a["px"]),
        "outcome": a["outcome"], "hour": a["hour"],
    } for (mid, bucket), a in agg.items()]


def build_calib_obs(usable, price_idx, market_symbol, spot_index, book_idx, stale):
    """Replica of probe_return_calibration's observations (band/direction at the
    stored-price basis) with EXTRA fields: the chosen row's event_type + token
    side, and the YES-token book bid/ask/mid at the same anchor (for check 3/5)."""
    obs = []
    for mid, info in usable.items():
        sym = market_symbol.get(mid)
        if not sym:
            continue
        d = price_idx.get(mid)
        if not d:
            continue
        for anc_label, anc_off in ANCHORS:
            t = (info["resolve_ts"] - timedelta(seconds=anc_off)).timestamp()
            j = _j_at_or_before(d["ts"], t, stale)
            if j is None:
                continue
            price = _clip01(d["p"][j])
            band = _band(price)
            if band is None:
                continue
            ret = _trailing_return(spot_index, sym, t, RET_WINDOW_SEC, stale)
            if ret is None or ret == 0:
                continue
            o = {
                "market_id": mid, "symbol": sym, "anchor": anc_label,
                "price": price, "outcome": info["outcome"], "ret": ret,
                "band": band, "dir": "up" if ret > 0 else "down",
                "hour": info["resolve_ts"].isoformat(),
                "etype": d["et"][j], "side": d["sd"][j],
                "bid": None, "ask": None, "mid": None,
            }
            bk = book_idx.get(mid)
            if bk:
                jb = _j_at_or_before(bk["ts"], t, stale)
                if jb is not None:
                    bb, ba = bk["bid"][jb], bk["ask"][jb]
                    o["bid"], o["ask"] = bb, ba
                    if bb is not None and ba is not None:
                        o["mid"] = (bb + ba) / 2.0
            obs.append(o)
    # magnitude terciles over |ret| (replica: pooled across anchors)
    if len(obs) >= 6:
        absr = sorted(abs(o["ret"]) for o in obs)
        try:
            q1, q2 = quantiles(absr, n=3)
            for o in obs:
                a = abs(o["ret"])
                o["mag"] = "mag1" if a < q1 else ("mag2" if a < q2 else "mag3")
        except Exception:
            for o in obs:
                o["mag"] = "n/a"
    else:
        for o in obs:
            o["mag"] = "n/a"
    return obs


# --------------------------------------------------------------------------- #
# clustered-null machinery
# --------------------------------------------------------------------------- #
def market_table(obs):
    """market_id -> {hour, p (clamped mean of its obs prices), idx list}."""
    mk = {}
    for i, o in enumerate(obs):
        m = mk.setdefault(o["market_id"], {"hour": o["hour"], "ps": [], "idx": []})
        m["ps"].append(o["price"])
        m["idx"].append(i)
    for m in mk.values():
        m["p"] = min(P_CLAMP_HI, max(P_CLAMP_LO, fmean(m["ps"])))
    return mk


def rho_z_from(triples):
    """Mean pairwise z-product within resolve hour. triples: (hour, p, y)."""
    by_hour = defaultdict(list)
    for hour, p, y in triples:
        by_hour[hour].append((y - p) / math.sqrt(p * (1 - p)))
    num, cnt = 0.0, 0
    for zs in by_hour.values():
        k = len(zs)
        if k < 2:
            continue
        s = sum(zs)
        ss = sum(z * z for z in zs)
        num += s * s - ss          # sum over ordered pairs i != j
        cnt += k * (k - 1)
    return (num / cnt) if cnt else 0.0


def calibrate_rho(mk, target, seed, sims=250):
    """Latent copula rho whose simulated within-hour z-correlation matches the
    observed target. Bisection on a noisy objective -- audit precision only."""
    if target <= 0:
        return 0.0
    items = [(m["hour"], m["p"]) for m in mk.values()]
    rng = random.Random(seed)

    def sim_rho(rho):
        sr, sc = math.sqrt(rho), math.sqrt(1.0 - rho)
        acc = 0.0
        for _ in range(sims):
            zh = {}
            triples = []
            for hour, p in items:
                z = zh.get(hour)
                if z is None:
                    z = zh[hour] = rng.gauss(0.0, 1.0)
                lat = sr * z + sc * rng.gauss(0.0, 1.0)
                triples.append((hour, p, 1 if _phi(lat) < p else 0))
            acc += rho_z_from(triples)
        return acc / sims

    lo, hi = 0.0, 0.95
    for _ in range(8):
        midp = (lo + hi) / 2.0
        if sim_rho(midp) < target:
            lo = midp
        else:
            hi = midp
    return (lo + hi) / 2.0


def clustered_mc(obs, mk, cells, rho, iters, seed):
    """Per-cell two-sided p + family-global max-|stat| p under a null that draws
    ONE outcome per MARKET (shared across its rows) with within-hour correlation
    rho via a Gaussian copula. cells: name -> {kind: 'corr'|'gap', idx, sig?}.
    Returns (obs_stat, p_cell, p_global)."""
    obs_stat = {}
    for name, c in cells.items():
        idx = c["idx"]
        if c["kind"] == "corr":
            res = [obs[i]["outcome"] - obs[i]["price"] for i in idx]
            obs_stat[name] = _pearson(c["sig"], res)
        else:
            obs_stat[name] = (fmean([obs[i]["outcome"] for i in idx])
                              - fmean([obs[i]["price"] for i in idx]))
    valid = {n for n, v in obs_stat.items() if v is not None}
    obs_max = max((abs(obs_stat[n]) for n in valid), default=0.0)

    rng = random.Random(seed)
    sr, sc = math.sqrt(rho), math.sqrt(1.0 - rho)
    mids = list(mk.keys())
    obs_mkt = [o["market_id"] for o in obs]
    ge = {n: 0 for n in cells}
    gmax_ge = 0
    for _ in range(iters):
        zh = {}
        ymk = {}
        for mid in mids:
            m = mk[mid]
            z = zh.get(m["hour"])
            if z is None:
                z = zh[m["hour"]] = rng.gauss(0.0, 1.0)
            lat = sr * z + sc * rng.gauss(0.0, 1.0)
            ymk[mid] = 1 if _phi(lat) < m["p"] else 0
        smax = 0.0
        for name, c in cells.items():
            idx = c["idx"]
            if c["kind"] == "corr":
                sim_res = [ymk[obs_mkt[i]] - obs[i]["price"] for i in idx]
                st = _pearson(c["sig"], sim_res)
            else:
                st = (fmean([ymk[obs_mkt[i]] for i in idx])
                      - fmean([obs[i]["price"] for i in idx]))
            if st is None:
                continue
            ob = obs_stat.get(name)
            if ob is not None and abs(st) >= abs(ob) - 1e-12:
                ge[name] += 1
            if name in valid and abs(st) > smax:
                smax = abs(st)
        if smax >= obs_max - 1e-12:
            gmax_ge += 1
    p_cell = {n: (ge[n] / iters if obs_stat.get(n) is not None else None) for n in cells}
    p_global = gmax_ge / iters if valid else None
    return obs_stat, p_cell, p_global


def cell_meta(obs, idx):
    mkts = {obs[i]["market_id"] for i in idx}
    hours = {obs[i]["hour"] for i in idx}
    return len(idx), len(mkts), len(hours)


# --------------------------------------------------------------------------- #
# cell constructions (mirroring each probe's families)
# --------------------------------------------------------------------------- #
def imb_cells(obs):
    cells = {"all": {"kind": "corr", "idx": list(range(len(obs)))}}
    for i, o in enumerate(obs):
        cells.setdefault(o["bucket"], {"kind": "corr", "idx": []})["idx"].append(i)
        cells.setdefault(f"sym={o['symbol'] or '(none)'}",
                         {"kind": "corr", "idx": []})["idx"].append(i)
    for c in cells.values():
        c["sig"] = [obs[i]["sig"] for i in c["idx"]]
    return cells


def calib_primary_cells(obs):
    cells = {}
    for i, o in enumerate(obs):
        cells.setdefault(f"{o['band']}|{o['dir']}|{o['anchor']}",
                         {"kind": "gap", "idx": []})["idx"].append(i)
    return cells


def calib_mag_cells(obs):
    cells = {}
    for i, o in enumerate(obs):
        if o.get("mag") in (None, "n/a"):
            continue
        cells.setdefault(f"{o['dir']}|{o['mag']}|{o['anchor']}",
                         {"kind": "gap", "idx": []})["idx"].append(i)
    return cells


def calib_mirror_cells(obs):
    cells = {}
    for i, o in enumerate(obs):
        cells.setdefault(f"{o['anchor']}|{o['dir']}",
                         {"kind": "gap", "idx": []})["idx"].append(i)
    return cells


KEY_IMB = ["all", "early", "mid", "late", "sym=BTC", "sym=BNB"]
KEY_CALIB = ["0.6-0.8|up|T-45", "0.6-0.8|up|T-30", "0.6-0.8|up|T-15",
             "0.2-0.4|down|T-45"]
KEY_MAG = ["up|mag3|T-45", "down|mag3|T-45", "up|mag3|T-30", "down|mag3|T-30"]
KEY_MIRROR = ["T-45|up", "T-45|down", "T-30|up", "T-30|down", "T-15|up", "T-15|down"]


# --------------------------------------------------------------------------- #
# CHECK 1 + 2 -- clustering census + clustered re-test
# --------------------------------------------------------------------------- #
def run_checks_1_2(imb_obs, calib_obs, iters, seed, do_mc):
    print("=" * 78)
    print("CHECK 1 -- hour-clustering severity census (n vs markets vs distinct hours)")
    print("=" * 78)
    families = []
    if imb_obs:
        families.append(("imbalance corr(imb_top, residual)", imb_obs, imb_cells(imb_obs),
                         KEY_IMB))
    if calib_obs:
        families.append(("calibration gap: primary band|dir|anchor", calib_obs,
                         calib_primary_cells(calib_obs), KEY_CALIB))
        families.append(("calibration gap: |ret| tercile dir|mag|anchor", calib_obs,
                         calib_mag_cells(calib_obs), KEY_MAG))
        families.append(("calibration gap: pooled mirror anchor|dir", calib_obs,
                         calib_mirror_cells(calib_obs), KEY_MIRROR))

    fam_data = []
    for fname, obs, cells, keys in families:
        mk = market_table(obs)
        triples = [(m["hour"], m["p"], obs[m["idx"][0]]["outcome"]) for m in mk.values()]
        rho_z = rho_z_from(triples)
        print(f"\n[{fname}]  obs={len(obs)}  markets={len(mk)}  "
              f"distinct resolve-hours={len({m['hour'] for m in mk.values()})}  "
              f"observed within-hour z-corr rho_z={rho_z:+.3f}")
        print(f"  {'cell':<22} {'n':>5} {'mkts':>5} {'hours':>6} {'mkts/hour':>10} {'~deff':>6}")
        for k in keys:
            c = cells.get(k)
            if not c:
                print(f"  {k:<22}  (cell not present in current data)")
                continue
            n, nm, nh = cell_meta(obs, c["idx"])
            mbar = nm / nh if nh else float("nan")
            deff = 1.0 + (mbar - 1.0) * max(0.0, rho_z)
            print(f"  {k:<22} {n:>5} {nm:>5} {nh:>6} {mbar:>10.2f} {deff:>6.2f}")
        print("  (~deff = 1+(mkts/hour-1)*rho_z: crude variance-inflation hint for GAP")
        print("   cells; the MC below is the real answer. corr cells also depend on the")
        print("   signal's own within-hour correlation.)")
        fam_data.append((fname, obs, cells, keys, mk, rho_z))

    if not do_mc:
        return

    print()
    print("=" * 78)
    print("CHECK 2 -- clustered-null re-test (one outcome per MARKET; hour copula)")
    print("=" * 78)
    print("p_indep  = per-market independent null (already stricter than the probes'")
    print("           per-ROW null: a market's buckets/anchors share one outcome draw)")
    print("p_clust  = same + within-hour Gaussian-copula correlation at calibrated rho")
    print("Survival rule: a canonized cell keeps its label only if p_clust < 0.05 AND")
    print("the family-global p_clust < 0.05.")
    for fi, (fname, obs, cells, keys, mk, rho_z) in enumerate(fam_data):
        rho_lat = calibrate_rho(mk, rho_z, seed + 11 + fi)
        st0, p0, g0 = clustered_mc(obs, mk, cells, 0.0, iters, seed + 100 + fi)
        st1, p1, g1 = clustered_mc(obs, mk, cells, rho_lat, iters, seed + 200 + fi)
        print(f"\n[{fname}]  rho_z={rho_z:+.3f} -> latent rho={rho_lat:.2f}  iters={iters}")
        print(f"  {'cell':<22} {'n':>5} {'stat':>8} {'p_indep':>8} {'p_clust':>8}")
        for k in keys:
            if k not in cells:
                continue
            n = len(cells[k]["idx"])
            s = st1.get(k)
            sv = "n/a" if s is None else f"{s:+.3f}"
            pv0 = "n/a" if p0.get(k) is None else f"{p0[k]:.3f}"
            pv1 = "n/a" if p1.get(k) is None else f"{p1[k]:.3f}"
            print(f"  {k:<22} {n:>5} {sv:>8} {pv0:>8} {pv1:>8}")
        gl0 = "n/a" if g0 is None else f"{g0:.3f}"
        gl1 = "n/a" if g1 is None else f"{g1:.3f}"
        print(f"  {'FAMILY GLOBAL (max-stat)':<36} {gl0:>8} {gl1:>8}")
    print("\nNOTE: stats are recomputed on TODAY's DB; they will differ slightly from")
    print("the canonized numbers (the DB has grown since those runs).")


# --------------------------------------------------------------------------- #
# CHECK 3 -- bid vs ask vs mid mirror
# --------------------------------------------------------------------------- #
def run_check_3(calib_obs):
    print()
    print("=" * 78)
    print("CHECK 3 -- overreaction mirror on bid / ask / mid (quote-artifact test)")
    print("=" * 78)
    common = [o for o in calib_obs if o["mid"] is not None]
    print(f"calibration obs total={len(calib_obs)}; with a fresh two-sided YES book "
          f"row at the anchor={len(common)} (common subset below)")
    if len(common) < 10:
        print("too few two-sided-book obs -- cannot run this check yet")
        return
    print(f"\n  {'anchor':<6} {'dir':<5} {'n':>4} {'actual%':>8} | "
          f"{'impl_stored':>11} {'gap':>6} | {'impl_bid':>8} {'gap':>6} | "
          f"{'impl_mid':>8} {'gap':>6} | {'impl_ask':>8} {'gap':>6}")
    for anc, _ in ANCHORS:
        for d in ("up", "down"):
            sel = [o for o in common if o["anchor"] == anc and o["dir"] == d]
            if not sel:
                continue
            act = fmean([o["outcome"] for o in sel])
            ist = fmean([o["price"] for o in sel])
            ibd = fmean([o["bid"] for o in sel])
            imd = fmean([o["mid"] for o in sel])
            iak = fmean([o["ask"] for o in sel])
            print(f"  {anc:<6} {d:<5} {len(sel):>4} {_pct(act):>8} | "
                  f"{_pct(ist):>11} {_pts(act - ist):>6} | {_pct(ibd):>8} {_pts(act - ibd):>6} | "
                  f"{_pct(imd):>8} {_pts(act - imd):>6} | {_pct(iak):>8} {_pts(act - iak):>6}")
    print("\nREAD: if the up/down mirror (up gap<0, down gap>0 at T-45/T-30) persists on")
    print("MID, it is not a bid-quote artifact. If gaps shrink toward ~0 on mid (and the")
    print("bid/ask gaps straddle it), the 'overreaction' lives in the quote, not the")
    print("market's calibration. Also compare stored vs bid: differences there measure")
    print("how much the mixed stored basis (NO-token rows ~ ask) distorts the probe.")


# --------------------------------------------------------------------------- #
# CHECK 4 -- exclusion census
# --------------------------------------------------------------------------- #
def run_check_4(usable, ambiguous, truncated, market_symbol):
    print()
    print("=" * 78)
    print("CHECK 4 -- exclusion census (decisive-price rule selection effects)")
    print("=" * 78)
    total = len(usable) + len(ambiguous) + len(truncated)
    print(f"markets: total={total}  usable={len(usable)}  "
          f"ambiguous-excluded={len(ambiguous)}  truncated={len(truncated)}")

    if ambiguous:
        buckets = defaultdict(int)
        sym_cnt = defaultdict(int)
        for mid, info in ambiguous.items():
            lp = info["last_price"]
            b = ("0.10-0.30" if lp < 0.3 else "0.30-0.50" if lp < 0.5
                 else "0.50-0.70" if lp < 0.7 else "0.70-0.90")
            buckets[b] += 1
            sym_cnt[market_symbol.get(mid, "(none)")] += 1
        print("\nambiguous markets -- final-price distribution: "
              + ", ".join(f"{k}={buckets[k]}" for k in sorted(buckets)))
        print("ambiguous markets -- by symbol: "
              + ", ".join(f"{k}={sym_cnt[k]}" for k in sorted(sym_cnt)))
        mins_to_hour = [abs((info["resolve_ts"] - info["last_ts"]).total_seconds()) / 60
                        for info in ambiguous.values()]
        print(f"ambiguous -- |last_ts - nearest hour| minutes: "
              f"median={median(mins_to_hour):.1f} max={max(mins_to_hour):.1f}")

    ceiled = {m: i for m, i in usable.items() if i["last_ts"] < i["resolve_ts"]}
    print(f"\nusable markets whose stream DIED BEFORE the recovered resolve hour "
          f"(ceiled): {len(ceiled)} / {len(usable)}")
    print("  (their outcome label is a PRE-resolution price >0.9/<0.1 and their")
    print("   anchors/ttr are measured against an hour the stream never reached;")
    print("   this also catches >30min lingerers whose resolve_ts got bumped +1h)")
    if ceiled:
        rows = []
        for mid, i in ceiled.items():
            early_min = (i["resolve_ts"] - i["last_ts"]).total_seconds() / 60
            life_min = (i["last_ts"] - i["first_ts"]).total_seconds() / 60
            rows.append((early_min, mid, market_symbol.get(mid, "?"),
                         i["last_price"], life_min))
        rows.sort(reverse=True)
        em = [r[0] for r in rows]
        print(f"  minutes-early: median={median(em):.1f}  max={max(em):.1f}")
        print(f"  {'market':<14} {'sym':<5} {'min_early':>9} {'last_price':>10} {'life_min':>9}")
        for early_min, mid, sym, lp, life in rows[:10]:
            print(f"  {mid[:12] + '..':<14} {str(sym):<5} {early_min:>9.1f} {lp:>10.3f} {life:>9.0f}")

    lingers = [(i["last_ts"] - i["resolve_ts"]).total_seconds() / 60
               for i in usable.values() if i["last_ts"] >= i["resolve_ts"]]
    if lingers:
        print(f"\nfloored (normal) markets -- linger minutes past resolve: "
              f"median={median(lingers):.1f}  p95~{sorted(lingers)[int(0.95 * (len(lingers) - 1))]:.1f}  "
              f"max={max(lingers):.1f}  (recovery docstring expects a few..~19)")

    lifespans = [(i["last_ts"] - i["first_ts"]).total_seconds() / 60 for i in usable.values()]
    if lifespans:
        short = sum(1 for v in lifespans if v < 45)
        print(f"usable market lifespans: median={median(lifespans):.0f}min  "
              f"<45min coverage: {short} markets (anchor availability bias)")


# --------------------------------------------------------------------------- #
# CHECK 5 -- price semantics census
# --------------------------------------------------------------------------- #
def run_check_5(conn, calib_obs, usable, price_idx, book_idx, detail):
    print()
    print("=" * 78)
    print("CHECK 5 -- price-semantics census (what 'price' actually is, per join)")
    print("=" * 78)

    rows = conn.execute(
        "SELECT event_type, COUNT(*) AS n FROM snapshots "
        "WHERE source='polymarket' AND price IS NOT NULL GROUP BY event_type"
    ).fetchall()
    print("(a) polymarket price-bearing rows by event_type: "
          + ", ".join(f"{r['event_type']}={r['n']}" for r in rows))

    if calib_obs:
        cnt = defaultdict(int)
        side_cnt = defaultdict(int)
        for o in calib_obs:
            cnt[(o["anchor"], o["etype"])] += 1
            side_cnt[(o["etype"], o["side"])] += 1
        print("\n(b) calibration anchor joins land on:")
        for (anc, et), n in sorted(cnt.items()):
            print(f"    {anc:<6} {et:<18} {n}")
        print("    by (event_type, token side): "
              + ", ".join(f"{et}/{'YES' if sd == 1 else 'NO' if sd == 2 else '?'}={n}"
                          for (et, sd), n in sorted(side_cnt.items())))
        print("    NOTE: book/YES = best_bid; book/NO = 1 - NO_bid ~ YES ASK;")
        print("    price_change = the changed LEVEL's price (not necessarily touch);")
        print("    last_trade_price = trade print. The probe treats all as P(YES).")

    # (c) price_change vs nearest fresh YES book bid
    pc_deltas, bk_deltas = [], []
    for mid in usable:
        d = price_idx.get(mid)
        bk = book_idx.get(mid)
        if not d or not bk:
            continue
        for i, et in enumerate(d["et"]):
            if et == "price_change" and len(pc_deltas) < 30000:
                j = _j_at_or_before(bk["ts"], d["ts"][i], 10.0)
                if j is not None and bk["bid"][j] is not None:
                    pc_deltas.append(abs(d["p"][i] - bk["bid"][j]))
        ts_b = bk["ts"]
        for j in range(1, len(ts_b)):
            if len(bk_deltas) >= 30000:
                break
            if ts_b[j] - ts_b[j - 1] <= 10.0 and bk["bid"][j] is not None \
                    and bk["bid"][j - 1] is not None:
                bk_deltas.append(abs(bk["bid"][j] - bk["bid"][j - 1]))
        if len(pc_deltas) >= 30000 and len(bk_deltas) >= 30000:
            break

    def _dstats(name, ds):
        if len(ds) < 20:
            print(f"    {name}: too few samples ({len(ds)})")
            return
        qs = quantiles(sorted(ds), n=20)
        share2 = sum(1 for v in ds if v > 0.02) / len(ds)
        share10 = sum(1 for v in ds if v > 0.10) / len(ds)
        print(f"    {name}: n={len(ds)}  p50={qs[9]:.4f}  p90={qs[17]:.4f}  "
              f"p95={qs[18]:.4f}  max={max(ds):.3f}  >2c: {share2 * 100:.1f}%  "
              f">10c: {share10 * 100:.1f}%")

    print("\n(c) |stored price - fresh YES best_bid| (<=10s):")
    _dstats("price_change rows vs book bid", pc_deltas)
    _dstats("book-vs-prev-book bid drift (baseline)", bk_deltas)
    print("    READ: if price_change deltas have fat tails vs the baseline, those rows")
    print("    carry LEVEL prices, contaminating anchor joins that land on them.")

    # (d) flip sanity: per usable market, the two assets must vote OPPOSITE sides
    both_same = mixed = single_asset = multi_asset = clean = 0
    examples = []
    for mid, assets in detail.items():
        sides = []
        is_mixed = False
        for aid, (y, n) in assets.items():
            tot = y + n
            if tot and min(y, n) / tot > 0.10:
                is_mixed = True
            sides.append(1 if y > n else 0)
        if len(assets) == 1:
            single_asset += 1
        elif len(assets) > 2:
            multi_asset += 1
        if is_mixed:
            mixed += 1
            if len(examples) < 5:
                examples.append((mid, "mixed-votes", dict(assets)))
        if len(sides) >= 2 and len(set(sides)) == 1:
            both_same += 1
            if len(examples) < 5:
                examples.append((mid, "both-tokens-same-side", dict(assets)))
        if len(sides) == 2 and len(set(sides)) == 2 and not is_mixed:
            clean += 1
    print(f"\n(d) flip sanity over {len(detail)} usable markets with book votes:")
    print(f"    clean (two tokens, opposite sides, <10% minority votes): {clean}")
    print(f"    BOTH tokens vote the SAME side (unflipped-NO suspect):   {both_same}")
    print(f"    a token with >10% minority votes (mixed):                {mixed}")
    print(f"    single-asset-only markets: {single_asset}   >2 assets: {multi_asset}")
    for mid, why, assets in examples:
        print(f"      e.g. {mid[:12]}.. {why}: "
              + "; ".join(f"{a[:10]}..=[y{v[0]}/n{v[1]}]" for a, v in assets.items()))


# --------------------------------------------------------------------------- #
# CHECK 6 -- momentum ledger vs the overreaction synthesis
# --------------------------------------------------------------------------- #
def run_check_6(conn, spot_index):
    print()
    print("=" * 78)
    print("CHECK 6 -- momentum ledger: does 'overreaction explains the loss' hold?")
    print("=" * 78)
    rows = conn.execute(
        "SELECT ts, symbol, side, entry_price, pnl_usdc FROM positions "
        "WHERE strategy='momentum' AND status='resolved' AND pnl_usdc IS NOT NULL "
        "AND entry_price IS NOT NULL"
    ).fetchall()
    if not rows:
        print("(no resolved momentum positions)")
        return
    total_pnl = sum(float(r["pnl_usdc"]) for r in rows)
    print(f"resolved momentum trades: n={len(rows)}  total pnl=${total_pnl:+.2f} "
          f"(ledger pnl is at the 3c SLIPPAGE_BUFFER)")
    print("(FINDINGS quotes -$166.95 over 95 trades -- reconcile against the line above)")

    groups = defaultdict(list)
    for r in rows:
        ret = None
        if r["symbol"]:
            ret = _trailing_return(spot_index, r["symbol"],
                                   _parse_ts(r["ts"]).timestamp(), RET_WINDOW_SEC, 60.0)
        if ret is None:
            g = "no-ret15"
        elif ret == 0:
            g = "flat"
        else:
            same = (r["side"] == "YES" and ret > 0) or (r["side"] == "NO" and ret < 0)
            g = "same-direction" if same else "counter-direction"
        groups[g].append(r)

    print(f"\n  {'entry group':<18} {'n':>4} {'implied%':>9} {'actual%':>8} "
          f"{'gap(pt)':>8} {'pnl$':>9}")
    for g in ("same-direction", "counter-direction", "flat", "no-ret15"):
        sel = groups.get(g, [])
        if not sel:
            continue
        implied = fmean([float(r["entry_price"]) for r in sel])
        actual = fmean([1.0 if float(r["pnl_usdc"]) > 0 else 0.0 for r in sel])
        pnl = sum(float(r["pnl_usdc"]) for r in sel)
        print(f"  {g:<18} {len(sel):>4} {_pct(implied):>9} {_pct(actual):>8} "
              f"{_pts(actual - implied):>8} {pnl:>9.2f}")
    print("\nREAD: the synthesis ('momentum buys the favorite right after the run-up")
    print("that created it; those cells run 17-28pt below implied') predicts: the")
    print("same-direction group dominates n AND shows a strongly negative gap, while")
    print("counter-direction is ~calibrated. A uniform small negative gap across both")
    print("groups instead means plain zero-edge + cost, and the FINDINGS synthesis is")
    print("overstated. NOTE: 'won' here = pnl>0 (3c-slippage ledger), so the implied-vs-")
    print("actual comparison is slightly pessimistic for entries near 0.97+.")


# --------------------------------------------------------------------------- #
# CHECK 7 -- recovered outcome vs spot move over the hour
# --------------------------------------------------------------------------- #
def run_check_7(usable, market_symbol, spot_index, polarity_map):
    print()
    print("=" * 78)
    print("CHECK 7 -- recovered outcome vs Binance spot move (POLARITY-AWARE)")
    print("=" * 78)
    if polarity_map:
        print(f"polarity source: label_truth.csv ({len(polarity_map)} markets with "
              f"up/down polarity); pred is FLIPPED for down-polarity markets")
    else:
        print("WARNING: no polarity info found -- run research/verify_labels.py first")
        print("to build research/diagnostics/label_truth.csv. Falling back to a")
        print("polarity-BLIND comparison: a ~50% flat agreement here may just mean")
        print("mixed market polarity, NOT label corruption.")
    recs = []
    for mid, info in usable.items():
        sym = market_symbol.get(mid)
        if not sym:
            continue
        t_end = info["resolve_ts"].timestamp()
        s_end = _at_or_before(spot_index, sym, t_end, 90.0)
        s_start = _at_or_before(spot_index, sym, t_end - 3600.0, 90.0)
        if s_end is None or s_start is None or s_start <= 0 or s_end == s_start:
            continue
        move = s_end / s_start - 1.0
        pred_raw = 1 if move > 0 else 0
        pol = polarity_map.get(mid, "?")
        pred = (1 - pred_raw) if pol == "down" else pred_raw
        recs.append((abs(move), move, pred, info["outcome"], mid, sym, pol))
    if len(recs) < 10:
        print(f"too few markets with spot coverage at both hour boundaries ({len(recs)})")
        return
    by_pol = defaultdict(lambda: [0, 0])
    for r in recs:
        by_pol[r[6]][0] += 1
        if r[2] == r[3]:
            by_pol[r[6]][1] += 1
    print(f"\nmarkets checked: {len(recs)}; polarity split: "
          + ", ".join(f"{k}={v[0]}" for k, v in sorted(by_pol.items())))
    for k, (n, a) in sorted(by_pol.items()):
        tag = " (no polarity info -- raw spot-up pred)" if k == "?" else ""
        print(f"  polarity {k:<4} n={n:>4}  agree={a:>4} ({a / n * 100:5.1f}%){tag}")
    aware = [r for r in recs if r[6] in ("up", "down")]
    label = "polarity-aware"
    if not aware:
        aware = recs
        label = "polarity-BLIND (no polarity info)"
    agree = sum(1 for r in aware if r[2] == r[3])
    print(f"{label} agreement over {len(aware)} markets: {agree} "
          f"({agree / len(aware) * 100:.1f}%)")
    aware.sort()
    k3 = len(aware) // 3
    for name, chunk in (("small |move|", aware[:k3]), ("mid |move|", aware[k3:2 * k3]),
                        ("large |move|", aware[2 * k3:])):
        if not chunk:
            continue
        a = sum(1 for r in chunk if r[2] == r[3])
        mism = len(chunk) - a
        print(f"  {name:<14} n={len(chunk):>4}  mismatches={mism:>3} "
              f"({mism / len(chunk) * 100:.1f}%)  |move| range "
              f"{chunk[0][0] * 100:.3f}%..{chunk[-1][0] * 100:.3f}%")
    mismatches = [r for r in aware if r[2] != r[3]]
    mismatches.sort(reverse=True)
    if mismatches:
        print("\n  largest-|move| mismatches (label suspects after polarity adjustment):")
        print(f"  {'market':<14} {'sym':<5} {'pol':<4} {'move%':>8} {'pred':>5} {'recovered':>9}")
        for am, move, pred, out, mid, sym, pol in mismatches[:10]:
            print(f"  {mid[:12] + '..':<14} {sym:<5} {pol:<4} {move * 100:>8.3f} "
                  f"{'YES' if pred else 'NO':>5} {'YES' if out else 'NO':>9}")
    print("\nREAD: near-zero-move disagreements are expected (oracle != Binance; open")
    print("vs close conventions). With polarity applied, residual LARGE-move")
    print("disagreements mean the label itself is wrong (recovery rule or, under")
    print("--labels, the API truth row) -- each one poisons every probe that consumed")
    print("it. Cross-check candidates against verify_labels.py's residual-mismatch list.")


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(prog="python research/audit_checks.py",
                                 description=__doc__)
    ap.add_argument("--db", type=Path, default=DB)
    ap.add_argument("--iters", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=777)
    ap.add_argument("--staleness", type=float, default=30.0)
    ap.add_argument("--labels", type=Path, default=None,
                    help="optional label_truth.csv from research/verify_labels.py; when "
                         "given, API ground truth REPLACES the recovery rule (check 7 "
                         "also auto-reads polarity from the default cache if present)")
    ap.add_argument("--checks", type=str, default="1,2,3,4,5,6,7")
    ap.add_argument("--quick", action="store_true", help="iters=800")
    args = ap.parse_args()
    iters = 800 if args.quick else args.iters
    wanted = {c.strip() for c in args.checks.split(",") if c.strip()}

    if not args.db.exists():
        print(f"DB not found: {args.db}")
        return
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        print(f"audit_checks: db={args.db}  seed={args.seed}  iters={iters}  "
              f"staleness={args.staleness:.0f}s  checks={sorted(wanted)}")
        usable, ambiguous, truncated, gmax = recover_with_census(conn)
        print(f"recovery replica (touch-only rule): usable={len(usable)}  "
              f"ambiguous={len(ambiguous)}  truncated={len(truncated)}  "
              f"gmax={gmax.isoformat() if gmax else 'n/a'}")
        if gmax is not None:
            print(legacy_change_report(conn, usable, gmax))
        if args.labels and args.labels.exists():
            usable = apply_label_overrides_census(usable, ambiguous, args.labels, gmax)
        yes_assets, detail = yes_vote_detail(conn, usable)
        spot_index = build_spot_index(conn)
        symbol_tally: dict = {}
        price_idx = build_price_index_typed(conn, usable, yes_assets, symbol_tally)
        market_symbol = {m: max(t, key=t.get) for m, t in symbol_tally.items()}
        book_idx = build_book_index(conn, yes_assets)

        calib_obs = build_calib_obs(usable, price_idx, market_symbol, spot_index,
                                    book_idx, args.staleness)
        imb_obs = build_imb_obs(conn, usable, yes_assets)
        print(f"replica observations: calibration={len(calib_obs)}  "
              f"imbalance={len(imb_obs)}")

        if "1" in wanted or "2" in wanted:
            run_checks_1_2(imb_obs, calib_obs, iters, args.seed, do_mc=("2" in wanted))
        if "3" in wanted:
            run_check_3(calib_obs)
        if "4" in wanted:
            run_check_4(usable, ambiguous, truncated, market_symbol)
        if "5" in wanted:
            run_check_5(conn, calib_obs, usable, price_idx, book_idx, detail)
        if "6" in wanted:
            run_check_6(conn, spot_index)
        if "7" in wanted:
            labels_path = args.labels if args.labels else LABELS_CSV
            polarity_map = load_polarity_map(labels_path) if labels_path.exists() else {}
            run_check_7(usable, market_symbol, spot_index, polarity_map)
        print("\ndone (read-only -- nothing was written).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
