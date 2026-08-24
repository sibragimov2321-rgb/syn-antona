import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path

from app.backtest.costs import PHASE4E_SPOT_PROFILES
from app.backtest.historical import (
    BinanceHistoricalDataProvider,
    BybitHistoricalDataProvider,
    CCXTHistoricalDataProvider,
)
from app.backtest.persistence import (
    BacktestRepository,
    CachedHistoricalDataProvider,
    HistoricalCandleCache,
)
from app.strategy_lab.phase4e import WARMUP_4E, build_features
from app.strategy_lab.phase4f import evaluate_hypothesis
from app.strategy_lab.phase4g import (
    CONFIRMATION_ASSETS,
    CONTROL_ASSETS,
    COST_MULTIPLES,
    EXCHANGES,
    FROZEN_CONFIG,
    FROZEN_CONFIG_HASH,
    FROZEN_VERSION,
    TaggedResult,
    aggregate,
    canonical_json,
    frozen_hypothesis,
    leave_one_asset_out,
    leave_one_exchange_out,
    period_attribution,
    regime_attribution,
    result_metrics,
    stability_diagnostic,
    statistical_validation,
    verify_frozen_implementation,
)


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _provider(exchange: str):
    if exchange == "binance":
        return BinanceHistoricalDataProvider()
    if exchange == "bybit":
        return BybitHistoricalDataProvider()
    return CCXTHistoricalDataProvider(exchange)


def _period_boundaries(phase4f_protocol: dict) -> tuple[datetime, datetime, datetime]:
    boundaries = phase4f_protocol["boundaries"]
    start = datetime.fromisoformat(boundaries["start"])
    end = datetime.fromisoformat(boundaries["walk_forward_end"])
    forbidden_holdout = datetime.fromisoformat(boundaries["holdout_start"])
    if end >= forbidden_holdout:
        raise RuntimeError("Phase 4G confirmation range overlaps Phase 4F FINAL HOLDOUT")
    return start, end, forbidden_holdout


def _build_protocol(phase4f_protocol_path: Path) -> dict:
    phase4f_protocol = _read(phase4f_protocol_path)
    start, end, holdout = _period_boundaries(phase4f_protocol)
    return {
        "phase": "4G_INDEPENDENT_CONFIRMATION",
        "locked_before_confirmation_data_evaluation": True,
        "strategy_version": FROZEN_VERSION,
        "strategy_config_hash": FROZEN_CONFIG_HASH,
        "strategy_config": FROZEN_CONFIG,
        "exchange_market_type": "spot",
        "exchanges": list(EXCHANGES),
        "assets": list(CONTROL_ASSETS + CONFIRMATION_ASSETS),
        "timeframe": "1h",
        "confirmation_start": start,
        "confirmation_end": end,
        "phase4f_final_holdout_start": holdout,
        "phase4f_final_holdout_access": "FORBIDDEN",
        "minimum_history_days": 365,
        "minimum_candles": 8760,
        "warmup_candles": WARMUP_4E,
        "cost_multiples": {key: value for key, value in COST_MULTIPLES.items()},
        "cost_profiles": {
            exchange: asdict(PHASE4E_SPOT_PROFILES[exchange])
            for exchange in EXCHANGES
        },
        "execution_assumptions": {
            "fees": "official non-VIP/base spot rates, no token discounts",
            "spread_slippage": (
                "pre-existing conservative venue profiles; OHLCV has no historical order book"
            ),
            "market_impact_per_leg": "0.00005 inherited unchanged from Phase 4F",
            "funding": "0 because dataset market type is spot",
            "short_model": (
                "unchanged Phase 4F synthetic short accounting on spot OHLCV; no borrow model"
            ),
        },
        "official_fee_sources": {
            exchange: PHASE4E_SPOT_PROFILES[exchange].source for exchange in EXCHANGES
        },
        "statistical_protocol": {
            "bootstrap_simulations": 2000,
            "bootstrap_seed": 42,
            "bootstrap_unit": "exchange-asset cluster",
            "inherited_multiple_tests": 36,
            "adequate_sample": (
                "trades>=200, exchange-asset clusters>=20, active quarters>=6"
            ),
        },
        "decision_gate": {
            "paper": (
                "aggregate PF>1, expectancy>0, net PnL>0; >=3 positive assets; "
                ">=3 positive exchanges; quarter and half-year stability; no asset/exchange "
                "concentration; max DD<20%; survives 1.25x; CI lower>0; adjusted p<0.10; "
                "deflated Sharpe>0; adequate sample"
            ),
            "research": (
                "aggregate PF>1, expectancy>0, net PnL>0; >=2 positive assets; "
                ">=2 positive exchanges; survives 1.25x; adequate sample"
            ),
        },
        "parameter_changes_after_results": False,
        "live_trading_enabled": False,
        "real_orders_allowed": False,
    }


def _lock_protocol(path: Path, protocol: dict) -> dict:
    if path.exists():
        locked = _read(path)
        if canonical_json(locked) != canonical_json(protocol):
            raise RuntimeError(
                "Existing Phase 4G protocol differs; refusing to change frozen validation"
            )
        return locked
    path.write_text(json.dumps(protocol, indent=2, default=str), encoding="utf-8")
    return protocol


def _ranking(
    results: dict[tuple[str, str, str], TaggedResult], unavailable: list[dict]
) -> list[dict]:
    rows = []
    for exchange in EXCHANGES:
        for symbol in CONTROL_ASSETS + CONFIRMATION_ASSETS:
            normal = results.get((exchange, symbol, "normal"))
            stressed = results.get((exchange, symbol, "1.5x"))
            if normal is None:
                reason = next(
                    (
                        item["reason"]
                        for item in unavailable
                        if item["exchange"] == exchange and item["symbol"] == symbol
                    ),
                    "not evaluated",
                )
                rows.append(
                    {
                        "asset": symbol,
                        "exchange": exchange,
                        "status": "INSUFFICIENT DATA",
                        "reason": reason,
                        "config_hash": FROZEN_CONFIG_HASH,
                    }
                )
                continue
            metrics = result_metrics(normal.result)
            stress_metrics = result_metrics(stressed.result)
            status = (
                "INSUFFICIENT SAMPLE"
                if metrics["trades"] < 20
                else "POSITIVE DIAGNOSTIC"
                if metrics["net_pf"] > 1
                and metrics["expectancy"] > 0
                and metrics["net_pnl"] > 0
                else "NEGATIVE"
            )
            rows.append(
                {
                    "asset": symbol,
                    "exchange": exchange,
                    **metrics,
                    "1.5x_cost_pf": stress_metrics["net_pf"],
                    "1.5x_cost_net_pnl": stress_metrics["net_pnl"],
                    "status": status,
                    "config_hash": FROZEN_CONFIG_HASH,
                }
            )
    return rows


def _exchange_table(normal: list[TaggedResult]) -> list[dict]:
    rows = []
    for exchange in EXCHANGES:
        selected = [item for item in normal if item.exchange == exchange]
        asset_groups = {
            symbol: aggregate([item for item in selected if item.symbol == symbol])
            for symbol in sorted({item.symbol for item in selected})
        }
        summary = aggregate(selected)
        rows.append(
            {
                "exchange": exchange,
                "assets_tested": len(asset_groups),
                "positive_assets": sum(
                    values["net_pnl"] > 0 and values["net_pf"] > 1
                    for values in asset_groups.values()
                ),
                **summary,
                "config_hash": FROZEN_CONFIG_HASH,
            }
        )
    return rows


def _asset_table(normal: list[TaggedResult]) -> list[dict]:
    rows = []
    for symbol in CONTROL_ASSETS + CONFIRMATION_ASSETS:
        selected = [item for item in normal if item.symbol == symbol]
        if selected:
            rows.append(
                {
                    "asset": symbol,
                    "exchanges_tested": len(selected),
                    **aggregate(selected),
                    "config_hash": FROZEN_CONFIG_HASH,
                }
            )
    return rows


def _decision(
    normal: list[TaggedResult],
    stressed_125: list[TaggedResult],
    quarters: dict,
    half_years: dict,
    asset_leaveout: dict,
    exchange_leaveout: dict,
    statistics: dict,
) -> tuple[str, dict]:
    overall = aggregate(normal)
    cost_125 = aggregate(stressed_125)
    asset_table = _asset_table(normal)
    exchange_table = _exchange_table(normal)
    positive_assets = sum(
        row["net_pnl"] > 0 and row["net_pf"] > 1 for row in asset_table
    )
    positive_exchanges = sum(
        row["net_pnl"] > 0 and row["net_pf"] > 1 for row in exchange_table
    )
    quarter_stability = stability_diagnostic(quarters)
    half_stability = stability_diagnostic(half_years)
    economics = (
        overall["net_pf"] > 1
        and overall["expectancy"] > 0
        and overall["net_pnl"] > 0
    )
    stress_survives = cost_125["net_pf"] > 1 and cost_125["net_pnl"] > 0
    adequate = statistics["trade_count_adequacy"] == "ADEQUATE"
    research = (
        economics
        and positive_assets >= 2
        and positive_exchanges >= 2
        and stress_survives
        and adequate
    )
    paper = (
        research
        and positive_assets >= 3
        and positive_exchanges >= 3
        and quarter_stability["stable"]
        and half_stability["stable"]
        and asset_leaveout["diagnosis"] != "CONCENTRATED EDGE"
        and exchange_leaveout["diagnosis"] != "EXCHANGE-SPECIFIC EDGE"
        and overall["max_drawdown_pct"] < 20
        and statistics["expectancy_ci_95"][0] > 0
        and statistics["multiple_testing_adjusted_p"] < 0.10
        and statistics["deflated_sharpe"] > 0
    )
    status = (
        "CANDIDATE FOR PAPER TRADING"
        if paper
        else "RESEARCH CANDIDATE"
        if research
        else "NO ROBUST EDGE FOUND"
    )
    return status, {
        "aggregate_economics_positive": economics,
        "positive_assets": positive_assets,
        "positive_exchanges": positive_exchanges,
        "survives_1.25x_costs": stress_survives,
        "quarter_stability": quarter_stability,
        "half_year_stability": half_stability,
        "asset_concentration": asset_leaveout["diagnosis"],
        "exchange_concentration": exchange_leaveout["diagnosis"],
        "trade_count_adequacy": statistics["trade_count_adequacy"],
        "paper_gate_passed": paper,
        "research_gate_passed": research,
    }


async def run(
    phase4f_protocol_path: Path, protocol_path: Path, output: Path
) -> dict:
    verify_frozen_implementation()
    protocol = _lock_protocol(
        protocol_path, _build_protocol(phase4f_protocol_path)
    )
    protocol_hash = _hash(protocol_path)
    start = datetime.fromisoformat(str(protocol["confirmation_start"]))
    end = datetime.fromisoformat(str(protocol["confirmation_end"]))
    forbidden = datetime.fromisoformat(str(protocol["phase4f_final_holdout_start"]))
    if end >= forbidden:
        raise RuntimeError("Refusing to open or overlap Phase 4F FINAL HOLDOUT")

    repository = BacktestRepository()
    repository.register_strategy(FROZEN_VERSION, FROZEN_CONFIG)
    hypothesis = frozen_hypothesis()
    results: dict[tuple[str, str, str], TaggedResult] = {}
    unavailable = []
    data = []
    clients = []
    try:
        for exchange in EXCHANGES:
            raw_provider = _provider(exchange)
            clients.append(raw_provider)
            provider = CachedHistoricalDataProvider(
                raw_provider, HistoricalCandleCache()
            )
            for symbol in CONTROL_ASSETS + CONFIRMATION_ASSETS:
                print(f"{exchange.upper()} {symbol}: loading real 1h candles...", flush=True)
                try:
                    history = await provider.fetch(
                        symbol,
                        "1h",
                        start - timedelta(hours=WARMUP_4E),
                        end,
                    )
                    selected = [
                        candle for candle in history if start <= candle.timestamp < end
                    ]
                    history_days = (
                        (selected[-1].timestamp - selected[0].timestamp).days
                        if len(selected) > 1
                        else 0
                    )
                    if len(selected) < protocol["minimum_candles"] or history_days < protocol[
                        "minimum_history_days"
                    ]:
                        raise ValueError(
                            f"insufficient history: candles={len(selected)}, days={history_days}"
                        )
                    features = build_features(history, start, end)
                    data.append(
                        {
                            "exchange": exchange,
                            "symbol": symbol,
                            "candles": len(selected),
                            "start": selected[0].timestamp,
                            "end": selected[-1].timestamp,
                            "provider": raw_provider.__class__.__name__,
                        }
                    )
                    for scenario, multiple in COST_MULTIPLES.items():
                        result = evaluate_hypothesis(
                            symbol,
                            hypothesis,
                            selected,
                            features,
                            PHASE4E_SPOT_PROFILES[exchange],
                            multiple,
                        )
                        tagged = TaggedResult(exchange, symbol, scenario, result)
                        results[(exchange, symbol, scenario)] = tagged
                        metrics = {
                            **result_metrics(result),
                            "exchange": exchange,
                            "symbol": symbol,
                            "config_hash": FROZEN_CONFIG_HASH,
                            "protocol_hash": protocol_hash,
                            "data_start": selected[0].timestamp,
                            "data_end": selected[-1].timestamp,
                        }
                        repository.save_experiment(
                            FROZEN_VERSION,
                            symbol,
                            "PHASE4G_CONFIRMATION",
                            f"{exchange}:{scenario}",
                            metrics,
                        )
                except Exception as error:
                    reason = f"{type(error).__name__}: {error}"
                    print(f"{exchange.upper()} {symbol}: SKIPPED — {reason}", flush=True)
                    unavailable.append(
                        {
                            "exchange": exchange,
                            "symbol": symbol,
                            "reason": reason,
                            "config_hash": FROZEN_CONFIG_HASH,
                        }
                    )
                    repository.save_experiment(
                        FROZEN_VERSION,
                        symbol,
                        "PHASE4G_CONFIRMATION",
                        f"{exchange}:not_run",
                        {
                            "status": "INSUFFICIENT DATA",
                            "reason": reason,
                            "exchange": exchange,
                            "config_hash": FROZEN_CONFIG_HASH,
                            "protocol_hash": protocol_hash,
                        },
                    )
    finally:
        for client in clients:
            close = getattr(client, "close", None)
            if close:
                await close()

    by_cost = {
        scenario: [
            item for (exchange, symbol, label), item in results.items() if label == scenario
        ]
        for scenario in COST_MULTIPLES
    }
    normal = by_cost["normal"]
    if not normal:
        raise RuntimeError("No sufficient Phase 4G datasets were available")
    quarters = period_attribution(normal)
    half_years = period_attribution(normal, half_year=True)
    asset_leaveout = leave_one_asset_out(normal)
    exchange_leaveout = leave_one_exchange_out(normal)
    statistics = statistical_validation(normal)
    final_status, decision = _decision(
        normal,
        by_cost["1.25x"],
        quarters,
        half_years,
        asset_leaveout,
        exchange_leaveout,
        statistics,
    )
    report = {
        "phase": "4G_INDEPENDENT_CONFIRMATION",
        "strategy_version": FROZEN_VERSION,
        "strategy_config_hash": FROZEN_CONFIG_HASH,
        "protocol_hash": protocol_hash,
        "protocol": protocol,
        "old_phase4f_final_holdout": {
            "opened": False,
            "loaded": False,
            "confirmation_end": end,
            "holdout_start": forbidden,
        },
        "data": data,
        "unavailable": unavailable,
        "ranking": _ranking(results, unavailable),
        "aggregated_by_exchange": _exchange_table(normal),
        "aggregated_by_asset": _asset_table(normal),
        "aggregate_cost_stress": {
            scenario: aggregate(tagged) for scenario, tagged in by_cost.items()
        },
        "market_regimes": regime_attribution(normal),
        "quarters": quarters,
        "half_years": half_years,
        "leave_one_asset_out": asset_leaveout,
        "leave_one_exchange_out": exchange_leaveout,
        "statistical_validation": statistics,
        "decision_gate": decision,
        "final_status": final_status,
        "methodology_notes": {
            "group_drawdown": (
                "realized trade-close drawdown on equal $1,000 allocation per exchange-asset"
            ),
            "venue_evidence": "each exchange uses its own real historical OHLCV",
            "parameter_changes": False,
        },
        "reproducibility": {
            "phase4f_protocol": str(phase4f_protocol_path.resolve()),
            "phase4f_protocol_sha256": _hash(phase4f_protocol_path),
            "phase4g_protocol": str(protocol_path.resolve()),
            "phase4g_protocol_sha256": protocol_hash,
        },
        "live_trading_enabled": False,
        "real_orders_sent": False,
    }
    output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output.resolve()),
                "status": final_status,
                "datasets": len(data),
                "unavailable": len(unavailable),
                "old_holdout_opened": False,
                "live_trading_enabled": False,
            },
            indent=2,
        ),
        flush=True,
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run frozen Phase 4G independent confirmation"
    )
    parser.add_argument(
        "--phase4f-protocol", type=Path, default=Path("phase4f-protocol-lock.json")
    )
    parser.add_argument(
        "--protocol-lock", type=Path, default=Path("phase4g-protocol-lock.json")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("phase4g-confirmation-report.json")
    )
    arguments = parser.parse_args()
    asyncio.run(
        run(arguments.phase4f_protocol, arguments.protocol_lock, arguments.output)
    )


if __name__ == "__main__":
    main()
