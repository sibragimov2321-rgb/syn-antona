import argparse
from hashlib import sha256
import json
from pathlib import Path


def finalize(source: Path, output: Path) -> dict:
    source_bytes = source.read_bytes()
    report = json.loads(source_bytes)
    normal = report["final_holdout_stress"]["normal"]["result"]
    stress_results = {
        label: {
            "trades": values["result"]["trades"],
            "net_pnl": values["result"]["net_pnl"],
            "net_pf": values["result"]["net_pf"],
            "net_expectancy": values["result"]["net_expectancy"],
            "return_pct": values["result"]["return_pct"],
        }
        for label, values in report["final_holdout_stress"].items()
    }
    report["final_candidate_table"] = [
        {
            "candidate": normal["candidate"],
            "timeframe": normal["timeframe"],
            "execution": normal["execution"],
            "trades": normal["trades"],
            "gross_pnl": normal["gross_pnl"],
            "fees": normal["fees"],
            "spread": normal["spread"],
            "slippage": normal["slippage"],
            "net_pnl": normal["net_pnl"],
            "gross_pf": normal["gross_pf"],
            "net_pf": normal["net_pf"],
            "gross_expectancy_per_trade": normal["gross_expectancy"],
            "net_expectancy_per_trade": normal["net_expectancy"],
            "max_drawdown_pct": normal["max_drawdown_pct"],
            "turnover": normal["turnover"],
            "average_holding_seconds": normal["average_holding_seconds"],
            "BTC_result": normal["BTC_result"],
            "ETH_result": normal["ETH_result"],
            "SOL_result": normal["SOL_result"],
            "stress_results": stress_results,
            "decision": report["final_status"],
        }
    ]
    report["finalization"] = {
        "source_research_report": str(source.resolve()),
        "source_research_sha256": sha256(source_bytes).hexdigest(),
        "holdout_reexecuted": False,
        "research_parameters_changed": False,
    }
    output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("phase4e-cost-aware-report.json"))
    parser.add_argument("--output", type=Path, default=Path("phase4e-final-report.json"))
    arguments = parser.parse_args()
    report = finalize(arguments.source, arguments.output)
    print(json.dumps({"output": str(arguments.output.resolve()), "status": report["final_status"], "holdout_reexecuted": False}, indent=2), flush=True)


if __name__ == "__main__":
    main()
