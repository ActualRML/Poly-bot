from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator


class ScoutSignal(BaseModel):
    market_id: str
    question: str
    category: str
    current_price: float
    probability_forecast: float
    confidence_score: float
    gemini_reasoning: str
    taker_fee_adjusted: bool
    volume_24h: float
    spread_pct: float
    scanned_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("gemini_reasoning")
    @classmethod
    def truncate_reasoning(cls, v: str) -> str:
        return v[:200]

    @field_validator("probability_forecast", "confidence_score", "current_price")
    @classmethod
    def clamp_0_1(cls, v: float) -> float:
        return max(0.0, min(1.0, v))
