from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class ExecutionRiskContext:
    equity_by_account: dict[tuple[str, str], Decimal]
    daily_pnl_by_account: dict[tuple[str, str], Decimal]
    exchange_limits: ExchangeRiskLimits = field(default_factory=ExchangeRiskLimits)
    global_limits: GlobalRiskLimits = field(default_factory=GlobalRiskLimits)


class MultiExchangeTradingEngine:
    """Single execution engine; exchange differences stay behind ExchangeAdapter."""

    def __init__(self, router: MarketDataRouter, health: ExchangeHealthMonitor, portfolio: PortfolioManager, risk: GlobalPortfolioRiskManager | None = None) -> None:
        self.router = router
        self.health = health
        self.portfolio = portfolio
        self.risk = risk or GlobalPortfolioRiskManager()
        self._client_order_ids: set[str] = set()

    async def execute(self, market: MarketContext, request: OrderRequest, stop_loss: Decimal, risk_context: ExecutionRiskContext) -> ExchangeOrder:
        if market.account_id != request.account_id:
            raise PermissionError("Market context and order account do not match")
        if market.symbol != request.symbol:
            raise PermissionError("Market context and order symbol do not match")
        if market.market_type is not request.market_type:
            raise PermissionError("Market context and order market type do not match")
        if request.client_order_id and request.client_order_id in self._client_order_ids:
            raise PermissionError("Duplicate client order id")
        adapter = self.router.adapter_for(market)
        report = await self.health.check(adapter)
        reference_price = request.price or (market.ticker.last if market.ticker else None)
        if reference_price is None:
            raise ValueError("Execution needs an exchange-specific reference price")
        proposal = ProposedExposure(
            market.exchange,
            request.account_id,
            request.symbol,
            request.side,
            reference_price * request.quantity,
            request.leverage,
            abs(reference_price - stop_loss) * request.quantity,
        )
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
        order = await adapter.create_order(request)
        if request.client_order_id:
            self._client_order_ids.add(request.client_order_id)
        return order

    def register_position(self, position: ManagedPosition) -> None:
        self.portfolio.add(position)
