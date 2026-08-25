"""Fail-closed preparation for one manually approved Bybit Mainnet validation order.

This module deliberately defines an execution protocol but no production HTTP gateway.  Phase 5A
can therefore validate the orchestration with deterministic fakes without being able to reach a
real order endpoint.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import select

from app.db import (
    ControlledLiveProposalRecord,
    ControlledLiveStateRecord,
    ExecutionOrderRecord,
)
from app.exchanges.models import InstrumentRules, OrderSide


PROFILE_PATH = Path(__file__).resolve().parents[2] / "config" / "controlled_live_v1.json"
CONTROLLED_LIVE_V1_HASH = "f9aef880cc9ac20b80d6db01adf8c0dab6e6085d84fd872889611013b1e69079"
MANUAL_SOURCE = "MANUAL_EXECUTION_VALIDATION"
BYBIT_TAKER_FEE_RATE = Decimal("0.00055")


class ControlledLiveBlocked(PermissionError):
    pass


class ProtectionFailure(RuntimeError):
    pass


class ReconciliationRequired(RuntimeError):
    pass


@dataclass(frozen=True)
class ControlledLiveProfile:
    name: str
    exchange: str
    symbol: str
    exchange_symbol: str
    market_type: str
    leverage: Decimal
    max_positions: int
    max_trades_per_day: int
    risk_per_trade_pct: Decimal
    daily_loss_limit_pct: Decimal
    max_consecutive_losses: int
    cooldown_minutes: int
    minimum_risk_reward: Decimal
    max_position_notional: Decimal
    first_execution_notional_cap: Decimal
    trailing_stop: bool
    config_hash: str


def load_controlled_live_profile(path: Path = PROFILE_PATH) -> ControlledLiveProfile:
    raw = json.loads(path.read_text(encoding="utf-8"))
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    config_hash = hashlib.sha256(canonical).hexdigest()
    if path == PROFILE_PATH and config_hash != CONTROLLED_LIVE_V1_HASH:
        raise RuntimeError("CONTROLLED_LIVE_V1 immutable profile hash mismatch")
    return ControlledLiveProfile(
        name=raw["profile"],
        exchange=raw["exchange"],
        symbol=raw["symbol"],
        exchange_symbol=raw["exchange_symbol"],
        market_type=raw["market_type"],
        leverage=Decimal(raw["leverage"]),
        max_positions=int(raw["max_positions"]),
        max_trades_per_day=int(raw["max_trades_per_day"]),
        risk_per_trade_pct=Decimal(raw["risk_per_trade_pct"]),
        daily_loss_limit_pct=Decimal(raw["daily_loss_limit_pct"]),
        max_consecutive_losses=int(raw["max_consecutive_losses"]),
        cooldown_minutes=int(raw["cooldown_minutes"]),
        minimum_risk_reward=Decimal(raw["minimum_risk_reward"]),
        max_position_notional=Decimal(raw["max_position_notional_usdt"]),
        first_execution_notional_cap=Decimal(raw["first_execution_notional_cap_usdt"]),
        trailing_stop=bool(raw["trailing_stop"]),
        config_hash=config_hash,
    )


CONTROLLED_LIVE_V1 = load_controlled_live_profile()


@dataclass(frozen=True)
class ArmingGates:
    live_trading_enabled: bool
    controlled_live_enabled: bool
    manual_first_order_approved: bool

    @classmethod
    def from_environment(cls) -> ArmingGates:
        return cls(
            _env_true("LIVE_TRADING_ENABLED"),
            _env_true("CONTROLLED_LIVE_ENABLED"),
            _env_true("MANUAL_FIRST_ORDER_APPROVED"),
        )

    def require_all(self) -> None:
        disabled = [
            name
            for name, enabled in (
                ("LIVE_TRADING_ENABLED", self.live_trading_enabled),
                ("CONTROLLED_LIVE_ENABLED", self.controlled_live_enabled),
                ("MANUAL_FIRST_ORDER_APPROVED", self.manual_first_order_approved),
            )
            if not enabled
        ]
        if disabled:
            raise ControlledLiveBlocked("Order submission gates are disabled: " + ", ".join(disabled))


def _env_true(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() == "true"


@dataclass(frozen=True)
class ControlledRiskSnapshot:
    equity: Decimal
    available_balance: Decimal
    open_positions: int = 0
    trades_today: int = 0
    daily_realized_pnl: Decimal = Decimal()
    consecutive_losses: int = 0
    cooldown_until: datetime | None = None


@dataclass(frozen=True)
class ManualOrderInputs:
    side: OrderSide
    reference_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal


@dataclass(frozen=True)
class ManualExecutionPreview:
    proposal_id: str
    profile_name: str
    profile_hash: str
    source: str
    symbol: str
    side: str
    quantity: Decimal
    expected_notional: Decimal
    leverage: Decimal
    expected_fee: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    maximum_planned_loss: Decimal
    risk_reward_ratio: Decimal
    executable: bool
    reason: str

    @property
    def client_order_id(self) -> str:
        return f"clv1-{self.proposal_id}"

    @property
    def proposal_hash(self) -> str:
        payload = self.safe_dict() | {"proposal_id": self.proposal_id}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def safe_dict(self) -> dict[str, Any]:
        return {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in asdict(self).items()
        }


def build_manual_preview(
    inputs: ManualOrderInputs,
    risk: ControlledRiskSnapshot,
    rules: InstrumentRules,
    *,
    profile: ControlledLiveProfile = CONTROLLED_LIVE_V1,
    now: datetime | None = None,
) -> ManualExecutionPreview:
    now = now or datetime.now(UTC)
    proposal_id = uuid4().hex
    reason = _risk_rejection(inputs, risk, profile, now)
    per_unit_risk = _per_unit_risk(inputs)
    reward = _per_unit_reward(inputs)
    ratio = reward / per_unit_risk if per_unit_risk > 0 else Decimal()
    if reason is None and ratio < profile.minimum_risk_reward:
        reason = "Minimum R/R 1:2 is not met"

    quantity = Decimal()
    expected_notional = Decimal()
    expected_fee = Decimal()
    maximum_loss = Decimal()
    if reason is None:
        risk_budget = risk.equity * profile.risk_per_trade_pct
        loss_and_fee_per_unit = (
            per_unit_risk
            + inputs.reference_price * BYBIT_TAKER_FEE_RATE
            + inputs.stop_loss * BYBIT_TAKER_FEE_RATE
        )
        risk_quantity = risk_budget / loss_and_fee_per_unit
        cap = min(profile.first_execution_notional_cap, profile.max_position_notional)
        cap_quantity = cap / inputs.reference_price
        raw_quantity = min(risk_quantity, cap_quantity)
        quantity = (
            raw_quantity / rules.quantity_step
        ).to_integral_value(rounding=ROUND_DOWN) * rules.quantity_step
        expected_notional = quantity * inputs.reference_price
        expected_fee = quantity * (
            inputs.reference_price + inputs.stop_loss
        ) * BYBIT_TAKER_FEE_RATE
        maximum_loss = quantity * per_unit_risk + expected_fee
        if quantity < rules.minimum_quantity:
            reason = (
                "Instrument minimum quantity exceeds the controlled first-order notional cap"
            )
        elif expected_notional < rules.minimum_notional:
            reason = "Instrument minimum notional is not met"
        elif expected_notional > cap:
            reason = "Controlled first-order notional cap exceeded"
        elif maximum_loss > risk_budget:
            reason = "Maximum planned loss exceeds 0.5% equity"
        elif expected_notional / profile.leverage + expected_fee > risk.available_balance:
            reason = "Insufficient available balance"

    return ManualExecutionPreview(
        proposal_id=proposal_id,
        profile_name=profile.name,
        profile_hash=profile.config_hash,
        source=MANUAL_SOURCE,
        symbol=profile.exchange_symbol,
        side=inputs.side.value,
        quantity=quantity,
        expected_notional=expected_notional,
        leverage=profile.leverage,
        expected_fee=expected_fee,
        stop_loss=inputs.stop_loss,
        take_profit=inputs.take_profit,
        maximum_planned_loss=maximum_loss,
        risk_reward_ratio=ratio,
        executable=reason is None,
        reason=reason or "READY_FOR_ADMIN_REVIEW",
    )


def _risk_rejection(
    inputs: ManualOrderInputs,
    risk: ControlledRiskSnapshot,
    profile: ControlledLiveProfile,
    now: datetime,
) -> str | None:
    if risk.equity <= 0 or risk.available_balance < 0 or inputs.reference_price <= 0:
        return "Invalid equity, available balance, or reference price"
    if profile.trailing_stop:
        return "Trailing stop must remain OFF"
    if risk.open_positions >= profile.max_positions:
        return "Maximum open positions reached"
    if risk.trades_today >= profile.max_trades_per_day:
        return "Maximum trades per UTC day reached"
    if risk.daily_realized_pnl <= -(risk.equity * profile.daily_loss_limit_pct):
        return "Daily loss limit reached"
    if risk.consecutive_losses >= profile.max_consecutive_losses:
        return "Consecutive-loss protection is active"
    if risk.cooldown_until is not None and now < _aware(risk.cooldown_until):
        return "60 minute cooldown is active"
    if _per_unit_risk(inputs) <= 0 or _per_unit_reward(inputs) <= 0:
        return "Invalid SL/TP ordering"
    return None


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _per_unit_risk(inputs: ManualOrderInputs) -> Decimal:
    if inputs.side is OrderSide.BUY:
        return inputs.reference_price - inputs.stop_loss
    return inputs.stop_loss - inputs.reference_price


def _per_unit_reward(inputs: ManualOrderInputs) -> Decimal:
    if inputs.side is OrderSide.BUY:
        return inputs.take_profit - inputs.reference_price
    return inputs.reference_price - inputs.take_profit


def format_preview_ru(preview: ManualExecutionPreview) -> str:
    return (
        "MANUAL EXECUTION VALIDATION\n"
        f"Symbol: {preview.symbol}\n"
        f"Side: {preview.side}\n"
        f"Quantity: {preview.quantity}\n"
        f"Expected notional: ${preview.expected_notional}\n"
        f"Leverage: {preview.leverage}x\n"
        f"Expected fee: ${preview.expected_fee}\n"
        f"SL: {preview.stop_loss}\n"
        f"TP: {preview.take_profit}\n"
        f"Maximum planned loss: ${preview.maximum_planned_loss}\n"
        f"Status: {'AWAITING ADMIN CONFIRMATION' if preview.executable else 'BLOCKED'}\n"
        f"Reason: {preview.reason}"
    )


class ControlledLiveRepository:
    def __init__(self, session_factory, profile: ControlledLiveProfile = CONTROLLED_LIVE_V1):
        self.session_factory = session_factory
        self.profile = profile

    def state(self) -> ControlledLiveStateRecord:
        with self.session_factory() as session:
            state = session.get(ControlledLiveStateRecord, self.profile.name)
            if state is None:
                state = ControlledLiveStateRecord(
                    profile_name=self.profile.name,
                    profile_hash=self.profile.config_hash,
                    updated_at=datetime.now(UTC),
                )
                session.add(state)
                session.commit()
            self._verify_hash(state.profile_hash)
            session.expunge(state)
            return state

    def save_preview(self, preview: ManualExecutionPreview, admin_id: int) -> None:
        if not preview.executable:
            raise ControlledLiveBlocked(preview.reason)
        self._verify_hash(preview.profile_hash)
        now = datetime.now(UTC)
        with self.session_factory() as session:
            session.add(
                ControlledLiveProposalRecord(
                    proposal_id=preview.proposal_id,
                    proposal_hash=preview.proposal_hash,
                    profile_name=self.profile.name,
                    profile_hash=self.profile.config_hash,
                    admin_telegram_id=admin_id,
                    source=preview.source,
                    preview_json=json.dumps(preview.safe_dict(), sort_keys=True),
                    status="PREVIEWED",
                    client_order_id=preview.client_order_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

    def approve(self, proposal_hash: str, admin_id: int) -> None:
        with self.session_factory() as session:
            record = session.scalar(
                select(ControlledLiveProposalRecord).where(
                    ControlledLiveProposalRecord.proposal_hash == proposal_hash
                )
            )
            if record is None or record.admin_telegram_id != admin_id:
                raise ControlledLiveBlocked("Proposal is missing or belongs to another admin")
            self._verify_hash(record.profile_hash)
            if record.status != "PREVIEWED":
                raise ControlledLiveBlocked("Proposal is not awaiting approval")
            record.status = "APPROVED"
            record.approved_at = datetime.now(UTC)
            record.updated_at = datetime.now(UTC)
            session.commit()

    def claim_submission(self, preview: ManualExecutionPreview, account_id: str) -> None:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            record = session.get(ControlledLiveProposalRecord, preview.proposal_id)
            state = session.get(ControlledLiveStateRecord, self.profile.name)
            if record is None or state is None:
                raise ControlledLiveBlocked("Approved persistent proposal/state is missing")
            self._verify_hash(record.profile_hash)
            self._verify_hash(state.profile_hash)
            if state.first_order_in_progress or state.first_order_executed:
                raise ControlledLiveBlocked("First Mainnet order was already claimed or executed")
            if record.proposal_hash != preview.proposal_hash or record.status != "APPROVED":
                raise ControlledLiveBlocked("Exact approved proposal is required")
            if state.kill_switch_active:
                raise ControlledLiveBlocked("Emergency kill switch is active")
            state.first_order_in_progress = True
            state.updated_at = now
            record.status = "SUBMITTING"
            record.submitted_at = now
            record.updated_at = now
            session.add(
                ExecutionOrderRecord(
                    exchange="bybit",
                    account_id=account_id,
                    client_order_id=preview.client_order_id,
                    symbol=self.profile.symbol,
                    side=preview.side,
                    quantity=preview.quantity,
                    request_hash=preview.proposal_hash,
                    status="PENDING",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

    def mark_filled(self, preview: ManualExecutionPreview, fill: LiveFill) -> None:
        self._update_execution(
            preview,
            proposal_status="FILLED_UNPROTECTED",
            ledger_status="SUBMITTED",
            exchange_order_id=fill.order_id,
            position_id=fill.position_id,
            exchange_status="FILLED",
        )

    def mark_protected(self, preview: ManualExecutionPreview) -> None:
        self._update_execution(
            preview,
            proposal_status="PROTECTED",
            ledger_status="FILLED_PROTECTED",
            finish_first_order=True,
        )

    def mark_emergency_closed(self, preview: ManualExecutionPreview, error: Exception) -> None:
        self._update_execution(
            preview,
            proposal_status="EMERGENCY_CLOSED",
            ledger_status="EMERGENCY_CLOSED",
            error_code=type(error).__name__,
            finish_first_order=True,
        )

    def mark_unknown(self, preview: ManualExecutionPreview, error: Exception) -> None:
        self._update_execution(
            preview,
            proposal_status="UNKNOWN",
            ledger_status="UNKNOWN",
            error_code=type(error).__name__,
        )

    def activate_kill_switch(self) -> None:
        with self.session_factory() as session:
            state = session.get(ControlledLiveStateRecord, self.profile.name)
            if state is None:
                state = ControlledLiveStateRecord(
                    profile_name=self.profile.name,
                    profile_hash=self.profile.config_hash,
                )
                session.add(state)
            self._verify_hash(state.profile_hash)
            state.kill_switch_active = True
            state.updated_at = datetime.now(UTC)
            session.commit()

    def proposal(self, proposal_id: str) -> ControlledLiveProposalRecord | None:
        with self.session_factory() as session:
            record = session.get(ControlledLiveProposalRecord, proposal_id)
            if record is not None:
                session.expunge(record)
            return record

    def _update_execution(
        self,
        preview: ManualExecutionPreview,
        *,
        proposal_status: str,
        ledger_status: str,
        exchange_order_id: str | None = None,
        position_id: str | None = None,
        exchange_status: str | None = None,
        error_code: str | None = None,
        finish_first_order: bool = False,
    ) -> None:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            record = session.get(ControlledLiveProposalRecord, preview.proposal_id)
            ledger = session.scalar(
                select(ExecutionOrderRecord).where(
                    ExecutionOrderRecord.exchange == "bybit",
                    ExecutionOrderRecord.client_order_id == preview.client_order_id,
                )
            )
            state = session.get(ControlledLiveStateRecord, self.profile.name)
            if record is None or ledger is None or state is None:
                raise RuntimeError("Controlled execution persistence is incomplete")
            record.status = proposal_status
            record.exchange_order_id = exchange_order_id or record.exchange_order_id
            record.position_id = position_id or record.position_id
            record.error_code = error_code
            record.completed_at = now if finish_first_order else record.completed_at
            record.updated_at = now
            ledger.status = ledger_status
            ledger.exchange_order_id = exchange_order_id or ledger.exchange_order_id
            ledger.exchange_status = exchange_status or ledger.exchange_status
            ledger.error_code = error_code
            ledger.updated_at = now
            if finish_first_order:
                state.first_order_in_progress = False
                state.first_order_executed = True
                state.updated_at = now
            session.commit()

    def _verify_hash(self, value: str) -> None:
        if value != self.profile.config_hash:
            raise ControlledLiveBlocked("CONTROLLED_LIVE_V1 profile hash mismatch")


@dataclass(frozen=True)
class LiveFill:
    order_id: str
    position_id: str
    filled_quantity: Decimal
    average_price: Decimal
    fee: Decimal


@dataclass(frozen=True)
class LivePositionSnapshot:
    position_id: str
    symbol: str
    quantity: Decimal


@dataclass(frozen=True)
class LiveGatewaySnapshot:
    positions: tuple[LivePositionSnapshot, ...]
    open_order_ids: frozenset[str]
    fill_order_ids: frozenset[str]


class ControlledLiveGateway(Protocol):
    async def submit_market(
        self, preview: ManualExecutionPreview, client_order_id: str
    ) -> LiveFill: ...

    async def install_native_protection(
        self,
        fill: LiveFill,
        *,
        symbol: str,
        stop_loss: Decimal,
        take_profit: Decimal,
        reduce_only: bool,
    ) -> None: ...

    async def emergency_close_reduce_only(self, fill: LiveFill, symbol: str) -> None: ...

    async def cancel_pending_orders(self, symbol: str) -> int: ...

    async def snapshot(self) -> LiveGatewaySnapshot: ...


class ManualExecutionService:
    def __init__(
        self,
        repository: ControlledLiveRepository,
        gateway: ControlledLiveGateway,
        admin_ids: set[int],
        *,
        profile: ControlledLiveProfile = CONTROLLED_LIVE_V1,
    ) -> None:
        self.repository = repository
        self.gateway = gateway
        self.admin_ids = set(admin_ids)
        self.profile = profile

    def preview(
        self,
        admin_id: int,
        inputs: ManualOrderInputs,
        risk: ControlledRiskSnapshot,
        rules: InstrumentRules,
    ) -> ManualExecutionPreview:
        self._require_admin(admin_id)
        preview = build_manual_preview(inputs, risk, rules, profile=self.profile)
        if preview.executable:
            self.repository.state()
            self.repository.save_preview(preview, admin_id)
        return preview

    def approve(self, admin_id: int, proposal_hash: str) -> None:
        self._require_admin(admin_id)
        self.repository.approve(proposal_hash, admin_id)

    async def execute_first_order(
        self,
        admin_id: int,
        preview: ManualExecutionPreview,
        *,
        account_id: str,
        gates: ArmingGates | None = None,
    ) -> LiveFill:
        self._require_admin(admin_id)
        (gates or ArmingGates.from_environment()).require_all()
        if preview.source != MANUAL_SOURCE:
            raise ControlledLiveBlocked("Strategy and AI cannot submit the first Mainnet order")
        self.repository.claim_submission(preview, account_id)
        try:
            fill = await self.gateway.submit_market(preview, preview.client_order_id)
        except Exception as error:
            self.repository.mark_unknown(preview, error)
            raise ReconciliationRequired(
                "Order outcome is unknown; automatic retry is forbidden"
            ) from error
        if fill.filled_quantity <= 0:
            error = RuntimeError("Mainnet order returned no fill")
            self.repository.mark_unknown(preview, error)
            raise ReconciliationRequired("No fill confirmed; reconciliation required")
        self.repository.mark_filled(preview, fill)
        try:
            await self.gateway.install_native_protection(
                fill,
                symbol=self.profile.exchange_symbol,
                stop_loss=preview.stop_loss,
                take_profit=preview.take_profit,
                reduce_only=True,
            )
        except Exception as protection_error:
            try:
                await self.gateway.emergency_close_reduce_only(
                    fill, self.profile.exchange_symbol
                )
            except Exception as close_error:
                self.repository.mark_unknown(preview, close_error)
                raise ProtectionFailure(
                    "UNPROTECTED POSITION: emergency reduce-only close failed"
                ) from close_error
            self.repository.mark_emergency_closed(preview, protection_error)
            raise ProtectionFailure(
                "Native SL/TP failed; position was emergency-closed reduce-only"
            ) from protection_error
        self.repository.mark_protected(preview)
        return fill

    async def emergency_stop(self, admin_id: int, *, close_position: bool) -> dict[str, Any]:
        self._require_admin(admin_id)
        self.repository.activate_kill_switch()
        cancelled = await self.gateway.cancel_pending_orders(self.profile.exchange_symbol)
        closed = 0
        if close_position:
            snapshot = await self.gateway.snapshot()
            for position in snapshot.positions:
                if position.symbol != self.profile.exchange_symbol:
                    continue
                fill = LiveFill(
                    order_id="emergency",
                    position_id=position.position_id,
                    filled_quantity=position.quantity,
                    average_price=Decimal(),
                    fee=Decimal(),
                )
                await self.gateway.emergency_close_reduce_only(
                    fill, self.profile.exchange_symbol
                )
                closed += 1
        return {"kill_switch": "ACTIVE", "cancelled_orders": cancelled, "closed_positions": closed}

    async def reconcile(self, preview: ManualExecutionPreview) -> dict[str, Any]:
        record = self.repository.proposal(preview.proposal_id)
        if record is None:
            return {"status": "MISMATCH", "reason": "Local proposal missing"}
        snapshot = await self.gateway.snapshot()
        if record.status == "PROTECTED":
            fill_match = bool(record.exchange_order_id) and record.exchange_order_id in snapshot.fill_order_ids
            position_match = any(
                item.position_id == record.position_id
                and item.symbol == self.profile.exchange_symbol
                and item.quantity == preview.quantity
                for item in snapshot.positions
            )
            return {
                "status": "MATCH" if fill_match and position_match else "MISMATCH",
                "fill_match": fill_match,
                "position_match": position_match,
                "duplicate_open_orders": len(snapshot.open_order_ids) > 1,
            }
        if record.status == "EMERGENCY_CLOSED":
            still_open = any(
                item.position_id == record.position_id for item in snapshot.positions
            )
            return {"status": "MISMATCH" if still_open else "MATCH", "position_open": still_open}
        return {"status": "REQUIRES_MANUAL_REVIEW", "local_status": record.status}

    def _require_admin(self, admin_id: int) -> None:
        if admin_id not in self.admin_ids:
            raise ControlledLiveBlocked("ADMIN_TELEGRAM_IDS authorization required")
