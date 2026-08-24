from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class BotMode(StrEnum):
    DEMO = "DEMO"
    SAFE_LIVE = "SAFE_LIVE"
    AUTO_LIVE = "AUTO_LIVE"


class BotState(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    EMERGENCY_STOP = "EMERGENCY_STOP"


class Side(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class Decision(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    WAIT = "WAIT"


@dataclass(frozen=True)
class RiskProfile:
    risk_per_trade_pct: Decimal = Decimal("0.005")
    max_position_notional: Decimal = Decimal("500")
    max_leverage: Decimal = Decimal("2")
    max_concurrent_positions: int = 2
    max_daily_loss_pct: Decimal = Decimal("0.02")
    min_risk_reward: Decimal = Decimal("2")
    max_consecutive_losses: int = 3
    cooldown_minutes: int = 30
    max_volatility_pct: Decimal = Decimal("0.04")
    max_spread_pct: Decimal = Decimal("0.002")


@dataclass(frozen=True)
class TradeIntent:
    trade_id: str
    user_id: int
    symbol: str
    side: Side
    entry: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    equity: Decimal
    daily_realized_pnl: Decimal
    open_positions: int
    consecutive_losses: int
    requested_leverage: Decimal = Decimal("1")
    available_balance: Decimal | None = None
    volatility_pct: Decimal = Decimal("0")
    spread_pct: Decimal = Decimal("0")
    cooldown_active: bool = False


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str
    quantity: Decimal = Decimal("0")
    risk_amount: Decimal = Decimal("0")


@dataclass(frozen=True)
class PaperFill:
    trade_id: str
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    fee: Decimal
    filled_at: datetime


@dataclass(frozen=True)
class Signal:
    symbol: str
    timeframe: str
    decision: Decision
    signal_score: int
    trend_score: int
    momentum_score: int
    volatility_score: int
    reasons: tuple[str, ...]
    proposed_entry: Decimal | None = None
    proposed_stop_loss: Decimal | None = None
    proposed_take_profit: Decimal | None = None
    risk_reward_ratio: Decimal | None = None


@dataclass(frozen=True)
class Position:
    trade_id: str
    user_id: int
    symbol: str
    side: Side
    quantity: Decimal
    entry_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    leverage: Decimal
    entry_fee: Decimal
    opened_at: datetime
    initial_risk: Decimal
    break_even_enabled: bool = True
    trailing_distance: Decimal | None = None
    high_watermark: Decimal | None = None
    low_watermark: Decimal | None = None
    exchange: str = "paper"
    account_id: str | None = None
    position_id: str | None = None
    strategy_version: str = "unknown"


@dataclass(frozen=True)
class ClosedPosition:
    position: Position
    exit_price: Decimal
    exit_fee: Decimal
    realized_pnl: Decimal
    reason: str
    closed_at: datetime
