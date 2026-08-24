"""Repeat Phase 4C after an execution-engine audit without changing strategies."""

import argparse
import asyncio
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path

from app.backtest.costs import BYBIT_SPOT_NON_VIP
from app.backtest.historical import BybitHistoricalDataProvider
from app.backtest.persistence import BacktestRepository, CachedHistoricalDataProvider, HistoricalCandleCache
from app.strategy_lab.framework import StrategyLabV2


async def rerun(original_path: Path, output_path: Path) -> dict:
    original_bytes = original_path.read_bytes()
    original = json.loads(original_bytes)
    starts = {datetime.fromisoformat(details["start"]) for details in original["data"].values()}
    ends = {datetime.fromisoformat(details["end"]) + timedelta(minutes=5) for details in original["data"].values()}
    if len(starts) != 1 or len(ends) != 1:
        raise RuntimeError("Original Phase 4C assets do not share immutable boundaries")
    start, end = starts.pop(), ends.pop()
    provider = CachedHistoricalDataProvider(BybitHistoricalDataProvider(), HistoricalCandleCache())
    lab = StrategyLabV2(provider, BacktestRepository())
    assets = {}
    for symbol in original["data"]:
        print(f"Loading immutable audit rerun data for {symbol}...", flush=True)
        assets[symbol] = await lab.load_asset(symbol, start, end)
        if len(assets[symbol].base) != original["data"][symbol]["candles_5m"]:
            raise RuntimeError(f"Immutable candle count changed for {symbol}")
    report = lab.run(assets)
    report["audit_rerun"] = {
        "reason": "Phase 4D execution accounting correction",
        "strategy_parameters_changed": False,
        "input_report_sha256": sha256(original_bytes).hexdigest(),
        "immutable_start": start,
        "immutable_end_exclusive": end,
    }
    report["source"] = {
        "exchange": BYBIT_SPOT_NON_VIP.exchange,
        "market_type": BYBIT_SPOT_NON_VIP.market_type,
        "real_candles": True,
        "maker_fee": BYBIT_SPOT_NON_VIP.maker_fee,
        "taker_fee": BYBIT_SPOT_NON_VIP.taker_fee,
        "slippage_assumption": BYBIT_SPOT_NON_VIP.slippage,
        "spread_assumption": BYBIT_SPOT_NON_VIP.spread,
        "fee_source": BYBIT_SPOT_NON_VIP.source,
        "note": "spot funding=0; OHLCV has no historical order-book spread",
    }
    output_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--original", type=Path, default=Path("strategy-lab-v2-report.json"))
    parser.add_argument("--output", type=Path, default=Path("strategy-lab-v2-audit-rerun-report.json"))
    arguments = parser.parse_args()
    report = asyncio.run(rerun(arguments.original, arguments.output))
    print(json.dumps({"output": str(arguments.output.resolve()), "selected": report["selected_candidate"], "status": report["final_status"]}, indent=2), flush=True)


if __name__ == "__main__":
    main()
