import argparse
from hashlib import sha256
import json
from pathlib import Path


FINAL_STATUSES = {
    "NO ROBUST EDGE FOUND",
    "RESEARCH CANDIDATE",
    "CANDIDATE FOR PAPER TRADING",
}


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _identifier(record: dict) -> str:
    hypothesis = record["hypothesis"]
    return f"{hypothesis['family']}:{hypothesis['timeframe']}:v1"


def _compact_stress(candidate: dict | None) -> dict | None:
    if candidate is None:
        return None
    return {
        label: {
            key: values["summary"][key]
            for key in (
                "trades",
                "net_pnl",
                "net_pf",
                "expectancy",
                "max_drawdown_pct",
            )
        }
        for label, values in candidate["stress"].items()
    }


def finalize(protocol_path: Path, research_path: Path, stress_path: Path) -> dict:
    protocol = _read(protocol_path)
    research = _read(research_path)
    stress = _read(stress_path)
    protocol_hash = _hash(protocol_path)
    research_hash = _hash(research_path)
    if research["protocol_hash"] != protocol_hash:
        raise RuntimeError("Research report does not match the immutable protocol")
    if stress["protocol_sha256"] != protocol_hash:
        raise RuntimeError("Stress report does not match the immutable protocol")
    if stress["source_research_sha256"] != research_hash:
        raise RuntimeError("Stress report does not match the research report")
    if research["final_status"] not in FINAL_STATUSES:
        raise RuntimeError("Invalid Phase 4F final status")
    if research["protocol"]["final_holdout_opened"]:
        raise RuntimeError("This finalizer is for the gated, unopened-holdout outcome only")

    walks = {_identifier(record): record for record in research["walk_forward"]}
    stresses = stress["candidates"]
    family_ranking = []
    for validation in research["validation"]:
        identifier = _identifier(validation)
        walk = walks.get(identifier)
        evidence = walk or validation
        summary = evidence["summary"]
        family_ranking.append(
            {
                "strategy": validation["hypothesis"]["family"],
                "timeframe": validation["hypothesis"]["timeframe"],
                "economic_rationale": validation["hypothesis"]["rationale"],
                "deepest_evidence": "WALK_FORWARD" if walk else "VALIDATION",
                "validation_confirmed": bool(validation["confirmed"]),
                "walk_forward_confirmed": bool(walk and walk["confirmed"]),
                "walk_forward_positive_windows": (
                    walk["positive_windows"] if walk else None
                ),
                "trades": summary["trades"],
                "win_rate": summary["win_rate"],
                "gross_pnl": summary["gross_pnl"],
                "net_pnl": summary["net_pnl"],
                "fees": summary["fees"],
                "spread": summary["spread"],
                "slippage": summary["slippage"],
                "funding": summary["funding"],
                "net_pf": summary["net_pf"],
                "expectancy": summary["expectancy"],
                "sharpe": summary["sharpe"],
                "sortino": summary["sortino"],
                "max_drawdown_pct": summary["max_drawdown_pct"],
                "turnover": summary["turnover"],
                "average_holding_seconds": summary["average_holding_seconds"],
                "market_regimes": summary["regimes"],
                "assets": evidence["assets"],
                "multiple_testing": evidence["multiple_testing"],
                "cost_stress": _compact_stress(stresses.get(identifier)),
                "research_stage_result": (
                    "WALK_FORWARD_NOT_CONFIRMED"
                    if walk
                    else "VALIDATION_NOT_CONFIRMED"
                ),
            }
        )
    family_ranking.sort(
        key=lambda item: (
            item["deepest_evidence"] == "WALK_FORWARD",
            float(item["net_pf"]),
            float(item["expectancy"]),
        ),
        reverse=True,
    )
    for rank, item in enumerate(family_ranking, 1):
        item["rank"] = rank

    return {
        "phase": "4F_NEW_EDGE_DISCOVERY_FINAL",
        "final_status": research["final_status"],
        "decision": (
            "No hypothesis passed validation, rolling walk-forward stability, and the "
            "pre-locked multiple-testing gate. FINAL HOLDOUT therefore remained untouched."
        ),
        "dataset": {
            "exchange": "binance",
            "market_type": "spot",
            "symbols": protocol["symbols"],
            "timeframes": protocol["timeframes"],
            "real_historical_candles": True,
            "venue_specific": True,
            "phase4e_used_this_venue_dataset": False,
            "ranges": research["data"],
        },
        "research_protocol": research["protocol"],
        "anti_overfitting": research["anti_overfitting"],
        "funding_basis": research["funding_basis"],
        "family_ranking": family_ranking,
        "all_36_train_experiments": research["train_ranking"],
        "portfolio": {
            "status": "NOT_APPLICABLE",
            "reason": "No strategy was independently walk-forward-confirmed",
        },
        "holdout": {
            "opened": False,
            "candidate_locked": False,
            "parameter_changes_after_holdout": False,
            "reason": "No strategy passed the pre-locked walk-forward gate",
        },
        "lineage": {
            "protocol_lock": str(protocol_path.resolve()),
            "protocol_sha256": protocol_hash,
            "research_report": str(research_path.resolve()),
            "research_sha256": research_hash,
            "walk_stress_report": str(stress_path.resolve()),
            "walk_stress_sha256": _hash(stress_path),
        },
        "reproduction": {
            "research": (
                "python -m app.strategy_lab.run_phase4f --days 730 "
                "--protocol-lock phase4f-protocol-lock.json "
                "--output phase4f-new-edge-report.json"
            ),
            "walk_stress": (
                "python -m app.strategy_lab.run_phase4f_stress "
                "--protocol-lock phase4f-protocol-lock.json "
                "--research-report phase4f-new-edge-report.json "
                "--output phase4f-walk-stress-report.json"
            ),
        },
        "live_trading_enabled": False,
        "real_orders_sent": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Finalize the reproducible Phase 4F report"
    )
    parser.add_argument(
        "--protocol-lock", type=Path, default=Path("phase4f-protocol-lock.json")
    )
    parser.add_argument(
        "--research-report", type=Path, default=Path("phase4f-new-edge-report.json")
    )
    parser.add_argument(
        "--stress-report", type=Path, default=Path("phase4f-walk-stress-report.json")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("phase4f-final-report.json")
    )
    arguments = parser.parse_args()
    report = finalize(
        arguments.protocol_lock, arguments.research_report, arguments.stress_report
    )
    arguments.output.write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": str(arguments.output.resolve()),
                "status": report["final_status"],
                "holdout_opened": report["holdout"]["opened"],
                "live_trading_enabled": False,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
