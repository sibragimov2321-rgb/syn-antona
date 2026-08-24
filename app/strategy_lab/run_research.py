import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.backtest.historical import BybitHistoricalDataProvider
from app.backtest.costs import BYBIT_SPOT_NON_VIP
from app.backtest.persistence import BacktestRepository, CachedHistoricalDataProvider, HistoricalCandleCache
from app.strategy_lab.framework import StrategyLabV2


async def run(years: int, output: Path) -> dict:
    provider = CachedHistoricalDataProvider(BybitHistoricalDataProvider(), HistoricalCandleCache())
    lab = StrategyLabV2(provider, BacktestRepository())
    now = datetime.now(UTC)
    end = datetime.fromtimestamp(int(now.timestamp()) // 300 * 300, UTC)
    start = end - timedelta(days=365 * years)
    assets = {}
    unavailable = {}
    for symbol in ("BTC/USDT", "ETH/USDT", "SOL/USDT"):
        print(f"Loading Phase 4C features for {symbol}...", flush=True)
        try:
            assets[symbol] = await lab.load_asset(symbol, start, end)
            print(f"{symbol}: {len(assets[symbol].base)} real closed 5m candles", flush=True)
        except Exception as error:
            unavailable[symbol] = str(error)
            print(f"{symbol}: unavailable ({error})", flush=True)
            if symbol in ("BTC/USDT", "ETH/USDT"):
                raise
    if len(assets) < 2:
        raise RuntimeError("Phase 4C requires at least BTC and ETH")
    print("Running frozen A/B/C validation, walk-forward, stress and one-time holdout...", flush=True)
    report = lab.run(assets)
    report["unavailable_assets"] = unavailable
    report["source"] = {"exchange": "bybit", "market_type": "spot", "real_candles": True, "maker_fee": str(BYBIT_SPOT_NON_VIP.maker_fee), "taker_fee": str(BYBIT_SPOT_NON_VIP.taker_fee), "slippage_assumption": str(BYBIT_SPOT_NON_VIP.slippage), "spread_assumption": str(BYBIT_SPOT_NON_VIP.spread), "fee_source": BYBIT_SPOT_NON_VIP.source, "note": "funding/open interest are intentionally absent for spot history; OHLCV has no historical order-book spread"}
    output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"output": str(output.resolve()), "selected_candidate": report["selected_candidate"], "final_status": report["final_status"], "assets": list(assets)}, indent=2), flush=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Phase 4C Strategy Lab V2")
    parser.add_argument("--years", type=int, default=2)
    parser.add_argument("--output", type=Path, default=Path("strategy-lab-v2-report.json"))
    arguments = parser.parse_args()
    asyncio.run(run(arguments.years, arguments.output))


if __name__ == "__main__":
    main()
