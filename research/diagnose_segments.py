"""READ-ONLY diagnostic pass over the backtest harness (CLAUDE.md TODO #1).

Replays ``data/bot.db`` through ``src.backtest.run_backtest`` and breaks the
settled trades down MARGINALLY by six axes (strategy, symbol, vol_regime,
price_zone, entry-price band, time-to-resolution) plus a profit/loss
concentration (Pareto) report -- to locate a robust, explainable +EV segment or
confirm none, and to settle the "contrarian +$23 = one fluke?" question.

Read-only on the snapshot DB (run_backtest opens it ``mode=ro``); the only writes
are the output artifacts under ``--out``. Run:

    .venv/Scripts/python.exe research/diagnose_segments.py
    .venv/Scripts/python.exe research/diagnose_segments.py --strategies momentum --min-trades 5

Writes ``segments.csv``, ``contribution.csv`` and ``diagnostics.md`` (and echoes
the markdown to stdout). Verdicts are regime- and sample-limited (one ~24h bull
window, throttled stream) -- read AGGREGATE direction, not individual thin cells.
"""
import argparse
import logging
import sys
from datetime import date
from pathlib import Path

# make `import src.*` resolve regardless of cwd (mirrors analyze_trades.py)
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.backtest.diagnostics import (  # noqa: E402
    CONTRIB_CSV_HEADER,
    MIN_TRADES_DEFAULT,
    SEGMENT_CSV_HEADER,
    contribution_rows,
    segment,
    segment_rows,
    slippage_markdown,
    to_csv,
)
from src.backtest.engine import DEFAULT_SLIPPAGES, STARTING_BALANCE, run_backtest  # noqa: E402


def _check_no_drift(result) -> None:
    """The by-strategy segment totals must sum to the net over settled -- a guard
    that the group-by never drops or double-counts a trade."""
    net = sum(p.pnl_usdc or 0.0 for p in result.settled)
    grouped = sum(s.total_pnl for s in segment(result.settled, lambda p: p.strategy))
    assert abs(grouped - net) < 1e-6, f"segment drift @ slip {result.slippage}: {grouped} != {net}"


def main() -> None:
    parser = argparse.ArgumentParser(prog="python research/diagnose_segments.py",
                                     description=__doc__)
    parser.add_argument("--db", type=Path, default=REPO_ROOT / "data" / "bot.db",
                        help="snapshot DB to replay (default: data/bot.db)")
    parser.add_argument("--strategies", default="contrarian,momentum",
                        help="comma-separated strategy names (default: contrarian,momentum)")
    parser.add_argument("--slippage", type=float, nargs="+", default=list(DEFAULT_SLIPPAGES),
                        help="one or more slippage values to score (default: 0.01 0.03)")
    parser.add_argument("--min-trades", type=int, default=MIN_TRADES_DEFAULT,
                        help=f"flag groups thinner than this (default: {MIN_TRADES_DEFAULT})")
    parser.add_argument("--balance", type=float, default=STARTING_BALANCE,
                        help=f"starting bankroll (default: {STARTING_BALANCE})")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "research" / "diagnostics",
                        help="output directory (default: research/diagnostics)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not args.db.exists():
        raise SystemExit(f"snapshot DB not found: {args.db}")

    names = [s.strip() for s in args.strategies.split(",") if s.strip()]
    results = run_backtest(args.db, names, tuple(args.slippage), starting_balance=args.balance)
    for r in results:
        _check_no_drift(r)

    seg_rows: list[list] = []
    con_rows: list[list] = []
    sections: list[str] = []
    for r in results:
        resolutions = r.recovery.usable
        seg_rows += segment_rows(r.settled, resolutions, slippage=r.slippage, strategies=r.strategies)
        con_rows += contribution_rows(r.settled, slippage=r.slippage, strategies=r.strategies)
        sections.append(slippage_markdown(
            r.settled, resolutions, slippage=r.slippage,
            strategies=r.strategies, min_trades=args.min_trades,
        ))

    first = results[0]
    rec = first.recovery
    header = "\n".join([
        "# Backtest diagnostics",
        "",
        f"- generated: {date.today().isoformat()}",
        f"- data: {args.db}",
        f"- strategies: {', '.join(names)}",
        f"- markets scored: {len(rec.usable)} ({len(rec.excluded)} excluded)",
        f"- settled trades: {len(first.settled)}",
        f"- thin flag: groups with < {args.min_trades} trades",
        "",
        "_Regime- and sample-limited (one ~24h bull window, throttled stream); "
        "read aggregate direction, not individual thin cells._",
        "",
    ])
    markdown = header + "\n" + "\n\n".join(sections) + "\n"

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "segments.csv").write_text(to_csv(SEGMENT_CSV_HEADER, seg_rows), encoding="utf-8")
    (args.out / "contribution.csv").write_text(to_csv(CONTRIB_CSV_HEADER, con_rows), encoding="utf-8")
    (args.out / "diagnostics.md").write_text(markdown, encoding="utf-8")

    print()
    print(markdown)
    print(f"wrote: {args.out / 'segments.csv'}")
    print(f"wrote: {args.out / 'contribution.csv'}")
    print(f"wrote: {args.out / 'diagnostics.md'}")


if __name__ == "__main__":
    main()
