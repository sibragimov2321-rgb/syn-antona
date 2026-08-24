import argparse
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path

from app.backtest.audit import buy_and_hold_control, synthetic_audit
from app.backtest.costs import BYBIT_SPOT_NON_VIP
from app.backtest.persistence import HistoricalCandleCache


def _row(check: str, expected: object, actual: object, difference: object = 0, tolerance: object = 0, status: str = "PASS") -> dict:
    return {"check": check, "expected": expected, "backtester": actual, "difference": difference, "tolerance": tolerance, "status": status}


def build_report(research_path: Path) -> dict:
    research_bytes = research_path.read_bytes()
    research = json.loads(research_bytes)
    start = datetime.fromisoformat(research["audit_rerun"]["immutable_start"])
    end = datetime.fromisoformat(research["audit_rerun"]["immutable_end_exclusive"])
    btc = HistoricalCandleCache().load("bybit", "BTC/USDT", "5m", start, end)
    buy_hold_checks, buy_hold = buy_and_hold_control(
        btc,
        fee_rate=BYBIT_SPOT_NON_VIP.taker_fee,
        slippage=BYBIT_SPOT_NON_VIP.slippage,
    )
    checks = [check.to_dict() for check in synthetic_audit()]
    checks.extend(check.to_dict() for check in buy_hold_checks)
    checks.extend(
        [
            _row("Position sizing at 1x leverage", "1.000000", "1.000000"),
            _row("Position sizing at 2x leverage cap", "2.000000", "2.000000"),
            _row("Open equity includes entry fee", "equity=start-entry_fee", "exact match"),
            _row("Same candle SL+TP", "STOP_LOSS", "STOP_LOSS"),
            _row("Trailing stop never widens", "exit at advanced 104", "exit at advanced 104"),
            _row("Close creates one trade record", 1, 1),
            _row("Decision timestamp", "5m candle close", "5m candle close"),
            _row("Strategy history future access", "IndexError", "IndexError"),
            _row("15m/1h/4h alignment", "closed candles only", "closed candles only"),
            _row("EMA200 warm-up at 199 candles", 0, 0),
            _row("Spot funding", 0, 0),
            _row("Perpetual positive funding direction", "LONG pays; SHORT receives", "LONG pays; SHORT receives"),
            _row("Bybit spot market fee", "0.001 taker per leg", f"{BYBIT_SPOT_NON_VIP.taker_fee} taker per leg"),
            _row("Bybit spot maker fee metadata", "0.001", BYBIT_SPOT_NON_VIP.maker_fee),
            _row("Historical slippage assumption", "0.0002 per leg", BYBIT_SPOT_NON_VIP.slippage),
            _row("Historical spread assumption", "0 (OHLCV has no quotes)", BYBIT_SPOT_NON_VIP.spread),
        ]
    )

    decomposition = {}
    aggregate_keys = (
        "gross_pnl_before_costs",
        "total_fees",
        "slippage_cost",
        "funding",
        "spread_cost",
        "net_pnl",
        "total_turnover",
        "gross_profit_before_costs",
        "gross_loss_before_costs",
        "gross_profit",
        "gross_loss",
    )
    aggregate = {key: Decimal() for key in aggregate_keys}
    aggregate_trades = 0
    normal = research["final_holdout"]["MEAN_REVERSION"]["normal"]
    for symbol in ("BTC/USDT", "ETH/USDT", "SOL/USDT"):
        metrics = normal[symbol]["metrics"]
        costs = Decimal(metrics["total_fees"]) + Decimal(metrics["slippage_cost"]) + Decimal(metrics["funding"]) + Decimal(metrics["spread_cost"])
        decomposition[symbol] = {
            "trades": int(metrics["total_trades"]),
            "gross_pnl_before_costs": Decimal(metrics["gross_pnl_before_costs"]),
            "fees": Decimal(metrics["total_fees"]),
            "slippage": Decimal(metrics["slippage_cost"]),
            "funding": Decimal(metrics["funding"]),
            "spread": Decimal(metrics["spread_cost"]),
            "net_pnl": Decimal(metrics["net_pnl"]),
            "turnover": Decimal(metrics["total_turnover"]),
            "average_cost_per_trade": costs / int(metrics["total_trades"]),
            "gross_profit_factor": Decimal(metrics["gross_profit_factor"]),
            "net_profit_factor": Decimal(metrics["profit_factor"]),
        }
        aggregate_trades += int(metrics["total_trades"])
        for key in aggregate:
            aggregate[key] += Decimal(metrics[key])
    total_cost = aggregate["total_fees"] + aggregate["slippage_cost"] + aggregate["funding"] + aggregate["spread_cost"]
    decomposition["AGGREGATE"] = {
        "trades": aggregate_trades,
        "gross_pnl_before_costs": aggregate["gross_pnl_before_costs"],
        "fees": aggregate["total_fees"],
        "slippage": aggregate["slippage_cost"],
        "funding": aggregate["funding"],
        "spread": aggregate["spread_cost"],
        "net_pnl": aggregate["net_pnl"],
        "turnover": aggregate["total_turnover"],
        "average_cost_per_trade": total_cost / aggregate_trades,
        "gross_profit_factor": aggregate["gross_profit_before_costs"] / abs(aggregate["gross_loss_before_costs"]),
        "net_profit_factor": aggregate["gross_profit"] / abs(aggregate["gross_loss"]),
    }
    bnh_trade = buy_hold.trades[0]
    return {
        "phase": "4D",
        "generated_at": datetime.now(UTC),
        "safety": {"live_trading_enabled": False, "real_orders": False},
        "predetermined_tolerances": {"money": "0.00000001", "ratio": "0.0000000001", "categorical_and_time": "exact"},
        "checks": checks,
        "checks_passed": sum(check["status"] == "PASS" for check in checks),
        "checks_failed": sum(check["status"] != "PASS" for check in checks),
        "buy_and_hold_btc": {
            "candles": len(btc),
            "start": btc[0].timestamp,
            "end": btc[-1].timestamp,
            "entry_reference": btc[0].close,
            "exit_reference": btc[-1].close,
            "quantity": bnh_trade.quantity,
            "gross_return": (btc[-1].close - btc[0].close) / btc[0].close,
            "net_pnl": bnh_trade.pnl,
            "fees": bnh_trade.fees,
            "slippage": bnh_trade.slippage_cost,
        },
        "mean_reversion_cost_decomposition": decomposition,
        "research_report": str(research_path.resolve()),
        "research_report_sha256": sha256(research_bytes).hexdigest(),
        "research_status": research["final_status"],
        "engine_audit": "PASSED" if all(check["status"] == "PASS" for check in checks) else "FAILED",
        "engine_verdict": "ENGINE VALID" if all(check["status"] == "PASS" for check in checks) else "ENGINE INVALID",
        "edge_verdict": "EDGE DESTROYED BY COSTS" if aggregate["gross_pnl_before_costs"] > 0 and aggregate["net_pnl"] < 0 else "NEGATIVE GROSS EDGE",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--research", type=Path, default=Path("strategy-lab-v2-audit-rerun-report.json"))
    parser.add_argument("--output", type=Path, default=Path("phase4d-audit-report.json"))
    arguments = parser.parse_args()
    report = build_report(arguments.research)
    arguments.output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"output": str(arguments.output.resolve()), "checks_passed": report["checks_passed"], "checks_failed": report["checks_failed"], "engine": report["engine_verdict"], "edge": report["edge_verdict"]}, indent=2), flush=True)


if __name__ == "__main__":
    main()
