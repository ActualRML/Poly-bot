from __future__ import annotations

from abc import ABC, abstractmethod

from src.scout.context import ScoutContext
from src.scout.result import FilterResult


class Filter(ABC):
    """Base class for entry filters. Each filter returns a FilterResult."""

    name: str = "filter"

    @abstractmethod
    def evaluate(self, ctx: ScoutContext) -> FilterResult:
        ...
