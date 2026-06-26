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
    entry_floor: float = 0.15    # skip opens below this price (thin-book realism floor)
    bet_fraction: float = 0.02   # stake = balance * this fraction
    # Cap the EFFECTIVE taker-fill price: the depth walk stops at this limit, so a
    # thin longshot book can never fill us into expensive (favorite-priced) shares
    # — only the liquidity available at/under the cap is taken, the rest is skipped.
    # 1.0 = no cap (prices are always < $1); set below 1.0 to enforce a ceiling
    # (e.g. contrarian = 0.30, below its empirical ~39% win rate so fills stay +EV).
    entry_ceiling: float = 1.0
