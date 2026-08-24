from dataclasses import dataclass
from decimal import Decimal

from app.backtest.core import BacktestEngine, Candle
from app.domain.models import RiskProfile
from app.exchanges.models import FeeSchedule


@dataclass(frozen=True)
class ExchangeBacktestProfile:
    exchange: str
    fees: FeeSchedule
    slippage: Decimal


@dataclass(frozen=True)
class ExchangeComparisonResult:
    exchange: str
    strategy_version: str
    metrics: dict


class ExchangeComparisonBacktest:
    """Runs one immutable strategy factory independently on each venue's candles."""

    def run(self, strategy_version: str, candles_by_exchange: dict[str, list[Candle]], profiles: dict[str, ExchangeBacktestProfile], strategy_factory, starting_balance: Decimal = Decimal("1000"), risk_profile: RiskProfile | None = None) -> dict[str, ExchangeComparisonResult]:
        if set(candles_by_exchange) != set(profiles):
            raise ValueError("Every exchange dataset requires exactly one cost profile")
        results = {}
        for exchange, candles in candles_by_exchange.items():
            profile = profiles[exchange]
            engine = BacktestEngine(fee_rate=profile.fees.taker, slippage=profile.slippage)
            result = engine.run(candles, strategy_factory(exchange), starting_balance, risk_profile=risk_profile)
            results[exchange] = ExchangeComparisonResult(exchange, strategy_version, result.metrics)
        return results

    @staticmethod
    def table(results: dict[str, ExchangeComparisonResult]) -> list[dict]:
        return [
            {
                "exchange": result.exchange,
                "strategy_version": result.strategy_version,
                "pnl": result.metrics["net_pnl"],
                "fees": result.metrics["total_fees"],
                "slippage": result.metrics["slippage_cost"],
                "profit_factor": result.metrics["profit_factor"],
                "drawdown_pct": result.metrics["max_drawdown_pct"],
                "trades": result.metrics["total_trades"],
            }
            for result in results.values()
        ]
