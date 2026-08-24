import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import mean

from app.backtest.core import BacktestEngine, BacktestResult, Candle, monte_carlo
from app.backtest.orchestrator import LOW_RISK, TIMEFRAMES, WARMUP_CANDLES
from app.backtest.persistence import BacktestRepository, TIMEFRAME_SECONDS
from app.backtest.research import (
    HistoricalFeatureBuilder,
    ResearchFeature,
    StrategyConfig,
    candidate_configs,
    cost_diagnostics,
    diagnose,
    strategy_from_features,
)

NORMAL_FEE = Decimal("0.0006")
NORMAL_SLIPPAGE = Decimal("0.0002")
COST_SCENARIOS = {
    "normal": (NORMAL_FEE, NORMAL_SLIPPAGE),
    "fees_slippage_1_5x": (NORMAL_FEE * Decimal("1.5"), NORMAL_SLIPPAGE * Decimal("1.5")),
    "slippage_2x": (NORMAL_FEE, NORMAL_SLIPPAGE * Decimal("2")),
}


@dataclass(frozen=True)
class AssetData:
    symbol: str
    histories: dict[str, list[Candle]]
    base: list[Candle]
    features: dict[datetime, ResearchFeature]
    train: list[Candle]
    validation: list[Candle]
    holdout: list[Candle]


class HoldoutVault:
    """Allows a selected immutable version to open each asset holdout exactly once."""

    def __init__(self) -> None:
        self.selected_version: str | None = None
        self.opened: set[str] = set()

    def lock_selection(self, version: str) -> None:
        if self.selected_version is not None:
            raise RuntimeError("Final candidate was already selected")
        self.selected_version = version

    def open(self, version: str, symbol: str) -> None:
        if version != self.selected_version:
            raise RuntimeError("Holdout access denied for unselected strategy")
        if symbol in self.opened:
            raise RuntimeError("Final holdout may be evaluated only once per asset")
        self.opened.add(symbol)


class StrategyResearchFramework:
    def __init__(self, provider, repository: BacktestRepository | None = None) -> None:
        self.provider = provider
        self.repository = repository
        self.feature_builder = HistoricalFeatureBuilder()
        self.vault = HoldoutVault()

    async def load_asset(self, symbol: str, start: datetime, end: datetime) -> AssetData:
        histories = {}
        for timeframe in TIMEFRAMES:
            warmup = timedelta(seconds=TIMEFRAME_SECONDS[timeframe] * WARMUP_CANDLES)
            histories[timeframe] = await self.provider.fetch(symbol,timeframe,start-warmup,end)
        base = [candle for candle in histories["5m"] if start <= candle.timestamp < end]
        features = self.feature_builder.build(histories,start,end)
        first,second=int(len(base)*.5),int(len(base)*.75)
        return AssetData(symbol,histories,base,features,base[:first],base[first:second],base[second:])

    def run(self, assets: dict[str,AssetData], output: Path | None = None) -> dict:
        configs=candidate_configs()
        if self.repository:
            for config in configs: self.repository.register_strategy(config.version,config.to_dict())
        baseline={symbol:self._evaluate(configs[0],data.base[-min(8640,len(data.base)):],data.features,"normal") for symbol,data in assets.items()}
        diagnostics={symbol:{"trade_analysis":diagnose(result),"costs":cost_diagnostics(result),"metrics":result.metrics} for symbol,result in baseline.items()}
        research={}
        for config in configs:
            research[config.version]={}
            for symbol,data in assets.items():
                train=self._evaluate(config,data.train,data.features,"normal")
                validation=self._evaluate(config,data.validation,data.features,"normal")
                research[config.version][symbol]={"train":train,"validation":validation}
                self._record(config.version,symbol,"TRAIN","normal",train.metrics)
                self._record(config.version,symbol,"VALIDATION","normal",validation.metrics)
        initial_rank=sorted(configs,key=lambda config:self._selection_score(research[config.version]),reverse=True)[:3]
        candidates={}
        for config in initial_rank:
            candidates[config.version]={"assets":research[config.version],"stress":{},"walk_forward":{}}
            for symbol,data in assets.items():
                candidates[config.version]["stress"][symbol]={}
                for scenario in ("fees_slippage_1_5x","slippage_2x"):
                    result=self._evaluate(config,data.validation,data.features,scenario)
                    candidates[config.version]["stress"][symbol][scenario]=result
                    self._record(config.version,symbol,"VALIDATION",scenario,result.metrics)
                windows=self._rolling_walk_forward(config,data)
                candidates[config.version]["walk_forward"][symbol]=windows
                for index,window in enumerate(windows): self._record(config.version,symbol,f"WALK_FORWARD_{index}","normal",window["validation"].metrics)
        selected=max(initial_rank,key=lambda config:self._final_selection_score(candidates[config.version]))
        self.vault.lock_selection(selected.version)
        holdout={}; holdout_stress={}
        for symbol,data in assets.items():
            self.vault.open(selected.version,symbol)
            holdout[symbol]=self._evaluate(selected,data.holdout,data.features,"normal")
            holdout_stress[symbol]={scenario:self._evaluate(selected,data.holdout,data.features,scenario) for scenario in ("fees_slippage_1_5x","slippage_2x")}
            self._record(selected.version,symbol,"FINAL_HOLDOUT","normal",holdout[symbol].metrics,True)
            for scenario,result in holdout_stress[symbol].items(): self._record(selected.version,symbol,"FINAL_HOLDOUT",scenario,result.metrics,True)
        combined_trades=[trade for result in holdout.values() for trade in result.trades]
        simulation=monte_carlo(combined_trades,Decimal("1000")*len(holdout),5000)
        status=self._status(holdout,holdout_stress)
        report={"baseline_30d":_serialize_results(baseline),"diagnostics_30d":_serialize(diagnostics),"candidate_versions":[config.to_dict() for config in initial_rank],"candidate_results":_serialize_candidates(candidates),"selected_version":selected.version,"final_holdout":_serialize_results(holdout),"final_holdout_stress":_serialize_nested_results(holdout_stress),"monte_carlo_5000":_serialize(simulation),"final_status":status,"holdout_opened_once":sorted(self.vault.opened)}
        if output: output.write_text(json.dumps(report,indent=2,default=str),encoding="utf-8")
        return report

    def _evaluate(self, config: StrategyConfig, candles: list[Candle], features: dict[datetime,ResearchFeature], scenario: str) -> BacktestResult:
        fee,slippage=COST_SCENARIOS[scenario]
        engine=BacktestEngine(fee_rate=fee,slippage=slippage)
        return engine.run(candles,strategy_from_features(config,features,fee,slippage),Decimal("1000"),risk_profile=LOW_RISK,retain_equity=False)

    def _rolling_walk_forward(self, config: StrategyConfig, data: AssetData) -> list[dict[str,BacktestResult]]:
        research_end=data.validation[-1].timestamp+timedelta(minutes=5); cursor=data.train[0].timestamp; windows=[]
        while cursor+timedelta(days=240)<=research_end:
            train_end=cursor+timedelta(days=180); validation_end=train_end+timedelta(days=60)
            train=[c for c in data.base if cursor<=c.timestamp<train_end]
            validation=[c for c in data.base if train_end<=c.timestamp<validation_end]
            if train and validation: windows.append({"train":self._evaluate(config,train,data.features,"normal"),"validation":self._evaluate(config,validation,data.features,"normal")})
            cursor+=timedelta(days=60)
        return windows

    @staticmethod
    def _selection_score(results: dict) -> float:
        validations=[value["validation"].metrics for value in results.values()]
        minimum_pf=min(float(item["profit_factor"]) for item in validations)
        avg_expectancy=mean(float(item["expectancy"]) for item in validations)
        avg_drawdown=mean(float(item["max_drawdown_pct"]) for item in validations)
        trade_count=sum(item["total_trades"] for item in validations)
        return minimum_pf+avg_expectancy*.2-avg_drawdown*.03+min(trade_count,100)/500

    @staticmethod
    def _final_selection_score(candidate: dict) -> float:
        base=StrategyResearchFramework._selection_score(candidate["assets"])
        stress_metrics=[result.metrics for values in candidate["stress"].values() for result in values.values()]
        walk_metrics=[window["validation"].metrics for windows in candidate["walk_forward"].values() for window in windows]
        stress_pf=min((float(item["profit_factor"]) for item in stress_metrics),default=0)
        walk_expectancy=mean([float(item["expectancy"]) for item in walk_metrics]) if walk_metrics else -10
        return base+stress_pf*.5+walk_expectancy*.1

    @staticmethod
    def _status(holdout: dict[str,BacktestResult], stress: dict) -> str:
        enough=sum(result.metrics["total_trades"] for result in holdout.values())>=50
        normal=all(result.metrics["profit_factor"]>1 and result.metrics["expectancy"]>0 and result.metrics["return_pct"]>0 and result.metrics["max_drawdown_pct"]<25 for result in holdout.values())
        robust=all(result.metrics["net_pnl"]>0 for values in stress.values() for result in values.values())
        if enough and normal and robust: return "CANDIDATE_FOR_PAPER_TRADING"
        if not enough: return "NEEDS_MORE_RESEARCH"
        return "NO_ROBUST_EDGE_FOUND"

    def _record(self,version,symbol,split,costs,metrics,selected=False):
        if self.repository: self.repository.save_experiment(version,symbol,split,costs,metrics,selected)


def _serialize_results(results): return {name:_result_summary(result) for name,result in results.items()}
def _serialize_nested_results(results): return {symbol:{name:_result_summary(value) for name,value in scenarios.items()} for symbol,scenarios in results.items()}
def _serialize_candidates(candidates):
    return {version:{"assets":{symbol:{split:_result_summary(result) for split,result in values.items()} for symbol,values in data["assets"].items()},"stress":_serialize_nested_results(data["stress"]),"walk_forward":{symbol:[{"train":_result_summary(window["train"]),"validation":_result_summary(window["validation"])} for window in windows] for symbol,windows in data["walk_forward"].items()}} for version,data in candidates.items()}
def _result_summary(result): return {"run_id":result.run_id,"starting_balance":result.starting_balance,"final_equity":result.final_equity,"metrics":result.metrics,"trades":len(result.trades)}
def _serialize(value): return json.loads(json.dumps(value,default=str))
