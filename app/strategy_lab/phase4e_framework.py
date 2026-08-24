from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

from app.backtest.costs import PHASE4E_SPOT_PROFILES
from app.backtest.persistence import TIMEFRAME_SECONDS
from app.strategy_lab.execution import ResearchVariant
from app.strategy_lab.framework import StrategyLabV2
from app.strategy_lab.phase4e import (
    DEFAULT_PROFILE,
    TIMEFRAMES_4E,
    WARMUP_4E,
    CostAwareConfig,
    ExecutionMode,
    Phase4EAsset,
    aggregate,
    attribution,
    build_features,
    evaluate,
    frozen_candidate_grid,
    persistent_negative_groups,
    result_table_row,
    scaled_costs,
    selection_score,
    validation_pass,
)
from app.strategy_lab.strategies import MeanReversionStrategy


class FinalHoldout4E:
    def __init__(self) -> None:
        self.candidate: str | None = None
        self.opened = False

    def lock(self, candidate: str) -> None:
        if self.candidate is not None:
            raise RuntimeError("Phase 4E final candidate already locked")
        self.candidate = candidate

    def open_once(self, candidate: str) -> None:
        if candidate != self.candidate or self.opened:
            raise RuntimeError("Phase 4E FINAL HOLDOUT access denied")
        self.opened = True


class Phase4EResearch:
    def __init__(self, provider) -> None:
        self.provider = provider
        self.grid = frozen_candidate_grid()
        self.vault = FinalHoldout4E()

    async def load_asset(self, symbol: str, start: datetime, end: datetime) -> Phase4EAsset:
        histories = {}
        selected = {}
        features = {}
        train = {}
        validation = {}
        holdout = {}
        for timeframe in TIMEFRAMES_4E:
            warmup = timedelta(seconds=TIMEFRAME_SECONDS[timeframe] * WARMUP_4E)
            histories[timeframe] = await self.provider.fetch(symbol, timeframe, start - warmup, end)
            selected[timeframe] = [candle for candle in histories[timeframe] if start <= candle.timestamp < end]
            features[timeframe] = build_features(histories[timeframe], start, end)
            train_end = int(len(selected[timeframe]) * 0.50)
            validation_end = int(len(selected[timeframe]) * 0.75)
            train[timeframe] = selected[timeframe][:train_end]
            validation[timeframe] = selected[timeframe][train_end:validation_end]
            holdout[timeframe] = selected[timeframe][validation_end:]
        return Phase4EAsset(symbol, selected, features, train, validation, holdout)

    def run(self, assets: dict[str, Phase4EAsset], baseline_assets: dict) -> dict:
        attribution_report = self._baseline_attribution(baseline_assets)
        train_records = {}
        training_table = []
        for config in self.grid:
            results, unfilled = self._evaluate_assets(assets, config, "train", DEFAULT_PROFILE)
            train_records[config.identifier] = (config, results, unfilled)
            training_table.append(result_table_row(config.identifier, config, "TRAIN", results, unfilled))
        ranked = sorted(self.grid, key=lambda config: selection_score(aggregate(train_records[config.identifier][1])), reverse=True)

        validation_set = []
        for timeframe in TIMEFRAMES_4E:
            validation_set.append(next(config for config in ranked if config.timeframe == timeframe))
        for config in ranked[:5]:
            if config not in validation_set:
                validation_set.append(config)
        validation_records = {}
        validation_table = []
        for config in validation_set:
            results, unfilled = self._evaluate_assets(assets, config, "validation", DEFAULT_PROFILE)
            validation_records[config.identifier] = (config, results, unfilled)
            validation_table.append(result_table_row(config.identifier, config, "VALIDATION", results, unfilled))
        setup = next((config for config in ranked if config.identifier in validation_records and validation_pass(aggregate(validation_records[config.identifier][1]))), ranked[0])
        setup_validation_confirmed = setup.identifier in validation_records and validation_pass(aggregate(validation_records[setup.identifier][1]))

        execution_rows = []
        execution_records = {}
        for mode in ExecutionMode:
            config = replace(setup, execution=mode)
            train_results, train_unfilled = self._evaluate_assets(assets, config, "train", DEFAULT_PROFILE)
            validation_results, validation_unfilled = self._evaluate_assets(assets, config, "validation", DEFAULT_PROFILE)
            execution_records[mode] = (config, train_results, validation_results, train_unfilled, validation_unfilled)
            execution_rows.append(result_table_row(config.identifier, config, "TRAIN", train_results, train_unfilled))
            execution_rows.append(result_table_row(config.identifier, config, "VALIDATION", validation_results, validation_unfilled))
        execution_rank = sorted(ExecutionMode, key=lambda mode: selection_score(aggregate(execution_records[mode][1])), reverse=True)
        selected_mode = next((mode for mode in execution_rank if validation_pass(aggregate(execution_records[mode][2]))), execution_rank[0])
        selected = execution_records[selected_mode][0]
        validation_confirmed = setup_validation_confirmed and validation_pass(aggregate(execution_records[selected_mode][2]))

        exchange_sensitivity = {}
        for exchange, profile in PHASE4E_SPOT_PROFILES.items():
            results, unfilled = self._evaluate_assets(assets, selected, "validation", profile)
            exchange_sensitivity[exchange] = {
                "profile": profile,
                "same_bybit_candles": True,
                "not_venue_performance_claim": True,
                "result": result_table_row(selected.identifier, selected, "VALIDATION_COST_SENSITIVITY", results, unfilled),
            }

        self.vault.lock(selected.identifier)
        self.vault.open_once(selected.identifier)
        stress = {}
        for label, multiple in (("normal", Decimal("1")), ("1.25x", Decimal("1.25")), ("1.5x", Decimal("1.5")), ("2x", Decimal("2"))):
            profile = scaled_costs(DEFAULT_PROFILE, multiple)
            results, unfilled = self._evaluate_assets(assets, selected, "holdout", profile)
            stress[label] = {
                "multiple": multiple,
                "result": result_table_row(selected.identifier, selected, "FINAL_HOLDOUT", results, unfilled),
            }
        normal_results = self._extract_results(stress["normal"]["result"])
        normal_summary = stress["normal"]["result"]
        per_asset_pass = all(
            result.metrics["profit_factor"] > 1 and result.metrics["expectancy"] > 0 and result.metrics["return_pct"] > 0
            for result in normal_results.values()
        )
        stress_pass = all(
            stress[label]["result"]["net_pf"] > 1 and stress[label]["result"]["net_expectancy"] > 0 and stress[label]["result"]["return_pct"] > 0
            for label in ("normal", "1.25x", "1.5x")
        )
        eligible = validation_confirmed and per_asset_pass and stress_pass and normal_summary["trades"] >= 15
        return {
            "protocol": {
                "train": "50%",
                "validation": "25%",
                "final_holdout": "25%",
                "candidate_ranking_data": "TRAIN_ONLY",
                "validation_role": "confirmation_only",
                "holdout_opened_once": self.vault.opened,
                "post_holdout_changes": False,
                "fixed_seed": 42,
            },
            "cost_gate_fields": ["expected_move", "estimated_entry_fee", "estimated_exit_fee", "estimated_spread", "estimated_slippage", "estimated_total_cost", "expected_net_edge"],
            "frozen_candidates": [config.to_dict() for config in self.grid],
            "attribution": attribution_report,
            "train_ranking": [config.identifier for config in ranked],
            "candidate_training_results": training_table,
            "candidate_validation_results": validation_table,
            "selected_setup": setup.to_dict(),
            "setup_validation_confirmed": setup_validation_confirmed,
            "maker_taker_comparison": execution_rows,
            "selected_candidate": selected.to_dict(),
            "selected_validation_confirmed": validation_confirmed,
            "timeframe_comparison": self._timeframe_comparison(ranked, train_records, validation_records),
            "larger_move_comparison": self._larger_move_comparison(training_table, validation_table),
            "multi_exchange_cost_sensitivity": exchange_sensitivity,
            "final_holdout_stress": stress,
            "final_status": "CANDIDATE FOR PAPER TRADING" if eligible else "NO NET EDGE AFTER COSTS",
            "live_trading_enabled": False,
            "real_orders_sent": False,
        }

    def _evaluate_assets(self, assets, config: CostAwareConfig, split: str, profile):
        results = {}
        unfilled = 0
        for symbol, asset in assets.items():
            result, missed = evaluate(symbol, getattr(asset, split)[config.timeframe], asset.features[config.timeframe], config, profile)
            results[symbol] = result
            unfilled += missed
        return results, unfilled

    def _baseline_attribution(self, baseline_assets: dict) -> dict:
        lab = StrategyLabV2(self.provider)
        strategy = MeanReversionStrategy()
        train_trades = []
        validation_trades = []
        for asset in baseline_assets.values():
            train_trades.extend(lab._evaluate(strategy, ResearchVariant.STRATEGY_ONLY, asset.train, asset.features, "normal").trades)
            validation_trades.extend(lab._evaluate(strategy, ResearchVariant.STRATEGY_ONLY, asset.validation, asset.features, "normal").trades)
        train = attribution(train_trades)
        validation = attribution(validation_trades)
        return {
            "train": train,
            "validation_confirmation": validation,
            "persistent_negative_groups": persistent_negative_groups(train, validation),
            "selection_policy": "Negative groups originate on TRAIN; VALIDATION only confirms them",
        }

    @staticmethod
    def _timeframe_comparison(ranked, train_records, validation_records) -> list[dict]:
        rows = []
        for timeframe in TIMEFRAMES_4E:
            config = next(item for item in ranked if item.timeframe == timeframe)
            row = {"timeframe": timeframe, "candidate": config.identifier, "train": aggregate(train_records[config.identifier][1])}
            if config.identifier in validation_records:
                row["validation"] = aggregate(validation_records[config.identifier][1])
            rows.append(row)
        return rows

    @staticmethod
    def _larger_move_comparison(training_table: list[dict], validation_table: list[dict]) -> list[dict]:
        selected = []
        for row in training_table + validation_table:
            if any(name in row["candidate"] for name in ("COST_BUFFER_1_50", "DEVIATION_2_2", "RARE_LARGE_MOVE")):
                selected.append(row)
        return selected

    @staticmethod
    def _extract_results(row: dict) -> dict:
        class ResultView:
            def __init__(self, values):
                self.metrics = values["metrics"]

        return {
            "BTC/USDT": ResultView(row["BTC_result"]),
            "ETH/USDT": ResultView(row["ETH_result"]),
            "SOL/USDT": ResultView(row["SOL_result"]),
        }
