"""One-shot Phase 5E proposal built from a new, natural frozen signal.

This module has no order-submission call.  Its only exchange dependency is a
read-only snapshot method, and Telegram approval only changes durable proposal
state.  The production execution gateway therefore remains unreachable here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
import json
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import (
    ControlledLiveProposalRecord,
    ControlledLiveStateRecord,
    ExecutionOrderRecord,
    FirstLiveProposalStateRecord,
    ShadowDecisionRecord,
    ShadowTradeRecord,
)
from app.exchanges.models import InstrumentRules, OrderSide
from app.shadow.engine import PROTOCOL_ID
from app.strategy_lab.phase4g import FROZEN_CONFIG_HASH
from app.trading.controlled_live import (
    CONTROLLED_LIVE_V1,
    CONTROLLED_LIVE_V1_FIRST_INSTRUMENT,
    ControlledLiveBlocked,
    ControlledLiveRepository,
    ControlledProposalReadSnapshot,
    ControlledRiskSnapshot,
    ManualExecutionPreview,
    ManualOrderInputs,
    build_manual_preview,
)


FROZEN_SIGNAL_SOURCE = "FROZEN_STRATEGY_ADMIN_REVIEW"
EXPECTED_QUANTITY = Decimal("0.1")


class ReadOnlyProposalGateway(Protocol):
    dry_run: bool

    async def controlled_proposal_snapshot(
        self, symbol: str
    ) -> ControlledProposalReadSnapshot: ...


class CandidateRejected(ControlledLiveBlocked):
    """A valid frozen signal that cannot satisfy current controlled-live rules."""


@dataclass(frozen=True)
class FrozenSignalCandidate:
    decision: ShadowDecisionRecord
    trade: ShadowTradeRecord


@dataclass(frozen=True)
class ProposalCycleResult:
    status: str
    reason: str
    preview: ManualExecutionPreview | None = None
    available_equity: Decimal = Decimal()
    admin_id: int | None = None
    notify: bool = False


class FirstLiveProposalRepository:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self.session_factory = session_factory

    def initialize(self, now: datetime | None = None) -> FirstLiveProposalStateRecord:
        now = now or datetime.now(UTC)
        with self.session_factory.begin() as session:
            state = session.get(FirstLiveProposalStateRecord, CONTROLLED_LIVE_V1.name)
            if state is None:
                state = FirstLiveProposalStateRecord(
                    profile_name=CONTROLLED_LIVE_V1.name,
                    started_at=now,
                    status="WAITING_FOR_SIGNAL",
                    created_at=now,
                    updated_at=now,
                )
                session.add(state)
        return self.state()

    def state(self) -> FirstLiveProposalStateRecord:
        with self.session_factory() as session:
            state = session.get(FirstLiveProposalStateRecord, CONTROLLED_LIVE_V1.name)
            if state is None:
                raise RuntimeError("Phase 5E state has not been initialized")
            session.expunge(state)
            return state

    def next_candidate(self) -> FrozenSignalCandidate | None:
        state = self.state()
        if state.proposal_id or state.status != "WAITING_FOR_SIGNAL":
            return None
        with self.session_factory() as session:
            filters = [
                ShadowDecisionRecord.protocol_id == PROTOCOL_ID,
                ShadowDecisionRecord.exchange == "bybit",
                ShadowDecisionRecord.symbol.in_(("SOL/USDT", "SOLUSDT")),
                ShadowDecisionRecord.decision.in_(("LONG", "SHORT")),
                ShadowDecisionRecord.risk_status == "ALLOW",
                ShadowDecisionRecord.strategy_hash == FROZEN_CONFIG_HASH,
                ShadowDecisionRecord.created_at > state.started_at,
            ]
            if state.last_scanned_candle_open is not None:
                filters.append(
                    ShadowDecisionRecord.candle_open_time
                    > state.last_scanned_candle_open
                )
            row = session.execute(
                select(ShadowDecisionRecord, ShadowTradeRecord)
                .join(
                    ShadowTradeRecord,
                    ShadowTradeRecord.decision_id == ShadowDecisionRecord.id,
                )
                .where(*filters)
                .order_by(
                    ShadowDecisionRecord.candle_open_time,
                    ShadowDecisionRecord.id,
                )
                .limit(1)
            ).first()
            if row is None:
                return None
            decision, trade = row
            session.expunge(decision)
            session.expunge(trade)
            return FrozenSignalCandidate(decision, trade)

    def reject_candidate(self, candidate: FrozenSignalCandidate, reason: str) -> None:
        with self.session_factory.begin() as session:
            state = session.get(FirstLiveProposalStateRecord, CONTROLLED_LIVE_V1.name)
            if state is None or state.proposal_id:
                return
            state.last_scanned_candle_open = candidate.decision.candle_open_time
            state.last_scanned_decision_id = candidate.decision.id
            state.last_error = reason[:1000]
            state.updated_at = datetime.now(UTC)

    def record_transient_error(self, reason: str) -> None:
        with self.session_factory.begin() as session:
            state = session.get(FirstLiveProposalStateRecord, CONTROLLED_LIVE_V1.name)
            if state is not None and not state.proposal_id:
                state.last_error = reason[:1000]
                state.updated_at = datetime.now(UTC)

    def save_ready(
        self,
        candidate: FrozenSignalCandidate,
        preview: ManualExecutionPreview,
        admin_id: int,
        available_equity: Decimal,
    ) -> bool:
        if not preview.executable or preview.source != FROZEN_SIGNAL_SOURCE:
            raise ControlledLiveBlocked("Only an executable frozen-signal preview may be saved")
        now = datetime.now(UTC)
        with self.session_factory.begin() as session:
            state = session.get(FirstLiveProposalStateRecord, CONTROLLED_LIVE_V1.name)
            controlled = session.get(ControlledLiveStateRecord, CONTROLLED_LIVE_V1.name)
            if state is None or controlled is None:
                raise ControlledLiveBlocked("Persistent Phase 5E/controlled-live state is missing")
            if state.proposal_id:
                return False
            if (
                controlled.profile_hash != CONTROLLED_LIVE_V1.config_hash
                or controlled.first_symbol != CONTROLLED_LIVE_V1_FIRST_INSTRUMENT.symbol
                or controlled.selection_hash
                != CONTROLLED_LIVE_V1_FIRST_INSTRUMENT.selection_hash
                or controlled.first_order_in_progress
                or controlled.first_order_executed
                or controlled.kill_switch_active
            ):
                raise ControlledLiveBlocked("Controlled-live persistent safety state is not ready")
            session.add(
                ControlledLiveProposalRecord(
                    proposal_id=preview.proposal_id,
                    proposal_hash=preview.proposal_hash,
                    profile_name=CONTROLLED_LIVE_V1.name,
                    profile_hash=CONTROLLED_LIVE_V1.config_hash,
                    selection_hash=CONTROLLED_LIVE_V1_FIRST_INSTRUMENT.selection_hash,
                    admin_telegram_id=admin_id,
                    source=preview.source,
                    preview_json=json.dumps(preview.safe_dict(), sort_keys=True),
                    status="PREVIEWED",
                    client_order_id=preview.client_order_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            state.last_scanned_candle_open = candidate.decision.candle_open_time
            state.last_scanned_decision_id = candidate.decision.id
            state.source_decision_id = candidate.decision.id
            state.proposal_id = preview.proposal_id
            state.available_equity = available_equity
            state.status = "READY_FOR_USER_APPROVAL"
            state.last_error = None
            state.updated_at = now
        return True

    def mark_notified(self, proposal_id: str) -> None:
        with self.session_factory.begin() as session:
            state = session.get(FirstLiveProposalStateRecord, CONTROLLED_LIVE_V1.name)
            if state and state.proposal_id == proposal_id and state.notified_at is None:
                state.notified_at = datetime.now(UTC)
                state.updated_at = datetime.now(UTC)

    def mark_approved_dry_run(self, proposal_id: str) -> None:
        with self.session_factory.begin() as session:
            state = session.get(FirstLiveProposalStateRecord, CONTROLLED_LIVE_V1.name)
            if state is None or state.proposal_id != proposal_id:
                raise ControlledLiveBlocked("Phase 5E proposal state mismatch")
            state.status = "APPROVED_DRY_RUN"
            state.updated_at = datetime.now(UTC)

    def cancel(self, proposal_id: str, admin_id: int) -> None:
        ControlledLiveRepository(self.session_factory).cancel(proposal_id, admin_id)
        with self.session_factory.begin() as session:
            state = session.get(FirstLiveProposalStateRecord, CONTROLLED_LIVE_V1.name)
            if state is None or state.proposal_id != proposal_id:
                raise ControlledLiveBlocked("Phase 5E proposal state mismatch")
            state.status = "CANCELLED"
            state.updated_at = datetime.now(UTC)

    def local_execution_attempts(self) -> int:
        with self.session_factory() as session:
            return int(
                session.scalar(
                    select(func.count(ExecutionOrderRecord.id)).where(
                        ExecutionOrderRecord.exchange == "bybit"
                    )
                )
                or 0
            )

    def proposal(self, proposal_id: str) -> ControlledLiveProposalRecord | None:
        return ControlledLiveRepository(self.session_factory).proposal(proposal_id)


class FirstControlledLiveProposalCoordinator:
    def __init__(
        self,
        repository: FirstLiveProposalRepository,
        gateway: ReadOnlyProposalGateway,
        admin_ids: set[int],
    ) -> None:
        self.repository = repository
        self.gateway = gateway
        self.admin_ids = set(admin_ids)

    async def cycle(self) -> ProposalCycleResult:
        state = self.repository.initialize()
        if state.proposal_id:
            record = self.repository.proposal(state.proposal_id)
            preview = preview_from_record(record) if record else None
            return ProposalCycleResult(
                state.status,
                "Immutable proposal already exists",
                preview,
                Decimal(state.available_equity or 0),
                record.admin_telegram_id if record else None,
                notify=bool(record and state.notified_at is None),
            )
        if not self.admin_ids:
            self.repository.record_transient_error("ADMIN_TELEGRAM_IDS is not configured")
            return ProposalCycleResult("WAITING_FOR_SIGNAL", "Admin is not configured")
        candidate = self.repository.next_candidate()
        if candidate is None:
            return ProposalCycleResult("WAITING_FOR_SIGNAL", "No new admissible frozen signal")
        try:
            snapshot = await self.gateway.controlled_proposal_snapshot("SOLUSDT")
            preview = self._build(candidate, snapshot)
            admin_id = min(self.admin_ids)
            created = self.repository.save_ready(
                candidate, preview, admin_id, snapshot.equity
            )
            return ProposalCycleResult(
                "READY_FOR_USER_APPROVAL",
                "Natural frozen SOLUSDT signal passed every read-only check",
                preview,
                snapshot.equity,
                admin_id,
                notify=created,
            )
        except CandidateRejected as error:
            self.repository.reject_candidate(candidate, str(error))
            return ProposalCycleResult("WAITING_FOR_SIGNAL", str(error))
        except Exception as error:
            self.repository.record_transient_error(f"{type(error).__name__}: {error}")
            return ProposalCycleResult(
                "WAITING_FOR_SIGNAL",
                "Read-only validation temporarily unavailable; signal was not consumed",
            )

    def _build(
        self,
        candidate: FrozenSignalCandidate,
        snapshot: ControlledProposalReadSnapshot,
    ) -> ManualExecutionPreview:
        if candidate.decision.strategy_hash != FROZEN_CONFIG_HASH:
            raise CandidateRejected("Frozen strategy hash mismatch")
        if snapshot.status != "Trading" or snapshot.contract_type != "LinearPerpetual":
            raise CandidateRejected("SOLUSDT LinearPerpetual is not Trading")
        if snapshot.tick_size <= 0 or snapshot.quantity_step <= 0:
            raise CandidateRejected("Invalid current Bybit instrument precision")
        if EXPECTED_QUANTITY < snapshot.minimum_quantity or EXPECTED_QUANTITY % snapshot.quantity_step:
            raise CandidateRejected("0.1 SOL violates current quantity limits")
        if snapshot.open_positions or snapshot.open_order_ids:
            raise CandidateRejected("Bybit already has an open position or open order")
        if not snapshot.fills_read or self.repository.local_execution_attempts():
            raise CandidateRejected("Read-only reconciliation is not clean")

        side = OrderSide.BUY if candidate.decision.decision == "LONG" else OrderSide.SELL
        entry = snapshot.ask_price if side is OrderSide.BUY else snapshot.bid_price
        notional = entry * EXPECTED_QUANTITY
        if notional < snapshot.minimum_notional or notional > Decimal("10"):
            raise CandidateRejected("Current 0.1 SOL notional is outside Bybit/$10 limits")
        stop, target = _native_levels(
            side,
            entry,
            Decimal(candidate.trade.stop_loss),
            Decimal(candidate.trade.take_profit),
            snapshot.tick_size,
        )
        rules = InstrumentRules(
            tick_size=snapshot.tick_size,
            quantity_step=snapshot.quantity_step,
            minimum_quantity=snapshot.minimum_quantity,
            minimum_notional=snapshot.minimum_notional,
            maximum_leverage=Decimal("1"),
        )
        preview = build_manual_preview(
            ManualOrderInputs(side, entry, stop, target),
            ControlledRiskSnapshot(
                equity=snapshot.equity,
                available_balance=snapshot.available_balance,
                open_positions=snapshot.open_positions,
            ),
            rules,
        )
        preview = replace(preview, source=FROZEN_SIGNAL_SOURCE)
        if not preview.executable:
            raise CandidateRejected(preview.reason)
        if preview.quantity != EXPECTED_QUANTITY:
            raise CandidateRejected("Risk sizing does not permit exactly 0.1 SOL")
        if preview.expected_notional > Decimal("10"):
            raise CandidateRejected("Controlled-live $10 notional cap exceeded")
        return preview


def _native_levels(
    side: OrderSide,
    entry: Decimal,
    strategy_stop: Decimal,
    strategy_target: Decimal,
    tick: Decimal,
) -> tuple[Decimal, Decimal]:
    if side is OrderSide.BUY:
        stop = _round_tick(strategy_stop, tick, ROUND_CEILING)
        target = _round_tick(strategy_target, tick, ROUND_CEILING)
        if not (stop < entry < target):
            raise CandidateRejected("Fresh LONG quote invalidated frozen SL/TP geometry")
    else:
        stop = _round_tick(strategy_stop, tick, ROUND_FLOOR)
        target = _round_tick(strategy_target, tick, ROUND_FLOOR)
        if not (target < entry < stop):
            raise CandidateRejected("Fresh SHORT quote invalidated frozen SL/TP geometry")
    reward = abs(target - entry)
    risk = abs(entry - stop)
    if risk <= 0 or reward / risk < Decimal("2"):
        raise CandidateRejected("Fresh quote no longer provides minimum R/R 1:2")
    return stop, target


def _round_tick(value: Decimal, tick: Decimal, rounding: str) -> Decimal:
    return (value / tick).to_integral_value(rounding=rounding) * tick


def preview_from_record(
    record: ControlledLiveProposalRecord | None,
) -> ManualExecutionPreview | None:
    if record is None:
        return None
    values = json.loads(record.preview_json)
    decimal_fields = {
        "quantity",
        "expected_notional",
        "leverage",
        "expected_fee",
        "stop_loss",
        "take_profit",
        "maximum_planned_loss",
        "risk_reward_ratio",
    }
    for name in decimal_fields:
        values[name] = Decimal(values[name])
    return ManualExecutionPreview(**values)


def format_controlled_proposal_ru(
    preview: ManualExecutionPreview, available_equity: Decimal
) -> str:
    direction = "LONG" if preview.side == "BUY" else "SHORT"
    entry = preview.expected_notional / preview.quantity
    return (
        "🛡 <b>ПЕРВАЯ КОНТРОЛИРУЕМАЯ СДЕЛКА — ПРЕДЛОЖЕНИЕ</b>\n\n"
        "Источник: естественный сигнал зафиксированной стратегии\n"
        f"Направление: <b>{direction}</b>\n"
        f"Вход (свежий bid/ask): {entry}\n"
        f"Количество: {preview.quantity} SOL\n"
        f"Номинал: ${preview.expected_notional}\n"
        f"Плечо: {preview.leverage}x\n"
        f"Stop Loss: {preview.stop_loss}\n"
        f"Take Profit: {preview.take_profit}\n"
        f"Риск/прибыль: 1:{preview.risk_reward_ratio}\n"
        f"Максимальный плановый убыток: ${preview.maximum_planned_loss}\n"
        f"Расчётные комиссии: ${preview.expected_fee}\n"
        f"Доступный equity: ${available_equity}\n\n"
        "DRY RUN включён. Кнопка подтверждения не отправит ордер."
    )
