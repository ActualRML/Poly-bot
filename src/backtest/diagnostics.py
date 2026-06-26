"""Diagnostic segment & concentration analysis over ``BacktestResult.settled``.

Answers TODO #1: is a strategy's PnL broad behavior, or a handful of outlier
trades? Two read-only views, both pure (mirrors ``report.py`` — every function
returns data or a string; the ``research/`` runner owns all I/O):

* **Segment** — slice settled trades MARGINALLY (one axis at a time, never the
  cross-product: the sample is far too thin for that) and report per group
  trades / win_rate / total_pnl / avg_pnl / expectancy_R / profit_factor. Groups
  thinner than ``min_trades`` are flagged ``[thin]`` — with contrarian's 3 trades
  every contrarian cell is noise, and that flag says so.
* **Concentration** — a Pareto split: how few trades make 80% of the gross
  profit (and of the gross loss). This is the high-value deliverable and the one
  that settles the "contrarian +$23 = one 5x fluke?" question directly.

The only datum not already on ``SimPosition`` is time-to-resolution, recovered
for free from ``opened_ts`` and the ``resolve_ts`` the backtest already carries in
``BacktestResult.recovery``. Output strings are kept ASCII so a Windows cp1252
console / file write never chokes.
"""
import csv
import io
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Iterable

from src.backtest.engine import SimPosition
from src.backtest.recovery import MarketResolution

MIN_TRADES_DEFAULT = 10     # thinner groups are flagged (precedent: calibrate_zones.MIN_ZONE_N)
PARETO_THRESHOLD = 0.80     # "top N trades make 80% of the gross" — the concentration cut
BAND_WIDTH_CENTS = 10       # entry-price band granularity (10c-wide, half-open [lo, lo+10c))

# Natural display orders for the ordered axes (unordered axes sort by total_pnl).
VOL_ORDER = ("low_vol", "mid_vol", "high_vol")
ZONE_ORDER = ("extreme_low", "low", "uncertain", "high", "extreme_high")

# time-to-resolution buckets as (label, lo_secs_inclusive, hi_secs_exclusive),
# displayed far->near. Floor is portfolio.MIN_TIME_TO_RESOLVE_SEC (120s) so nothing
# legitimately opens under 2m; anything that slips through lands in "other".
_TTR_BUCKETS = (
    (">=30m", 30 * 60, math.inf),
    ("15-30m", 15 * 60, 30 * 60),
    ("5-15m", 5 * 60, 15 * 60),
    ("2-5m", 2 * 60, 5 * 60),
)
TTR_ORDER = tuple(b[0] for b in _TTR_BUCKETS)

_NONE_KEY = "(none)"


# --------------------------------------------------------------------------- #
# Per-segment statistics
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SegmentStat:
    key: str
    trades: int
    win_rate: float        # percent, 0..100
    total_pnl: float
    avg_pnl: float
    expectancy_r: float    # mean(pnl / stake) -- per-dollar, comparable across bet sizes
    profit_factor: float   # gross_profit / gross_loss; math.inf when there are no losses


def _pnl(p: SimPosition) -> float:
    return p.pnl_usdc or 0.0


def _stat(key: str, ps: list[SimPosition]) -> SegmentStat:
    """All six metrics for one group. ``profit_factor`` is inf when a group has
    profit but no losses, 0 when it has neither (so it never divides by zero)."""
    n = len(ps)
    if not n:
        return SegmentStat(key, 0, 0.0, 0.0, 0.0, 0.0, 0.0)
    wins = sum(1 for p in ps if p.won)
    total = sum(_pnl(p) for p in ps)
    gross_profit = sum(_pnl(p) for p in ps if _pnl(p) > 0)
    gross_loss = -sum(_pnl(p) for p in ps if _pnl(p) < 0)
    if gross_loss > 0:
        pf = gross_profit / gross_loss
    else:
        pf = math.inf if gross_profit > 0 else 0.0
    exp_r = sum(_pnl(p) / p.size_usdc for p in ps if p.size_usdc) / n
    return SegmentStat(
        key=key,
        trades=n,
        win_rate=wins / n * 100,
        total_pnl=total,
        avg_pnl=total / n,
        expectancy_r=exp_r,
        profit_factor=pf,
    )


def segment(
    settled: list[SimPosition],
    keyfunc: Callable[[SimPosition], str | None],
    *,
    key_order: Iterable[str] | None = None,
) -> list[SegmentStat]:
    """Group ``settled`` by ``keyfunc`` and compute per-group stats. Ordered by
    ``key_order`` when given (so gradient axes read in their natural order), else
    by ``total_pnl`` descending (so the money-makers/losers sort to the top)."""
    groups: dict[str, list[SimPosition]] = defaultdict(list)
    for p in settled:
        groups[keyfunc(p) or _NONE_KEY].append(p)
    stats = [_stat(k, ps) for k, ps in groups.items()]
    if key_order is not None:
        rank = {k: i for i, k in enumerate(key_order)}
        stats.sort(key=lambda s: rank.get(s.key, len(rank)))
    else:
        stats.sort(key=lambda s: s.total_pnl, reverse=True)
    return stats


# --------------------------------------------------------------------------- #
# Axis key functions
# --------------------------------------------------------------------------- #
def entry_band(p: SimPosition) -> str:
    """10c-wide entry band, e.g. ``0.60-0.70`` (half-open). Polymarket fills are
    penny-aligned, so rounding to the nearest cent avoids float-floor errors
    (0.60/0.10 == 5.999...) without misclassifying real prices."""
    cents = round(p.entry_price * 100)
    lo = (cents // BAND_WIDTH_CENTS) * BAND_WIDTH_CENTS
    return f"{lo / 100:.2f}-{(lo + BAND_WIDTH_CENTS) / 100:.2f}"


def ttr_keyfunc(
    resolutions: dict[str, MarketResolution],
) -> Callable[[SimPosition], str | None]:
    """Build a keyfunc that buckets a trade by seconds from entry to resolution,
    using the resolve_ts the backtest already recovered (no DB / network)."""
    def key(p: SimPosition) -> str | None:
        res = resolutions.get(p.market_id)
        if res is None:
            return None
        secs = (res.resolve_ts - p.opened_ts).total_seconds()
        for label, lo, hi in _TTR_BUCKETS:
            if lo <= secs < hi:
                return label
        return None
    return key


def _secondary_axes(
    settled: list[SimPosition], resolutions: dict[str, MarketResolution]
) -> dict[str, list[SegmentStat]]:
    """The five non-strategy axes for a given trade subset (strategy is sliced
    one level up, so it would be a single-value axis here)."""
    bands = sorted({entry_band(p) for p in settled})
    return {
        "symbol": segment(settled, lambda p: p.symbol),
        "vol_regime": segment(settled, lambda p: p.vol_regime, key_order=VOL_ORDER),
        "price_zone": segment(settled, lambda p: p.price_zone, key_order=ZONE_ORDER),
        "entry_band": segment(settled, entry_band, key_order=bands),
        "time_to_resolve": segment(settled, ttr_keyfunc(resolutions), key_order=TTR_ORDER),
    }


# --------------------------------------------------------------------------- #
# Profit / loss concentration (Pareto)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TradeContribution:
    rank: int
    strategy: str
    symbol: str | None
    market_id: str
    side: str
    entry_price: float
    size_usdc: float
    pnl_usdc: float
    cum_pnl: float        # signed running total down this side
    cum_share: float      # cumulative fraction of the side's gross total, 0..1


@dataclass(frozen=True)
class ConcentrationSide:
    label: str            # "profit" | "loss"
    gross_total: float    # gross profit, or gross loss as a positive magnitude
    n_trades: int         # trades contributing to this side
    n_for_80pct: int      # fewest trades whose cumulative magnitude reaches 80%
    rows: list[TradeContribution]

    @property
    def concentration_pct(self) -> float:
        """Share of this side's trades needed to reach 80% of its gross. Small =>
        outlier-driven; large => broad."""
        return self.n_for_80pct / self.n_trades * 100 if self.n_trades else 0.0

    @property
    def verdict(self) -> str:
        if not self.n_trades:
            return "none"
        pct = self.concentration_pct
        if pct <= 35:
            return "OUTLIER-driven"
        if pct >= 65:
            return "broad"
        return "mixed"


@dataclass(frozen=True)
class ConcentrationReport:
    profit: ConcentrationSide
    loss: ConcentrationSide


def _side(positions: list[SimPosition], *, is_profit: bool) -> ConcentrationSide:
    sel = [p for p in positions if (_pnl(p) > 0) == is_profit and _pnl(p) != 0]
    sel.sort(key=lambda p: abs(_pnl(p)), reverse=True)
    gross = sum(abs(_pnl(p)) for p in sel)
    rows: list[TradeContribution] = []
    cum = 0.0
    n_for_80 = len(sel)
    reached = False
    for i, p in enumerate(sel, 1):
        cum += abs(_pnl(p))
        share = cum / gross if gross else 0.0
        rows.append(
            TradeContribution(
                rank=i, strategy=p.strategy, symbol=p.symbol, market_id=p.market_id,
                side=p.side, entry_price=p.entry_price, size_usdc=p.size_usdc,
                pnl_usdc=_pnl(p), cum_pnl=cum if is_profit else -cum, cum_share=share,
            )
        )
        if not reached and share >= PARETO_THRESHOLD:
            n_for_80, reached = i, True
    return ConcentrationSide(
        label="profit" if is_profit else "loss",
        gross_total=gross, n_trades=len(sel), n_for_80pct=n_for_80, rows=rows,
    )


def concentration(settled: list[SimPosition]) -> ConcentrationReport:
    """Pareto split of profit and loss, scored independently."""
    return ConcentrationReport(
        profit=_side(settled, is_profit=True),
        loss=_side(settled, is_profit=False),
    )


# --------------------------------------------------------------------------- #
# CSV (long format) -- pure: returns the file body as a string
# --------------------------------------------------------------------------- #
SEGMENT_CSV_HEADER = [
    "slippage", "scope", "axis", "key", "trades", "win_rate",
    "total_pnl", "avg_pnl", "expectancy_r", "profit_factor",
]
CONTRIB_CSV_HEADER = [
    "slippage", "scope", "side", "rank", "strategy", "symbol", "market_id",
    "trade_side", "entry_price", "size_usdc", "pnl_usdc", "cum_pnl", "cum_share",
]


def _pf_cell(pf: float) -> object:
    return "inf" if pf == math.inf else round(pf, 4)


def _stat_cells(s: SegmentStat) -> list:
    return [
        s.key, s.trades, round(s.win_rate, 2), round(s.total_pnl, 4),
        round(s.avg_pnl, 4), round(s.expectancy_r, 4), _pf_cell(s.profit_factor),
    ]


def segment_rows(
    settled: list[SimPosition],
    resolutions: dict[str, MarketResolution],
    *,
    slippage: float,
    strategies: list[str],
) -> list[list]:
    """Long-format segment rows for every (scope, axis): the strategy axis once
    under scope ALL, then the five secondary axes for each strategy and for ALL."""
    slip = f"{slippage:.2f}"
    rows: list[list] = []
    for s in segment(settled, lambda p: p.strategy):
        rows.append([slip, "ALL", "strategy", *_stat_cells(s)])
    for scope in [*strategies, "ALL"]:
        subset = settled if scope == "ALL" else [p for p in settled if p.strategy == scope]
        for axis, stats in _secondary_axes(subset, resolutions).items():
            for s in stats:
                rows.append([slip, scope, axis, *_stat_cells(s)])
    return rows


def contribution_rows(
    settled: list[SimPosition], *, slippage: float, strategies: list[str]
) -> list[list]:
    slip = f"{slippage:.2f}"
    rows: list[list] = []
    for scope in [*strategies, "ALL"]:
        subset = settled if scope == "ALL" else [p for p in settled if p.strategy == scope]
        conc = concentration(subset)
        for side in (conc.profit, conc.loss):
            for r in side.rows:
                rows.append([
                    slip, scope, side.label, r.rank, r.strategy, r.symbol or "",
                    r.market_id, r.side, round(r.entry_price, 4), round(r.size_usdc, 4),
                    round(r.pnl_usdc, 4), round(r.cum_pnl, 4), round(r.cum_share, 4),
                ])
    return rows


def to_csv(header: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #
def _pf_md(pf: float) -> str:
    return "inf" if pf == math.inf else f"{pf:.2f}"


def _segment_table_md(title: str, stats: list[SegmentStat], min_trades: int) -> list[str]:
    lines = [f"**By {title}**", "", "| key | trades | win% | total_pnl | avg_pnl | exp_R | PF |",
             "|---|--:|--:|--:|--:|--:|--:|"]
    if not stats:
        lines.append("| _(no trades)_ | | | | | | |")
    for s in stats:
        flag = "" if s.trades >= min_trades else " [thin]"
        lines.append(
            f"| {s.key}{flag} | {s.trades} | {s.win_rate:.1f} | {s.total_pnl:+.2f} "
            f"| {s.avg_pnl:+.3f} | {s.expectancy_r:+.3f} | {_pf_md(s.profit_factor)} |"
        )
    lines.append("")
    return lines


def _concentration_md(conc: ConcentrationReport, *, top_n: int = 15) -> list[str]:
    lines = ["**Profit / loss concentration (Pareto)**", ""]
    for side in (conc.profit, conc.loss):
        if not side.n_trades:
            lines += [f"- _{side.label}:_ none", ""]
            continue
        lines.append(
            f"- _{side.label}:_ gross {side.gross_total:.2f} over {side.n_trades} trades; "
            f"top {side.n_for_80pct} ({side.concentration_pct:.0f}% of them) make 80% "
            f"-> **{side.verdict}**"
        )
        lines += ["", "| rank | strat | sym | side | entry | stake | pnl | cum% |",
                  "|--:|---|---|---|--:|--:|--:|--:|"]
        for r in side.rows[:top_n]:
            lines.append(
                f"| {r.rank} | {r.strategy} | {r.symbol or '-'} | {r.side} | {r.entry_price:.2f} "
                f"| {r.size_usdc:.2f} | {r.pnl_usdc:+.2f} | {r.cum_share * 100:.0f}% |"
            )
        if len(side.rows) > top_n:
            lines.append(f"| ... | | | | | | | (+{len(side.rows) - top_n} more) |")
        lines.append("")
    return lines


def slippage_markdown(
    settled: list[SimPosition],
    resolutions: dict[str, MarketResolution],
    *,
    slippage: float,
    strategies: list[str],
    min_trades: int = MIN_TRADES_DEFAULT,
) -> str:
    """Full markdown section for one slippage: a headline by-strategy table, then
    per-strategy (and ALL) secondary-axis tables + a concentration report."""
    lines = [f"## Slippage {slippage:.2f}", ""]
    lines += _segment_table_md("strategy", segment(settled, lambda p: p.strategy), min_trades)
    for scope in [*strategies, "ALL"]:
        subset = settled if scope == "ALL" else [p for p in settled if p.strategy == scope]
        lines += [f"### Scope: {scope} ({len(subset)} trades)", ""]
        for axis, stats in _secondary_axes(subset, resolutions).items():
            lines += _segment_table_md(axis, stats, min_trades)
        lines += _concentration_md(concentration(subset))
    return "\n".join(lines)
