from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal

from app.backtest.persistence import BacktestRepository, TIMEFRAME_SECONDS
from app.strategy_lab.phase4e import WARMUP_4E
from app.strategy_lab.phase4f import (
    BINANCE_PROFILE,
    TIMEFRAMES_4F,
    Hypothesis,
    Phase4FAsset,
    SplitBoundaries,
    build_aligned_features,
    confirmation_pass,
    evaluate_hypothesis,
    frozen_hypotheses,
    multiple_testing_adjustment,
    summarize,
    train_score,
)


class FinalHoldout4F:
    def __init__(self) -> None:
        self.locked: str | None = None
        self.opened = False

    def lock(self, candidate: str) -> None:
        if self.locked is not None:
            raise RuntimeError("Phase 4F candidate already locked")
        self.locked = candidate

    def open_once(self, candidate: str) -> None:
        if self.locked != candidate or self.opened:
            raise RuntimeError("Phase 4F FINAL HOLDOUT access denied")
        self.opened = True


class Phase4FResearch:
    def __init__(self, provider, repository: BacktestRepository | None = None) -> None:
        self.provider = provider
        self.repository = repository
        self.hypotheses = frozen_hypotheses()
        self.vault = FinalHoldout4F()

    async def load_asset(self, symbol: str, boundaries: SplitBoundaries) -> Phase4FAsset:
        histories = {}
        selected = {}
        for timeframe in TIMEFRAMES_4F:
            warmup = timedelta(seconds=TIMEFRAME_SECONDS[timeframe] * WARMUP_4E)
            histories[timeframe] = await self.provider.fetch(symbol, timeframe, boundaries.start - warmup, boundaries.end)
            selected[timeframe] = [candle for candle in histories[timeframe] if boundaries.start <= candle.timestamp < boundaries.end]
        features = build_aligned_features(histories, boundaries.start, boundaries.end)
        train = {timeframe: _slice(candles, boundaries.start, boundaries.train_end) for timeframe, candles in selected.items()}
        validation = {timeframe: _slice(candles, boundaries.validation_start, boundaries.validation_end) for timeframe, candles in selected.items()}
        walk = {timeframe: _slice(candles, boundaries.walk_forward_start, boundaries.walk_forward_end) for timeframe, candles in selected.items()}
        holdout = {timeframe: _slice(candles, boundaries.holdout_start, boundaries.end) for timeframe, candles in selected.items()}
        return Phase4FAsset(symbol, selected, features, train, validation, walk, holdout)

    def run(self, assets: dict[str, Phase4FAsset], boundaries: SplitBoundaries, protocol_hash: str) -> dict:
        experiments = len(self.hypotheses)
        train_records = {}
        for hypothesis in self.hypotheses:
            self._register(hypothesis)
            results = self._evaluate_assets(assets, hypothesis, "train")
            summary = summarize(results)
            correction = multiple_testing_adjustment(summary, experiments)
            train_records[hypothesis.identifier] = {"hypothesis": hypothesis, "results": results, "summary": summary, "correction": correction, "score": train_score(summary, correction)}
            self._record(hypothesis, "TRAIN", "normal", results)
        ranking = sorted(self.hypotheses, key=lambda item: train_records[item.identifier]["score"], reverse=True)
        family_candidates = [next(item for item in ranking if item.family == family) for family in dict.fromkeys(item.family for item in self.hypotheses)]

        validation_records = {}
        for hypothesis in family_candidates:
            results = self._evaluate_assets(assets, hypothesis, "validation")
            summary = summarize(results)
            correction = multiple_testing_adjustment(summary, experiments)
            validation_records[hypothesis.identifier] = {"hypothesis": hypothesis, "results": results, "summary": summary, "correction": correction, "confirmed": confirmation_pass(summary)}
            self._record(hypothesis, "VALIDATION", "normal", results)
        validation_confirmed = [item for item in family_candidates if validation_records[item.identifier]["confirmed"]]

        walk_records = {}
        for hypothesis in validation_confirmed:
            windows = self._walk_windows(assets, hypothesis, boundaries)
            combined_results = self._evaluate_assets(assets, hypothesis, "walk_forward")
            summary = summarize(combined_results)
            correction = multiple_testing_adjustment(summary, experiments)
            positive_windows = sum(confirmation_pass(window["summary"], Decimal("1")) for window in windows)
            confirmed = confirmation_pass(summary, Decimal("1.10")) and positive_windows >= 2 and correction["bonferroni_p"] < 0.10
            walk_records[hypothesis.identifier] = {"hypothesis": hypothesis, "windows": windows, "results": combined_results, "summary": summary, "correction": correction, "positive_windows": positive_windows, "confirmed": confirmed}
            self._record(hypothesis, "WALK_FORWARD", "normal", combined_results)
        walk_confirmed = [item for item in validation_confirmed if walk_records[item.identifier]["confirmed"]]

        portfolio = {
            "status": "NOT_APPLICABLE",
            "reason": "Fewer than two independently walk-forward-confirmed strategies",
        }
        if len(walk_confirmed) >= 2:
            portfolio = self._portfolio_diagnostic(walk_confirmed, walk_records)

        final = None
        final_status = "NO ROBUST EDGE FOUND"
        selected = None
        if walk_confirmed:
            selected = max(walk_confirmed, key=lambda item: train_score(walk_records[item.identifier]["summary"], walk_records[item.identifier]["correction"]))
            self.vault.lock(selected.identifier)
            self.vault.open_once(selected.identifier)
            stress = {}
            for label, multiple in (("normal", Decimal("1")), ("1.25x", Decimal("1.25")), ("1.5x", Decimal("1.5")), ("2x", Decimal("2"))):
                results = self._evaluate_assets(assets, selected, "holdout", multiple)
                summary = summarize(results)
                stress[label] = {"summary": summary, "assets": _serialize_results(results)}
                self._record(selected, "FINAL_HOLDOUT", label, results, selected=True)
            normal = stress["normal"]["summary"]
            cost_125 = stress["1.25x"]["summary"]
            positive_assets = sum(
                values["metrics"]["net_pnl"] > 0 and values["metrics"]["profit_factor"] > 1
                for values in stress["normal"]["assets"].values()
            )
            sufficient = normal["trades"] >= 30
            paper = sufficient and normal["net_pf"] > Decimal("1.15") and normal["expectancy"] > 0 and normal["net_pnl"] > 0 and normal["max_drawdown_pct"] < 20 and cost_125["net_pf"] > 1 and cost_125["net_pnl"] > 0 and positive_assets >= 2
            research_candidate = normal["net_pf"] > 1 and normal["expectancy"] > 0 and normal["net_pnl"] > 0
            final_status = "CANDIDATE FOR PAPER TRADING" if paper else "RESEARCH CANDIDATE" if research_candidate else "NO ROBUST EDGE FOUND"
            final = {"selected": selected.to_dict(), "stress": stress, "sample_status": "SUFFICIENT" if sufficient else "INSUFFICIENT SAMPLE", "positive_assets": positive_assets}

        return {
            "phase": "4F_NEW_EDGE_DISCOVERY",
            "protocol_hash": protocol_hash,
            "protocol": {
                "exchange": "binance",
                "market_type": "spot",
                "new_untouched_venue_dataset": True,
                "phase4e_used_this_dataset": False,
                "boundaries": asdict(boundaries),
                "purge_days": 7,
                "ranking": "TRAIN_ONLY",
                "validation": "confirmation_only",
                "walk_forward_windows": 3,
                "final_holdout_opened": self.vault.opened,
                "post_holdout_parameter_changes": False,
            },
            "economic_hypotheses": [item.to_dict() for item in self.hypotheses],
            "funding_basis": {"status": "SKIPPED", "reason": "The immutable Phase 4F dataset is Binance spot; correct synchronized derivatives funding/basis history is not available in the current provider"},
            "anti_overfitting": {"experiments": experiments, "method": "Bonferroni one-sided Sharpe p-value plus deflated-Sharpe penalty", "family_selection": "one TRAIN-ranked timeframe per family"},
            "train_ranking": [_record_summary(item, train_records[item.identifier]) for item in ranking],
            "validation": [_record_summary(item, validation_records[item.identifier]) for item in family_candidates],
            "walk_forward": [_walk_summary(item, walk_records[item.identifier]) for item in validation_confirmed],
            "portfolio": portfolio,
            "selected_candidate": selected.to_dict() if selected else None,
            "final": final,
            "final_status": final_status,
            "live_trading_enabled": False,
            "real_orders_sent": False,
        }

    def _evaluate_assets(self, assets, hypothesis: Hypothesis, split: str, cost_multiple: Decimal = Decimal("1")):
        return {
            symbol: evaluate_hypothesis(symbol, hypothesis, getattr(asset, split)[hypothesis.timeframe], asset.features[hypothesis.timeframe], BINANCE_PROFILE, cost_multiple)
            for symbol, asset in assets.items()
        }

    def _walk_windows(self, assets, hypothesis, boundaries):
        duration = boundaries.walk_forward_end - boundaries.walk_forward_start
        windows = []
        for index in range(3):
            start = boundaries.walk_forward_start + duration * (index / 3)
            end = boundaries.walk_forward_start + duration * ((index + 1) / 3)
            results = {
                symbol: evaluate_hypothesis(symbol, hypothesis, _slice(asset.walk_forward[hypothesis.timeframe], start, end), asset.features[hypothesis.timeframe], BINANCE_PROFILE)
                for symbol, asset in assets.items()
            }
            windows.append({"index": index, "start": start, "end": end, "summary": summarize(results), "assets": _serialize_results(results)})
        return windows

    @staticmethod
    def _portfolio_diagnostic(candidates, records):
        summaries = [records[item.identifier]["summary"] for item in candidates]
        return {
            "status": "DIAGNOSTIC_ONLY",
            "members": [item.identifier for item in candidates],
            "all_members_independently_passed": True,
            "combined_net_pnl": sum((item["net_pnl"] for item in summaries), Decimal()),
            "combined_trades": sum(item["trades"] for item in summaries),
            "note": "No portfolio optimization or weight fitting was performed",
        }

    def _register(self, hypothesis):
        if self.repository:
            self.repository.register_strategy(f"phase4f_{hypothesis.family.lower()}_{hypothesis.timeframe}_v1", {"phase": "4F", **hypothesis.to_dict(), "minimum_signal_score": 75, "safety_margin_multiple": "0.50", "market_impact_per_leg": "0.00005"})

    def _record(self, hypothesis, split, costs, results, selected=False):
        if not self.repository:
            return
        version = f"phase4f_{hypothesis.family.lower()}_{hypothesis.timeframe}_v1"
        for symbol, result in results.items():
            self.repository.save_experiment(version, symbol, split, costs, result.metrics, selected)


def _slice(candles, start, end):
    return [candle for candle in candles if start <= candle.timestamp < end]


def _serialize_results(results):
    return {symbol: {"trades": len(result.trades), "metrics": result.metrics} for symbol, result in results.items()}


def _record_summary(hypothesis, record):
    return {
        "hypothesis": hypothesis.to_dict(),
        "summary": record["summary"],
        "multiple_testing": record["correction"],
        "score": record.get("score"),
        "confirmed": record.get("confirmed"),
        "assets": _serialize_results(record["results"]),
    }


def _walk_summary(hypothesis, record):
    return {
        "hypothesis": hypothesis.to_dict(),
        "summary": record["summary"],
        "multiple_testing": record["correction"],
        "positive_windows": record["positive_windows"],
        "confirmed": record["confirmed"],
        "windows": record["windows"],
        "assets": _serialize_results(record["results"]),
    }
