import argparse
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
import json
from math import erfc, log, sqrt
from pathlib import Path
import random
from statistics import mean, stdev

from app.shadow.engine import PROTOCOL_ID
from app.shadow.repository import ShadowRepository
from app.strategy_lab.phase4g import FROZEN_CONFIG_HASH


def _trade_pnl(trade, multiple: Decimal = Decimal("1")) -> Decimal:
    costs = (
        Decimal(trade.entry_fee)
        + Decimal(trade.exit_fee)
        + Decimal(trade.entry_spread_cost)
        + Decimal(trade.exit_spread_cost)
        + Decimal(trade.entry_slippage_cost)
        + Decimal(trade.exit_slippage_cost)
    )
    return Decimal(trade.gross_pnl) - costs * multiple


def summarize(trades, capital: Decimal, multiple: Decimal = Decimal("1")) -> dict:
    pnls = [_trade_pnl(trade, multiple) for trade in trades]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    net = sum(pnls, Decimal())
    equity = peak = capital
    max_drawdown = max_drawdown_pct = Decimal()
    for value in pnls:
        equity += value
        peak = max(peak, equity)
        drawdown = peak - equity
        max_drawdown = max(max_drawdown, drawdown)
        max_drawdown_pct = max(
            max_drawdown_pct, drawdown / peak * 100 if peak else Decimal()
        )
    values = [float(value / capital) for value in pnls] if capital else []
    average = mean(values) if values else 0.0
    sigma = stdev(values) if len(values) > 1 else 0.0
    downside = sqrt(mean([min(value, 0.0) ** 2 for value in values])) if values else 0.0
    fees = sum((Decimal(trade.entry_fee) + Decimal(trade.exit_fee) for trade in trades), Decimal())
    spread = sum((Decimal(trade.entry_spread_cost) + Decimal(trade.exit_spread_cost) for trade in trades), Decimal())
    slippage = sum((Decimal(trade.entry_slippage_cost) + Decimal(trade.exit_slippage_cost) for trade in trades), Decimal())
    holding = [
        Decimal(
            str(
                (
                    (trade.closed_at.replace(tzinfo=UTC) if trade.closed_at.tzinfo is None else trade.closed_at)
                    - (trade.opened_at.replace(tzinfo=UTC) if trade.opened_at.tzinfo is None else trade.opened_at)
                ).total_seconds()
            )
        )
        for trade in trades
    ]
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": Decimal(len(wins) * 100) / len(trades) if trades else Decimal(),
        "gross_pnl": sum((Decimal(trade.gross_pnl) for trade in trades), Decimal()),
        "fees": fees * multiple,
        "spread": spread * multiple,
        "slippage": slippage * multiple,
        "net_pnl": net,
        "net_return_pct": net / capital * 100 if capital else Decimal(),
        "net_pf": sum(wins, Decimal()) / abs(sum(losses, Decimal())) if losses else Decimal("Infinity") if wins else Decimal(),
        "expectancy": net / len(trades) if trades else Decimal(),
        "average_trade": net / len(trades) if trades else Decimal(),
        "sharpe": average / sigma if sigma else 0.0,
        "sortino": average / downside if downside else 0.0,
        "max_drawdown": max_drawdown,
        "max_drawdown_pct": max_drawdown_pct,
        "average_holding_seconds": sum(holding, Decimal()) / len(holding) if holding else Decimal(),
    }


def statistical_validation(trades, simulations: int = 2000) -> dict:
    clusters = defaultdict(list)
    for trade in trades:
        clusters[f"{trade.exchange}:{trade.symbol}"].append(float(_trade_pnl(trade)))
    values = [value for cluster in clusters.values() for value in cluster]
    if not values:
        return {"expectancy_ci_95": [0, 0], "adjusted_p": 1, "deflated_sharpe": 0, "adequacy": "INSUFFICIENT SAMPLE"}
    rng = random.Random(42)
    keys = sorted(clusters)
    estimates = []
    for _ in range(simulations):
        sampled = [rng.choice(keys) for _ in keys]
        sample = [value for key in sampled for value in clusters[key]]
        estimates.append(mean(sample))
    estimates.sort()
    sigma = stdev(values) if len(values) > 1 else 0.0
    sharpe = mean(values) / sigma if sigma else 0.0
    statistic = mean(values) / (sigma / sqrt(len(values))) if sigma else 0.0
    raw_p = 0.5 * erfc(statistic / sqrt(2))
    return {
        "expectancy_ci_95": [estimates[int(simulations * 0.025)], estimates[int(simulations * 0.975)]],
        "bootstrap_simulations": simulations,
        "bootstrap_seed": 42,
        "bootstrap_unit": "exchange-asset cluster",
        "raw_p": raw_p,
        "adjusted_p": min(1.0, raw_p * 36),
        "deflated_sharpe": sharpe - sqrt(2 * log(36)),
        "raw_trades": len(values),
        "clusters": len(clusters),
        "adequacy": "ADEQUATE" if len(values) >= 200 and len(clusters) >= 20 else "INSUFFICIENT SAMPLE",
    }


def build_report(repository: ShadowRepository, preview: bool = False) -> dict:
    protocol_record = repository.protocol(PROTOCOL_ID)
    if not protocol_record:
        raise RuntimeError("Prospective lock does not exist")
    protocol = json.loads(protocol_record.protocol_json)
    if protocol["strategy_config_hash"] != FROZEN_CONFIG_HASH:
        raise RuntimeError("Frozen config hash mismatch")
    locked_at = protocol_record.locked_at.replace(tzinfo=UTC) if protocol_record.locked_at.tzinfo is None else protocol_record.locked_at
    observed_days = (datetime.now(UTC) - locked_at).total_seconds() / 86400
    if observed_days < 30 and not preview:
        raise RuntimeError(f"Minimum 30-day observation is incomplete: {observed_days:.2f} days")
    trades = repository.closed_trades(PROTOCOL_ID)
    combinations = len(protocol["exchanges"]) * len(protocol["assets"])
    overall = summarize(trades, Decimal("1000") * combinations)
    by_exchange = {
        exchange: summarize([trade for trade in trades if trade.exchange == exchange], Decimal("1000") * len(protocol["assets"]))
        for exchange in protocol["exchanges"]
    }
    by_asset = {
        symbol: summarize([trade for trade in trades if trade.symbol == symbol], Decimal("1000") * len(protocol["exchanges"]))
        for symbol in protocol["assets"]
    }
    best_asset = max(by_asset, key=lambda item: by_asset[item]["net_pnl"])
    best_exchange = max(by_exchange, key=lambda item: by_exchange[item]["net_pnl"])
    without_asset = summarize([trade for trade in trades if trade.symbol != best_asset], Decimal("1000") * (combinations - len(protocol["exchanges"])))
    without_exchange = summarize([trade for trade in trades if trade.exchange != best_exchange], Decimal("1000") * (combinations - len(protocol["assets"])))
    stress = {label: summarize(trades, Decimal("1000") * combinations, multiple) for label, multiple in (("normal", Decimal("1")), ("1.25x", Decimal("1.25")), ("1.5x", Decimal("1.5")), ("2x", Decimal("2")))}
    stats = statistical_validation(trades)
    has_concentration_sample = bool(trades)
    concentrated = has_concentration_sample and (
        without_asset["net_pnl"] <= 0 or without_asset["net_pf"] <= 1
    )
    exchange_specific = has_concentration_sample and (
        without_exchange["net_pnl"] <= 0 or without_exchange["net_pf"] <= 1
    )
    economic = overall["net_pnl"] > 0 and overall["net_pf"] > 1 and overall["expectancy"] > 0 and overall["max_drawdown_pct"] < 20 and stress["1.25x"]["net_pnl"] > 0 and stress["1.25x"]["net_pf"] > 1 and not concentrated and not exchange_specific
    status = "PROSPECTIVE VALIDATION IN PROGRESS" if observed_days < 30 else "PROSPECTIVE EDGE CONFIRMED" if economic else "PROSPECTIVE VALIDATION FAILED"
    candle_rows = []
    for exchange in protocol["exchanges"]:
        for symbol in protocol["assets"]:
            candle_rows.extend(repository.prospective_candles(PROTOCOL_ID, exchange, symbol))
    holdout_data_hash = sha256("".join(sorted(row.data_hash for row in candle_rows)).encode()).hexdigest()
    return {
        "phase": "4I_PROSPECTIVE_LIVE_SHADOW",
        "status": status,
        "observed_days": observed_days,
        "minimum_days": 30,
        "strategy_version": protocol["strategy_version"],
        "config_hash": protocol["strategy_config_hash"],
        "protocol_hash": protocol_record.protocol_hash,
        "locked_at": locked_at,
        "generated_at": datetime.now(UTC),
        "holdout_data_hash": holdout_data_hash,
        "candle_count": len(candle_rows),
        "overall": overall,
        "by_exchange": by_exchange,
        "by_asset": by_asset,
        "without_best_asset": {
            "removed": best_asset if has_concentration_sample else None,
            "metrics": without_asset,
            "diagnosis": (
                "INSUFFICIENT SAMPLE"
                if not has_concentration_sample
                else "CONCENTRATED EDGE"
                if concentrated
                else "NOT CONCENTRATED"
            ),
        },
        "without_best_exchange": {
            "removed": best_exchange if has_concentration_sample else None,
            "metrics": without_exchange,
            "diagnosis": (
                "INSUFFICIENT SAMPLE"
                if not has_concentration_sample
                else "EXCHANGE-SPECIFIC EDGE"
                if exchange_specific
                else "NOT EXCHANGE-SPECIFIC"
            ),
        },
        "cost_stress": stress,
        "statistical_validation": stats,
        "next_stage": "CANDIDATE FOR PAPER TRADING" if status == "PROSPECTIVE EDGE CONFIRMED" else None,
        "full_trades": [
            {column.name: getattr(trade, column.name) for column in trade.__table__.columns}
            for trade in trades
        ],
        "live_trading_enabled": False,
        "real_orders_sent": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build immutable Phase 4I prospective report")
    parser.add_argument("--output", type=Path, default=Path("phase4i-prospective-report.json"))
    parser.add_argument("--preview", action="store_true")
    arguments = parser.parse_args()
    if arguments.output.exists() and not arguments.preview:
        raise RuntimeError("Immutable final prospective report already exists")
    report = build_report(ShadowRepository(), arguments.preview)
    arguments.output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"output": str(arguments.output.resolve()), "status": report["status"], "observed_days": report["observed_days"], "live_trading_enabled": False}, indent=2))


if __name__ == "__main__":
    main()
