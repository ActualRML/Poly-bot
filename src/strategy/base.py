from abc import ABC, abstractmethod

from src.data.snapshot import MarketSnapshot
from src.execute.decision import Decision
from src.strategy.params import StrategyParams


class Strategy(ABC):
    """
    Plugin contract. Every strategy is a Strategy subclass that exposes
    a single async `evaluate(snapshot) -> Decision`.

    Hard rules — enforced by convention, not the type system:

    1. Do NOT import from src.api or src.data. Strategies receive
       everything they need in the MarketSnapshot. If a strategy needs
       new data, add the field to MarketSnapshot and populate it in the
       orchestrator. This keeps strategies unit-testable without mocks.

    2. State lives in `self` and is explicit. The orchestrator
       instantiates each strategy once at startup; subsequent calls
       share that instance. No globals.

    3. evaluate() must always return a Decision — never raise. If you
       can't decide, return Decision.skip(reason="..."). Raised
       exceptions in one strategy must not blow up sibling strategies.
       (The orchestrator catches anyway, but returning SKIP is cleaner.)
    """

    name: str = "unnamed"
    # Per-strategy knobs: entry floor + bet sizing. Override on the subclass to
    # tune; the default is the conservative house value. It's frozen, so sharing
    # one instance across strategies that don't override is safe. Only these two
    # knobs are strategy-controlled — global safety limits (slippage, dust,
    # time-to-resolve) live in src/execute/portfolio.py and are NOT overridable.
    params: StrategyParams = StrategyParams()

    @abstractmethod
    async def evaluate(self, snapshot: MarketSnapshot) -> Decision:
        ...
