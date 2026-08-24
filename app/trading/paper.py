from datetime import UTC, datetime
from decimal import Decimal

from app.domain.models import PaperFill, RiskProfile, TradeIntent
from app.risk.manager import RiskManager


class DuplicateTradeError(ValueError):
    pass


class PaperExecutionEngine:
    """Paper-only execution with fee/slippage and idempotency protection."""

    def __init__(
        self,
        risk_manager: RiskManager,
        fee_rate: Decimal = Decimal("0.0006"),
        slippage_rate: Decimal = Decimal("0.0002"),
    ) -> None:
        self._risk_manager = risk_manager
        self._fee_rate = fee_rate
        self._slippage_rate = slippage_rate
        self._executed_trade_ids: set[str] = set()

    def execute(self, intent: TradeIntent, profile: RiskProfile) -> PaperFill:
        if intent.trade_id in self._executed_trade_ids:
            raise DuplicateTradeError(f"Trade {intent.trade_id} was already executed")
        decision = self._risk_manager.approve(intent, profile)
        if not decision.approved:
            raise PermissionError(decision.reason)
        price = self._slipped_price(intent)
        notional = price * decision.quantity
        self._executed_trade_ids.add(intent.trade_id)
        return PaperFill(
            trade_id=intent.trade_id,
            symbol=intent.symbol,
            side=intent.side,
            quantity=decision.quantity,
            price=price,
            fee=(notional * self._fee_rate).quantize(Decimal("0.01")),
            filled_at=datetime.now(UTC),
        )

    def _slipped_price(self, intent: TradeIntent) -> Decimal:
        modifier = (
            Decimal("1") + self._slippage_rate
            if intent.side.value == "LONG"
            else Decimal("1") - self._slippage_rate
        )
        return (intent.entry * modifier).quantize(Decimal("0.01"))
