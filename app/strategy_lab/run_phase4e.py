import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path

from app.backtest.costs import PHASE4E_SPOT_PROFILES
from app.backtest.historical import BybitHistoricalDataProvider
from app.backtest.persistence import CachedHistoricalDataProvider, HistoricalCandleCache
from app.strategy_lab.framework import StrategyLabV2
from app.strategy_lab.phase4e_framework import Phase4EResearch


async def run(boundaries_path: Path, output: Path) -> dict:
    boundary_report = json.loads(boundaries_path.read_bytes())
    starts = {datetime.fromisoformat(details["start"]) for details in boundary_report["data"].values()}
    ends = {datetime.fromisoformat(details["end"]) + timedelta(minutes=5) for details in boundary_report["data"].values()}
    if len(starts) != 1 or len(ends) != 1:
        raise RuntimeError("Phase 4E requires identical immutable asset boundaries")
    start, end = starts.pop(), ends.pop()
    provider = CachedHistoricalDataProvider(BybitHistoricalDataProvider(), HistoricalCandleCache())
    baseline_lab = StrategyLabV2(provider)
    research = Phase4EResearch(provider)
    assets = {}
    baseline_assets = {}
    for symbol in ("BTC/USDT", "ETH/USDT", "SOL/USDT"):
        print(f"Loading immutable Phase 4E data for {symbol}...", flush=True)
        baseline_assets[symbol] = await baseline_lab.load_asset(symbol, start, end)
        assets[symbol] = await research.load_asset(symbol, start, end)
        print(
            f"{symbol}: " + ", ".join(f"{timeframe}={len(candles)}" for timeframe, candles in assets[symbol].candles.items()),
            flush=True,
        )
    print("Running TRAIN-only candidate ranking and attribution...", flush=True)
    report = research.run(assets, baseline_assets)
    report["phase"] = "4E_COST_AWARE_EDGE_RESEARCH"
    report["immutable_boundaries"] = {"start": start, "end_exclusive": end, "source_report": str(boundaries_path.resolve()), "source_report_sha256": sha256(boundaries_path.read_bytes()).hexdigest()}
    report["data"] = {
        symbol: {
            timeframe: {"candles": len(candles), "start": candles[0].timestamp, "end": candles[-1].timestamp}
            for timeframe, candles in asset.candles.items()
        }
        for symbol, asset in assets.items()
    }
    report["cost_profiles"] = {name: asdict(profile) for name, profile in PHASE4E_SPOT_PROFILES.items()}
    output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"output": str(output.resolve()), "selected_candidate": report["selected_candidate"], "final_status": report["final_status"], "live_trading_enabled": report["live_trading_enabled"]}, indent=2, default=str), flush=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Phase 4E cost-aware Mean Reversion research")
    parser.add_argument("--boundaries", type=Path, default=Path("strategy-lab-v2-audit-rerun-report.json"))
    parser.add_argument("--output", type=Path, default=Path("phase4e-cost-aware-report.json"))
    arguments = parser.parse_args()
    asyncio.run(run(arguments.boundaries, arguments.output))


if __name__ == "__main__":
    main()
