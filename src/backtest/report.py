"""Human-readable summary of a backtest run.

Win-rate and trade counts are slippage-independent (the winner is the recovered
outcome, not the cost), so they're shown once; only net PnL varies by slippage —
the headline being whether a strategy flips sign between the production 0.03 and a
realistic ~0.01 (CLAUDE.md's momentum finding).
"""
from collections import defaultdict

from src.backtest.engine import BacktestResult, SimPosition


def _by_strategy(settled: list[SimPosition]) -> dict[str, list[SimPosition]]:
    out: dict[str, list[SimPosition]] = defaultdict(list)
    for p in settled:
        out[p.strategy].append(p)
    return out


def _wr(positions: list[SimPosition]) -> tuple[int, int, float]:
    wins = sum(1 for p in positions if p.won)
    n = len(positions)
    return wins, n, (wins / n * 100 if n else 0.0)


def _net(positions: list[SimPosition]) -> float:
    return sum(p.pnl_usdc or 0.0 for p in positions)


def format_report(results: list[BacktestResult], *, db_label: str = "") -> str:
    """Render one-or-more (per-slippage) results into a single report string."""
    if not results:
        return "(no results)"

    first = results[0]
    rec = first.recovery
    yes = sum(1 for m in rec.usable.values() if m.outcome == "YES")
    no = len(rec.usable) - yes
    lines: list[str] = []
    lines.append("=== BACKTEST: snapshot-replay ===")
    if db_label:
        lines.append(f"data:       {db_label}")
    lines.append(f"strategies: {', '.join(first.strategies)}")
    lines.append(
        f"markets:    {len(rec.usable)} scored (YES {yes} / NO {no}), "
        f"{len(rec.excluded)} excluded"
    )
    opened = len(first.settled)
    skip_s = ", ".join(f"{k}={v}" for k, v in sorted(first.skipped.items())) or "none"
    lines.append(f"opened:     {opened} positions   (gate-blocked opens: {skip_s})")
    lines.append("")

    # --- headline: trades + WR once, net PnL per slippage ---
    slips = [r.slippage for r in results]
    by_strat_per_slip = {r.slippage: _by_strategy(r.settled) for r in results}
    strat_names = list(first.strategies)

    head = f"  {'strategy':<12} {'trades':>6} {'WR':>7}" + "".join(
        f" {('net@' + format(s, '.2f')):>12}" for s in slips
    )
    lines.append("--- PnL by strategy x slippage ---")
    lines.append(head)

    def _row(label: str, picker) -> str:
        ref = picker(by_strat_per_slip[slips[0]])
        wins, n, wr = _wr(ref)
        cells = "".join(
            f" {_net(picker(by_strat_per_slip[s])):>+12.2f}" for s in slips
        )
        return f"  {label:<12} {n:>6} {wr:>6.1f}%" + cells

    for name in strat_names:
        lines.append(_row(name, lambda d, name=name: d.get(name, [])))
    lines.append(_row("ALL", lambda d: [p for ps in d.values() for p in ps]))
    lines.append("")

    # --- per-slippage balance / ROI ---
    for r in results:
        roi = (r.final_balance - r.starting_balance) / r.starting_balance * 100
        lines.append(
            f"slippage {r.slippage:.2f}: final ${r.final_balance:.2f} "
            f"(start ${r.starting_balance:.2f}, ROI {roi:+.2f}%, "
            f"net {_net(r.settled):+.2f})"
        )

    return "\n".join(lines)
