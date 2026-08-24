import argparse
import asyncio
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path

from app.backtest.historical import BinanceHistoricalDataProvider
from app.backtest.persistence import BacktestRepository, CachedHistoricalDataProvider, HistoricalCandleCache
from app.strategy_lab.phase4f import BINANCE_PROFILE, SplitBoundaries, frozen_hypotheses, make_boundaries
from app.strategy_lab.phase4f_framework import Phase4FResearch


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


async def run(days: int, protocol_path: Path, output: Path) -> dict:
    now = datetime.now(UTC)
    end = datetime.fromtimestamp(int(now.timestamp()) // 300 * 300, UTC)
    start = end - timedelta(days=days)
    boundaries = make_boundaries(start, end)
    protocol = {
        "phase": "4F",
        "locked_before_data_evaluation": True,
        "created_at": now,
        "exchange": "binance",
        "market_type": "spot",
        "symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
        "timeframes": ["5m", "15m", "1h", "4h"],
        "boundaries": asdict(boundaries),
        "hypotheses": [item.to_dict() for item in frozen_hypotheses()],
        "cost_profile": asdict(BINANCE_PROFILE),
        "minimum_signal_score": 75,
        "cost_safety_margin": "0.50x estimated round trip cost",
        "market_impact_per_leg": "0.00005",
        "experiment_count": len(frozen_hypotheses()),
        "multiple_testing": "Bonferroni plus deflated Sharpe",
        "funding_basis": "skip unless synchronized derivatives history exists",
        "live_trading_enabled": False,
    }
    if protocol_path.exists():
        locked = json.loads(protocol_path.read_bytes())
        static_keys = ("exchange", "market_type", "symbols", "timeframes", "hypotheses", "cost_profile", "minimum_signal_score", "cost_safety_margin", "market_impact_per_leg", "experiment_count", "multiple_testing", "funding_basis", "live_trading_enabled")
        if any(_canonical({key: locked[key]}) != _canonical({key: protocol[key]}) for key in static_keys):
            raise RuntimeError("Existing Phase 4F protocol lock differs; refusing to change the search space")
        protocol = locked
        boundaries = SplitBoundaries(**{key: datetime.fromisoformat(value) for key, value in protocol["boundaries"].items()})
    else:
        protocol_path.write_text(json.dumps(protocol, indent=2, default=str), encoding="utf-8")
    protocol_hash = sha256(protocol_path.read_bytes()).hexdigest()

    provider = CachedHistoricalDataProvider(BinanceHistoricalDataProvider(), HistoricalCandleCache())
    research = Phase4FResearch(provider, BacktestRepository())
    assets = {}
    for symbol in protocol["symbols"]:
        print(f"Loading new Binance Phase 4F dataset for {symbol}...", flush=True)
        assets[symbol] = await research.load_asset(symbol, boundaries)
        print(f"{symbol}: " + ", ".join(f"{timeframe}={len(candles)}" for timeframe, candles in assets[symbol].candles.items()), flush=True)
    print("Running locked TRAIN ranking, VALIDATION, WALK-FORWARD and gated FINAL HOLDOUT...", flush=True)
    report = research.run(assets, boundaries, protocol_hash)
    report["data"] = {
        symbol: {
            timeframe: {"candles": len(candles), "start": candles[0].timestamp, "end": candles[-1].timestamp}
            for timeframe, candles in asset.candles.items()
        }
        for symbol, asset in assets.items()
    }
    report["reproducibility"] = {
        "protocol_lock": str(protocol_path.resolve()),
        "protocol_sha256": protocol_hash,
        "report_generated_at": datetime.now(UTC),
        "provider": "BinanceHistoricalDataProvider",
        "real_historical_candles": True,
    }
    output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"output": str(output.resolve()), "status": report["final_status"], "selected": report["selected_candidate"], "holdout_opened": report["protocol"]["final_holdout_opened"], "live_trading_enabled": False}, indent=2, default=str), flush=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Run locked Phase 4F new-edge discovery")
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--protocol-lock", type=Path, default=Path("phase4f-protocol-lock.json"))
    parser.add_argument("--output", type=Path, default=Path("phase4f-new-edge-report.json"))
    arguments = parser.parse_args()
    asyncio.run(run(arguments.days, arguments.protocol_lock, arguments.output))


if __name__ == "__main__":
    main()
