"""READ-ONLY conditional-edge miner (structural edge detection).

Replays ``data/bot.db`` through ``src.backtest.run_backtest`` and mines the settled
trades for STABLE, repeatable, non-outlier-driven +EV slices across shallow (1- and
2-axis) combinations of strategy / symbol / entry-band / time-to-resolve / vol_regime
(degenerate single-value axes are skipped). Priority is stability > magnitude,
median > mean -- NOT profit maximization.

Read-only on the snapshot DB (run_backtest opens it ``mode=ro``); the only writes are
the artifacts under ``--out``. Run:

    .venv/Scripts/python.exe research/mine_conditional_edge.py
    .venv/Scripts/python.exe research/mine_conditional_edge.py --min-trades 8 --max-depth 2

Writes ``conditional_edge.md`` + ``conditional_edge.csv`` (and echoes the markdown).
Verdicts are regime/sample-limited (one ~24h bull window, all low_vol).
"""
import argparse
import logging
import sys
from datetime import date
from pathlib import Path

# make `import src.*` resolve regardless of cwd (mirrors analyze_trades.py)
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.backtest.conditional_miner import (  # noqa: E402
    CSV_HEADER,
    MAX_DEPTH,
    MIN_TRADES_DEFAULT,
    mine,
    to_csv_rows,
    to_markdown,
)
from src.backtest.diagnostics import to_csv  # noqa: E402
from src.backtest.engine import DEFAULT_SLIPPAGE, STARTING_BALANCE, run_backtest  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(prog="python research/mine_conditional_edge.py",
                                     description=__doc__)
    parser.add_argument("--db", type=Path, default=REPO_ROOT / "data" / "bot.db",
                        help="snapshot DB to replay (default: data/bot.db)")
    parser.add_argument("--strategies", default="contrarian,momentum",
                        help="comma-separated strategy names (default: contrarian,momentum)")
    parser.add_argument("--slippage", type=float, default=DEFAULT_SLIPPAGE,
                        help=f"slippage to score at (default: {DEFAULT_SLIPPAGE} = realistic ~1c)")
    parser.add_argument("--min-trades", type=int, default=MIN_TRADES_DEFAULT,
                        help=f"a slice needs >= this many trades to be VALID (default: {MIN_TRADES_DEFAULT})")
    parser.add_argument("--max-depth", type=int, default=MAX_DEPTH,
                        help=f"max axes combined per slice (default: {MAX_DEPTH} = shallow)")
    parser.add_argument("--balance", type=float, default=STARTING_BALANCE,
                        help=f"starting bankroll (default: {STARTING_BALANCE})")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "research" / "diagnostics",
                        help="output directory (default: research/diagnostics)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not args.db.exists():
        raise SystemExit(f"snapshot DB not found: {args.db}")

    names = [s.strip() for s in args.strategies.split(",") if s.strip()]
    result = run_backtest(args.db, names, (args.slippage,), starting_balance=args.balance)[0]
    resolutions = result.recovery.usable

    groups = mine(result.settled, resolutions,
                  min_trades=args.min_trades, max_depth=args.max_depth)

    rec = result.recovery
    header = "\n".join([
        "# Conditional edge mining",
        "",
        f"- generated: {date.today().isoformat()}",
        f"- data: {args.db}",
        f"- strategies: {', '.join(names)}",
        f"- slippage: {result.slippage:.2f}",
        f"- markets scored: {len(rec.usable)} ({len(rec.excluded)} excluded)",
        f"- settled trades: {len(result.settled)}",
        "",
        "_Structural edge DETECTION, not optimization. Regime/sample-limited "
        "(one ~24h bull window, all low_vol)._",
        "",
    ])
    markdown = header + "\n" + to_markdown(
        groups, result.settled, resolutions,
        slippage=result.slippage, min_trades=args.min_trades, max_depth=args.max_depth,
    ) + "\n"
    csv_body = to_csv(CSV_HEADER, to_csv_rows(groups, slippage=result.slippage))

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "conditional_edge.md").write_text(markdown, encoding="utf-8")
    (args.out / "conditional_edge.csv").write_text(csv_body, encoding="utf-8")

    print()
    print(markdown)
    print(f"wrote: {args.out / 'conditional_edge.md'}")
    print(f"wrote: {args.out / 'conditional_edge.csv'}")


if __name__ == "__main__":
    main()
