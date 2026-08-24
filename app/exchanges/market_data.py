from decimal import Decimal

from app.exchanges.base import ExchangeAdapter, ExchangeError
from app.exchanges.models import (
    HealthStatus,
    MarketContext,
    MarketType,
    SelectionMode,
    VenueQuote,
)


class MarketDataRouter:
    """Binds every analyzed market context to its exact exchange/account venue."""

    def __init__(self) -> None:
        self._adapters: dict[tuple[str, str], ExchangeAdapter] = {}

    def register(self, account_id: str, adapter: ExchangeAdapter) -> None:
        key = (adapter.name, account_id)
        if key in self._adapters:
            raise ValueError(f"Adapter already registered for {adapter.name}/{account_id}")
        self._adapters[key] = adapter

    def unregister(self, exchange: str, account_id: str) -> None:
        self._adapters.pop((exchange, account_id), None)

    def adapter_for(self, context: MarketContext) -> ExchangeAdapter:
        if context.account_id is None:
            raise ExchangeError("Execution context requires account_id")
        try:
            return self._adapters[(context.exchange, context.account_id)]
        except KeyError as error:
            raise ExchangeError("No adapter registered for market context") from error

    async def get_context(self, exchange: str, account_id: str, symbol: str, market_type: MarketType = MarketType.PERPETUAL) -> MarketContext:
        try:
            adapter = self._adapters[(exchange, account_id)]
        except KeyError as error:
            raise ExchangeError(f"Unknown exchange account {exchange}/{account_id}") from error
        ticker = await adapter.get_ticker(symbol, market_type)
        orderbook = await adapter.get_orderbook(symbol, market_type)
        funding = await adapter.get_funding_rate(symbol) if adapter.capabilities.funding else None
        open_interest = await adapter.get_open_interest(symbol) if adapter.capabilities.open_interest else None
        return MarketContext(exchange, account_id, adapter.normalize_symbol(symbol), market_type, ticker, orderbook, funding, open_interest)

    async def quotes(self, accounts: list[tuple[str, str]], symbol: str, market_type: MarketType = MarketType.PERPETUAL) -> list[MarketContext]:
        contexts = []
        for exchange, account_id in accounts:
            contexts.append(await self.get_context(exchange, account_id, symbol, market_type))
        return contexts


class ExchangeSelector:
    def select(self, mode: SelectionMode, quotes: list[VenueQuote], selected_exchange: str | None = None) -> list[VenueQuote]:
        healthy = [quote for quote in quotes if quote.health.status is not HealthStatus.UNAVAILABLE]
        if mode is SelectionMode.SINGLE_EXCHANGE:
            selected = [quote for quote in healthy if quote.exchange == selected_exchange]
            if len(selected) != 1:
                raise ExchangeError("Selected exchange is unavailable or ambiguous")
            return selected
        if mode is SelectionMode.MULTI_EXCHANGE:
            return healthy
        if not healthy:
            raise ExchangeError("No healthy exchange available for execution")
        return [min(healthy, key=self.execution_cost_score)]

    @staticmethod
    def execution_cost_score(quote: VenueQuote) -> Decimal:
        liquidity_penalty = Decimal("1") / quote.available_liquidity if quote.available_liquidity > 0 else Decimal("1000")
        latency_penalty = quote.health.latency_ms / Decimal("1000000")
        degraded_penalty = Decimal("0.002") if quote.health.status is HealthStatus.DEGRADED else Decimal()
        return quote.ticker.spread_pct + quote.estimated_slippage_pct + quote.fees.taker + abs(quote.funding_rate) + liquidity_penalty + latency_penalty + degraded_penalty
