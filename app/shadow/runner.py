import argparse
import asyncio
from datetime import UTC, datetime, timedelta
import json
import logging
import os
from pathlib import Path
import socket
import uuid

from app.core.config import get_settings
from app.shadow.engine import PROTOCOL_ID, ProspectiveShadowEngine
from app.shadow.logging import configure_structured_logging, log_event
from app.shadow.market import PublicLiveMarketData
from app.shadow.notifier import ShadowNotifier
from app.shadow.protocol import verify_existing_lock
from app.shadow.recovery import recover_after_downtime
from app.shadow.repository import ShadowRepository
from app.shadow.status import snapshot_metrics
from app.shadow.warmup_bundle import load_warmup_bundle
from app.strategy_lab.phase4g import EXCHANGES


logger = logging.getLogger(__name__)


async def _notify_event(
    repository: ShadowRepository,
    notifier: ShadowNotifier,
    event_type: str,
    title: str,
    message: str,
    *,
    exchange: str | None = None,
    severity: str = "WARNING",
    dedupe_minutes: int | None = None,
) -> None:
    since = (
        datetime.now(UTC) - timedelta(minutes=dedupe_minutes)
        if dedupe_minutes
        else None
    )
    event_id, created = repository.record_system_event(
        PROTOCOL_ID,
        event_type,
        severity,
        message,
        exchange=exchange,
        dedupe_since=since,
    )
    if not created:
        return
    await notifier.system(title, message)
    repository.mark_event_alerted(event_id, datetime.now(UTC))


async def _update_exchange_health(
    repository: ShadowRepository,
    notifier: ShadowNotifier,
    protocol: dict,
    result: dict,
    offline_after: int,
) -> dict[str, str]:
    now = datetime.now(UTC)
    quote_times = repository.latest_quote_times(PROTOCOL_ID)
    existing = repository.exchange_health(PROTOCOL_ID)
    errors_by_exchange = {exchange: [] for exchange in protocol["exchanges"]}
    for error in result["errors"]:
        errors_by_exchange[error["exchange"]].append(error["error"])
    statuses = {}
    for exchange, errors in errors_by_exchange.items():
        previous_record = existing.get(exchange)
        if not errors:
            status, reason = "HEALTHY", ""
        elif len(errors) < len(protocol["assets"]):
            status, reason = "DEGRADED", errors[0]
        else:
            failures = (previous_record.consecutive_failures if previous_record else 0) + 1
            status = "OFFLINE" if failures >= offline_after else "DEGRADED"
            reason = errors[0]
        previous, current = repository.update_exchange_health(
            PROTOCOL_ID,
            exchange,
            status,
            reason,
            now,
            quote_times.get(exchange),
        )
        statuses[exchange] = current
        stale = any("STALE DATA" in error for error in errors)
        if stale:
            await _notify_event(
                repository,
                notifier,
                "STALE_DATA",
                "STALE DATA",
                f"{exchange.title()}: новый shadow signal запрещён до восстановления свежих данных.",
                exchange=exchange,
                dedupe_minutes=60,
            )
        if current == "OFFLINE" and previous != "OFFLINE":
            await _notify_event(
                repository,
                notifier,
                "EXCHANGE_OFFLINE",
                "EXCHANGE OFFLINE",
                f"{exchange.title()}: {reason}",
                exchange=exchange,
            )
        elif previous == "OFFLINE" and current == "HEALTHY":
            await _notify_event(
                repository,
                notifier,
                "EXCHANGE_RESTORED",
                "EXCHANGE RESTORED",
                f"{exchange.title()} снова получает свежие public market data.",
                exchange=exchange,
                severity="INFO",
            )
    return statuses


async def _send_pending_daily_report(
    repository: ShadowRepository, notifier: ShadowNotifier, protocol: dict
) -> None:
    today = datetime.now(UTC).date()
    record = repository.pending_daily_snapshot(PROTOCOL_ID, today)
    if not record:
        return
    locked_at = datetime.fromisoformat(protocol["locked_at"]).date()
    snapshot_day = record.snapshot_date.date()
    day_number = max(1, (snapshot_day - locked_at).days + 1)
    await notifier.daily(day_number, snapshot_metrics(record))
    repository.mark_daily_snapshot_sent(record.id, datetime.now(UTC))


async def run(arguments) -> None:
    settings = get_settings()
    settings.assert_safe_runtime()
    project_root = Path(__file__).resolve().parents[2]
    repository = ShadowRepository()
    market = PublicLiveMarketData(EXCHANGES)
    instance_id = os.getenv("SHADOW_INSTANCE_ID") or str(uuid.uuid4())
    notifier = ShadowNotifier(
        settings.telegram_bot_token,
        settings.admin_telegram_ids,
        repository=repository,
        protocol_id=PROTOCOL_ID,
    )
    lease_acquired = False
    try:
        repository.ping()
        if not arguments.protocol_lock.exists():
            raise RuntimeError(
                "PROTOCOL HASH MISMATCH: protocol lock file is missing; "
                "automatic creation is disabled"
            )
        existing = repository.protocol(PROTOCOL_ID)
        if not existing:
            raise RuntimeError("PROTOCOL HASH MISMATCH: database protocol record is missing")
        stored_protocol = json.loads(existing.protocol_json)
        cutoff = datetime.fromisoformat(stored_protocol["warmup"]["cutoff"])
        log_event(
            logger,
            logging.INFO,
            "warmup_loading",
            {"cutoff": cutoff, "purpose": "indicator initialization only"},
        )
        warmups = load_warmup_bundle(arguments.warmup_file, stored_protocol)
        protocol = verify_existing_lock(
            arguments.protocol_lock, repository, project_root, warmups
        )
        orphan_decisions = repository.repair_orphan_decisions(
            PROTOCOL_ID, protocol["strategy_config_hash"]
        )
        await notifier.deliver_pending()
        acquired, restart_count = repository.acquire_collector_lease(
            PROTOCOL_ID,
            instance_id,
            socket.gethostname(),
            os.getpid(),
            datetime.now(UTC),
            settings.shadow_lease_seconds,
        )
        if not acquired:
            raise RuntimeError("Another Shadow Collector holds the active database lease")
        lease_acquired = True
        if restart_count:
            await _notify_event(
                repository,
                notifier,
                "COLLECTOR_RESTARTED",
                "COLLECTOR RESTARTED",
                f"Collector продолжил протокол {existing.protocol_hash[:12]}…; перезапуск №{restart_count}.",
                severity="INFO",
            )

        startup_health = await market.health()
        quote_times = repository.latest_quote_times(PROTOCOL_ID)
        for exchange, health in startup_health.items():
            previous, current = repository.update_exchange_health(
                PROTOCOL_ID,
                exchange,
                health["status"],
                health.get("reason", ""),
                datetime.now(UTC),
                quote_times.get(exchange),
            )
            if current == "OFFLINE" and previous != "OFFLINE":
                await _notify_event(
                    repository,
                    notifier,
                    "EXCHANGE_OFFLINE",
                    "EXCHANGE OFFLINE",
                    f"{exchange.title()}: {health.get('reason', 'ошибка проверки при запуске')}",
                    exchange=exchange,
                )

        recovery = await recover_after_downtime(
            repository,
            market,
            protocol,
            through=datetime.now(UTC),
        )
        log_event(logger, logging.INFO, "downtime_recovery", recovery)
        engine = ProspectiveShadowEngine(
            protocol,
            repository,
            market,
            {key: list(candles) for key, candles in warmups.items()},
            notifier,
        )
        repository.heartbeat(
            PROTOCOL_ID,
            instance_id,
            "RUNNING",
            datetime.now(UTC),
            settings.shadow_lease_seconds,
        )
        log_event(
            logger,
            logging.INFO,
            "collector_started",
            {
                "protocol_id": protocol["id"],
                "locked_at": protocol["locked_at"],
                "strategy": protocol["strategy_version"],
                "config_hash": protocol["strategy_config_hash"],
                "protocol_hash": existing.protocol_hash,
                "health": startup_health,
                "recovery": recovery,
                "orphan_decisions_repaired": orphan_decisions,
                "live_trading_enabled": False,
                "real_orders_allowed": False,
            },
        )

        cycles = 0
        while True:
            result = await engine.cycle()
            await notifier.deliver_pending()
            cycles += 1
            statuses = await _update_exchange_health(
                repository,
                notifier,
                protocol,
                result,
                settings.shadow_offline_after_failures,
            )
            recovery_cutoff = datetime.now(UTC) - timedelta(
                seconds=settings.shadow_live_candle_grace_seconds
            )
            recovery = await recover_after_downtime(
                repository,
                market,
                protocol,
                through=recovery_cutoff,
            )
            if recovery["recovered"]:
                engine = ProspectiveShadowEngine(
                    protocol,
                    repository,
                    market,
                    {key: list(candles) for key, candles in warmups.items()},
                    notifier,
                )
            today = datetime.now(UTC).date()
            repository.save_daily_snapshot(
                PROTOCOL_ID, today, repository.daily_activity(PROTOCOL_ID, today)
            )
            await _send_pending_daily_report(repository, notifier, protocol)
            collector_status = (
                "RUNNING"
                if all(value == "HEALTHY" for value in statuses.values())
                else "DEGRADED"
            )
            repository.heartbeat(
                PROTOCOL_ID,
                instance_id,
                collector_status,
                datetime.now(UTC),
                settings.shadow_lease_seconds,
                json.dumps(result["errors"], default=str) if result["errors"] else None,
            )
            first_candle = repository.first_candle(PROTOCOL_ID)
            first_signal = repository.first_signal(PROTOCOL_ID)
            log_event(
                logger,
                logging.INFO,
                "collector_cycle",
                {
                    "cycle": cycles,
                    "errors": result["errors"],
                    "exchange_health": statuses,
                    "recovery": recovery,
                    "decisions": repository.decisions_count(PROTOCOL_ID),
                    "signals": repository.decisions_count(
                        PROTOCOL_ID, signals_only=True
                    ),
                    "first_closed_candle": (
                        {
                            "exchange": first_candle.exchange,
                            "symbol": first_candle.symbol,
                            "open_time": first_candle.candle_open_time,
                            "close_time": first_candle.candle_close_time,
                            "data_hash": first_candle.data_hash,
                        }
                        if first_candle
                        else None
                    ),
                    "first_shadow_signal": (
                        {
                            "exchange": first_signal.exchange,
                            "symbol": first_signal.symbol,
                            "direction": first_signal.decision,
                            "timestamp": first_signal.signal_timestamp,
                        }
                        if first_signal
                        else None
                    ),
                },
            )
            if arguments.once or arguments.max_cycles and cycles >= arguments.max_cycles:
                break
            if arguments.until_first_closed and first_candle:
                break
            await asyncio.sleep(arguments.poll_seconds)
    except Exception as error:
        event = (
            "protocol_mismatch"
            if "PROTOCOL HASH MISMATCH" in str(error)
            else "collector_failure"
        )
        log_event(
            logger,
            logging.ERROR,
            event,
            {"error": f"{type(error).__name__}: {error}"},
            exc_info=True,
        )
        title = (
            "PROTOCOL HASH MISMATCH"
            if event == "protocol_mismatch"
            else "SHADOW DATABASE/COLLECTOR FAILURE"
        )
        await notifier.system(title, str(error))
        raise
    finally:
        if lease_acquired:
            try:
                repository.release_collector_lease(
                    PROTOCOL_ID, instance_id, datetime.now(UTC)
                )
            except Exception:
                log_event(
                    logger,
                    logging.ERROR,
                    "collector_lease_release_failed",
                    exc_info=True,
                )
        await notifier.close()
        await market.close()


def main() -> None:
    configure_structured_logging()
    parser = argparse.ArgumentParser(
        description="Run resilient prospective public-data shadow validation"
    )
    parser.add_argument(
        "--protocol-lock", type=Path, default=Path("phase4i-prospective-lock.json")
    )
    parser.add_argument(
        "--warmup-file", type=Path, default=Path("phase4i-warmup.json.gz")
    )
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--until-first-closed", action="store_true")
    parser.add_argument("--max-cycles", type=int)
    arguments = parser.parse_args()
    try:
        asyncio.run(run(arguments))
    except KeyboardInterrupt:
        return
    except RuntimeError as error:
        if "PROTOCOL HASH MISMATCH" in str(error):
            raise SystemExit(78) from error
        raise


if __name__ == "__main__":
    main()
