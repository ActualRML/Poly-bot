"""CLI entry point for the snapshot-replay backtest.

    .venv/Scripts/python.exe -m src.backtest
    .venv/Scripts/python.exe -m src.backtest --strategies contrarian --slippage 0.01 0.03
    .venv/Scripts/python.exe -m src.backtest --db data/bot.db --balance 1000

Read-only on the snapshot DB; never places orders or writes state.
"""
import argparse
import logging
from pathlib import Path

from src.backtest.engine import DEFAULT_SLIPPAGES, STARTING_BALANCE, run_backtest
from src.backtest.report import format_report


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m src.backtest", description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/bot.db"),
                        help="snapshot DB to replay (default: data/bot.db)")
    parser.add_argument("--strategies", default="contrarian",
                        help="comma-separated strategy names (default: contrarian)")
    parser.add_argument("--slippage", type=float, nargs="+", default=list(DEFAULT_SLIPPAGES),
                        help="EXTRA fill-stress cents on top of the realistic fill "
                             "(default: 0.0 0.01; 0.0 = the honest fill)")
    parser.add_argument("--balance", type=float, default=STARTING_BALANCE,
                        help=f"starting bankroll (default: {STARTING_BALANCE})")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not args.db.exists():
        raise SystemExit(f"snapshot DB not found: {args.db}")

    names = [s.strip() for s in args.strategies.split(",") if s.strip()]
    results = run_backtest(
        args.db, names, tuple(args.slippage), starting_balance=args.balance,
    )
    print()
    print(format_report(results, db_label=str(args.db)))


if __name__ == "__main__":
    main()
