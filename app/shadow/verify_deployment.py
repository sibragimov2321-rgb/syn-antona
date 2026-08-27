import argparse
from datetime import UTC, datetime
import json
from pathlib import Path

from sqlalchemy import func, select

from app.core.config import get_settings
from app.shadow.engine import PROTOCOL_ID
from app.shadow.repository import ShadowRepository
from app.shadow.watchdog import check_health
from app.strategy_lab.phase4g import FROZEN_CONFIG_HASH
from app.db import ShadowCandleRecord, ShadowDecisionRecord, ShadowQuoteRecord


def _utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def verify_deployment(
    repository: ShadowRepository,
    protocol_lock: Path,
    manifest_path: Path,
    *,
    require_new_candle: bool = False,
) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    protocol_file = json.loads(protocol_lock.read_text(encoding="utf-8"))
    record = repository.protocol(PROTOCOL_ID)
    failures = []
    if not record:
        failures.append("database protocol missing")
        return {"status": "FAILED", "failures": failures}
    if record.protocol_hash != manifest["protocol_hash"]:
        failures.append("protocol hash changed")
    if record.config_hash != FROZEN_CONFIG_HASH:
        failures.append("config hash changed")
    if protocol_file["locked_at"] != manifest["locked_at"]:
        failures.append("original protocol timestamp changed")
    locked_at = _utc(record.locked_at)
    if locked_at != datetime.fromisoformat(manifest["locked_at"]).astimezone(UTC):
        failures.append("database protocol timestamp changed")

    with repository.session_factory() as session:
        counts = {
            "shadow_candles": int(
                session.scalar(
                    select(func.count()).select_from(ShadowCandleRecord).where(
                        ShadowCandleRecord.protocol_id == PROTOCOL_ID
                    )
                )
                or 0
            ),
            "shadow_quotes": int(
                session.scalar(
                    select(func.count()).select_from(ShadowQuoteRecord).where(
                        ShadowQuoteRecord.protocol_id == PROTOCOL_ID
                    )
                )
                or 0
            ),
            "shadow_decisions": int(
                session.scalar(
                    select(func.count()).select_from(ShadowDecisionRecord).where(
                        ShadowDecisionRecord.protocol_id == PROTOCOL_ID
                    )
                )
                or 0
            ),
        }
        duplicate_candles = session.execute(
            select(func.count())
            .select_from(ShadowCandleRecord)
            .where(ShadowCandleRecord.protocol_id == PROTOCOL_ID)
            .group_by(
                ShadowCandleRecord.exchange,
                ShadowCandleRecord.symbol,
                ShadowCandleRecord.candle_open_time,
            )
            .having(func.count() > 1)
        ).all()
        duplicate_decisions = session.execute(
            select(func.count())
            .select_from(ShadowDecisionRecord)
            .where(ShadowDecisionRecord.protocol_id == PROTOCOL_ID)
            .group_by(
                ShadowDecisionRecord.exchange,
                ShadowDecisionRecord.symbol,
                ShadowDecisionRecord.candle_open_time,
            )
            .having(func.count() > 1)
        ).all()
    for table, count in counts.items():
        baseline = manifest["tables"][table]["rows"]
        if count < baseline:
            failures.append(f"{table} lost rows: {count} < {baseline}")
    if duplicate_candles:
        failures.append("duplicate candles detected")
    if duplicate_decisions:
        failures.append("duplicate decisions detected")

    latest = repository.latest_candle(PROTOCOL_ID)
    latest_close = _utc(latest.candle_close_time) if latest else None
    baseline_close = (
        _utc(datetime.fromisoformat(manifest["baseline_last_candle_close"]))
        if manifest.get("baseline_last_candle_close")
        else None
    )
    if require_new_candle and (not latest_close or latest_close <= baseline_close):
        failures.append("no new closed 1h candle after PostgreSQL resume")
    settings = get_settings()
    health = check_health(
        repository,
        protocol_lock,
        heartbeat_max_age=settings.shadow_heartbeat_max_age_seconds,
        quote_max_age=300,
        candle_max_age=7500,
    )
    if health["critical"]:
        failures.extend(health["critical"])
    if any(
        values["status"] != "HEALTHY" for values in health["exchanges"].values()
    ):
        failures.append("not all four exchanges are HEALTHY")
    return {
        "status": "VERIFIED" if not failures else "FAILED",
        "original_protocol_timestamp_preserved": not any(
            "timestamp" in item for item in failures
        ),
        "config_hash_preserved": record.config_hash == FROZEN_CONFIG_HASH,
        "protocol_hash": record.protocol_hash,
        "existing_records_preserved": all(
            counts[name] >= manifest["tables"][name]["rows"] for name in counts
        ),
        "collector_resumed": health["collector_status"] in {"RUNNING", "DEGRADED"},
        "exchange_health": {
            name: values["status"] for name, values in health["exchanges"].items()
        },
        "latest_closed_1h_candle": latest_close,
        "new_closed_candle_after_transfer": bool(
            latest_close and baseline_close and latest_close > baseline_close
        ),
        "duplicate_candles": len(duplicate_candles),
        "duplicate_decisions": len(duplicate_decisions),
        "counts": counts,
        "live_trading_enabled": False,
        "failures": failures,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify Phase 4I PostgreSQL deployment")
    parser.add_argument(
        "--protocol-lock", type=Path, default=Path("phase4i-prospective-lock.json")
    )
    parser.add_argument(
        "--manifest", type=Path, default=Path("phase4i-postgres-transfer-manifest.json")
    )
    parser.add_argument("--require-new-candle", action="store_true")
    arguments = parser.parse_args()
    result = verify_deployment(
        ShadowRepository(),
        arguments.protocol_lock,
        arguments.manifest,
        require_new_candle=arguments.require_new_candle,
    )
    print(json.dumps(result, indent=2, default=str, sort_keys=True))
    if result["status"] != "VERIFIED":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
