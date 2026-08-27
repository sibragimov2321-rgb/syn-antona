"""Railway worker for Controlled Live only; contains no Shadow runtime."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging
import os
import signal
import uuid

from app.core.config import get_settings
from app.db import (
    ControlledLiveRuntimeRecord,
    SessionLocal,
    ShadowCollectorStateRecord,
)
from app.exchanges.bybit_v5_gateway import BybitV5OrderGateway
from app.shadow.engine import PROTOCOL_ID
from app.shadow.logging import configure_structured_logging, log_event
from app.shadow.notifier import ShadowNotifier
from app.shadow.runner import _controlled_execution_cycle
from app.trading.controlled_live import ControlledLiveRepository
from app.trading.controlled_signal_engine import ControlledLiveSignalEngine
from app.trading.controlled_universe import PROFILE_NAME
from app.trading.first_live_proposal import (
    FirstLiveProposalRepository,
    format_controlled_proposal_ru,
)
from app.trading.multi_symbol_scanner import (
    BybitMultiSymbolReadOnlyReader,
    MultiSymbolFirstProposalCoordinator,
    MultiSymbolScannerRepository,
)


logger = logging.getLogger(__name__)


def _heartbeat(instance_id: str, status: str, error: str | None = None) -> None:
    settings = get_settings()
    now = datetime.now(UTC)
    with SessionLocal.begin() as session:
        record = session.get(ControlledLiveRuntimeRecord, PROFILE_NAME)
        if record is None:
            record = ControlledLiveRuntimeRecord(
                profile_name=PROFILE_NAME,
                instance_id=instance_id,
                status=status,
                started_at=now,
                heartbeat_at=now,
                dry_run=settings.dry_run,
                live_trading_enabled=settings.live_trading_enabled,
                controlled_live_enabled=settings.controlled_live_enabled,
                manual_first_order_approved=settings.manual_first_order_approved,
                real_order_execution_enabled=False,
                deployment_id=os.getenv("RAILWAY_DEPLOYMENT_ID"),
                updated_at=now,
            )
            session.add(record)
        record.instance_id = instance_id
        record.status = status
        record.heartbeat_at = now
        record.dry_run = settings.dry_run
        record.live_trading_enabled = settings.live_trading_enabled
        record.controlled_live_enabled = settings.controlled_live_enabled
        record.manual_first_order_approved = settings.manual_first_order_approved
        record.real_order_execution_enabled = bool(
            not settings.dry_run
            and settings.live_trading_enabled
            and settings.controlled_live_enabled
            and settings.manual_first_order_approved
        )
        record.deployment_id = os.getenv("RAILWAY_DEPLOYMENT_ID")
        record.last_error = error[:1000] if error else None
        record.updated_at = now


def _mark_shadow_disabled() -> None:
    """Release the persisted lease without deleting any prospective data."""
    now = datetime.now(UTC)
    with SessionLocal.begin() as session:
        record = session.get(ShadowCollectorStateRecord, PROTOCOL_ID)
        if record is not None:
            record.status = "DISABLED"
            record.heartbeat_at = now
            record.last_db_write_at = now
            record.lease_expires_at = now
            record.last_error = None
            record.updated_at = now


async def run(poll_seconds: int = 60) -> None:
    settings = get_settings()
    settings.assert_safe_runtime()
    if settings.shadow_execution_enabled:
        raise RuntimeError("SHADOW_EXECUTION_ENABLED must be false in this worker")
    instance_id = str(uuid.uuid4())
    process_started_at = datetime.now(UTC)
    phase_repository = FirstLiveProposalRepository(SessionLocal)
    scanner_repository = MultiSymbolScannerRepository(SessionLocal)
    controlled_repository = ControlledLiveRepository(SessionLocal)
    signal_engine = ControlledLiveSignalEngine(SessionLocal)
    notifier = ShadowNotifier(settings.telegram_bot_token, settings.admin_telegram_ids)
    gateway = None
    coordinator = None
    current_task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    if current_task is not None:
        for item in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(item, current_task.cancel)
                installed.append(item)
            except (NotImplementedError, RuntimeError):
                break
    try:
        _mark_shadow_disabled()
        phase_repository.initialize()
        scanner_repository.initialize()
        controlled_repository.state()
        if not os.getenv("BYBIT_API_KEY") or not os.getenv("BYBIT_API_SECRET"):
            raise RuntimeError("Bybit credentials are missing")
        gateway = BybitV5OrderGateway.from_environment(SessionLocal)
        coordinator = MultiSymbolFirstProposalCoordinator(
            scanner_repository,
            phase_repository,
            BybitMultiSymbolReadOnlyReader.from_environment(),
            settings.admin_telegram_ids,
        )
        _heartbeat(instance_id, "RUNNING")
        log_event(
            logger,
            logging.INFO,
            "controlled_live_worker_started",
            {
                "shadow": "DISABLED",
                "shadow_watchdog": "OFF",
                "shadow_auto_restart": "OFF",
                "live_trading_enabled": settings.live_trading_enabled,
                "controlled_live_enabled": settings.controlled_live_enabled,
                "dry_run": settings.dry_run,
                "real_order_execution_enabled": bool(
                    not settings.dry_run
                    and settings.live_trading_enabled
                    and settings.controlled_live_enabled
                    and settings.manual_first_order_approved
                ),
            },
        )
        while True:
            try:
                signals = await signal_engine.cycle()
                proposal = await coordinator.cycle()
                if (
                    proposal.notify
                    and proposal.preview is not None
                    and proposal.admin_id is not None
                ):
                    delivered = await notifier.controlled_proposal(
                        format_controlled_proposal_ru(
                            proposal.preview, proposal.available_equity
                        ),
                        proposal.preview.proposal_id,
                        proposal.admin_id,
                    )
                    if delivered:
                        phase_repository.mark_notified(proposal.preview.proposal_id)
                await _controlled_execution_cycle(
                    phase_repository,
                    controlled_repository,
                    gateway,
                    notifier,
                    settings.admin_telegram_ids,
                    process_started_at,
                )
                _heartbeat(instance_id, "RUNNING")
                log_event(
                    logger,
                    logging.INFO,
                    "controlled_live_cycle",
                    {
                        "signals": signals,
                        "proposal_status": proposal.status,
                        "proposal_reason": proposal.reason,
                    },
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                _heartbeat(instance_id, "DEGRADED", f"{type(error).__name__}: {error}")
                log_event(
                    logger,
                    logging.ERROR,
                    "controlled_live_cycle_failed",
                    {"error": f"{type(error).__name__}: {error}"},
                    exc_info=True,
                )
            await asyncio.sleep(poll_seconds)
    finally:
        try:
            _heartbeat(instance_id, "STOPPED")
        except Exception:
            logger.exception("controlled_live_stop_heartbeat_failed")
        for item in installed:
            loop.remove_signal_handler(item)
        if coordinator is not None:
            await coordinator.close()
        if gateway is not None:
            await gateway.close()
        await signal_engine.close()
        await notifier.close()


def main() -> None:
    configure_structured_logging()
    asyncio.run(run())


if __name__ == "__main__":
    main()
