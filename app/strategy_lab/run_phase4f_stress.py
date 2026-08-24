import argparse
import asyncio
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path

from app.backtest.historical import BinanceHistoricalDataProvider
from app.backtest.persistence import (
    BacktestRepository,
    CachedHistoricalDataProvider,
    HistoricalCandleCache,
)
from app.strategy_lab.phase4f import SplitBoundaries, frozen_hypotheses, summarize
from app.strategy_lab.phase4f_framework import Phase4FResearch, _serialize_results


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


async def run(protocol_path: Path, research_path: Path, output: Path) -> dict:
    protocol = _read_json(protocol_path)
    research_report = _read_json(research_path)
    protocol_hash = _hash(protocol_path)
    if research_report["protocol_hash"] != protocol_hash:
        raise RuntimeError("Research report does not match the immutable Phase 4F protocol")
    if research_report["protocol"]["final_holdout_opened"]:
        raise RuntimeError("Refusing a post-holdout exploratory stress run")

    boundaries = SplitBoundaries(
        **{
            key: datetime.fromisoformat(value)
            for key, value in protocol["boundaries"].items()
        }
    )
    hypotheses = {item.identifier: item for item in frozen_hypotheses()}
    candidates = [
        hypotheses[
            f"{record['hypothesis']['family']}:{record['hypothesis']['timeframe']}:v1"
        ]
        for record in research_report["walk_forward"]
    ]

    provider = CachedHistoricalDataProvider(
        BinanceHistoricalDataProvider(), HistoricalCandleCache()
    )
    repository = BacktestRepository()
    phase = Phase4FResearch(provider, repository)
    assets = {}
    for symbol in protocol["symbols"]:
        print(f"Loading cached Phase 4F data for {symbol}...", flush=True)
        assets[symbol] = await phase.load_asset(symbol, boundaries)

    normal = {
        f"{record['hypothesis']['family']}:{record['hypothesis']['timeframe']}:v1": {
            "summary": record["summary"],
            "assets": record["assets"],
        }
        for record in research_report["walk_forward"]
    }
    results = {}
    for hypothesis in candidates:
        print(f"Stress testing {hypothesis.identifier} on WALK_FORWARD...", flush=True)
        stress = {"normal": normal[hypothesis.identifier]}
        for label, multiple in (
            ("1.25x", Decimal("1.25")),
            ("1.5x", Decimal("1.5")),
            ("2x", Decimal("2")),
        ):
            evaluated = phase._evaluate_assets(
                assets, hypothesis, "walk_forward", multiple
            )
            stress[label] = {
                "summary": summarize(evaluated),
                "assets": _serialize_results(evaluated),
            }
            phase._record(
                hypothesis,
                "WALK_FORWARD_STRESS",
                label,
                evaluated,
            )
        cost_125 = stress["1.25x"]["summary"]
        results[hypothesis.identifier] = {
            "hypothesis": hypothesis.to_dict(),
            "stress": stress,
            "survives_1_25x": (
                cost_125["trades"] >= 20
                and cost_125["net_pf"] > 1
                and cost_125["expectancy"] > 0
                and cost_125["net_pnl"] > 0
            ),
        }

    report = {
        "phase": "4F_WALK_FORWARD_COST_STRESS",
        "protocol_sha256": protocol_hash,
        "source_research_sha256": _hash(research_path),
        "scope": "Frozen validation-confirmed hypotheses on WALK_FORWARD only",
        "parameter_changes": False,
        "final_holdout_accessed": False,
        "candidates": results,
        "live_trading_enabled": False,
        "real_orders_sent": False,
    }
    output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output.resolve()),
                "candidates": len(results),
                "final_holdout_accessed": False,
            },
            indent=2,
        ),
        flush=True,
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stress frozen Phase 4F candidates without opening FINAL HOLDOUT"
    )
    parser.add_argument(
        "--protocol-lock", type=Path, default=Path("phase4f-protocol-lock.json")
    )
    parser.add_argument(
        "--research-report", type=Path, default=Path("phase4f-new-edge-report.json")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("phase4f-walk-stress-report.json")
    )
    arguments = parser.parse_args()
    asyncio.run(run(arguments.protocol_lock, arguments.research_report, arguments.output))


if __name__ == "__main__":
    main()
