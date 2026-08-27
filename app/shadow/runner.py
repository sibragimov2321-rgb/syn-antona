import argparse
import asyncio
from datetime import UTC, datetime, timedelta
import json
import logging
import os
from pathlib import Path
import signal
import socket
import uuid

from app.core.config import get_settings
from app.db import SessionLocal
from app.exchanges.bybit_v5_gateway import BybitV5OrderGateway
from app.shadow.engine import PROTOCOL_ID, ProspectiveShadowEngine
from app.shadow.logging import configure_structured_logging, log_event
from app.shadow.market import PublicLiveMarketData
from app.shadow.notifier import ShadowNotifier
from app.shadow.protocol import verify_existing_lock
from app.shadow.recovery import recover_after_downtime
from app.shadow.repository import ShadowRepository
from app.shadow.signal_wait_status import (
    BybitSignalWaitReader,
    SignalWaitStatusRepository,
    SignalWaitStatusService,
)
from app.shadow.status import snapshot_metrics
from app.shadow.warmup_bundle import load_warmup_bundle
from app.strategy_lab.phase4g import EXCHANGES
from app.trading.controlled_live import (
    ArmingGates,
    ControlledLiveRepository,
    ManualExecutionService,
    ReconciliationRequired,
)
from app.trading.first_live_proposal import (
    FirstLiveProposalRepository,
    format_controlled_proposal_ru,
    preview_from_record,
)
from app.trading.multi_symbol_scanner import (
    BybitMultiSymbolReadOnlyReader,
    MultiSymbolFirstProposalCoordinator,
    MultiSymbolScannerRepository,
)


logger = logging.getLogger(__name__)


async def _wait_for_collector_lease(
    repository: ShadowRepository,
    instance_id: str,
    host: str,
    pid: int,
    lease_seconds: int,
    *,
    poll_seconds: float = 5.0,
) -> int:
    """Wait as a passive standby during Railway's rolling container handoff.

    A busy lease is an expected deployment state, not a collector crash. Only the
    lease owner may proceed to scanner, notifier, or execution initialization.
    """
    wait_logged = False
    while True:
        acquired, restart_count = repository.acquire_collector_lease(
            PROTOCOL_ID,
            instance_id,
            host,
            pid,
            datetime.now(UTC),
            lease_seconds,
        )
        if acquired:
            if wait_logged:
                log_event(
                    logger,
                    logging.INFO,
                    "collector_lease_handoff_complete",
                    {"instance_id": instance_id},
                )
            return restart_count
        if not wait_logged:
            state = repository.collector_state(PROTOCOL_ID)
            log_event(
                logger,
                logging.INFO,
                "collector_lease_standby",
                {
                    "instance_id": instance_id,
                    "active_instance_id": state.instance_id if state else None,
                    "reason": "waiting for graceful Railway deployment handoff",
                },
            )
            wait_logged = True
        await asyncio.sleep(poll_seconds)


async def _controlled_execution_cycle(
    phase_repository: FirstLiveProposalRepository,
    controlled_repository: ControlledLiveRepository,
    gateway: BybitV5OrderGateway,
    notifier: ShadowNotifier,
    admin_ids: set[int],
    process_started_at: datetime,
) -> None:
    """Execute only an exact, persisted admin approval after all env gates arm."""
    state = phase_repository.state()
    if not state.proposal_id:
        return
    record = phase_repository.proposal(state.proposal_id)
    preview = preview_from_record(record)
    if record is None or preview is None:
        return
    service = ManualExecutionService(controlled_repository, gateway, admin_ids)
    if state.status == "EXECUTED_AWAITING_RESTART_VALIDATION":
        state_updated = (
            state.updated_at.replace(tzinfo=UTC)
            if state.updated_at.tzinfo is None
            else state.updated_at
        )
        if process_started_at <= state_updated:
            return
        reconciliation = await service.reconcile(preview)
        if reconciliation.get("status") == "MATCH":
            controlled_repository.enable_automatic_execution_after_first_validation()
            phase_repository.mark_execution_status("FIRST_EXECUTION_VALIDATED")
            await notifier.system(
                "CONTROLLED LIVE RESTART RECOVERY",
                "Первая позиция восстановлена после restart; ledger/position reconciliation MATCH.",
            )
        else:
            controlled_repository.activate_kill_switch()
            phase_repository.mark_execution_status(
                "HALTED_RECONCILIATION_MISMATCH", json.dumps(reconciliation, default=str)
            )
            await notifier.system(
                "CONTROLLED LIVE HALTED",
                "Reconciliation после restart не совпал. Новые сделки запрещены.",
            )
        return
    if state.status != "APPROVED_FOR_EXECUTION":
        return
    settings = get_settings()
    gates = ArmingGates.from_environment()
    if settings.dry_run:
        return
    try:
        automatic = controlled_repository.state().automatic_execution_enabled
        gates.require_all(expected_symbol=preview.symbol)
        fill = await service.execute_first_order(
            record.admin_telegram_id,
            preview,
            account_id="bybit-mainnet-unified",
            gates=gates,
        )
        reconciliation = None
        for _ in range(3):
            reconciliation = await service.reconcile(preview)
            if reconciliation.get("status") == "MATCH":
                break
            await asyncio.sleep(0.5)
        if reconciliation is None or reconciliation.get("status") != "MATCH":
            raise ReconciliationRequired("Immediate post-fill reconciliation mismatch")
        if automatic:
            phase_repository.mark_execution_status("AUTO_POSITION_OPEN")
            await notifier.system(
                "CONTROLLED LIVE EXECUTION",
                f"{preview.symbol}: fill {fill.filled_quantity}; native SL/TP установлены; "
                "reconciliation MATCH.",
            )
        else:
            phase_repository.mark_execution_status(
                "EXECUTED_AWAITING_RESTART_VALIDATION"
            )
            await notifier.system(
                "CONTROLLED LIVE FIRST EXECUTION",
                f"{preview.symbol}: fill {fill.filled_quantity}; native SL/TP установлены; "
                "reconciliation MATCH. Требуется Railway restart validation.",
            )
    except Exception as error:
        controlled_repository.activate_kill_switch()
        phase_repository.mark_execution_status(
            "HALTED_EXECUTION_FAILURE", f"{type(error).__name__}: {error}"
        )
        await notifier.system(
            "CONTROLLED LIVE HALTED",
            f"{type(error).__name__}: новые сделки запрещены; требуется ручная проверка.",
        )


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
    process_started_at = datetime.now(UTC)
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
    current_task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    if current_task is not None:
        for shutdown_signal in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(shutdown_signal, current_task.cancel)
                installed_signals.append(shutdown_signal)
            except (NotImplementedError, RuntimeError):
                # Windows event loops and embedded runtimes may not expose signal handlers.
                break
    lease_acquired = False
    proposal_repository = FirstLiveProposalRepository(SessionLocal)
    proposal_coordinator = None
    scanner_repository = MultiSymbolScannerRepository(SessionLocal)
    controlled_repository = ControlledLiveRepository(SessionLocal)
    execution_gateway = None
    signal_wait_service = None
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
        restart_count = await _wait_for_collector_lease(
            repository,
            instance_id,
            socket.gethostname(),
            os.getpid(),
            settings.shadow_lease_seconds,
        )
        lease_acquired = True
        repository.record_collector_runtime(
            PROTOCOL_ID,
            instance_id,
            dry_run=settings.dry_run,
            live_trading_enabled=settings.live_trading_enabled,
            controlled_live_enabled=settings.controlled_live_enabled,
            manual_first_order_approved=settings.manual_first_order_approved,
            deployment_id=os.getenv("RAILWAY_DEPLOYMENT_ID"),
            replica_id=os.getenv("RAILWAY_REPLICA_ID"),
            now=datetime.now(UTC),
        )
        # Only the active lease owner may initialize scanner/execution services.
        # A rolling-deploy standby remains a passive protocol/lease verifier.
        proposal_repository.initialize()
        scanner_repository.initialize()
        controlled_repository.state()
        if os.getenv("BYBIT_API_KEY") and os.getenv("BYBIT_API_SECRET"):
            execution_gateway = BybitV5OrderGateway.from_environment(SessionLocal)
            proposal_coordinator = MultiSymbolFirstProposalCoordinator(
                scanner_repository,
                proposal_repository,
                BybitMultiSymbolReadOnlyReader.from_environment(),
                settings.admin_telegram_ids,
            )
            signal_wait_service = SignalWaitStatusService(
                SignalWaitStatusRepository(SessionLocal),
                BybitSignalWaitReader.from_environment(),
                heartbeat_max_age_seconds=settings.shadow_heartbeat_max_age_seconds,
            )
        orphan_decisions = repository.repair_orphan_decisions(
            PROTOCOL_ID, protocol["strategy_config_hash"]
        )
        await notifier.deliver_pending()
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
                "live_trading_enabled": settings.live_trading_enabled,
                "real_orders_allowed": bool(
                    settings.live_trading_enabled
                    and settings.controlled_live_enabled
                    and settings.manual_first_order_approved
                    and not settings.dry_run
                ),
            },
        )

        cycles = 0
        while True:
            result = await engine.cycle()
            if signal_wait_service is not None:
                await signal_wait_service.snapshot()
            if proposal_coordinator is not None:
                proposal_result = await proposal_coordinator.cycle()
                if (
                    proposal_result.notify
                    and proposal_result.preview is not None
                    and proposal_result.admin_id is not None
                ):
                    delivered = await notifier.controlled_proposal(
                        format_controlled_proposal_ru(
                            proposal_result.preview,
                            proposal_result.available_equity,
                        ),
                        proposal_result.preview.proposal_id,
                        proposal_result.admin_id,
                    )
                    if delivered:
                        proposal_repository.mark_notified(
                            proposal_result.preview.proposal_id
                        )
                log_event(
                    logger,
                    logging.INFO,
                    "first_controlled_live_proposal",
                    {
                        "status": proposal_result.status,
                        "reason": proposal_result.reason,
                        "real_orders_sent": 0,
                    },
                )
            if execution_gateway is not None:
                await _controlled_execution_cycle(
                    proposal_repository,
                    controlled_repository,
                    execution_gateway,
                    notifier,
                    settings.admin_telegram_ids,
                    process_started_at,
                )
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
        for shutdown_signal in installed_signals:
            loop.remove_signal_handler(shutdown_signal)
        await notifier.close()
        if proposal_coordinator is not None:
            await proposal_coordinator.close()
        if execution_gateway is not None:
            await execution_gateway.close()
        if signal_wait_service is not None:
            await signal_wait_service.close()
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
    except asyncio.CancelledError:
        return
    except KeyboardInterrupt:
        return
    except RuntimeError as error:
        if "PROTOCOL HASH MISMATCH" in str(error):
            raise SystemExit(78) from error
        raise


if __name__ == "__main__":
    main()
