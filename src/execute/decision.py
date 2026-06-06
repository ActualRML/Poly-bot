from dataclasses import dataclass
from enum import Enum


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    SKIP = "SKIP"


@dataclass(frozen=True)
class Decision:
    """
    What a strategy hands back. The executor reads this and either logs
    it (dry-run) or — eventually — turns it into a real order.

    SKIP is the safe default. Any field except `action` and `strategy`
    can be None when action == SKIP.
    """
    action: Action
    strategy: str
    reason: str = ""
    side: str | None = None         # "YES" / "NO" for Polymarket binary
    size_usdc: float | None = None
    price: float | None = None
    market_id: str | None = None

    @classmethod
    def skip(cls, strategy: str, reason: str = "") -> "Decision":
        return cls(action=Action.SKIP, strategy=strategy, reason=reason)
