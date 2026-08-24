from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from app.domain.models import Decision


class Trend(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    EXTREME = "EXTREME"


class MarketContext(BaseModel):
    symbol: str
    current_price: Decimal = Field(gt=0)
    timestamp: str
    trends: dict[str, Trend]
    rsi: Decimal = Field(ge=0, le=100)
    macd: Decimal
    ema_9: Decimal = Field(gt=0)
    ema_21: Decimal = Field(gt=0)
    ema_50: Decimal = Field(gt=0)
    ema_200: Decimal | None = Field(default=None, gt=0)
    atr: Decimal = Field(gt=0)
    volatility: Decimal = Field(ge=0)
    spread: Decimal = Field(ge=0)
    technical_decision: Decision
    technical_score: int = Field(ge=0, le=100)
    open_positions: int = Field(ge=0)
    risk_summary: str


class AIResult(BaseModel):
    decision: Decision
    confidence: int = Field(ge=0, le=100)
    trend_score: int = Field(ge=0, le=100)
    momentum_score: int = Field(ge=0, le=100)
    volatility_score: int = Field(ge=0, le=100)
    setup_quality: int = Field(ge=0, le=100)
    suggested_entry: Decimal | None = Field(default=None, gt=0)
    suggested_stop_loss: Decimal | None = Field(default=None, gt=0)
    suggested_take_profit: Decimal | None = Field(default=None, gt=0)
    invalidation_price: Decimal | None = Field(default=None, gt=0)
    reasons: tuple[str, ...] = ()
    risk_flags: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_prices(self):
        prices = (self.suggested_entry, self.suggested_stop_loss, self.suggested_take_profit)
        if self.decision is Decision.WAIT and any(prices):
            raise ValueError("WAIT may not propose prices")
        if self.decision is not Decision.WAIT and not all(prices):
            raise ValueError("Actionable result requires entry, stop loss and take profit")
        if self.decision is Decision.LONG and not self.suggested_stop_loss < self.suggested_entry < self.suggested_take_profit:
            raise ValueError("Invalid LONG price ordering")
        if self.decision is Decision.SHORT and not self.suggested_take_profit < self.suggested_entry < self.suggested_stop_loss:
            raise ValueError("Invalid SHORT price ordering")
        return self


class RoleResult(BaseModel):
    direction: Trend
    score: int = Field(ge=0, le=100)
    risk: RiskLevel | None = None
    reasons: tuple[str, ...] = ()
