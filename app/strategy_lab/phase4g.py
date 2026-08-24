from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
from math import erfc, log, sqrt
from pathlib import Path
import random
from statistics import mean, stdev

from app.backtest.core import BacktestResult, BacktestTrade
from app.strategy_lab.phase4f import Regime4F, frozen_hypotheses

FROZEN_VERSION = "phase4g_volatility_expansion_1h_frozen_v1"
PHASE4F_SOURCE_SHA256 = (
    "0dd7815c36f0d9c70ffa85057b8cc7feeeb38ea7e1ae9c6894b53b03522aa606"
)
PHASE4F_PROTOCOL_SHA256 = (
    "50cce2c210a0721f1e86b5f932a49f5550aa76c2d255c49c4e639d1cf2033642"
)
INHERITED_MULTIPLE_TESTS = 36
STARTING_BALANCE = Decimal("1000")
COST_MULTIPLES = {
    "normal": Decimal("1"),
    "1.25x": Decimal("1.25"),
    "1.5x": Decimal("1.5"),
    "2x": Decimal("2"),
}
CONTROL_ASSETS = ("BTC/USDT", "ETH/USDT", "SOL/USDT")
CONFIRMATION_ASSETS = (
    "XRP/USDT",
    "ADA/USDT",
    "LINK/USDT",
    "AVAX/USDT",
    "DOGE/USDT",
    "LTC/USDT",
    "BCH/USDT",
    "SUI/USDT",
    "NEAR/USDT",
)
EXCHANGES = ("binance", "bybit", "okx", "bitget")

FROZEN_CONFIG = {
    "version": FROZEN_VERSION,
    "source_phase": "4F",
    "source_strategy": "VOLATILITY_EXPANSION:1h:v1",
    "phase4f_protocol_sha256": PHASE4F_PROTOCOL_SHA256,
    "phase4f_implementation_sha256": PHASE4F_SOURCE_SHA256,
    "timeframe": "1h",
    "feature_pipeline": "phase4e.build_features:v1",
    "allowed_regimes": ["BREAKOUT", "HIGH_VOLATILITY"],
    "atr_percentile_minimum": "75",
    "bollinger_width_minimum": "0.008",
    "breakout_direction_required": True,
    "momentum_acceleration_confirmation": True,
    "signal_score": "min(96, 70 + atr_percentile * 0.2)",
    "minimum_signal_score": 75,
    "stop_atr": "2",
    "reward_r": "3",
    "expected_move_atr": "2.8",
    "risk_profile": {
        "risk_per_trade_pct": "0.005",
        "max_position_notional": "500",
        "max_leverage": "2",
        "max_concurrent_positions": 2,
        "max_daily_loss_pct": "0.02",
        "min_risk_reward": "2",
        "max_consecutive_losses": 3,
        "cooldown_minutes": 30,
        "max_volatility_pct": "0.04",
        "max_spread_pct": "0.002",
    },
    "execution": {
        "order_type": "market_taker",
        "market_type": "spot_ohlcv_research",
        "leverage": "1",
        "market_impact_per_leg": "0.00005",
        "cost_safety_margin_multiple": "0.50",
        "same_candle_policy": "PositionManager.resolve_candle_exit",
    },
}


def canonical_json(value: dict) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode()


FROZEN_CONFIG_HASH = sha256(canonical_json(FROZEN_CONFIG)).hexdigest()


def verify_frozen_implementation() -> None:
    source = Path(__file__).with_name("phase4f.py")
    actual = sha256(source.read_bytes()).hexdigest()
    if actual != PHASE4F_SOURCE_SHA256:
        raise RuntimeError(
            "Frozen Phase 4G strategy implementation changed; refusing validation"
        )


def frozen_hypothesis():
    return next(
        item
        for item in frozen_hypotheses()
        if item.family == "VOLATILITY_EXPANSION" and item.timeframe == "1h"
    )


@dataclass(frozen=True)
class TaggedResult:
    exchange: str
    symbol: str
    cost_scenario: str
    result: BacktestResult


def trade_metrics(
    trades: list[BacktestTrade], starting_capital: Decimal = STARTING_BALANCE
) -> dict:
    ordered = sorted(trades, key=lambda trade: (trade.exit_time, trade.entry_time))
    net = sum((trade.pnl for trade in ordered), Decimal())
    wins = [trade.pnl for trade in ordered if trade.pnl > 0]
    losses = [trade.pnl for trade in ordered if trade.pnl < 0]
    equity = peak = starting_capital
    max_drawdown = max_drawdown_pct = Decimal()
    for trade in ordered:
        equity += trade.pnl
        peak = max(peak, equity)
        drawdown = peak - equity
        max_drawdown = max(max_drawdown, drawdown)
        if peak:
            max_drawdown_pct = max(
                max_drawdown_pct, drawdown / peak * Decimal("100")
            )
    fees = sum((trade.fees for trade in ordered), Decimal())
    spread = sum((trade.spread_cost for trade in ordered), Decimal())
    slippage = sum((trade.slippage_cost for trade in ordered), Decimal())
    funding = sum((trade.funding for trade in ordered), Decimal())
    return {
        "trades": len(ordered),
        "wins": len(wins),
        "win_rate": Decimal(len(wins) * 100) / len(ordered) if ordered else Decimal(),
        "net_pnl": net,
        "net_return_pct": (
            net / starting_capital * Decimal("100") if starting_capital else Decimal()
        ),
        "net_pf": (
            sum(wins, Decimal()) / abs(sum(losses, Decimal()))
            if losses
            else Decimal("Infinity")
            if wins
            else Decimal()
        ),
        "expectancy": net / len(ordered) if ordered else Decimal(),
        "max_drawdown": max_drawdown,
        "max_drawdown_pct": max_drawdown_pct,
        "fees": fees,
        "spread": spread,
        "slippage": slippage,
        "funding": funding,
        "gross_pnl": net + fees + spread + slippage + funding,
        "turnover": sum((trade.turnover for trade in ordered), Decimal()),
        "average_holding_seconds": (
            sum(
                (
                    Decimal(str((trade.exit_time - trade.entry_time).total_seconds()))
                    for trade in ordered
                ),
                Decimal(),
            )
            / len(ordered)
            if ordered
            else Decimal()
        ),
    }


def result_metrics(result: BacktestResult) -> dict:
    metrics = trade_metrics(result.trades)
    metrics["max_drawdown"] = Decimal(str(result.metrics["max_drawdown"]))
    metrics["max_drawdown_pct"] = Decimal(str(result.metrics["max_drawdown_pct"]))
    metrics["sharpe"] = Decimal(str(result.metrics["sharpe"]))
    metrics["sortino"] = Decimal(str(result.metrics["sortino"]))
    return metrics


def aggregate(tagged: list[TaggedResult]) -> dict:
    trades = [trade for item in tagged for trade in item.result.trades]
    return trade_metrics(trades, STARTING_BALANCE * len(tagged))


def regime_attribution(tagged: list[TaggedResult]) -> dict:
    capital = STARTING_BALANCE * len(tagged)
    trades = [trade for item in tagged for trade in item.result.trades]
    return {
        regime.value: trade_metrics(
            [trade for trade in trades if trade.regime == regime.value], capital
        )
        for regime in Regime4F
    }


def period_attribution(tagged: list[TaggedResult], half_year: bool = False) -> dict:
    grouped: dict[str, list[tuple[str, str, BacktestTrade]]] = defaultdict(list)
    for item in tagged:
        for trade in item.result.trades:
            month_group = 1 if trade.exit_time.month <= 6 else 2
            quarter = (trade.exit_time.month - 1) // 3 + 1
            label = (
                f"{trade.exit_time.year}-H{month_group}"
                if half_year
                else f"{trade.exit_time.year}-Q{quarter}"
            )
            grouped[label].append((item.exchange, item.symbol, trade))
    output = {}
    for label, values in sorted(grouped.items()):
        combinations = {(exchange, symbol) for exchange, symbol, _ in values}
        output[label] = trade_metrics(
            [trade for _, _, trade in values], STARTING_BALANCE * len(combinations)
        )
    return output


def leave_one_asset_out(tagged: list[TaggedResult]) -> dict:
    assets = sorted({item.symbol for item in tagged})
    asset_net = {
        asset: aggregate([item for item in tagged if item.symbol == asset])["net_pnl"]
        for asset in assets
    }
    best = max(asset_net, key=asset_net.get) if asset_net else None
    requested = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    output = {"ALL ASSETS": aggregate(tagged)}
    for asset in requested:
        output[f"WITHOUT {asset.split('/')[0]}"] = aggregate(
            [item for item in tagged if item.symbol != asset]
        )
    if best:
        output["WITHOUT BEST PERFORMING ASSET"] = {
            **aggregate([item for item in tagged if item.symbol != best]),
            "removed_asset": best,
        }
    without_best = output.get("WITHOUT BEST PERFORMING ASSET", {})
    concentrated = bool(
        best
        and (
            without_best.get("net_pnl", Decimal()) <= 0
            or without_best.get("net_pf", Decimal()) <= 1
        )
    )
    return {
        "results": output,
        "asset_net_pnl": asset_net,
        "diagnosis": "CONCENTRATED EDGE" if concentrated else "NOT CONCENTRATED",
    }


def leave_one_exchange_out(tagged: list[TaggedResult]) -> dict:
    exchanges = sorted({item.exchange for item in tagged})
    exchange_net = {
        exchange: aggregate([item for item in tagged if item.exchange == exchange])[
            "net_pnl"
        ]
        for exchange in exchanges
    }
    best = max(exchange_net, key=exchange_net.get) if exchange_net else None
    output = {"ALL EXCHANGES": aggregate(tagged)}
    for exchange in exchanges:
        output[f"WITHOUT {exchange.upper()}"] = aggregate(
            [item for item in tagged if item.exchange != exchange]
        )
    if best:
        output["WITHOUT BEST PERFORMING EXCHANGE"] = {
            **aggregate([item for item in tagged if item.exchange != best]),
            "removed_exchange": best,
        }
    without_best = output.get("WITHOUT BEST PERFORMING EXCHANGE", {})
    specific = bool(
        best
        and (
            without_best.get("net_pnl", Decimal()) <= 0
            or without_best.get("net_pf", Decimal()) <= 1
        )
    )
    return {
        "results": output,
        "exchange_net_pnl": exchange_net,
        "diagnosis": "EXCHANGE-SPECIFIC EDGE" if specific else "NOT EXCHANGE-SPECIFIC",
    }


def statistical_validation(tagged: list[TaggedResult], simulations: int = 2000) -> dict:
    clusters = {
        f"{item.exchange}:{item.symbol}": [float(trade.pnl) for trade in item.result.trades]
        for item in tagged
        if item.result.trades
    }
    pnl = [value for values in clusters.values() for value in values]
    if not pnl:
        return {
            "expectancy_ci_95": [0.0, 0.0],
            "bootstrap_simulations": simulations,
            "raw_one_sided_p": 1.0,
            "multiple_testing_adjusted_p": 1.0,
            "deflated_sharpe": 0.0,
            "trade_count_adequacy": "INSUFFICIENT SAMPLE",
        }
    rng = random.Random(42)
    keys = sorted(clusters)
    estimates = []
    if len(keys) > 1:
        for _ in range(simulations):
            sampled = [rng.choice(keys) for _ in keys]
            values = [value for key in sampled for value in clusters[key]]
            estimates.append(mean(values))
    else:
        values = clusters[keys[0]]
        block = max(2, int(sqrt(len(values))))
        for _ in range(simulations):
            sampled = []
            while len(sampled) < len(values):
                start = rng.randrange(max(1, len(values) - block + 1))
                sampled.extend(values[start : start + block])
            estimates.append(mean(sampled[: len(values)]))
    estimates.sort()
    low = estimates[int(simulations * 0.025)]
    high = estimates[min(simulations - 1, int(simulations * 0.975))]
    sigma = stdev(pnl) if len(pnl) > 1 else 0.0
    observed_sharpe = mean(pnl) / sigma if sigma else 0.0
    statistic = mean(pnl) / (sigma / sqrt(len(pnl))) if sigma else 0.0
    raw_p = 0.5 * erfc(statistic / sqrt(2))
    adjusted = min(1.0, raw_p * INHERITED_MULTIPLE_TESTS)
    active_quarters = len(period_attribution(tagged))
    adequate = (
        len(pnl) >= 200 and len(clusters) >= 20 and active_quarters >= 6
    )
    return {
        "expectancy_ci_95": [low, high],
        "bootstrap_method": "cluster bootstrap by exchange-asset; seed 42",
        "bootstrap_simulations": simulations,
        "observed_trade_sharpe": observed_sharpe,
        "raw_one_sided_p": raw_p,
        "multiple_testing_adjusted_p": adjusted,
        "inherited_multiple_tests": INHERITED_MULTIPLE_TESTS,
        "deflated_sharpe": observed_sharpe
        - sqrt(2 * log(INHERITED_MULTIPLE_TESTS)),
        "raw_trades": len(pnl),
        "exchange_asset_clusters": len(clusters),
        "active_quarters": active_quarters,
        "adequacy_rule": "trades>=200, exchange-asset clusters>=20, active quarters>=6",
        "trade_count_adequacy": "ADEQUATE" if adequate else "INSUFFICIENT SAMPLE",
    }


def stability_diagnostic(periods: dict) -> dict:
    populated = [values for values in periods.values() if values["trades"]]
    positive = [values for values in populated if values["net_pnl"] > 0]
    total_positive = sum((values["net_pnl"] for values in positive), Decimal())
    largest_share = (
        max((values["net_pnl"] for values in positive), default=Decimal())
        / total_positive
        if total_positive
        else Decimal()
    )
    stable = bool(
        populated
        and len(positive) / len(populated) >= 0.5
        and largest_share <= Decimal("0.60")
    )
    return {
        "periods": len(populated),
        "positive_periods": len(positive),
        "largest_positive_pnl_share": largest_share,
        "rule": ">=50% positive periods and no period contributes >60% of positive PnL",
        "stable": stable,
    }
