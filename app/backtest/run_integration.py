import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.backtest.historical import BinanceHistoricalDataProvider, BybitHistoricalDataProvider
from app.backtest.orchestrator import HistoricalBacktestOrchestrator, LOW_RISK
from app.backtest.persistence import BacktestRepository, CachedHistoricalDataProvider, HistoricalCandleCache


async def run(exchange: str, symbol: str, days: int, balance: Decimal) -> dict:
    raw = BybitHistoricalDataProvider() if exchange == "bybit" else BinanceHistoricalDataProvider()
    cached = CachedHistoricalDataProvider(raw, HistoricalCandleCache())
    orchestrator = HistoricalBacktestOrchestrator({exchange: cached}, BacktestRepository())
    now = datetime.now(UTC)
    end = datetime.fromtimestamp(int(now.timestamp()) // 300 * 300, UTC)
    report = await orchestrator.run(
        exchange, symbol, end - timedelta(days=days), end, balance, LOW_RISK, persist=True
    )
    return {
        "run_id": report.result.run_id,
        "exchange": exchange,
        "symbol": symbol,
        "started_at": report.started_at,
        "ended_at": report.ended_at,
        "candle_counts": report.candle_counts,
        "starting_balance": report.result.starting_balance,
        "final_equity": report.result.final_equity,
        "metrics": report.result.metrics,
        "walk_forward": {name: result.metrics for name, result in report.walk_forward.items()},
        "overfitting_warning": report.overfitting_warning,
        "benchmark_final_equity": report.benchmark_final_equity,
        "benchmark_return_pct": report.benchmark_return_pct,
        "monte_carlo": report.monte_carlo,
        "regimes": report.regimes,
        "signal_score_calibration": report.score_calibration,
        "validation_status": report.validation_status,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a real public-data Phase 4A integration backtest")
    parser.add_argument("--exchange", choices=("bybit", "binance"), default="bybit")
    parser.add_argument("--symbol", choices=("BTC/USDT", "ETH/USDT"), default="BTC/USDT")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--balance", type=Decimal, default=Decimal("1000"))
    arguments = parser.parse_args()
    print(json.dumps(asyncio.run(run(arguments.exchange, arguments.symbol, arguments.days, arguments.balance)), default=str, indent=2))


if __name__ == "__main__":
    main()
