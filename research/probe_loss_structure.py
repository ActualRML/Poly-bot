"""READ-ONLY loss-structure falsification: is loss structured or stochastic noise?

H0: a trade's outcome is random conditional on its entry price -- it LOSES with
probability `1 - entry_price` (an efficient/calibrated 0-1 market). H1: losses
cluster into repeatable regimes that lose MORE than their entry prices imply.

WHY THE OBVIOUS TEST IS WRONG: entry price == P(win) here (both strategies set
`entry = price if YES else 1-price`; `calibrate_zones.py` shows zones calibrated,
Brier 0.131). So raw loss rate MUST vary by price/zone/band -- that is the market
pricing correctly, NOT inefficiency. The only real signal is the CALIBRATION
RESIDUAL: `observed_loss(0/1) - (1 - entry_price)`. We test whether any segment's
residual exceeds chance under a Monte-Carlo Bernoulli(`1-entry_price`) null, with a
multiple-comparison-aware global test. (And note: even a real loss regime is NOT a
free strategy -- inverting pays the spread on the other side too.)

Runs on TWO independent samples and cross-checks: a regime counts only if it shows
in BOTH. Read-only (positions `mode=ro`; harness via run_backtest `mode=ro`). Run:

    .venv/Scripts/python.exe research/probe_loss_structure.py
"""
import argparse
import logging
import math
import random
import sqlite3
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.backtest.diagnostics import to_csv  # noqa: E402
from src.backtest.engine import DEFAULT_SLIPPAGE, run_backtest  # noqa: E402

_TTR_BUCKETS = (
    (">=30m", 30 * 60, math.inf), ("15-30m", 15 * 60, 30 * 60),
    ("5-15m", 5 * 60, 15 * 60), ("2-5m", 2 * 60, 5 * 60), ("0-2m", 0, 2 * 60),
)
_SIZE_EDGES = [0.0, 10.0, 15.0, 20.0, math.inf]


def _band(price: float) -> str:
    """10c entry band, cent-rounded (same rule as diagnostics.entry_band)."""
    cents = round(price * 100)
    lo = (cents // 10) * 10
    return f"{lo / 100:.2f}-{(lo + 10) / 100:.2f}"


def _ttr_bucket(secs: float | None) -> str | None:
    if secs is None:
        return None
    for label, lo, hi in _TTR_BUCKETS:
        if lo <= secs < hi:
            return label
    return None


def _size_band(size: float) -> str:
    for lo, hi in zip(_SIZE_EDGES, _SIZE_EDGES[1:]):
        if lo <= size < hi:
            return f">={lo:.0f}" if hi == math.inf else f"{lo:.0f}-{hi:.0f}"
    return "?"


@dataclass
class LossRecord:
    sample: str
    strategy: str
    symbol: str | None
    entry_price: float
    size_usdc: float
    pnl_usdc: float
    ttr_secs: float | None = None
    price_zone: str | None = None     # harness only
    vol_regime: str | None = None     # harness only

    @property
    def is_loss(self) -> bool:
        return self.pnl_usdc <= 0

    @property
    def expected_loss(self) -> float:
        return 1.0 - self.entry_price


def axes_of(r: LossRecord, *, harness: bool) -> dict[str, str]:
    a = {
        "strategy": r.strategy,
        "symbol": r.symbol or "(none)",
        "entry_band": _band(r.entry_price),
        "ttr": _ttr_bucket(r.ttr_secs) or "(none)",
        "size_band": _size_band(r.size_usdc),
    }
    if harness:
        a["price_zone"] = r.price_zone or "(none)"
        a["vol_regime"] = r.vol_regime or "(none)"
    return a


# --------------------------------------------------------------------------- #
# Monte-Carlo Bernoulli null (the load-bearing statistic)
# --------------------------------------------------------------------------- #
def mc_pvalues(obs_loss: list[int], probs: list[float], cells: dict[str, list[int]],
               *, iters: int, seed: int):
    """One-sided excess-loss p-values under H0 (each trade loses w.p. probs[i]).

    Returns (p_cell, p_global, cell_stat) where cell_stat[c] = (n, obs_loss_frac,
    exp_loss_frac, residual). One simulation pass is reused for every cell and for
    the multiple-comparison-aware global max-residual test."""
    cell_stat, obs_res = {}, {}
    for c, idx in cells.items():
        n = len(idx)
        of = sum(obs_loss[i] for i in idx) / n
        ef = sum(probs[i] for i in idx) / n
        cell_stat[c] = (n, of, ef, of - ef)
        obs_res[c] = of - ef
    obs_max = max(obs_res.values()) if obs_res else 0.0

    rng = random.Random(seed)
    ge = {c: 0 for c in cells}
    gmax_ge = 0
    n_all = len(obs_loss)
    for _ in range(iters):
        sim = [1 if rng.random() < probs[i] else 0 for i in range(n_all)]
        smax = -1e9
        for c, idx in cells.items():
            sres = sum(sim[i] for i in idx) / len(idx) - cell_stat[c][2]
            if sres >= obs_res[c] - 1e-12:
                ge[c] += 1
            if sres > smax:
                smax = sres
        if smax >= obs_max - 1e-12:
            gmax_ge += 1
    p_cell = {c: ge[c] / iters for c in cells}
    p_global = gmax_ge / iters if cells else 1.0
    return p_cell, p_global, cell_stat


def pareto_loss(recs: list[LossRecord]):
    """(n_losers, n_for_80pct, gross_loss, median_loss, mean_loss) for the loss side."""
    losses = sorted((abs(r.pnl_usdc) for r in recs if r.is_loss), reverse=True)
    if not losses:
        return 0, 0, 0.0, 0.0, 0.0
    gross = sum(losses)
    cum = 0.0
    n80 = len(losses)
    for i, v in enumerate(losses, 1):
        cum += v
        if gross and cum / gross >= 0.80:
            n80 = i
            break
    return len(losses), n80, gross, statistics.median(losses), statistics.fmean(losses)


# --------------------------------------------------------------------------- #
# Loaders
# --------------------------------------------------------------------------- #
def load_live(db: Path) -> list[LossRecord]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    out: list[LossRecord] = []
    try:
        rows = conn.execute(
            "SELECT entry_price, size_usdc, pnl_usdc, strategy, symbol, ts, resolve_time "
            "FROM positions WHERE status='resolved' AND pnl_usdc IS NOT NULL "
            "AND entry_price IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    for r in rows:
        ttr = None
        if r["resolve_time"]:
            ttr = (datetime.fromisoformat(r["resolve_time"])
                   - datetime.fromisoformat(r["ts"])).total_seconds()
        out.append(LossRecord(
            sample="live", strategy=r["strategy"], symbol=r["symbol"],
            entry_price=r["entry_price"], size_usdc=r["size_usdc"],
            pnl_usdc=r["pnl_usdc"], ttr_secs=ttr,
        ))
    return out


def load_harness(db: Path, slippage: float) -> list[LossRecord]:
    result = run_backtest(db, ["contrarian", "momentum"], (slippage,))[0]
    resolutions = result.recovery.usable
    out: list[LossRecord] = []
    for p in result.settled:
        res = resolutions.get(p.market_id)
        ttr = (res.resolve_ts - p.opened_ts).total_seconds() if res else None
        out.append(LossRecord(
            sample="harness", strategy=p.strategy, symbol=p.symbol,
            entry_price=p.entry_price, size_usdc=p.size_usdc,
            pnl_usdc=p.pnl_usdc or 0.0, ttr_secs=ttr,
            price_zone=p.price_zone, vol_regime=p.vol_regime,
        ))
    return out


# --------------------------------------------------------------------------- #
# Per-sample analysis
# --------------------------------------------------------------------------- #
_PAIRS = [("strategy", "symbol"), ("entry_band", "symbol"), ("ttr", "vol_regime")]


def _build_cells(recs, *, harness, min_trades):
    """Marginal cells + the named 2-axis interactions, keeping only n>=min_trades."""
    axis_keys = [axes_of(r, harness=harness) for r in recs]
    groups: dict[str, list[int]] = {}
    for i, ak in enumerate(axis_keys):
        for axis, val in ak.items():
            groups.setdefault(f"{axis}={val}", []).append(i)
        for a, b in _PAIRS:
            if a in ak and b in ak:
                groups.setdefault(f"{a}={ak[a]} & {b}={ak[b]}", []).append(i)
    return {c: idx for c, idx in groups.items() if len(idx) >= min_trades}


def _marginal_tables(recs, *, harness) -> list[str]:
    axis_names = ["strategy", "symbol", "entry_band", "ttr", "size_band"]
    if harness:
        axis_names += ["price_zone", "vol_regime"]
    lines: list[str] = []
    for axis in axis_names:
        groups: dict[str, list[LossRecord]] = {}
        for r in recs:
            groups.setdefault(axes_of(r, harness=harness)[axis], []).append(r)
        lines += [f"**By {axis}**", "", "| key | n | losses | loss% | exp_loss% |",
                  "|---|--:|--:|--:|--:|"]
        for k in sorted(groups, key=lambda k: -len(groups[k])):
            g = groups[k]
            losses = sum(1 for r in g if r.is_loss)
            exp = statistics.fmean([r.expected_loss for r in g]) * 100
            lines.append(f"| {k} | {len(g)} | {losses} | {losses / len(g) * 100:.1f} | {exp:.1f} |")
        lines.append("")
    return lines


def analyze(recs, *, harness, min_trades, iters, seed):
    cells = _build_cells(recs, harness=harness, min_trades=min_trades)
    obs_loss = [1 if r.is_loss else 0 for r in recs]
    probs = [r.expected_loss for r in recs]
    p_cell, p_global, cell_stat = mc_pvalues(obs_loss, probs, cells, iters=iters, seed=seed)
    flagged = {c for c in cells
               if cell_stat[c][3] > 0 and p_cell[c] < 0.05 and cell_stat[c][0] >= min_trades}
    return cells, p_cell, p_global, cell_stat, flagged


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def _sample_section(name, recs, res, *, harness) -> list[str]:
    cells, p_cell, p_global, cell_stat, flagged = res
    n = len(recs)
    losses = sum(1 for r in recs if r.is_loss)
    overall_res = (losses / n - statistics.fmean([r.expected_loss for r in recs])) if n else 0.0
    nl, n80, gross, med, mean = pareto_loss(recs)

    L = [f"## Sample: {name} ({n} trades)", ""]
    if n == 0:
        return L + ["_(no trades)_", ""]
    L += [f"- losses {losses} / {n} = **{losses / n * 100:.1f}%**; "
          f"mean entry-implied loss = {statistics.fmean([r.expected_loss for r in recs]) * 100:.1f}%; "
          f"**overall calibration residual = {overall_res:+.3f}** (~0 => calibrated)",
          f"- loss distribution: top {n80} of {nl} losers make 80% of gross loss "
          f"({(n80 / nl * 100) if nl else 0:.0f}%); median loss ${med:.2f} vs mean ${mean:.2f}",
          ""]
    L += ["### Raw loss rate by axis (apparent -- driven by price)", ""]
    L += _marginal_tables(recs, harness=harness)
    L += ["### Calibration test -- excess loss beyond entry price (n>=min only)", "",
          f"- **global test (multiple-comparison-aware): p = {p_global:.3f}** "
          f"({'STRUCTURE' if p_global < 0.05 else 'no excess structure'} at 0.05)",
          "", "| cell | n | loss% | exp% | residual | p | flag |",
          "|---|--:|--:|--:|--:|--:|---|"]
    for c in sorted(cells, key=lambda c: -cell_stat[c][3]):
        nn, of, ef, rsd = cell_stat[c]
        flag = "EXCESS" if c in flagged else ""
        L.append(f"| {c} | {nn} | {of * 100:.1f} | {ef * 100:.1f} | {rsd:+.3f} | {p_cell[c]:.3f} | {flag} |")
    if not cells:
        L.append("| _(no cell reached n>=min)_ | | | | | | |")
    L += ["", f"_verdict ({name}): "
          f"{'structured (excess loss exists)' if p_global < 0.05 else 'no excess structure -- loss explained by entry price'}_",
          ""]
    return L


def main() -> None:
    ap = argparse.ArgumentParser(prog="python research/probe_loss_structure.py", description=__doc__)
    ap.add_argument("--db", type=Path, default=REPO_ROOT / "data" / "bot.db")
    ap.add_argument("--slippage", type=float, default=DEFAULT_SLIPPAGE)
    ap.add_argument("--min-trades", type=int, default=10)
    ap.add_argument("--iters", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "research" / "diagnostics")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not args.db.exists():
        raise SystemExit(f"snapshot DB not found: {args.db}")

    samples = {
        "live": (load_live(args.db), False),
        "harness": (load_harness(args.db, args.slippage), True),
    }
    results = {name: analyze(recs, harness=h, min_trades=args.min_trades,
                             iters=args.iters, seed=args.seed)
               for name, (recs, h) in samples.items()}

    L = ["# Loss-structure falsification", "",
         "_H0: outcome random given entry price (loss w.p. 1-entry). H1: segments lose MORE than"
         " price implies. Reframe: raw loss varies by price by construction -- only the calibration"
         " residual matters. Conservative: a regime must clear n>=min, p<0.05, AND appear in BOTH"
         " samples. Structured-by-price loss is NOT invertible (cost paid both sides)._", ""]
    for name, (recs, h) in samples.items():
        L += _sample_section(name, recs, results[name], harness=h)

    # cross-check: a regime is credible only if flagged in BOTH samples
    common = results["live"][4] & results["harness"][4]
    L += ["## Cross-check + FINAL verdict", ""]
    L += [f"- flagged in live: {sorted(results['live'][4]) or 'none'}",
          f"- flagged in harness: {sorted(results['harness'][4]) or 'none'}",
          f"- **cross-confirmed (both): {sorted(common) or 'none'}**", ""]
    structured = bool(common)
    if structured:
        L += [f"**(B) STRUCTURED loss** -- regime(s) {sorted(common)} lose beyond entry price in BOTH"
              " samples. NOTE: still not directly invertible (cost); investigate as a calibration"
              " break, conservatively (small n)."]
    else:
        L += ["**(A) RANDOM / efficient** -- no segment loses beyond what entry price implies in both"
              " samples (any single-sample flag is consistent with multiple-comparison noise at this"
              " n). Loss is structured ONLY by price = the market pricing correctly; taker inference"
              " is exhausted. Re-run as data grows."]
    L.append("")
    markdown = "\n".join(L)

    # csv
    header = ["sample", "scope", "segment", "n", "loss_rate", "exp_loss_rate", "residual",
              "p_value", "flag", "p_global"]
    rows = []
    for name in samples:
        cells, p_cell, p_global, cell_stat, flagged = results[name]
        for c in cells:
            nn, of, ef, rsd = cell_stat[c]
            scope = c.split("=")[0] if " & " not in c else "interaction"
            rows.append([name, scope, c, nn, round(of, 4), round(ef, 4), round(rsd, 4),
                         round(p_cell[c], 4), "EXCESS" if c in flagged else "", round(p_global, 4)])

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "loss_structure.md").write_text(markdown + "\n", encoding="utf-8")
    (args.out / "loss_structure.csv").write_text(to_csv(header, rows), encoding="utf-8")

    print()
    print(markdown)
    print(f"wrote: {args.out / 'loss_structure.md'}")
    print(f"wrote: {args.out / 'loss_structure.csv'}")


if __name__ == "__main__":
    main()
