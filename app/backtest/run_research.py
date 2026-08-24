import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.backtest.historical import BybitHistoricalDataProvider
from app.backtest.persistence import BacktestRepository, CachedHistoricalDataProvider, HistoricalCandleCache
from app.backtest.research_framework import StrategyResearchFramework


async def run(years: int, output: Path) -> dict:
    provider=CachedHistoricalDataProvider(BybitHistoricalDataProvider(),HistoricalCandleCache())
    framework=StrategyResearchFramework(provider,BacktestRepository())
    now=datetime.now(UTC); end=datetime.fromtimestamp(int(now.timestamp())//300*300,UTC)
    start=end-timedelta(days=365*years)
    assets={}
    for symbol in ("BTC/USDT","ETH/USDT"):
        print(f"Loading and building features for {symbol}...",flush=True)
        assets[symbol]=await framework.load_asset(symbol,start,end)
        print(f"{symbol}: {len(assets[symbol].base)} closed 5m candles",flush=True)
    print("Running immutable candidate research; holdout remains locked until selection...",flush=True)
    report=framework.run(assets,output)
    print(json.dumps({"output":str(output.resolve()),"selected_version":report["selected_version"],"final_status":report["final_status"],"holdout_opened_once":report["holdout_opened_once"]},indent=2),flush=True)
    return report


def main() -> None:
    parser=argparse.ArgumentParser(description="Run chronological BTC/ETH strategy research")
    parser.add_argument("--years",type=int,default=2)
    parser.add_argument("--output",type=Path,default=Path("strategy-research-report.json"))
    arguments=parser.parse_args()
    asyncio.run(run(arguments.years,arguments.output))


if __name__=="__main__": main()
