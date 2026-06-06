"""Per-strategy, TIGHTENABLE knobs.

A strategy declares its own entry floor and bet sizing via a StrategyParams
instance on its Plugin. These are the ONLY two knobs a strategy controls.

The hard safety limits — slippage assumption, dust minimum, time-to-resolution
— are GLOBAL module constants in src/execute/portfolio.py and are deliberately
NOT fields here, so no strategy can loosen them.

The defaults below match what used to be the module-level MIN_ENTRY_PRICE /
BET_FRACTION constants in portfolio.py; this dataclass is now their single
source of truth. An unregistered or unknown strategy resolves to these
defaults (see Portfolio._params_for).
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyParams:
    entry_floor: float = 0.15   # skip opens below this price (thin-book realism floor)
    bet_fraction: float = 0.02  # stake = balance * this fraction
