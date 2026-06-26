"""READ-ONLY probe: does YES-side order-book IMBALANCE predict a market's RESOLUTION
*beyond what the YES price already implies*?

Price approximately equals win rate is our through-line, so any test that doesn't control
for price is confounded. The real statistic here is corr(imbalance, RESIDUAL) where
residual = outcome(1/0) - yes_price; we report it next to the raw corr(imbalance, outcome)
so the price confound is explicit. Expected informative sign is POSITIVE (YES bid pressure
-> YES over-wins vs price). Default expectation: NO_SIGNAL (efficient market).

STANDALONE: stdlib sqlite3 only, opens data/bot.db mode=ro, never writes the DB. Runnable
with ANY python (no venv/aiosqlite) -- so the resolution rule and the Bernoulli-null
machinery are REPLICATED here rather than imported (importing src.backtest.recovery would
pull src.backtest.__init__ -> engine -> src.main -> aiosqlite, defeating "any python").
Mirrors the conventions of research/probe_loss_structure.py (per-cell + global-max
Bernoulli(price) null, seeded; CSV/markdown into research/diagnostics/). ASCII output only
(Windows console safe).

HOW THE YES-SIDE BOOK ROW IS IDENTIFIED (verified, not guessed):
  parsers.parse_polymarket sets, for a `book` event, `price = best_bid` (the ticking
  TOKEN's own raw best bid). main.py then normalizes ONLY price for the NO token:
      snapshot.outcome = outcome_lookup.get(asset_id)
      if snapshot.outcome == "NO": snapshot.price = 1.0 - snapshot.price
  best_bid/best_ask and the four size/depth columns are NEVER flipped. The snapshots table
  stores no token-side column. THEREFORE a stored book row is the YES token iff its stored
  (YES-normalized) `price` still equals its own `best_bid` (price was not flipped); the NO
  token row instead has `price == 1 - best_bid`. We label each asset_id by majority vote of
  its book rows (robust to the price==0.5 tie) and use ONLY YES-token rows -- so YES-side
  imbalance is read directly and the two-rows-per-update stream is never double-counted.

ORTHOGONALIZATION UPGRADE (signal-vs-price confound fix; FINDINGS "Signal-vs-price confound"): the
residual controls the OUTCOME against price but not the SIGNAL. We now also pair each YES book row
with its YES price AND the own-coin trailing 15m spot return (ret15) at the SAME timestamp (at-or-
before + 30s staleness binance join, inherited from the fixed probe_cross_coin.py), aggregated the
SAME way as the signal (MEAN over the market x ttr-bucket), and report per cell: corr(sig,price),
corr(sig,ret15), partial(sig,residual|price) and partial(sig,residual|price+ret15) -- OLS-residualize
the SIGNAL, then correlate with the outcome residual (same Bernoulli(price) MC null + global max-stat
+ dominance). Depth imbalance is a built-in POSITIVE CONTROL: as the known price-proxy it MUST
collapse after partialling on price, else the machinery is suspect. All existing columns/gates are
unchanged; this is added on top.
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
YES_ABOVE, NO_BELOW = 0.9, 0.1        # recovery.py decisive-resolution rule
LATE_MIN, MID_MIN = 10.0, 30.0        # ttr buckets: late<=10m, mid<=30m, early>30m
MIN_OBS = 10                          # cells thinner than this are [thin] / not verdict-eligible
DOMINANCE = 0.40                      # one market may be at most 40% of a cell's covariance
# Orthogonalization upgrade: pair each book row's imbalance with the YES price AND own-coin ret15 at
# the SAME timestamp, then residualize the signal against them (signal-vs-price confound fix).
STALENESS_MAX_SEC = 30.0              # spot sample must be <= this old, AT-OR-BEFORE the book row ts (inherited)
RET_WINDOW_SEC = 15 * 60             # own-coin trailing return window (ret15) -- the overreaction channel
COLLINEAR_MAX = 0.95                 # |corr(price,ret15)| above this in a cell -> fall back to price-only
MACHINERY_THRESH = 0.15             # positive control: |partial(depth|price)| above this & p<0.05 = SUSPECT
MATERIAL_SHRINK = 0.50              # partial-vs-raw shrinkage >= this on 'all' -> ATTENUATED not SURVIVES


# --------------------------------------------------------------------------- #
# small stats helpers
# --------------------------------------------------------------------------- #
def _parse_ts(s: str) -> datetime:
    ts = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def round_to_hour(ts: datetime) -> datetime:
    """Nearest hour boundary -- replicated from src/backtest/recovery.round_to_hour."""
    floored = ts.replace(minute=0, second=0, microsecond=0)
    return floored + timedelta(hours=1) if ts.minute >= 30 else floored


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


def _ttr_bucket(ttr_secs: float) -> str:
    m = ttr_secs / 60.0
    if m <= LATE_MIN:
        return "late"
    if m <= MID_MIN:
        return "mid"
    return "early"


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
    """asset_ids whose book rows are predominantly YES-token (stored price == own
    best_bid, i.e. price was NOT flipped). Majority vote per asset_id is robust to the
    price==best_bid==0.5 tie."""
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
        if yes_like == no_like:            # tie (p==ref==0.5) or neither -> skip
            continue
        t = tally.setdefault(r["asset_id"], [0, 0])
        t[0 if yes_like else 1] += 1
    return {aid for aid, (y, n) in tally.items() if y > n}


# --------------------------------------------------------------------------- #
# Bernoulli(price) null -- mirrors probe_loss_structure.mc_pvalues
# --------------------------------------------------------------------------- #
def mc_corr_pvalues(all_obs, cells_idx, imb_key, obs_corr_res, *, iters, seed):
    """One-pass seeded null: under H0, outcome ~ Bernoulli(price) independent of
    imbalance, so residual carries no imbalance signal. Returns (p_cell, p_global) for
    |corr(imbalance, residual)| using a multiple-comparison-aware global max."""
    prices = [o["price"] for o in all_obs]
    imbs = [o[imb_key] for o in all_obs]
    obs_max = max((abs(v) for v in obs_corr_res.values() if v is not None), default=0.0)
    rng = random.Random(seed)
    ge = {c: 0 for c in cells_idx}
    gmax_ge = 0
    n = len(all_obs)
    for _ in range(iters):
        sim_res = [(1 if rng.random() < prices[i] else 0) - prices[i] for i in range(n)]
        smax = 0.0
        for c, idx in cells_idx.items():
            cc = _pearson([imbs[i] for i in idx], [sim_res[i] for i in idx])
            if cc is None:
                continue
            oc = obs_corr_res[c]
            if oc is not None and abs(cc) >= abs(oc) - 1e-12:
                ge[c] += 1
            if abs(cc) > smax:
                smax = abs(cc)
        if smax >= obs_max - 1e-12:
            gmax_ge += 1
    p_cell = {c: (ge[c] / iters if obs_corr_res[c] is not None else 1.0) for c in cells_idx}
    return p_cell, (gmax_ge / iters if cells_idx else 1.0)


# --------------------------------------------------------------------------- #
# per-metric analysis + verdict
# --------------------------------------------------------------------------- #
VERDICT_BUCKETS = ["all", "early", "mid", "late"]   # primary cells the verdict scans


def analyze_metric(observations, imb_key, *, iters, seed):
    # cells: overall + per ttr-bucket (primary) + per symbol (secondary, shallow)
    cells: dict[str, list[int]] = {"all": list(range(len(observations)))}
    for i, o in enumerate(observations):
        cells.setdefault(o["bucket"], []).append(i)
        cells.setdefault(f"sym={o['symbol'] or '(none)'}", []).append(i)

    stat = {}
    obs_corr_res = {}
    for c, idx in cells.items():
        xs = [observations[i][imb_key] for i in idx]
        outc = [observations[i]["outcome"] for i in idx]
        res = [observations[i]["outcome"] - observations[i]["price"] for i in idx]
        cr_raw = _pearson(xs, outc)
        cr_res = _pearson(xs, res)
        stat[c] = {
            "n": len(idx), "corr_outcome": cr_raw, "corr_residual": cr_res,
            "gap": (None if cr_raw is None or cr_res is None else cr_raw - cr_res),
            "dominance": _dominance(xs, res),
        }
        obs_corr_res[c] = cr_res

    p_cell, p_global = mc_corr_pvalues(observations, cells, imb_key, obs_corr_res,
                                       iters=iters, seed=seed)
    for c in cells:
        stat[c]["p"] = p_cell[c]

    robust, weak = [], []
    for c in VERDICT_BUCKETS:
        s = stat.get(c)
        if not s or s["n"] < MIN_OBS or s["corr_residual"] is None:
            continue
        sig = s["p"] < 0.05
        dominated = s["dominance"] is not None and s["dominance"] > DOMINANCE
        positive = s["corr_residual"] > 0
        if sig and positive and not dominated and p_global < 0.05:
            robust.append(c)
        elif sig and positive:
            weak.append(c)
    verdict = "ROBUST_SIGNAL" if robust else ("WEAK_SIGNAL" if weak else "NO_SIGNAL")
    return stat, cells, p_global, verdict, robust + weak


# --------------------------------------------------------------------------- #
def _fmt(v, d=3):
    return "n/a" if v is None else f"{v:+.{d}f}"


def _metric_md(name, stat, p_global, verdict, flagged):
    order = ["all", "early", "mid", "late"] + sorted(c for c in stat if c.startswith("sym="))
    lines = [f"### {name}  ->  **{verdict}**  (global p={p_global:.3f})", "",
             "| cell | n | corr(imb,outcome) | corr(imb,residual) | confound gap | p | max-dom |",
             "|---|--:|--:|--:|--:|--:|--:|"]
    for c in order:
        if c not in stat:
            continue
        s = stat[c]
        thin = "" if s["n"] >= MIN_OBS else " [thin]"
        dom = "n/a" if s["dominance"] is None else f"{s['dominance'] * 100:.0f}%"
        flag = "  <-" if c in flagged else ""
        lines.append(f"| {c}{thin} | {s['n']} | {_fmt(s['corr_outcome'])} | "
                     f"{_fmt(s['corr_residual'])} | {_fmt(s['gap'])} | {s['p']:.3f} | {dom}{flag} |")
    lines.append("")
    return lines


# --------------------------------------------------------------------------- #
# Orthogonalization upgrade: binance ret15 join + OLS partial correlations
# (signal-vs-price confound fix -- see module docstring + FINDINGS).
# --------------------------------------------------------------------------- #
def build_spot_index(conn):
    """symbol -> (sorted epoch list, price list) from binance ticker rows (for the ret15 join)."""
    raw: dict[str, list[tuple[float, float]]] = {}
    rows = conn.execute(
        "SELECT ts, symbol, price FROM snapshots "
        "WHERE source='binance' AND price IS NOT NULL AND symbol IS NOT NULL ORDER BY ts"
    ).fetchall()
    for r in rows:
        raw.setdefault(r["symbol"], []).append((_parse_ts(r["ts"]).timestamp(), float(r["price"])))
    return {sym: ([t for t, _ in seq], [p for _, p in seq]) for sym, seq in raw.items()}


def _at_or_before(index, key, t_epoch, max_stale):
    """Last sample AT-OR-BEFORE t_epoch, only if <= max_stale old; else None. Inherited verbatim
    from the corrected probe_cross_coin.py -- at-or-before IS the look-ahead guard, never nearest."""
    arr = index.get(key)
    if not arr:
        return None
    ts_list, val_list = arr
    j = bisect.bisect_right(ts_list, t_epoch) - 1
    if j < 0:
        return None
    if t_epoch - ts_list[j] > max_stale:
        return None
    return val_list[j]


def _trailing_return(spot_index, symbol, anchor_epoch, window_sec, max_stale):
    """own-coin price_now/price_then - 1, both endpoints at-or-before their nominal times under the
    same staleness gate (one clock with the book row that produced the signal)."""
    p_now = _at_or_before(spot_index, symbol, anchor_epoch, max_stale)
    p_then = _at_or_before(spot_index, symbol, anchor_epoch - window_sec, max_stale)
    if p_now is None or p_then is None or p_then <= 0:
        return None
    return p_now / p_then - 1.0


def _ols_resid1(xs, ps):
    """Residuals of xs on [1, ps] (mean-centered; correlation-invariant). None if < 2 points."""
    n = len(xs)
    if n < 2:
        return None
    mx, mp = statistics.fmean(xs), statistics.fmean(ps)
    spp = sum((p - mp) ** 2 for p in ps)
    if spp <= 0:
        return [x - mx for x in xs]                  # price constant in cell -> just mean-center
    b = sum((x - mx) * (p - mp) for x, p in zip(xs, ps)) / spp
    return [(x - mx) - b * (p - mp) for x, p in zip(xs, ps)]


def _ols_resid2(xs, ps, rs):
    """Residuals of xs on [1, ps, rs] via centered 2-var normal equations. Returns
    (residuals, corr_price_ret) or (None, corr_price_ret) when degenerate (n<3 or det<=0)."""
    n = len(xs)
    if n < 3:
        return None, None
    mx, mp, mr = statistics.fmean(xs), statistics.fmean(ps), statistics.fmean(rs)
    spp = sum((p - mp) ** 2 for p in ps)
    srr = sum((r - mr) ** 2 for r in rs)
    spr = sum((p - mp) * (r - mr) for p, r in zip(ps, rs))
    sxp = sum((x - mx) * (p - mp) for x, p in zip(xs, ps))
    sxr = sum((x - mx) * (r - mr) for x, r in zip(xs, rs))
    corr_pr = (spr / math.sqrt(spp * srr)) if (spp > 0 and srr > 0) else None
    det = spp * srr - spr * spr
    if spp <= 0 or srr <= 0 or det <= 0:
        return None, corr_pr
    b = (sxp * srr - sxr * spr) / det
    c = (sxr * spp - sxp * spr) / det
    resid = [(x - mx) - b * (p - mp) - c * (r - mr) for x, p, r in zip(xs, ps, rs)]
    return resid, corr_pr


def mc_partial_pvalues(all_obs, cells_signal, obs_corr, *, iters, seed):
    """Same Bernoulli(price) null as mc_corr_pvalues, but each cell carries its OWN fixed
    residualized-signal array (signal already orthogonalized vs price[/ret15]); only the outcome
    residual is resampled. Returns (p_cell, p_global) over exactly these cells."""
    prices = [o["price"] for o in all_obs]
    n = len(all_obs)
    obs_max = max((abs(v) for v in obs_corr.values() if v is not None), default=0.0)
    rng = random.Random(seed)
    ge = {c: 0 for c in cells_signal}
    gmax_ge = 0
    for _ in range(iters):
        sim_res = [(1 if rng.random() < prices[i] else 0) - prices[i] for i in range(n)]
        smax = 0.0
        for c, (sig, idx) in cells_signal.items():
            cc = _pearson(sig, [sim_res[i] for i in idx])
            if cc is None:
                continue
            oc = obs_corr[c]
            if oc is not None and abs(cc) >= abs(oc) - 1e-12:
                ge[c] += 1
            if abs(cc) > smax:
                smax = abs(cc)
        if smax >= obs_max - 1e-12:
            gmax_ge += 1
    p_cell = {c: (ge[c] / iters if obs_corr[c] is not None else 1.0) for c in cells_signal}
    return p_cell, (gmax_ge / iters if cells_signal else 1.0)


def analyze_partials(observations, imb_key, *, iters, seed):
    """Orthogonalization pass over the SAME cells as analyze_metric. Per cell adds corr(sig,price),
    corr(sig,ret15), partial(sig,residual|price) and partial(sig,residual|price+ret15): OLS-
    residualize the SIGNAL, then correlate with the outcome residual. The two-control partial uses
    only ret15-present obs; collinearity (|corr(price,ret15)|>COLLINEAR_MAX) -> price-only fallback.
    Returns (pstat, p1_global, p2_global)."""
    cells: dict[str, list[int]] = {"all": list(range(len(observations)))}
    for i, o in enumerate(observations):
        cells.setdefault(o["bucket"], []).append(i)
        cells.setdefault(f"sym={o['symbol'] or '(none)'}", []).append(i)

    pstat = {}
    cells_sig1, obs_corr1 = {}, {}
    cells_sig2, obs_corr2 = {}, {}
    for c, idx in cells.items():
        xs = [observations[i][imb_key] for i in idx]
        ps = [observations[i]["price"] for i in idx]
        res = [observations[i]["outcome"] - observations[i]["price"] for i in idx]
        idx_r = [i for i in idx if observations[i]["ret15"] is not None]
        xs_r = [observations[i][imb_key] for i in idx_r]
        ps_r = [observations[i]["price"] for i in idx_r]
        rs_r = [observations[i]["ret15"] for i in idx_r]
        res_r = [observations[i]["outcome"] - observations[i]["price"] for i in idx_r]

        resid1 = _ols_resid1(xs, ps)
        partial1 = _pearson(resid1, res) if resid1 is not None else None
        dom1 = _dominance(resid1, res) if resid1 is not None else None

        resid2, corr_pr = _ols_resid2(xs_r, ps_r, rs_r)
        collinear = corr_pr is not None and abs(corr_pr) > COLLINEAR_MAX
        fellback = collinear or resid2 is None
        if fellback:
            resid2 = _ols_resid1(xs_r, ps_r)         # price-only on the ret15-present subset
        partial2 = _pearson(resid2, res_r) if resid2 is not None else None
        dom2 = _dominance(resid2, res_r) if resid2 is not None else None

        pstat[c] = {
            "n": len(idx), "n_ret": len(idx_r),
            "corr_sig_price": _pearson(xs, ps),
            "corr_sig_ret": _pearson(xs_r, rs_r),
            "corr_res_ret": _pearson(xs_r, res_r),    # raw corr on the ret15 subset (shrinkage base)
            "partial1": partial1, "dom1": dom1,
            "partial2": partial2, "dom2": dom2,
            "collinear": collinear, "fellback": fellback, "corr_pr": corr_pr,
        }
        if resid1 is not None:
            cells_sig1[c] = (resid1, idx)
            obs_corr1[c] = partial1
        if resid2 is not None and len(idx_r) >= 2:
            cells_sig2[c] = (resid2, idx_r)
            obs_corr2[c] = partial2

    p1_cell, p1_global = mc_partial_pvalues(observations, cells_sig1, obs_corr1, iters=iters, seed=seed)
    p2_cell, p2_global = mc_partial_pvalues(observations, cells_sig2, obs_corr2, iters=iters, seed=seed)
    for c in pstat:
        pstat[c]["p1"] = p1_cell.get(c, 1.0)
        pstat[c]["p2"] = p2_cell.get(c, 1.0)
    return pstat, p1_global, p2_global


def _ortho_verdict(raw_stat, pstat, p2_global):
    """SURVIVES / ATTENUATED / COLLAPSES for top-of-book, from partial(|price+ret15) in the primary
    cells (all/early/mid/late). INCONCLUSIVE if the ret15-paired sample is too thin to test (a data-
    availability guard, NOT a loosened gate). Returns (verdict, justification, shrink_pct_or_None)."""
    allp = pstat.get("all", {})
    if allp.get("n_ret", 0) < MIN_OBS:
        return ("INCONCLUSIVE",
                f"partial|price+ret15 untestable -- only {allp.get('n_ret', 0)} ret15-paired obs on "
                f"'all' (< min-obs); re-run as the binance-paired sample grows (partial|price is in the table)",
                None)
    survivors = [c for c in VERDICT_BUCKETS
                 if (s := pstat.get(c)) and s["n_ret"] >= MIN_OBS and s["partial2"] is not None
                 and s["partial2"] > 0 and s["p2"] < 0.05 and (s["dom2"] is None or s["dom2"] <= DOMINANCE)]
    base, p2_all = allp.get("corr_res_ret"), allp.get("partial2")
    shrink_pct = (1.0 - p2_all / base) * 100.0 if (base not in (None, 0) and p2_all is not None) else None
    material = shrink_pct is not None and shrink_pct >= MATERIAL_SHRINK * 100.0
    shp = "n/a" if shrink_pct is None else f"{shrink_pct:.0f}%"
    if survivors and p2_global < 0.05 and not material:
        return ("SURVIVES",
                f"partial stays + & significant in {survivors}, global p={p2_global:.3f}; shrinkage "
                f"{shp} vs raw -> information INDEPENDENT of price level and the overreaction channel",
                shrink_pct)
    if survivors:
        reason = "fails the global multiple-comparison test" if p2_global >= 0.05 else f"materially smaller (shrank {shp})"
        return ("ATTENUATED",
                f"+ & nominally significant in {survivors} but {reason} (global p={p2_global:.3f}, "
                f"shrinkage {shp}) -> partly its own signal, partly echo", shrink_pct)
    return ("COLLAPSES",
            f"no primary cell keeps a +, significant partial (~0 or sign-flip; global p={p2_global:.3f}, "
            f"shrinkage {shp}) -> price/return proxy -> WITHDRAW the PROVISIONAL ROBUST label", shrink_pct)


def _machinery_check(pstat, p1_global):
    """Positive control: depth|price MUST collapse toward ~0. SUSPECT if any primary aggregate cell
    keeps |partial(depth|price)| > MACHINERY_THRESH with p<0.05. Returns (label, detail)."""
    suspect = [c for c in VERDICT_BUCKETS
               if (s := pstat.get(c)) and s["n"] >= MIN_OBS and s["partial1"] is not None
               and abs(s["partial1"]) > MACHINERY_THRESH and s["p1"] < 0.05]
    p1_all = pstat.get("all", {}).get("partial1")
    av = "n/a" if p1_all is None else f"{p1_all:+.3f}"
    if suspect:
        return ("MACHINERY_SUSPECT",
                f"depth|price partial stays large & significant in {suspect} (all-cell {av}, global "
                f"p={p1_global:.3f}) -> the price-control machinery is NOT clean; trust NO orthogonalization verdict")
    return ("MACHINERY_OK",
            f"depth|price partial collapsed (all-cell {av}, global p={p1_global:.3f}) -> price "
            f"residualization works; the orthogonalization verdicts are trustworthy")


def _btc_homogenization(raw_stat, pstat):
    rs, ps = raw_stat.get("sym=BTC"), pstat.get("sym=BTC")
    if not rs or not ps:
        return "no BTC cell"
    raw, p1, p2 = rs.get("corr_residual"), ps.get("partial1"), ps.get("partial2")
    toward0 = raw is not None and p2 is not None and abs(p2) < abs(raw)
    tag = ("attenuates toward 0 (its negativity was the price/return channel)" if toward0
           else "does NOT attenuate (negativity survives partialling)")
    return (f"BTC raw corr_residual {_fmt(raw)} -> partial|price {_fmt(p1)} -> "
            f"partial|price+ret15 {_fmt(p2)} ({tag})")


def _partial_md(pstat, p1_global, p2_global):
    order = ["all", "early", "mid", "late"] + sorted(c for c in pstat if c.startswith("sym="))
    lines = [f"(partial global p: |price={p1_global:.3f}, |price+ret15={p2_global:.3f})", "",
             "| cell | n | n_ret | corr(sig,price) | corr(sig,ret15) | partial(|price) | p | "
             "partial(|price+ret15) | p | max-dom | note |",
             "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|"]
    for c in order:
        if c not in pstat:
            continue
        s = pstat[c]
        thin = "" if s["n"] >= MIN_OBS else " [thin]"
        dom = "n/a" if s["dom2"] is None else f"{s['dom2'] * 100:.0f}%"
        note = "collinear->price-only" if s.get("fellback") else ""
        lines.append(f"| {c}{thin} | {s['n']} | {s['n_ret']} | {_fmt(s['corr_sig_price'])} | "
                     f"{_fmt(s['corr_sig_ret'])} | {_fmt(s['partial1'])} | {s['p1']:.3f} | "
                     f"{_fmt(s['partial2'])} | {s['p2']:.3f} | {dom} | {note} |")
    lines.append("")
    return lines


def main() -> None:
    global MIN_OBS  # declared up-front: main() overrides it from --min-obs (read below as the default)
    ap = argparse.ArgumentParser(prog="python research/probe_orderbook_imbalance.py",
                                 description=__doc__)
    ap.add_argument("--db", type=Path, default=DB)
    ap.add_argument("--iters", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--min-obs", type=int, default=MIN_OBS)
    ap.add_argument("--labels", type=Path, default=None,
                    help="optional label_truth.csv from research/verify_labels.py; when "
                         "given, API ground truth REPLACES the recovery rule")
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "research" / "diagnostics")
    args = ap.parse_args()
    MIN_OBS = args.min_obs

    if not args.db.exists():
        print(f"VERDICT: top-of-book=NO_SIGNAL | depth=NO_SIGNAL -- DB not found: {args.db}")
        return

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        resolutions = recover_resolutions(conn)
        if args.labels:
            resolutions = apply_label_overrides(conn, resolutions, args.labels)
        yes_assets = label_yes_assets(conn, set(resolutions))
        spot_index = build_spot_index(conn)   # binance ticker per symbol, for the ret15 join

        agg: dict[tuple, dict] = {}
        used_rows = excl_postres = excl_nodepth = 0
        rows = conn.execute(
            "SELECT ts, market_id, asset_id, symbol, price, bid_size, ask_size, "
            "bid_depth, ask_depth FROM snapshots "
            "WHERE source='polymarket' AND event_type='book' AND asset_id IS NOT NULL "
            "AND price IS NOT NULL"
        ).fetchall()
        for r in rows:
            if r["asset_id"] not in yes_assets:
                continue
            res = resolutions.get(r["market_id"])
            if res is None:
                continue
            resolve_ts, outcome = res
            row_dt = _parse_ts(r["ts"])
            ttr = (resolve_ts - row_dt).total_seconds()
            if ttr <= 0:                      # at/after resolution -> look-ahead, drop
                excl_postres += 1
                continue
            bs, ask, bd, ad = r["bid_size"], r["ask_size"], r["bid_depth"], r["ask_depth"]
            if None in (bs, ask, bd, ad) or (bs + ask) <= 0 or (bd + ad) <= 0:
                excl_nodepth += 1
                continue
            used_rows += 1
            key = (r["market_id"], _ttr_bucket(ttr))
            a = agg.setdefault(key, {"top": [], "dep": [], "px": [], "ret": [],
                                     "symbol": r["symbol"], "outcome": outcome})
            a["top"].append((bs - ask) / (bs + ask))
            a["dep"].append((bd - ad) / (bd + ad))
            a["px"].append(r["price"])
            # ret15 at THE SAME row timestamp (one clock with the signal); None if no fresh spot
            ret = _trailing_return(spot_index, r["symbol"], row_dt.timestamp(),
                                   RET_WINDOW_SEC, STALENESS_MAX_SEC)
            if ret is not None:
                a["ret"].append(ret)

        observations = [{
            "market_id": mid, "bucket": bucket, "symbol": a["symbol"],
            "imb_top": statistics.fmean(a["top"]), "imb_depth": statistics.fmean(a["dep"]),
            "price": statistics.fmean(a["px"]), "outcome": a["outcome"], "nrows": len(a["top"]),
            "ret15": (statistics.fmean(a["ret"]) if a["ret"] else None),
        } for (mid, bucket), a in agg.items()]
    finally:
        conn.close()

    n_with_ret = sum(1 for o in observations if o["ret15"] is not None)
    header_lines = [
        "# Order-book imbalance vs resolution (price-controlled)", "",
        f"- usable resolved markets: {len(resolutions)}; YES-token asset_ids: {len(yes_assets)}",
        f"- YES book rows used: {used_rows} (excluded: post-resolution {excl_postres}, "
        f"missing/zero depth {excl_nodepth})",
        f"- observations (market x ttr-bucket): {len(observations)}; null iters={args.iters}, "
        f"min-obs={MIN_OBS}",
        "- imbalance = (bid-ask)/(bid+ask) on the YES side; residual = outcome - yes_price.",
        "- corr(imb,outcome) is price-confounded; **corr(imb,residual)** is the edge signal.",
        "- AGGREGATION (now documented): each observation = the MEAN over all YES book rows in that "
        "(market x ttr-bucket); price and ret15 are the MEAN over THE SAME rows, so signal/price/ret15 "
        "share one clock (never nearest-match).",
        f"- ret15 = own-coin trailing 15m spot return at each book row's ts (at-or-before + "
        f"{STALENESS_MAX_SEC:.0f}s staleness, binance-ticker join); observations with ret15: "
        f"{n_with_ret}/{len(observations)}.",
        "",
    ]

    if len(observations) < 2:
        verdict_line = "VERDICT: top-of-book=NO_SIGNAL | depth=NO_SIGNAL (insufficient depth data)"
        md = verdict_line + "\n\n" + "\n".join(header_lines) + \
            "\n_Not enough populated YES book rows yet -- re-run after depth data accrues._\n"
        _emit(args.out, md, [])
        print(verdict_line)
        print("\n".join(header_lines))
        return

    top_stat, _, top_pg, top_v, top_flag = analyze_metric(observations, "imb_top",
                                                          iters=args.iters, seed=args.seed)
    dep_stat, _, dep_pg, dep_v, dep_flag = analyze_metric(observations, "imb_depth",
                                                          iters=args.iters, seed=args.seed)

    # --- orthogonalization (NEW): residualize the SIGNAL against price[/ret15], not just outcome ---
    top_p, top_p1g, top_p2g = analyze_partials(observations, "imb_top", iters=args.iters, seed=args.seed)
    dep_p, dep_p1g, dep_p2g = analyze_partials(observations, "imb_depth", iters=args.iters, seed=args.seed)
    ortho, ortho_just, _shrink = _ortho_verdict(top_stat, top_p, top_p2g)
    machinery, mach_detail = _machinery_check(dep_p, dep_p1g)
    btc_line = _btc_homogenization(top_stat, top_p)
    collinear_cells = sorted(c for c in set(top_p) | set(dep_p)
                             if top_p.get(c, {}).get("collinear") or dep_p.get(c, {}).get("collinear"))

    ortho_line = f"ORTHO VERDICT: top-of-book imbalance | price+ret15 = {ortho} -- {ortho_just}"
    machinery_line = f"POSITIVE CONTROL: {machinery} -- {mach_detail}"
    if collinear_cells:
        machinery_line += f"  [collinear price~ret15 (>{COLLINEAR_MAX}) -> price-only fallback in: {collinear_cells}]"

    verdict_line = f"VERDICT: top-of-book={top_v} | depth={dep_v}"
    md_lines = [ortho_line, machinery_line, "", verdict_line, ""] + header_lines
    md_lines += ["## top-of-book imbalance"] + _metric_md("top-of-book", top_stat, top_pg, top_v, top_flag)
    md_lines += ["## depth imbalance"] + _metric_md("depth", dep_stat, dep_pg, dep_v, dep_flag)
    md_lines += [
        "## Orthogonalization -- partial correlations (signal-vs-price confound fix)", "",
        f"- **top-of-book | price+ret15: {ortho}** -- {ortho_just}",
        f"- positive control **depth | price: {machinery}** -- {mach_detail}",
        f"- BTC homogenization (exploratory): {btc_line}",
        "",
    ]
    md_lines += ["### top-of-book partials"] + _partial_md(top_p, top_p1g, top_p2g)
    md_lines += ["### depth partials"] + _partial_md(dep_p, dep_p1g, dep_p2g)
    md_lines += [
        "## Reading guide", "",
        "- A real edge shows as **corr(imb,residual) > 0, p<0.05, global p<0.05, max-dom<=40%, "
        "n>=min** -- ROBUST_SIGNAL. WEAK = nominal but fails the multiple-comparison/dominance "
        "guard. The confound gap (outcome - residual corr) shows how much of the raw correlation "
        "was just price.",
        "- Verdict scans only the primary cells (all/early/mid/late); `sym=` rows are exploratory.",
        "- Regime/sample-limited; read aggregate direction, not [thin] cells.",
        "- ORTHOGONALIZATION (this upgrade): corr(sig,price) & corr(sig,ret15) expose the two "
        "contamination channels; partial(|price) and partial(|price+ret15) OLS-residualize the SIGNAL "
        "before correlating with the outcome residual. **SURVIVES** = partial stays +, p<0.05, global "
        "p<0.05 -> info INDEPENDENT of price level and the overreaction channel. **ATTENUATED** = + & "
        "nominal but materially smaller or fails the global test -> part echo (shrinkage quantified). "
        "**COLLAPSES** = partial ~0 / sign-flip -> price/return proxy, withdraw PROVISIONAL ROBUST.",
        "- Positive control: depth|price MUST collapse (depth is the known price-proxy artifact, "
        "uniform -0.35..-0.57); if it stays >0.15 & significant -> MACHINERY_SUSPECT and NO "
        "orthogonalization verdict is trustworthy.",
        "- Still post-restart-YOUNG (<=30h, one regime): orthogonalization settles the CHANNEL "
        "question (is imbalance its own info?), NOT the regime question.",
        "",
    ]
    md = "\n".join(md_lines) + "\n"

    csv_rows = []
    partials_by_metric = {"top-of-book": (top_p, top_p2g), "depth": (dep_p, dep_p2g)}
    for metric, stat in (("top-of-book", top_stat), ("depth", dep_stat)):
        pg = top_pg if metric == "top-of-book" else dep_pg
        pmap, p2g = partials_by_metric[metric]
        for c, s in stat.items():
            scope = "ttr" if c in ("early", "mid", "late") else ("symbol" if c.startswith("sym=") else "all")
            p = pmap.get(c, {})
            csv_rows.append([metric, scope, c, s["n"],
                             _r(s["corr_outcome"]), _r(s["corr_residual"]), _r(s["gap"]),
                             round(s["p"], 4), round(pg, 4), _r(s["dominance"]),
                             _r(p.get("corr_sig_price")), _r(p.get("corr_sig_ret")),
                             _r(p.get("partial1")), round(p.get("p1", 1.0), 4),
                             _r(p.get("partial2")), round(p.get("p2", 1.0), 4),
                             round(p2g, 4), p.get("n_ret", ""), _r(p.get("dom2")),
                             ("collinear->price-only" if p.get("fellback") else "")])

    _emit(args.out, md, csv_rows)
    print(md)
    print(f"wrote: {args.out / 'orderbook_imbalance.md'}")
    print(f"wrote: {args.out / 'orderbook_imbalance.csv'}")


def _r(v):
    return "" if v is None else round(v, 4)


def _emit(out: Path, md: str, csv_rows: list) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "orderbook_imbalance.md").write_text(md, encoding="utf-8")
    header = ["metric", "scope", "cell", "n", "corr_outcome", "corr_residual", "gap",
              "p_value", "p_global", "max_dominance",
              "corr_sig_price", "corr_sig_ret15", "partial_price", "p_partial_price",
              "partial_price_ret15", "p_partial_price_ret15", "partial_global_p", "n_ret",
              "partial_max_dom", "partial_note"]
    (out / "orderbook_imbalance.csv").write_text(_to_csv(header, csv_rows), encoding="utf-8")


if __name__ == "__main__":
    main()
