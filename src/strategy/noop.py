from src.data.snapshot import MarketSnapshot
from src.execute.decision import Decision
from src.strategy.base import Strategy


class Plugin(Strategy):
    """
    Always-skip strategy. Proves the plugin pipeline end-to-end without
    coupling to any real signal: orchestrator loads it, every event
    triggers evaluate(), every call returns SKIP, executor logs nothing
    trade-related. Delete or replace once you ship a real strategy.

    Each strategy module must export a class named `Plugin` — that's
    the convention the orchestrator looks for. Class name is fixed;
    `name` attribute is the human-readable label.
    """

    name = "noop"

    async def evaluate(self, snapshot: MarketSnapshot) -> Decision:
        return Decision.skip(strategy=self.name, reason="noop strategy never trades")
