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
    enter: bool = False
    breakdown: dict[str, FilterResult] = field(default_factory=dict)
    score: int = 0
    max_score: int = 0
    reasons_failed: list[str] = field(default_factory=list)
    reasons_passed: list[str] = field(default_factory=list)

    def add(self, name: str, result: FilterResult) -> None:
        self.breakdown[name] = result
        self.max_score += 1
        if result.passed:
            self.score += 1
            self.reasons_passed.append(name)
        else:
            self.reasons_failed.append(f"{name}: {result.reason}")

    def summary(self) -> str:
        return (
            f"enter={self.enter} score={self.score}/{self.max_score} "
            f"failed=({'; '.join(self.reasons_failed) or '-'})"
        )
