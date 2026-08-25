from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from app.exchanges.health import ExchangeHealthMonitor
from app.exchanges.market_data import MarketDataRouter
from app.exchanges.models import ExchangeOrder, MarketContext, OrderRequest
from app.exchanges.portfolio import ManagedPosition, PortfolioManager
from app.risk.portfolio import (
    ExchangeRiskLimits,
    GlobalPortfolioRiskManager,
    GlobalRiskLimits,
    ProposedExposure,
)
from app.trading.execution_store import (
    ExecutionOrderStore,
    InMemoryExecutionOrderStore,
    OrderOutcomeUnknown,
)


@dataclass(frozen=True)
class ExecutionRiskContext:
    equity_by_account: dict[tuple[str, str], Decimal]
    daily_pnl_by_account: dict[tuple[str, str], Decimal]
    exchange_limits: ExchangeRiskLimits = field(default_factory=ExchangeRiskLimits)
    global_limits: GlobalRiskLimits = field(default_factory=GlobalRiskLimits)
    available_by_account: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    consecutive_losses_by_account: dict[tuple[str, str], int] = field(default_factory=dict)
    cooldown_accounts: frozenset[tuple[str, str]] = frozenset()
    allowed_symbols: frozenset[str] | None = None
    kill_switch_active: bool = False
    max_market_data_age_seconds: Decimal = Decimal("30")


class MultiExchangeTradingEngine:
    """Single execution engine; exchange differences stay behind ExchangeAdapter."""

    def __init__(
        self,
        router: MarketDataRouter,
        health: ExchangeHealthMonitor,
        portfolio: PortfolioManager,
        risk: GlobalPortfolioRiskManager | None = None,
        order_store: ExecutionOrderStore | None = None,
        require_persistent_store: bool = True,
    ) -> None:
        self.router = router
        self.health = health
        self.portfolio = portfolio
        self.risk = risk or GlobalPortfolioRiskManager()
        self.order_store = order_store or InMemoryExecutionOrderStore()
        self.require_persistent_store = require_persistent_store

    async def execute(self, market: MarketContext, request: OrderRequest, stop_loss: Decimal, risk_context: ExecutionRiskContext) -> ExchangeOrder:
        if market.account_id != request.account_id:
            raise PermissionError("Market context and order account do not match")
        if market.symbol != request.symbol:
            raise PermissionError("Market context and order symbol do not match")
        if market.market_type is not request.market_type:
            raise PermissionError("Market context and order market type do not match")
        adapter = self.router.adapter_for(market)
        if self.require_persistent_store and not self.order_store.persistent:
            raise PermissionError("Private execution requires a persistent idempotency store")
        account_key = (market.exchange, request.account_id)
        if not request.reduce_only:
            if risk_context.kill_switch_active:
                raise PermissionError("Emergency kill switch is active")
            if risk_context.allowed_symbols is not None and request.symbol not in risk_context.allowed_symbols:
                raise PermissionError("Symbol is not in the server-side allowlist")
            if account_key in risk_context.cooldown_accounts:
                raise PermissionError("Consecutive-loss cooldown is active")
            if risk_context.consecutive_losses_by_account.get(account_key, 0) >= risk_context.exchange_limits.max_consecutive_losses:
                raise PermissionError("Consecutive-loss protection is active")
        if market.ticker is None:
            raise PermissionError("Fresh exchange ticker is mandatory")
        market_age = Decimal(str((datetime.now(UTC) - market.ticker.timestamp).total_seconds()))
        if market_age < 0 or market_age > risk_context.max_market_data_age_seconds:
            raise PermissionError("STALE DATA: private order is forbidden")
        report = await self.health.check(adapter)
        reference_price = request.price or (market.ticker.last if market.ticker else None)
        if reference_price is None:
            raise ValueError("Execution needs an exchange-specific reference price")
        if not request.reduce_only:
            if request.side.value == "BUY" and stop_loss >= reference_price:
                raise PermissionError("LONG stop loss must be below entry")
            if request.side.value == "SELL" and stop_loss <= reference_price:
                raise PermissionError("SHORT stop loss must be above entry")
        proposal = ProposedExposure(
            market.exchange,
            request.account_id,
            request.symbol,
            request.side,
            reference_price * request.quantity,
            request.leverage,
            abs(reference_price - stop_loss) * request.quantity,
        )
        if not request.reduce_only:
            decision = self.risk.approve(
                proposal,
                self.portfolio.all(),
                risk_context.equity_by_account,
                risk_context.daily_pnl_by_account,
                report.status,
                risk_context.exchange_limits,
                risk_context.global_limits,
            )
            if not decision.approved:
                raise PermissionError(decision.reason)
            available = risk_context.available_by_account.get(account_key)
            if available is not None and proposal.notional / proposal.leverage > available:
                raise PermissionError("Insufficient available margin")

        self.order_store.claim(market.exchange, request)
        try:
            order = await adapter.create_order(request)
        except Exception as error:
            self.order_store.mark_unknown(market.exchange, request, type(error).__name__)
            raise OrderOutcomeUnknown(
                "Order call failed after durable claim; automatic retry is forbidden until reconciliation"
            ) from error
        self.order_store.mark_order(market.exchange, request, order)
        return order

    def register_position(self, position: ManagedPosition) -> None:
        self.portfolio.add(position)
