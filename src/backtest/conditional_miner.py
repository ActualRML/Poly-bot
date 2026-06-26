"""Conditional-edge miner over ``BacktestResult.settled`` (structural edge DETECTION).

The diagnostic pass answered the MARGINAL question (one axis at a time). This goes
one level deeper: enumerate shallow CONDITIONAL slices (1- and 2-axis combinations)
and ask whether any is a STABLE, repeatable, +EV pocket that is NOT driven by a
single outlier trade. This is detection, not optimization -- the ranking priority
is deliberately **stability > magnitude, median > mean, distribution > total PnL**.

A group earns ``ROBUST_EDGE`` only when it clears every stability gate at once:
enough trades, positive mean AND median, an acceptable profit factor, and no single
trade dominating its profit. A merely-positive-but-fragile group (low sample, or
negative median, or one-trade-dominated) is ``WEAK_SIGNAL``; negative/noise is
``NO_EDGE``.

Pure module (mirrors ``diagnostics.py``): functions return data/strings; the
``research/`` runner owns all I/O. Reuses the axis keyfuncs, CSV writer and Pareto
concentration already in ``diagnostics.py``. Output strings are ASCII (Windows-safe).
"""
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations

from src.backtest.diagnostics import concentration, entry_band, ttr_keyfunc
from src.backtest.engine import SimPosition
from src.backtest.recovery import MarketResolution

MIN_TRADES_DEFAULT = 10     # a slice thinner than this cannot be called stable
MAX_DOMINANCE = 0.40        # one trade may be <= 40% of a positive group's total_pnl
ROBUST_PF = 1.2             # profit factor a ROBUST_EDGE must clear
MAX_DEPTH = 2               # shallow only: 1- and 2-axis combinations

ROBUST_EDGE = "ROBUST_EDGE"
WEAK_SIGNAL = "WEAK_SIGNAL"
NO_EDGE = "NO_EDGE"
_LABEL_RANK = {ROBUST_EDGE: 0, WEAK_SIGNAL: 1, NO_EDGE: 2}

CSV_HEADER = [
    "slippage", "depth", "condition", "trades", "win_rate", "total_pnl", "avg_pnl",
    "median_pnl", "profit_factor", "pnl_std", "max_drawdown", "max_trade_pnl",
    "dominance_share", "valid", "label",
]


def _pnl(p: SimPosition) -> float:
    return p.pnl_usdc or 0.0


# --------------------------------------------------------------------------- #
# Axes (degenerate single-value axes are skipped gracefully)
# --------------------------------------------------------------------------- #
def _axis_registry(resolutions: dict[str, MarketResolution]):
    """All candidate axes. ``vol_regime`` is included so it auto-participates once
    the data carries >1 regime; today it is single-valued and gets skipped."""
    return {
        "strategy": lambda p: p.strategy,
        "symbol": lambda p: p.symbol,
        "entry_band": entry_band,
        "vol_regime": lambda p: p.vol_regime,
        "time_to_resolve": ttr_keyfunc(resolutions),
    }


def _active_axes(settled: list[SimPosition], resolutions: dict[str, MarketResolution]):
    """Axis name -> keyfunc, dropping any axis with <2 distinct values over
    ``settled`` (a single-valued axis cannot create a real 'condition')."""
    reg = _axis_registry(resolutions)
    return {name: fn for name, fn in reg.items() if len({fn(p) for p in settled}) >= 2}


# --------------------------------------------------------------------------- #
# Per-group statistics
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ConditionalGroup:
    combo: tuple[tuple[str, str], ...]   # ((axis, value), ...)
    depth: int
    trades: int
    win_rate: float
    total_pnl: float
    avg_pnl: float
    median_pnl: float
    profit_factor: float
    pnl_std: float
    max_drawdown: float
    max_trade_pnl: float
    dominance_share: float | None        # max_trade_pnl/total_pnl for +groups, else None
    valid: bool
    label: str

    @property
    def condition(self) -> str:
        return " & ".join(f"{a}={v}" for a, v in self.combo)


def _max_drawdown(ps: list[SimPosition]) -> float:
    """Largest peak-to-trough drop ($) of the cumulative-PnL curve, trades ordered
    by ``opened_ts``. 0 if the curve only rises."""
    cum = peak = mdd = 0.0
    for p in sorted(ps, key=lambda x: x.opened_ts):
        cum += _pnl(p)
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return mdd


def _stat_fields(ps: list[SimPosition]) -> dict:
    n = len(ps)
    pnls = [_pnl(p) for p in ps]
    total = sum(pnls)
    wins = sum(1 for p in ps if p.won)
    gross_profit = sum(x for x in pnls if x > 0)
    gross_loss = -sum(x for x in pnls if x < 0)
    if gross_loss > 0:
        pf = gross_profit / gross_loss
    else:
        pf = math.inf if gross_profit > 0 else 0.0
    max_trade = max(pnls) if pnls else 0.0
    return {
        "trades": n,
        "win_rate": wins / n * 100 if n else 0.0,
        "total_pnl": total,
        "avg_pnl": total / n if n else 0.0,
        "median_pnl": statistics.median(pnls) if pnls else 0.0,
        "profit_factor": pf,
        "pnl_std": statistics.pstdev(pnls) if n >= 2 else 0.0,
        "max_drawdown": _max_drawdown(ps),
        "max_trade_pnl": max_trade,
        "dominance_share": (max_trade / total) if total > 0 else None,
    }


def _classify(f: dict, *, min_trades: int) -> tuple[bool, str]:
    """(valid, label). Stability gates, in priority order: negative/zero expectancy
    is NO_EDGE outright; a positive group is ROBUST only if well-sampled, positive in
    BOTH mean and median, profitable enough, and not single-trade-dominated; anything
    else positive is the fragile WEAK_SIGNAL."""
    valid = f["trades"] >= min_trades
    if f["avg_pnl"] <= 0:
        return valid, NO_EDGE
    dominated = f["dominance_share"] is not None and f["dominance_share"] > MAX_DOMINANCE
    robust = (
        valid
        and f["median_pnl"] >= 0
        and not dominated
        and f["profit_factor"] >= ROBUST_PF
    )
    return valid, (ROBUST_EDGE if robust else WEAK_SIGNAL)


def mine(
    settled: list[SimPosition],
    resolutions: dict[str, MarketResolution],
    *,
    min_trades: int = MIN_TRADES_DEFAULT,
    max_depth: int = MAX_DEPTH,
) -> list[ConditionalGroup]:
    """Every 1..max_depth axis-combination's populated value-groups, classified and
    sorted ROBUST -> WEAK -> NO (then by avg_pnl, then trades). Trades with a None on
    any axis of a combo are skipped for that combo only."""
    axes = _active_axes(settled, resolutions)
    names = list(axes)
    groups: list[ConditionalGroup] = []
    for depth in range(1, max_depth + 1):
        for combo_names in combinations(names, depth):
            buckets: dict[tuple, list[SimPosition]] = defaultdict(list)
            for p in settled:
                key = tuple(axes[name](p) for name in combo_names)
                if any(k is None for k in key):
                    continue
                buckets[key].append(p)
            for key, ps in buckets.items():
                fields = _stat_fields(ps)
                valid, label = _classify(fields, min_trades=min_trades)
                groups.append(ConditionalGroup(
                    combo=tuple(zip(combo_names, key)), depth=depth,
                    valid=valid, label=label, **fields,
                ))
    groups.sort(key=lambda g: (_LABEL_RANK[g.label], -g.avg_pnl, -g.trades))
    return groups


def _baseline(ps: list[SimPosition]) -> dict:
    pnls = [_pnl(p) for p in ps]
    n = len(pnls)
    return {
        "trades": n,
        "avg_pnl": sum(pnls) / n if n else 0.0,
        "median_pnl": statistics.median(pnls) if pnls else 0.0,
    }


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #
def _pf_cell(pf: float):
    return "inf" if pf == math.inf else round(pf, 4)


def to_csv_rows(groups: list[ConditionalGroup], *, slippage: float) -> list[list]:
    slip = f"{slippage:.2f}"
    return [
        [
            slip, g.depth, g.condition, g.trades, round(g.win_rate, 2),
            round(g.total_pnl, 4), round(g.avg_pnl, 4), round(g.median_pnl, 4),
            _pf_cell(g.profit_factor), round(g.pnl_std, 4), round(g.max_drawdown, 4),
            round(g.max_trade_pnl, 4),
            "" if g.dominance_share is None else round(g.dominance_share, 4),
            g.valid, g.label,
        ]
        for g in groups
    ]


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #
def _pf_md(pf: float) -> str:
    return "inf" if pf == math.inf else f"{pf:.2f}"


def _dom_md(d: float | None) -> str:
    return "-" if d is None else f"{d * 100:.0f}%"


def _group_table(groups: list[ConditionalGroup]) -> list[str]:
    lines = [
        "| condition | trades | win% | avg_pnl | median_pnl | PF | pnl_std | max_dd | dom% | label |",
        "|---|--:|--:|--:|--:|--:|--:|--:|--:|---|",
    ]
    if not groups:
        lines.append("| _(none)_ | | | | | | | | | |")
    for g in groups:
        lines.append(
            f"| {g.condition} | {g.trades} | {g.win_rate:.1f} | {g.avg_pnl:+.3f} "
            f"| {g.median_pnl:+.3f} | {_pf_md(g.profit_factor)} | {g.pnl_std:.2f} "
            f"| {g.max_drawdown:.2f} | {_dom_md(g.dominance_share)} | {g.label} |"
        )
    return lines


def _contrib_table(label: str, rows) -> list[str]:
    lines = [f"**{label}:**", "", "| rank | strat | sym | side | entry | stake | pnl | cum% |",
             "|--:|---|---|---|--:|--:|--:|--:|"]
    if not rows:
        lines.append("| _(none)_ | | | | | | | |")
    for r in rows:
        lines.append(
            f"| {r.rank} | {r.strategy} | {r.symbol or '-'} | {r.side} | {r.entry_price:.2f} "
            f"| {r.size_usdc:.2f} | {r.pnl_usdc:+.2f} | {r.cum_share * 100:.0f}% |"
        )
    lines.append("")
    return lines


def to_markdown(
    groups: list[ConditionalGroup],
    settled: list[SimPosition],
    resolutions: dict[str, MarketResolution],
    *,
    slippage: float,
    min_trades: int = MIN_TRADES_DEFAULT,
    max_depth: int = MAX_DEPTH,
) -> str:
    active = _active_axes(settled, resolutions)
    skipped = [a for a in _axis_registry(resolutions) if a not in active]
    robust = [g for g in groups if g.label == ROBUST_EDGE]
    weak_valid = [g for g in groups if g.label == WEAK_SIGNAL and g.valid]
    valid_groups = [g for g in groups if g.valid]
    base = _baseline(settled)

    L: list[str] = [f"## Conditional edge -- slippage {slippage:.2f}", ""]
    L.append(f"- settled trades: {len(settled)}")
    L.append(f"- axes used: {', '.join(active) or '(none)'}")
    L.append(f"- axes auto-skipped (single value): {', '.join(skipped) or 'none'}")
    L.append(f"- enumeration: depth 1..{max_depth} (shallow); valid = trades >= {min_trades}")
    L.append(
        f"- ROBUST gate: avg>0 AND median>=0 AND PF>={ROBUST_PF} AND no single trade "
        f"> {MAX_DOMINANCE * 100:.0f}% of group profit"
    )
    L.append(f"- baseline (all trades): avg_pnl {base['avg_pnl']:+.3f}, median_pnl {base['median_pnl']:+.3f}")
    L += ["", "**Baseline by strategy**", "", "| strategy | trades | avg_pnl | median_pnl |",
          "|---|--:|--:|--:|"]
    by_strat: dict[str, list[SimPosition]] = defaultdict(list)
    for p in settled:
        by_strat[p.strategy].append(p)
    for s in sorted(by_strat):
        b = _baseline(by_strat[s])
        L.append(f"| {s} | {b['trades']} | {b['avg_pnl']:+.3f} | {b['median_pnl']:+.3f} |")
    L.append("")

    L += ["### Top ROBUST_EDGE groups", ""]
    if robust:
        L += _group_table(robust)
    else:
        L.append("**None found.** No conditional slice cleared every stability gate.")
    L.append("")

    L += [f"### All VALID groups (trades >= {min_trades})", ""]
    L += _group_table(valid_groups)
    L.append("")

    L += ["### Is there any repeatable edge?", ""]
    if robust:
        L.append(
            f"**Yes -- {len(robust)} ROBUST_EDGE group(s).** Each has positive mean AND median, "
            f"PF >= {ROBUST_PF}, and no single-trade dominance. Still read with the regime/sample caveat."
        )
    else:
        L.append(
            f"**No.** Zero groups cleared the stability gates. {len(weak_valid)} valid group(s) were "
            f"positive-but-fragile (WEAK_SIGNAL) and the rest NO_EDGE -- their positive averages come "
            f"without a positive median or survive only on one large trade."
        )
    L.append("")

    conc = concentration(settled)
    L += ["### Is performance driven by outliers?", ""]
    for side in (conc.profit, conc.loss):
        if side.n_trades:
            L.append(
                f"- {side.label}: top {side.n_for_80pct} of {side.n_trades} trades "
                f"({side.concentration_pct:.0f}%) make 80% -> **{side.verdict}**"
            )
    dom_groups = [g for g in valid_groups
                  if g.dominance_share is not None and g.dominance_share > MAX_DOMINANCE]
    L.append(
        f"- valid positive groups where one trade > {MAX_DOMINANCE * 100:.0f}% of profit: {len(dom_groups)}"
    )
    L.append("")

    L += ["### Contribution -- top 10 profit / top 10 loss trades", ""]
    L += _contrib_table("profit", conc.profit.rows[:10])
    L += _contrib_table("loss", conc.loss.rows[:10])

    L += ["### FINAL: Does ANY conditional slice show repeatable +EV behavior that is not outlier-driven?", ""]
    if robust:
        L.append(
            f"**YES.** {len(robust)} ROBUST_EDGE slice(s) found -- but regime/sample-limited "
            f"(one ~24h bull window, all low_vol); confirm as more regimes accumulate."
        )
    else:
        L.append(
            f"**NO.** No depth-1..{max_depth} slice is simultaneously well-sampled (>= {min_trades} "
            f"trades), positive in mean AND median, profitable enough (PF >= {ROBUST_PF}), and free of "
            f"single-trade dominance. Consistent with the efficient-market through-line; re-run as more "
            f"(non-bull / higher-vol) regimes accumulate."
        )
    L.append("")
    return "\n".join(L)
