"""Read-only Phase 5E.1 status built exclusively from persisted observations.

The module never invokes the strategy, Risk Manager, proposal coordinator, or
execution gateway. "Check now" means re-reading PostgreSQL and GET-only Bybit
account endpoints; an open candle cannot enter this view.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import json
import os
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import (
    FirstLiveProposalStateRecord,
    ShadowCandleRecord,
    ShadowCollectorStateRecord,
    ShadowDecisionRecord,
    ShadowExchangeHealthRecord,
)
from app.exchanges.bybit_readonly import BybitMainnetReadOnlyClient
from app.shadow.engine import PROTOCOL_ID
from app.trading.controlled_live import CONTROLLED_LIVE_V1


SYMBOLS = ("SOL/USDT", "SOLUSDT")
EXCHANGE = "bybit"


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True)
class BybitWaitAccount:
    equity: Decimal
    open_positions: int
    open_orders: int


class WaitAccountReader(Protocol):
    async def read(self) -> BybitWaitAccount: ...

    async def close(self) -> None: ...


class BybitSignalWaitReader:
    """SOLUSDT account view backed by the strict GET-only allowlist client."""

    def __init__(self, client: BybitMainnetReadOnlyClient) -> None:
        self.client = client

    @classmethod
    def from_environment(cls) -> BybitSignalWaitReader:
        return cls(
            BybitMainnetReadOnlyClient(
                os.getenv("BYBIT_API_KEY", ""),
                os.getenv("BYBIT_API_SECRET", ""),
            )
        )

    async def read(self) -> BybitWaitAccount:
        await self.client.synchronize_time()
        wallet = await self.client.private_get(
            "/v5/account/wallet-balance",
            {"accountType": "UNIFIED", "coin": "USDT"},
        )
        positions = await self.client.private_get(
            "/v5/position/list", {"category": "linear", "symbol": "SOLUSDT"}
        )
        orders = await self.client.private_get(
            "/v5/order/realtime",
            {"category": "linear", "symbol": "SOLUSDT", "openOnly": 0, "limit": 50},
        )
        accounts = wallet.result.get("list") or []
        equity = Decimal(str(accounts[0].get("totalEquity") or "0")) if accounts else Decimal()
        open_positions = sum(
            Decimal(str(item.get("size") or "0")) > 0
            for item in positions.result.get("list") or []
        )
        return BybitWaitAccount(
            equity,
            open_positions,
            len(orders.result.get("list") or []),
        )

    async def close(self) -> None:
        await self.client.close()


@dataclass(frozen=True)
class SignalWaitSnapshot:
    phase_status: str
    started_at: datetime | None
    waiting_seconds: int
    last_closed_candle: datetime | None
    last_analysis: datetime | None
    analysis_age_seconds: int | None
    latest_candle_processed: bool
    decisions: int
    wait: int
    long: int
    short: int
    latest_decision: str | None
    factual_reasons: tuple[str, ...]
    equity: Decimal | None
    open_positions: int | None
    open_orders: int | None
    bybit_connection: str
    collector_status: str
    stale_data: bool


@dataclass(frozen=True)
class _PersistedWaitFacts:
    phase_status: str
    started_at: datetime | None
    latest_candle: ShadowCandleRecord | None
    latest_decision: ShadowDecisionRecord | None
    latest_candle_processed: bool
    counts: dict[str, int]
    collector: ShadowCollectorStateRecord | None
    bybit_health: ShadowExchangeHealthRecord | None


class SignalWaitStatusRepository:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self.session_factory = session_factory

    def facts(self, now: datetime) -> _PersistedWaitFacts:
        with self.session_factory() as session:
            phase = session.get(FirstLiveProposalStateRecord, CONTROLLED_LIVE_V1.name)
            started_at = _utc(phase.started_at) if phase else None
            latest_candle = session.scalar(
                select(ShadowCandleRecord)
                .where(
                    ShadowCandleRecord.protocol_id == PROTOCOL_ID,
                    ShadowCandleRecord.exchange == EXCHANGE,
                    ShadowCandleRecord.symbol.in_(SYMBOLS),
                    ShadowCandleRecord.candle_close_time <= now,
                )
                .order_by(
                    ShadowCandleRecord.candle_close_time.desc(),
                    ShadowCandleRecord.id.desc(),
                )
                .limit(1)
            )
            decision_filters = [
                ShadowDecisionRecord.protocol_id == PROTOCOL_ID,
                ShadowDecisionRecord.exchange == EXCHANGE,
                ShadowDecisionRecord.symbol.in_(SYMBOLS),
            ]
            if started_at is not None:
                decision_filters.append(ShadowDecisionRecord.created_at > started_at)
            latest_decision = session.scalar(
                select(ShadowDecisionRecord)
                .where(*decision_filters)
                .order_by(
                    ShadowDecisionRecord.created_at.desc(),
                    ShadowDecisionRecord.id.desc(),
                )
                .limit(1)
            )
            count_rows = session.execute(
                select(ShadowDecisionRecord.decision, func.count())
                .where(*decision_filters)
                .group_by(ShadowDecisionRecord.decision)
            ).all()
            counts = {"WAIT": 0, "LONG": 0, "SHORT": 0}
            counts.update({name: int(total) for name, total in count_rows})
            processed = False
            if latest_candle is not None:
                processed = bool(
                    session.scalar(
                        select(ShadowDecisionRecord.id).where(
                            ShadowDecisionRecord.protocol_id == PROTOCOL_ID,
                            ShadowDecisionRecord.exchange == latest_candle.exchange,
                            ShadowDecisionRecord.symbol == latest_candle.symbol,
                            ShadowDecisionRecord.candle_open_time
                            == latest_candle.candle_open_time,
                        )
                    )
                )
            collector = session.get(ShadowCollectorStateRecord, PROTOCOL_ID)
            bybit_health = session.scalar(
                select(ShadowExchangeHealthRecord).where(
                    ShadowExchangeHealthRecord.protocol_id == PROTOCOL_ID,
                    ShadowExchangeHealthRecord.exchange == EXCHANGE,
                )
            )
            for record in (latest_candle, latest_decision, collector, bybit_health):
                if record is not None:
                    session.expunge(record)
        return _PersistedWaitFacts(
            phase.status if phase else "NOT_INITIALIZED",
            started_at,
            latest_candle,
            latest_decision,
            processed,
            counts,
            collector,
            bybit_health,
        )


class SignalWaitStatusService:
    def __init__(
        self,
        repository: SignalWaitStatusRepository,
        account_reader: WaitAccountReader | None,
        *,
        heartbeat_max_age_seconds: int = 300,
        candle_stale_seconds: int = 7500,
    ) -> None:
        self.repository = repository
        self.account_reader = account_reader
        self.heartbeat_max_age_seconds = heartbeat_max_age_seconds
        self.candle_stale_seconds = candle_stale_seconds

    async def snapshot(self, now: datetime | None = None) -> SignalWaitSnapshot:
        current = now or datetime.now(UTC)
        facts = self.repository.facts(current)
        last_candle = (
            _utc(facts.latest_candle.candle_close_time)
            if facts.latest_candle
            else None
        )
        last_analysis = (
            _utc(facts.latest_decision.created_at)
            if facts.latest_decision
            else None
        )
        heartbeat = _utc(facts.collector.heartbeat_at) if facts.collector else None
        collector_running = bool(
            facts.collector
            and facts.collector.status in {"RUNNING", "DEGRADED"}
            and heartbeat
            and (current - heartbeat).total_seconds() <= self.heartbeat_max_age_seconds
        )
        stale = bool(
            last_candle
            and (current - last_candle).total_seconds() > self.candle_stale_seconds
        ) or last_candle is None

        account = None
        account_error = False
        if self.account_reader is not None:
            try:
                account = await self.account_reader.read()
            except Exception:
                account_error = True
        else:
            account_error = True

        persisted_health = facts.bybit_health.status if facts.bybit_health else "OFFLINE"
        if persisted_health == "OFFLINE":
            connection = "OFFLINE"
        elif account_error or stale or persisted_health == "DEGRADED":
            connection = "DEGRADED"
        else:
            connection = "HEALTHY"
        counts = facts.counts
        return SignalWaitSnapshot(
            phase_status=facts.phase_status,
            started_at=facts.started_at,
            waiting_seconds=(
                max(0, int((current - facts.started_at).total_seconds()))
                if facts.started_at
                else 0
            ),
            last_closed_candle=last_candle,
            last_analysis=last_analysis,
            analysis_age_seconds=(
                max(0, int((current - last_analysis).total_seconds()))
                if last_analysis
                else None
            ),
            latest_candle_processed=facts.latest_candle_processed,
            decisions=sum(counts.values()),
            wait=counts["WAIT"],
            long=counts["LONG"],
            short=counts["SHORT"],
            latest_decision=(
                facts.latest_decision.decision if facts.latest_decision else None
            ),
            factual_reasons=_factual_reasons(facts.latest_decision),
            equity=account.equity if account else None,
            open_positions=account.open_positions if account else None,
            open_orders=account.open_orders if account else None,
            bybit_connection=connection,
            collector_status="RUNNING" if collector_running else "STOPPED",
            stale_data=stale,
        )

    async def close(self) -> None:
        if self.account_reader is not None:
            await self.account_reader.close()


def _factual_reasons(decision: ShadowDecisionRecord | None) -> tuple[str, ...]:
    if decision is None:
        return ("После запуска Phase 5E решений SOLUSDT ещё нет.",)
    reasons = []
    if decision.risk_reason:
        reasons.append(str(decision.risk_reason))
    try:
        context = json.loads(decision.context_json or "{}")
    except (TypeError, ValueError):
        context = {}
    context_reason = context.get("reason")
    if context_reason and str(context_reason) not in {"WAIT", *reasons}:
        reasons.append(str(context_reason))
    if not reasons:
        reasons.append("Pipeline не сохранил дополнительную диагностическую причину.")
    return tuple(reasons)


def format_signal_wait_status(snapshot: SignalWaitSnapshot) -> str:
    latest = snapshot.latest_decision or "НЕТ"
    reason = _display_reason(snapshot.factual_reasons[0])
    phase_label = (
        "ЖДУ LONG/SHORT"
        if snapshot.phase_status == "WAITING_FOR_SIGNAL"
        else "СИГНАЛ НАЙДЕН — ЖДУ ПОДТВЕРЖДЕНИЯ"
        if snapshot.phase_status == "READY_FOR_USER_APPROVAL"
        else snapshot.phase_status
    )
    return (
        "⏳ <b>ОЖИДАНИЕ СИГНАЛА</b>\n\n"
        f"Статус: <b>{phase_label}</b>\n"
        "Пара: SOLUSDT\n"
        "Стратегия: Volatility Expansion 1H\n"
        f"Последняя закрытая 1H свеча: {_time(snapshot.last_closed_candle)}\n"
        f"Свеча обработана: {'ДА' if snapshot.latest_candle_processed else 'НЕТ'}\n"
        f"Время последнего анализа: {_time(snapshot.last_analysis)}\n"
        f"С последнего анализа: {_duration(snapshot.analysis_age_seconds)}\n\n"
        f"Всего решений после Phase 5E: {snapshot.decisions}\n"
        f"WAIT: {snapshot.wait}\n"
        f"LONG: {snapshot.long}\n"
        f"SHORT: {snapshot.short}\n"
        f"Последний результат: {latest} — {reason}\n\n"
        f"Equity Bybit: {_money(snapshot.equity)}\n"
        f"Open positions: {_number(snapshot.open_positions)}\n"
        f"Open orders: {_number(snapshot.open_orders)}\n"
        f"Bybit connection: {snapshot.bybit_connection}\n"
        f"Shadow Collector: {snapshot.collector_status}\n"
        f"Ожидание первого сигнала: {_duration(snapshot.waiting_seconds)}"
    )


def format_wait_reasons(snapshot: SignalWaitSnapshot) -> str:
    if snapshot.latest_decision != "WAIT":
        result = snapshot.latest_decision or "НЕТ"
        return (
            "📊 <b>ПОЧЕМУ WAIT?</b>\n\n"
            f"Последнее решение: {result}. Для него WAIT-диагностики нет."
        )
    lines = "\n".join(
        f"• {_display_reason(reason)}" for reason in snapshot.factual_reasons
    )
    return (
        "📊 <b>ПОЧЕМУ WAIT?</b>\n\n"
        f"Последняя закрытая свеча: {_time(snapshot.last_closed_candle)}\n"
        f"Фактические причины, сохранённые pipeline:\n{lines}\n\n"
        "Дополнительные причины не выводятся, если pipeline их не сохранил."
    )


def _time(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S UTC") if value else "НЕТ"


def _duration(seconds: int | None) -> str:
    if seconds is None:
        return "НЕТ"
    days, remainder = divmod(max(0, seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    return f"{days}д {hours}ч {minutes}м" if days else f"{hours}ч {minutes}м"


def _money(value: Decimal | None) -> str:
    return f"{value} USDT" if value is not None else "НЕДОСТУПНО"


def _number(value: int | None) -> str:
    return str(value) if value is not None else "НЕДОСТУПНО"


def _display_reason(value: str) -> str:
    if value == "No frozen signal":
        return "зафиксированная стратегия не сформировала сигнал (pipeline: No frozen signal)"
    return value
