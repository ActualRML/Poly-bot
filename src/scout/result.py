from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FilterResult:
    passed: bool
    reason: str
    value: Any = None

    @classmethod
    def pass_(cls, reason: str = "ok", value: Any = None) -> "FilterResult":
        return cls(passed=True, reason=reason, value=value)

    @classmethod
    def fail(cls, reason: str, value: Any = None) -> "FilterResult":
        return cls(passed=False, reason=reason, value=value)


@dataclass
class ScoutDecision:
    """
    Invariant: len(reasons_passed) + len(reasons_failed) + len(reasons_skipped) == max_score.

    max_score = total filters in pipeline (fixed across markets, set at evaluate_entry start).
    add() records evaluated filters; skip() records filters that never ran due to
    pipeline short-circuit.
    """

    enter: bool = False
    breakdown: dict[str, FilterResult] = field(default_factory=dict)
    score: int = 0
    max_score: int = 0
    reasons_failed: list[str] = field(default_factory=list)
    reasons_passed: list[str] = field(default_factory=list)
    reasons_skipped: list[str] = field(default_factory=list)

    def add(self, name: str, result: FilterResult) -> None:
        self.breakdown[name] = result
        if result.passed:
            self.score += 1
            self.reasons_passed.append(name)
        else:
            self.reasons_failed.append(f"{name}: {result.reason}")

    def skip(self, name: str) -> None:
        self.reasons_skipped.append(name)

    def summary(self) -> str:
        skipped = f" skipped={len(self.reasons_skipped)}" if self.reasons_skipped else ""
        return (
            f"enter={self.enter} score={self.score}/{self.max_score} "
            f"failed=({'; '.join(self.reasons_failed) or '-'}){skipped}"
        )
