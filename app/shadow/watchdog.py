import argparse
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path

from app.core.config import get_settings
from app.shadow.engine import PROTOCOL_ID
from app.shadow.protocol import canonical_json
from app.shadow.repository import ShadowRepository
from app.strategy_lab.phase4g import FROZEN_CONFIG_HASH


def check_health(
    repository: ShadowRepository,
    protocol_lock: Path,
    *,
    heartbeat_max_age: int,
    quote_max_age: int,
    candle_max_age: int,
    now: datetime | None = None,
) -> dict:
    current = now or datetime.now(UTC)
    issues = []
    critical = []
    repository.ping()
    record = repository.protocol(PROTOCOL_ID)
    if not record or not protocol_lock.exists():
        raise RuntimeError("PROTOCOL HASH MISMATCH: lock file/database record missing")
    protocol = json.loads(protocol_lock.read_text(encoding="utf-8"))
    file_hash = sha256(canonical_json(protocol)).hexdigest()
    if file_hash != record.protocol_hash:
        raise RuntimeError("PROTOCOL HASH MISMATCH: file/database hash differs")
    if protocol.get("strategy_config_hash") != FROZEN_CONFIG_HASH:
        raise RuntimeError("PROTOCOL HASH MISMATCH: frozen config differs")

    state = repository.collector_state(PROTOCOL_ID)
    heartbeat_age = None
    if not state:
        critical.append("collector heartbeat missing")
    else:
        heartbeat = state.heartbeat_at
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=UTC)
        heartbeat_age = max(0.0, (current - heartbeat).total_seconds())
        if heartbeat_age > heartbeat_max_age:
            critical.append(f"collector heartbeat stale: {heartbeat_age:.1f}s")
        if state.status not in {"STARTING", "RUNNING", "DEGRADED"}:
            critical.append(f"collector process is {state.status}")

    quote_times = repository.latest_quote_times(PROTOCOL_ID)
    health = repository.exchange_health(PROTOCOL_ID)
    exchange_status = {}
    for exchange in protocol["exchanges"]:
        quote = quote_times.get(exchange)
        quote_age = (current - quote).total_seconds() if quote else None
        persisted = health.get(exchange)
        status = persisted.status if persisted else "OFFLINE"
        if quote_age is None or quote_age > quote_max_age:
            status = "OFFLINE" if status == "OFFLINE" else "DEGRADED"
            issues.append(f"{exchange} quote stale or missing")
        exchange_status[exchange] = {
            "status": status,
            "last_quote": quote,
            "quote_age_seconds": quote_age,
        }
    if all(values["status"] == "OFFLINE" for values in exchange_status.values()):
        critical.append("all exchanges offline")

    candle = repository.latest_candle(PROTOCOL_ID)
    candle_age = None
    if candle:
        close_time = candle.candle_close_time
        if close_time.tzinfo is None:
            close_time = close_time.replace(tzinfo=UTC)
        candle_age = max(0.0, (current - close_time).total_seconds())
        if candle_age > candle_max_age:
            issues.append(f"last closed candle stale: {candle_age:.1f}s")
    else:
        issues.append("last closed candle missing")

    return {
        "status": "OFFLINE" if critical else "DEGRADED" if issues else "HEALTHY",
        "protocol": "LOCKED",
        "protocol_hash": record.protocol_hash,
        "config_hash": record.config_hash,
        "live_trading_enabled": False,
        "collector_status": state.status if state else "OFFLINE",
        "heartbeat_age_seconds": heartbeat_age,
        "last_candle_age_seconds": candle_age,
        "exchanges": exchange_status,
        "issues": issues,
        "critical": critical,
    }


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Phase 4I collector watchdog check")
    parser.add_argument(
        "--protocol-lock", type=Path, default=Path("phase4i-prospective-lock.json")
    )
    parser.add_argument(
        "--heartbeat-max-age",
        type=int,
        default=settings.shadow_heartbeat_max_age_seconds,
    )
    parser.add_argument("--quote-max-age", type=int, default=300)
    parser.add_argument("--candle-max-age", type=int, default=7500)
    arguments = parser.parse_args()
    try:
        result = check_health(
            ShadowRepository(),
            arguments.protocol_lock,
            heartbeat_max_age=arguments.heartbeat_max_age,
            quote_max_age=arguments.quote_max_age,
            candle_max_age=arguments.candle_max_age,
        )
        print(json.dumps(result, indent=2, default=str, sort_keys=True))
        if result["critical"]:
            raise SystemExit(1)
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "OFFLINE",
                    "error": f"{type(error).__name__}: {error}",
                    "live_trading_enabled": False,
                },
                sort_keys=True,
            )
        )
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
