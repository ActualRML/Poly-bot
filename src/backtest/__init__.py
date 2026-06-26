"""Snapshot-replay backtest harness.

Replays the stored market stream through the REAL ``strategy.evaluate()`` plus a
simulated portfolio that fills via the SAME realistic ``simulate_taker_fill`` as
the live path (lift ask + walk depth), scored against resolutions recovered
offline from the stream itself. A NEGATIVE SCREEN (kills mirages), not a
positive-confirm — see ``CLAUDE.md`` Working Rules + FINDINGS *Realistic-fill BACKTEST*.

Public surface:
    recover_resolutions(db_path) -> RecoveryResult   # resolution.py
    run_backtest(...)            -> BacktestResult    # engine.py
    format_report(...)           -> str               # report.py
"""
from src.backtest.engine import BacktestResult, SimPortfolio, run_backtest
from src.backtest.recovery import (
    MarketResolution,
    RecoveryResult,
    recover_resolutions,
    round_to_hour,
)
from src.backtest.report import format_report

__all__ = [
    "BacktestResult",
    "MarketResolution",
    "RecoveryResult",
    "SimPortfolio",
    "format_report",
    "recover_resolutions",
    "round_to_hour",
    "run_backtest",
]
