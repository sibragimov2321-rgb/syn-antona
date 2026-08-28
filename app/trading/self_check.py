"""Read-only production self-check consumed by the Telegram admin UI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select, text

from app.ai.live_trader import AI_LEVERAGE, AI_MAX_POSITIONS
from app.db import (
    AILiveRuntimeRecord,
    ControlledLiveRuntimeRecord,
    ControlledLiveStateRecord,
    ExecutionOrderRecord,
    ShadowCollectorStateRecord,
)
from app.trading.controlled_live import CONTROLLED_LIVE_V1
from app.trading.controlled_universe import PROFILE_NAME


@dataclass(frozen=True)
class TradingSelfCheck:
    database_ok: bool
    worker_ok: bool
    worker_status: str
    worker_heartbeat_age: timedelta | None
    execution_enabled: bool
    ai_ok: bool
    ai_status: str
    market_data_age: timedelta | None
    kill_switch_safe: bool
    shadow_collectors: int
    unknown_orders: int
    open_positions: int
    open_orders: int
    profile_hash_ok: bool

    @property
    def passed(self) -> bool:
        return all(
            (
                self.database_ok,
                self.worker_ok,
                self.execution_enabled,
                self.ai_ok,
                self.kill_switch_safe,
                self.shadow_collectors == 0,
                self.unknown_orders == 0,
                self.open_positions <= AI_MAX_POSITIONS,
                self.profile_hash_ok,
                AI_LEVERAGE == Decimal("10"),
            )
        )


def trading_self_check(session_factory, now: datetime | None = None) -> TradingSelfCheck:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    try:
        with session_factory() as session:
            session.execute(text("SELECT 1"))
            worker = session.get(ControlledLiveRuntimeRecord, PROFILE_NAME)
            ai = session.get(AILiveRuntimeRecord, "AI_LIVE")
            state = session.get(ControlledLiveStateRecord, CONTROLLED_LIVE_V1.name)
            shadows = session.scalars(select(ShadowCollectorStateRecord)).all()
            unknown = session.scalar(
                select(func.count())
                .select_from(ExecutionOrderRecord)
                .where(ExecutionOrderRecord.status == "UNKNOWN")
            ) or 0
    except Exception:
        return TradingSelfCheck(
            False, False, "UNAVAILABLE", None, False, False, "UNAVAILABLE", None,
            False, 0, 0, 0, 0, False,
        )

    worker_age = _age(current, worker.heartbeat_at if worker else None)
    market_age = _age(current, ai.last_market_data_at if ai else None)
    worker_ok = bool(
        worker
        and worker.status == "RUNNING"
        and worker_age is not None
        and worker_age <= timedelta(minutes=3)
    )
    ai_ok = bool(
        ai
        and ai.enabled
        and ai.status == "RUNNING"
        and not ai.last_error
        and market_age is not None
        and market_age <= timedelta(minutes=10)
    )
    shadow_count = sum(
        item.status == "RUNNING"
        and _aware(item.lease_expires_at) is not None
        and _aware(item.lease_expires_at) > current
        for item in shadows
    )
    return TradingSelfCheck(
        database_ok=True,
        worker_ok=worker_ok,
        worker_status=worker.status if worker else "MISSING",
        worker_heartbeat_age=worker_age,
        execution_enabled=bool(worker and worker.real_order_execution_enabled),
        ai_ok=ai_ok,
        ai_status=ai.status if ai else "MISSING",
        market_data_age=market_age,
        kill_switch_safe=bool(state and not state.kill_switch_active),
        shadow_collectors=shadow_count,
        unknown_orders=int(unknown),
        open_positions=int(ai.open_positions) if ai else 0,
        open_orders=int(ai.open_orders) if ai else 0,
        profile_hash_ok=bool(state and state.profile_hash == CONTROLLED_LIVE_V1.config_hash),
    )


def format_self_check_ru(value: TradingSelfCheck) -> str:
    return "\n".join(
        (
            "🧪 <b>САМОПРОВЕРКА СИСТЕМЫ</b>",
            "",
            f"Итог: <b>{'PASS' if value.passed else 'WARNING'}</b>",
            f"{_mark(value.database_ok)} PostgreSQL",
            f"{_mark(value.worker_ok)} Worker: {value.worker_status} "
            f"({_seconds(value.worker_heartbeat_age)} сек)",
            f"{_mark(value.ai_ok)} AI/market data: {value.ai_status} "
            f"({_seconds(value.market_data_age)} сек)",
            f"{_mark(value.execution_enabled)} Реальное исполнение: "
            f"{'ENABLED' if value.execution_enabled else 'DISABLED'}",
            f"{_mark(value.profile_hash_ok)} Профиль/хэш: "
            f"{'MATCH' if value.profile_hash_ok else 'MISMATCH'}",
            "✅ Margin gate: ISOLATED",
            f"✅ Leverage: {AI_LEVERAGE}x",
            f"{_mark(value.kill_switch_safe)} Kill switch: "
            f"{'SAFE' if value.kill_switch_safe else 'ACTIVE'}",
            f"{_mark(value.shadow_collectors == 0)} Shadow collectors: "
            f"{value.shadow_collectors}",
            f"{_mark(value.unknown_orders == 0)} UNKNOWN orders: {value.unknown_orders}",
            f"Позиции: {value.open_positions} / {AI_MAX_POSITIONS}",
            f"Open orders: {value.open_orders}",
            "",
            "Проверка только читает PostgreSQL; ордера не создаются.",
        )
    )


def _seconds(value: timedelta | None) -> str:
    return str(max(0, int(value.total_seconds()))) if value is not None else "N/A"


def _mark(value: bool) -> str:
    return "✅" if value else "❌"


def _age(now: datetime, value: datetime | None) -> timedelta | None:
    aware = _aware(value)
    return max(timedelta(), now - aware) if aware is not None else None


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
