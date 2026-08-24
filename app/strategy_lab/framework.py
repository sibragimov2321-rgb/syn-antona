import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import mean

from app.backtest.core import BacktestEngine, BacktestResult, Candle
from app.backtest.costs import BYBIT_SPOT_NON_VIP
from app.backtest.orchestrator import LOW_RISK, TIMEFRAMES, WARMUP_CANDLES
from app.backtest.persistence import BacktestRepository, TIMEFRAME_SECONDS
from app.strategy_lab.ai_provider import AIAnalysisProvider, FrozenResearchAIProvider
from app.strategy_lab.execution import ResearchVariant, action_for
from app.strategy_lab.features import AuxiliaryMarketSeries, LabFeature, LabFeatureBuilder
from app.strategy_lab.strategies import LabStrategy, strategy_families

NORMAL_FEE = BYBIT_SPOT_NON_VIP.taker_fee
NORMAL_SLIPPAGE = BYBIT_SPOT_NON_VIP.slippage
COSTS = {
    "normal": (NORMAL_FEE, NORMAL_SLIPPAGE),
    "fees_slippage_1_5x": (NORMAL_FEE * Decimal("1.5"), NORMAL_SLIPPAGE * Decimal("1.5")),
    "slippage_2x": (NORMAL_FEE, NORMAL_SLIPPAGE * Decimal("2")),
}


@dataclass(frozen=True)
class LabAsset:
    symbol: str
    base: list[Candle]
    features: dict[datetime, LabFeature]
    train: list[Candle]
    validation: list[Candle]
    holdout: list[Candle]
    funding_available: bool
    open_interest_available: bool


class FinalHoldoutVault:
    """The frozen candidate batch can inspect final data exactly once."""

    def __init__(self) -> None:
        self.frozen_candidates: tuple[str, ...] | None = None
        self.opened = False

    def lock(self, candidates: tuple[str, ...]) -> None:
        if self.frozen_candidates is not None:
            raise RuntimeError("Final candidates were already frozen")
        self.frozen_candidates = candidates

    def open_once(self, candidates: tuple[str, ...]) -> None:
        if self.frozen_candidates != candidates:
            raise RuntimeError("Final holdout access denied for changed candidates")
        if self.opened:
            raise RuntimeError("FINAL HOLDOUT can be opened only once")
        self.opened = True


class StrategyLabV2:
    def __init__(self, provider, repository: BacktestRepository | None = None, ai_provider: AIAnalysisProvider | None = None) -> None:
        self.provider = provider
        self.repository = repository
        self.ai_provider = ai_provider or FrozenResearchAIProvider()
        self.feature_builder = LabFeatureBuilder()
        self.strategies = strategy_families()
        self.vault = FinalHoldoutVault()

    async def load_asset(self, symbol: str, start: datetime, end: datetime, auxiliary: AuxiliaryMarketSeries | None = None) -> LabAsset:
        histories = {}
        for timeframe in TIMEFRAMES:
            warmup = timedelta(seconds=TIMEFRAME_SECONDS[timeframe] * WARMUP_CANDLES)
            histories[timeframe] = await self.provider.fetch(symbol, timeframe, start - warmup, end)
        base = [candle for candle in histories["5m"] if start <= candle.timestamp < end]
        features = self.feature_builder.build(histories, start, end, auxiliary)
        train_end = int(len(base) * 0.50)
        validation_end = int(len(base) * 0.75)
        auxiliary = auxiliary or AuxiliaryMarketSeries({}, {})
        return LabAsset(symbol, base, features, base[:train_end], base[train_end:validation_end], base[validation_end:], bool(auxiliary.funding_rate), bool(auxiliary.open_interest))

    def run(self, assets: dict[str, LabAsset], output: Path | None = None) -> dict:
        variants = tuple(ResearchVariant)
        research: dict[str, dict] = {}
        for strategy in self.strategies:
            research[strategy.name] = {}
            for variant in variants:
                version = self._version(strategy, variant)
                self._register(version, strategy, variant)
                asset_results = {}
                for symbol, data in assets.items():
                    train = self._evaluate(strategy, variant, data.train, data.features, "normal")
                    validation = self._evaluate(strategy, variant, data.validation, data.features, "normal")
                    asset_results[symbol] = {"train": train, "validation": validation}
                    self._record(version, symbol, "TRAIN", "normal", train.metrics)
                    self._record(version, symbol, "VALIDATION", "normal", validation.metrics)
                research[strategy.name][variant.value] = asset_results

        chosen_variants: dict[str, ResearchVariant] = {}
        candidate_details: dict[str, dict] = {}
        for strategy in self.strategies:
            chosen = max(variants, key=lambda variant: _selection_score([research[strategy.name][variant.value][symbol]["validation"] for symbol in assets]))
            chosen_variants[strategy.name] = chosen
            validation_results = {symbol: research[strategy.name][chosen.value][symbol]["validation"] for symbol in assets}
            stress = {}
            walk_forward = {}
            for symbol, data in assets.items():
                stress[symbol] = {scenario: self._evaluate(strategy, chosen, data.validation, data.features, scenario) for scenario in COSTS if scenario != "normal"}
                for scenario, result in stress[symbol].items():
                    self._record(self._version(strategy, chosen), symbol, "VALIDATION", scenario, result.metrics)
                walk_forward[symbol] = self._walk_forward(strategy, chosen, data)
                for index, window in enumerate(walk_forward[symbol]):
                    self._record(self._version(strategy, chosen), symbol, f"WALK_FORWARD_{index}", "normal", window["validation"].metrics)
            candidate_details[strategy.name] = {"variant": chosen, "validation": validation_results, "stress": stress, "walk_forward": walk_forward}

        selected_strategy = max(self.strategies, key=lambda strategy: _candidate_score(candidate_details[strategy.name]))
        selected_variant = chosen_variants[selected_strategy.name]
        frozen = tuple(f"{strategy.name}:{chosen_variants[strategy.name].value}" for strategy in self.strategies)
        self.vault.lock(frozen)
        self.vault.open_once(frozen)

        final: dict[str, dict] = {}
        for strategy in self.strategies:
            variant = chosen_variants[strategy.name]
            normal = {}
            stress = {}
            for symbol, data in assets.items():
                normal[symbol] = self._evaluate(strategy, variant, data.holdout, data.features, "normal")
                stress[symbol] = {scenario: self._evaluate(strategy, variant, data.holdout, data.features, scenario) for scenario in COSTS if scenario != "normal"}
                is_selected = strategy.name == selected_strategy.name and variant is selected_variant
                self._record(self._version(strategy, variant), symbol, "FINAL_HOLDOUT", "normal", normal[symbol].metrics, is_selected)
                for scenario, result in stress[symbol].items():
                    self._record(self._version(strategy, variant), symbol, "FINAL_HOLDOUT", scenario, result.metrics, is_selected)
            final[strategy.name] = {"variant": variant, "normal": normal, "stress": stress}

        table = []
        robust_selected = False
        for strategy in self.strategies:
            name = strategy.name
            normal = final[name]["normal"]
            aggregate = _aggregate(list(normal.values()))
            stress_pass = _stress_pass(final[name]["stress"])
            walk_pass = _walk_forward_pass(candidate_details[name]["walk_forward"])
            cross_period = _cross_period_pass(candidate_details[name]["walk_forward"], normal)
            eligible = _eligible(aggregate, stress_pass, walk_pass, cross_period)
            selected = name == selected_strategy.name
            status = "CANDIDATE_FOR_PAPER_TRADING" if selected and eligible else "NO_ROBUST_EDGE"
            robust_selected = robust_selected or status == "CANDIDATE_FOR_PAPER_TRADING"
            table.append({
                "Strategy": name,
                "Variant": final[name]["variant"].value,
                "BTC PF": normal.get("BTC/USDT").metrics["profit_factor"] if "BTC/USDT" in normal else None,
                "ETH PF": normal.get("ETH/USDT").metrics["profit_factor"] if "ETH/USDT" in normal else None,
                "SOL PF": normal.get("SOL/USDT").metrics["profit_factor"] if "SOL/USDT" in normal else None,
                "OOS Return": aggregate["return_pct"],
                "Max DD": aggregate["max_drawdown_pct"],
                "Expectancy": aggregate["expectancy"],
                "Stress Result": "PASS" if stress_pass else "FAIL",
                "Walk Forward": "PASS" if walk_pass else "FAIL",
                "Status": status,
            })

        report = {
            "protocol": {"train": "50%", "validation": "25%", "final_holdout": "25%", "holdout_opened_once": self.vault.opened, "post_holdout_optimization": False, "ai_provider": type(self.ai_provider).__name__, "ai_provider_warning": "Frozen deterministic research surrogate; not evidence of live LLM edge"},
            "data": {symbol: {"candles_5m": len(data.base), "start": data.base[0].timestamp, "end": data.base[-1].timestamp, "funding_available": data.funding_available, "open_interest_available": data.open_interest_available} for symbol, data in assets.items()},
            "abc_validation": _serialize_research(research),
            "chosen_variants": {name: variant.value for name, variant in chosen_variants.items()},
            "selected_candidate": {"strategy": selected_strategy.name, "variant": selected_variant.value},
            "positive_expectancy_regimes_validation": {name: _positive_regimes(candidate_details[name]["validation"]) for name in candidate_details},
            "walk_forward": {name: _serialize_walk(candidate_details[name]["walk_forward"]) for name in candidate_details},
            "validation_stress": {name: _serialize_nested(candidate_details[name]["stress"]) for name in candidate_details},
            "final_holdout": {name: {"variant": details["variant"].value, "normal": _serialize_results(details["normal"]), "stress": _serialize_nested(details["stress"])} for name, details in final.items()},
            "final_table": table,
            "final_status": "CANDIDATE FOR PAPER TRADING" if robust_selected else "NO ROBUST EDGE FOUND — DO NOT ENABLE LIVE TRADING",
            "live_trading_enabled": False,
        }
        if output:
            output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        return report

    def _evaluate(self, strategy: LabStrategy, variant: ResearchVariant, candles: list[Candle], features: dict[datetime, LabFeature], scenario: str) -> BacktestResult:
        fee, slippage = COSTS[scenario]

        def decide(history) -> object:
            feature = features.get(history[-1].timestamp)
            return action_for(strategy, variant, feature, self.ai_provider, fee, slippage) if feature else None

        return BacktestEngine(fee_rate=fee, slippage=slippage, spread=BYBIT_SPOT_NON_VIP.spread, market_type=BYBIT_SPOT_NON_VIP.market_type).run(candles, decide, Decimal("1000"), risk_profile=LOW_RISK, retain_equity=False)

    def _walk_forward(self, strategy: LabStrategy, variant: ResearchVariant, data: LabAsset) -> list[dict[str, BacktestResult]]:
        research = data.train + data.validation
        end = research[-1].timestamp + timedelta(minutes=5)
        cursor = research[0].timestamp
        windows = []
        while cursor + timedelta(days=240) <= end:
            train_end = cursor + timedelta(days=180)
            validation_end = train_end + timedelta(days=60)
            train = [candle for candle in research if cursor <= candle.timestamp < train_end]
            validation = [candle for candle in research if train_end <= candle.timestamp < validation_end]
            if train and validation:
                windows.append({"train": self._evaluate(strategy, variant, train, data.features, "normal"), "validation": self._evaluate(strategy, variant, validation, data.features, "normal")})
            cursor += timedelta(days=60)
        return windows

    def _version(self, strategy: LabStrategy, variant: ResearchVariant) -> str:
        return f"phase4c_{strategy.name.lower()}_{variant.value[0].lower()}_v1"

    def _register(self, version: str, strategy: LabStrategy, variant: ResearchVariant) -> None:
        if self.repository:
            self.repository.register_strategy(version, {"phase": "4C", "strategy": strategy.name, "variant": variant.value, "ai_provider": getattr(self.ai_provider, "version", type(self.ai_provider).__name__), "minimum_edge": 68, "cost_multiple": 2})

    def _record(self, version: str, symbol: str, split: str, costs: str, metrics: dict, selected: bool = False) -> None:
        if self.repository:
            self.repository.save_experiment(version, symbol, split, costs, metrics, selected)


def _aggregate(results: list[BacktestResult]) -> dict:
    metrics = [result.metrics for result in results]
    trades = sum(item["total_trades"] for item in metrics)
    net = sum((item["net_pnl"] for item in metrics), Decimal())
    gross_profit = sum((item["gross_profit"] for item in metrics), Decimal())
    gross_loss = sum((item["gross_loss"] for item in metrics), Decimal())
    return {
        "trades": trades,
        "net_pnl": net,
        "return_pct": net / (Decimal("1000") * len(results)) * 100 if results else Decimal(),
        "profit_factor": gross_profit / abs(gross_loss) if gross_loss else (Decimal("Infinity") if gross_profit else Decimal()),
        "expectancy": net / trades if trades else Decimal(),
        "max_drawdown_pct": max((item["max_drawdown_pct"] for item in metrics), default=Decimal()),
    }


def _selection_score(results: list[BacktestResult]) -> float:
    aggregate = _aggregate(results)
    if aggregate["trades"] < 20:
        return -100 + aggregate["trades"]
    profit_factor = min(float(aggregate["profit_factor"]), 5)
    return profit_factor + float(aggregate["expectancy"]) * 0.15 + float(aggregate["return_pct"]) * 0.03 - float(aggregate["max_drawdown_pct"]) * 0.02


def _candidate_score(candidate: dict) -> float:
    base = _selection_score(list(candidate["validation"].values()))
    stress = [_aggregate([values[scenario] for values in candidate["stress"].values()]) for scenario in COSTS if scenario != "normal"]
    stress_score = mean(min(float(item["profit_factor"]), 5) for item in stress)
    windows = [window["validation"] for values in candidate["walk_forward"].values() for window in values]
    walk_score = _selection_score(windows) if windows else -100
    return base + stress_score * 0.4 + walk_score * 0.3


def _stress_pass(stress: dict[str, dict[str, BacktestResult]]) -> bool:
    for scenario in ("fees_slippage_1_5x", "slippage_2x"):
        aggregate = _aggregate([values[scenario] for values in stress.values()])
        if aggregate["net_pnl"] <= 0 or aggregate["profit_factor"] <= 1:
            return False
    return True


def _walk_forward_pass(walk: dict[str, list[dict[str, BacktestResult]]]) -> bool:
    results = [window["validation"] for windows in walk.values() for window in windows]
    if len(results) < 6:
        return False
    positive = sum(result.metrics["net_pnl"] > 0 and result.metrics["expectancy"] > 0 for result in results)
    return positive / len(results) >= 0.60 and _aggregate(results)["expectancy"] > 0


def _cross_period_pass(walk: dict[str, list[dict[str, BacktestResult]]], holdout: dict[str, BacktestResult]) -> bool:
    positive_assets = sum(result.metrics["net_pnl"] > 0 for result in holdout.values())
    by_asset = []
    for windows in walk.values():
        validations = [window["validation"] for window in windows]
        by_asset.append(sum(result.metrics["net_pnl"] > 0 for result in validations) >= max(1, len(validations) // 2))
    return positive_assets >= 2 and sum(by_asset) >= 2


def _eligible(aggregate: dict, stress_pass: bool, walk_pass: bool, cross_period: bool) -> bool:
    return aggregate["trades"] >= 30 and aggregate["return_pct"] > 0 and aggregate["profit_factor"] > 1 and aggregate["expectancy"] > 0 and aggregate["max_drawdown_pct"] < 15 and stress_pass and walk_pass and cross_period


def _positive_regimes(results: dict[str, BacktestResult]) -> dict:
    grouped: dict[str, list] = {}
    for result in results.values():
        for trade in result.trades:
            grouped.setdefault(trade.regime, []).append(trade.pnl)
    return {regime: {"trades": len(pnls), "expectancy": sum(pnls, Decimal()) / len(pnls), "net_pnl": sum(pnls, Decimal())} for regime, pnls in grouped.items() if pnls and sum(pnls, Decimal()) / len(pnls) > 0}


def _summary(result: BacktestResult) -> dict:
    return {"run_id": result.run_id, "trades": len(result.trades), "metrics": result.metrics}


def _serialize_results(results: dict[str, BacktestResult]) -> dict:
    return {name: _summary(result) for name, result in results.items()}


def _serialize_nested(results: dict[str, dict[str, BacktestResult]]) -> dict:
    return {symbol: {scenario: _summary(result) for scenario, result in values.items()} for symbol, values in results.items()}


def _serialize_research(research: dict) -> dict:
    return {strategy: {variant: {symbol: {split: _summary(result) for split, result in values.items()} for symbol, values in assets.items()} for variant, assets in variants.items()} for strategy, variants in research.items()}


def _serialize_walk(walk: dict[str, list[dict[str, BacktestResult]]]) -> dict:
    return {symbol: [{"train": _summary(window["train"]), "validation": _summary(window["validation"])} for window in windows] for symbol, windows in walk.items()}
