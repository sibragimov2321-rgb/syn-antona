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
from app.trading.execution_store import OrderRejected
from app.trading.controlled_universe import (
    AI_SIGNAL_SOURCE,
    ALLOWED_SCANNER_SYMBOLS,
    FROZEN_SIGNAL_SOURCE,
    internal_symbol,
    scanner_selection_hash,
)


PROFILE_PATH = Path(__file__).resolve().parents[2] / "config" / "controlled_live_v1.json"
CONTROLLED_LIVE_V1_HASH = "b382d2251bed6558bb14cc17e5ee411f76428c111e0fa5c26868b75cc739136f"
FIRST_INSTRUMENT_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "controlled_live_v1_first_symbol.json"
)
CONTROLLED_LIVE_V1_FIRST_INSTRUMENT_HASH = (
    "8a453a1c1dd9b274d14b7dec2dc0e61adf3e79d56cf860194cc2e01fbcbb2938"
)
MANUAL_SOURCE = "MANUAL_EXECUTION_VALIDATION"
BYBIT_TAKER_FEE_RATE = Decimal("0.00055")
MULTI_SYMBOL_GATE_VALUE = "MULTI_SYMBOL_SCANNER"


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
    signal_threshold: int
    leverage: Decimal
    max_positions: int
    max_trades_per_day: int | None
    risk_per_trade_pct: Decimal
    daily_max_loss_usdt: Decimal
    total_experiment_loss_limit: Decimal
    max_consecutive_losses: int
    consecutive_loss_stop_until_next_utc_day: bool
    cooldown_minutes: int
    minimum_risk_reward: Decimal
    max_position_notional: Decimal
    first_execution_notional_cap: Decimal
    estimated_slippage_per_leg: Decimal
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
        signal_threshold=int(raw["signal_threshold"]),
        leverage=Decimal(raw["leverage"]),
        max_positions=int(raw["max_positions"]),
        max_trades_per_day=(
            int(raw["max_trades_per_day"])
            if raw.get("max_trades_per_day") is not None
            else None
        ),
        risk_per_trade_pct=Decimal(raw["risk_per_trade_pct"]),
        daily_max_loss_usdt=Decimal(raw["daily_max_loss_usdt"]),
        total_experiment_loss_limit=Decimal(
            raw["total_experiment_loss_limit_usdt"]
        ),
        max_consecutive_losses=int(raw["max_consecutive_losses"]),
        consecutive_loss_stop_until_next_utc_day=bool(
            raw["consecutive_loss_stop_until_next_utc_day"]
        ),
        cooldown_minutes=int(raw["cooldown_minutes"]),
        minimum_risk_reward=Decimal(raw["minimum_risk_reward"]),
        max_position_notional=Decimal(raw["max_position_notional_usdt"]),
        first_execution_notional_cap=Decimal(raw["first_execution_notional_cap_usdt"]),
        estimated_slippage_per_leg=Decimal(raw["estimated_slippage_per_leg"]),
        trailing_stop=bool(raw["trailing_stop"]),
        config_hash=config_hash,
    )


CONTROLLED_LIVE_V1 = load_controlled_live_profile()


@dataclass(frozen=True)
class FirstInstrumentSelection:
    symbol: str
    internal_symbol: str
    exchange: str
    market_type: str
    base_profile_hash: str
    first_order_notional_cap: Decimal
    selection_hash: str


def load_first_instrument_selection(
    path: Path = FIRST_INSTRUMENT_PATH,
) -> FirstInstrumentSelection:
    raw = json.loads(path.read_text(encoding="utf-8"))
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    selection_hash = hashlib.sha256(canonical).hexdigest()
    if path == FIRST_INSTRUMENT_PATH and selection_hash != CONTROLLED_LIVE_V1_FIRST_INSTRUMENT_HASH:
        raise RuntimeError("CONTROLLED_LIVE_V1 first-instrument hash mismatch")
    if raw["base_profile_hash"] != CONTROLLED_LIVE_V1.config_hash:
        raise RuntimeError("First-instrument selection does not match CONTROLLED_LIVE_V1")
    return FirstInstrumentSelection(
        symbol=raw["symbol"],
        internal_symbol=raw["internal_symbol"],
        exchange=raw["exchange"],
        market_type=raw["market_type"],
        base_profile_hash=raw["base_profile_hash"],
        first_order_notional_cap=Decimal(
            raw["runtime_rules"]["first_order_notional_cap"]
        ),
        selection_hash=selection_hash,
    )


CONTROLLED_LIVE_V1_FIRST_INSTRUMENT = load_first_instrument_selection()
CONTROLLED_LIVE_V1_FIRST_SYMBOL = CONTROLLED_LIVE_V1_FIRST_INSTRUMENT.symbol


@dataclass(frozen=True)
class ArmingGates:
    live_trading_enabled: bool
    controlled_live_enabled: bool
    manual_first_order_approved: bool
    first_symbol: str | None = None

    @classmethod
    def from_environment(cls) -> ArmingGates:
        return cls(
            _env_true("LIVE_TRADING_ENABLED"),
            _env_true("CONTROLLED_LIVE_ENABLED"),
            _env_true("MANUAL_FIRST_ORDER_APPROVED"),
            os.getenv("CONTROLLED_LIVE_V1_FIRST_SYMBOL"),
        )

    def require_all(self, *, expected_symbol: str | None = None) -> None:
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
        multi_symbol_gate = (
            self.first_symbol == MULTI_SYMBOL_GATE_VALUE
            and expected_symbol in ALLOWED_SCANNER_SYMBOLS
        )
        if (
            expected_symbol is not None
            and self.first_symbol != expected_symbol
            and not multi_symbol_gate
        ):
            raise ControlledLiveBlocked(
                "CONTROLLED_LIVE_V1_FIRST_SYMBOL does not authorize this proposal"
            )


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
    starting_day_equity: Decimal | None = None
    experiment_start_equity: Decimal | None = None
    open_planned_risk: Decimal = Decimal()


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
    selection_hash: str
    source: str
    signal_score: int
    symbol: str
    side: str
    quantity: Decimal
    expected_notional: Decimal
    leverage: Decimal
    expected_fee: Decimal
    estimated_slippage: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    maximum_planned_loss: Decimal
    risk_reward_ratio: Decimal
    executable: bool
    reason: str

    @property
    def client_order_id(self) -> str:
        # Bybit V5 orderLinkId is limited to 36 characters. Keep the ID
        # deterministic without exposing or truncating the proposal itself.
        digest = hashlib.sha256(self.proposal_id.encode()).hexdigest()
        return f"clv1-{digest[:31]}"

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
    instrument: FirstInstrumentSelection = CONTROLLED_LIVE_V1_FIRST_INSTRUMENT,
    now: datetime | None = None,
) -> ManualExecutionPreview:
    now = now or datetime.now(UTC)
    proposal_id = uuid4().hex
    reason = _risk_rejection(inputs, risk, profile, now)
    per_unit_risk = _per_unit_risk(inputs)
    reward = _per_unit_reward(inputs)
    ratio = reward / per_unit_risk if per_unit_risk > 0 else Decimal()
    if reason is None and ratio < profile.minimum_risk_reward:
        reason = f"Minimum R/R 1:{profile.minimum_risk_reward} is not met"
    if reason is None and profile.leverage > rules.maximum_leverage:
        reason = "Configured leverage exceeds the current instrument limit"

    quantity = Decimal()
    expected_notional = Decimal()
    expected_fee = Decimal()
    estimated_slippage = Decimal()
    maximum_loss = Decimal()
    risk_budget = Decimal()
    daily_budget_limited = False
    if reason is None:
        per_trade_cap = risk.equity * profile.risk_per_trade_pct
        remaining_daily_budget = remaining_daily_risk_budget(risk, profile)
        risk_budget = min(per_trade_cap, remaining_daily_budget)
        daily_budget_limited = remaining_daily_budget < per_trade_cap
        if risk_budget <= 0:
            reason = DAILY_RISK_BUDGET_REASON
    if reason is None:
        loss_fee_and_slippage_per_unit = (
            per_unit_risk
            + inputs.reference_price * BYBIT_TAKER_FEE_RATE
            + inputs.stop_loss * BYBIT_TAKER_FEE_RATE
            + (inputs.reference_price + inputs.stop_loss)
            * profile.estimated_slippage_per_leg
        )
        risk_quantity = risk_budget / loss_fee_and_slippage_per_unit
        cap = min(instrument.first_order_notional_cap, profile.max_position_notional)
        cap_quantity = cap / inputs.reference_price
        raw_quantity = min(risk_quantity, cap_quantity)
        quantity = (
            raw_quantity / rules.quantity_step
        ).to_integral_value(rounding=ROUND_DOWN) * rules.quantity_step
        expected_notional = quantity * inputs.reference_price
        expected_fee = quantity * (
            inputs.reference_price + inputs.stop_loss
        ) * BYBIT_TAKER_FEE_RATE
        estimated_slippage = quantity * (
            inputs.reference_price + inputs.stop_loss
        ) * profile.estimated_slippage_per_leg
        maximum_loss = quantity * per_unit_risk + expected_fee + estimated_slippage
        if quantity < rules.minimum_quantity:
            reason = DAILY_RISK_BUDGET_REASON if daily_budget_limited else (
                "Instrument minimum quantity exceeds the controlled first-order notional cap"
            )
        elif expected_notional < rules.minimum_notional:
            reason = (
                DAILY_RISK_BUDGET_REASON
                if daily_budget_limited
                else "Instrument minimum notional is not met"
            )
        elif expected_notional > cap:
            reason = "Controlled first-order notional cap exceeded"
        elif maximum_loss > risk_budget:
            reason = (
                "Maximum planned loss exceeds "
                f"{profile.risk_per_trade_pct * 100}% equity"
            )
        elif (
            realized_daily_loss(risk.daily_realized_pnl)
            + risk.open_planned_risk
            + maximum_loss
            > profile.daily_max_loss_usdt
        ):
            reason = DAILY_RISK_BUDGET_REASON
        elif expected_notional / profile.leverage + expected_fee > risk.available_balance:
            reason = "Insufficient available balance"

    return ManualExecutionPreview(
        proposal_id=proposal_id,
        profile_name=profile.name,
        profile_hash=profile.config_hash,
        selection_hash=instrument.selection_hash,
        source=MANUAL_SOURCE,
        signal_score=0,
        symbol=instrument.symbol,
        side=inputs.side.value,
        quantity=quantity,
        expected_notional=expected_notional,
        leverage=profile.leverage,
        expected_fee=expected_fee,
        estimated_slippage=estimated_slippage,
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
    if risk.open_planned_risk < 0:
        return "Invalid open planned risk"
    if remaining_daily_risk_budget(risk, profile) <= 0:
        return DAILY_RISK_BUDGET_REASON
    if (
        risk.experiment_start_equity is not None
        and risk.experiment_start_equity - risk.equity
        >= profile.total_experiment_loss_limit
    ):
        return "Total controlled-live experiment loss limit reached"
    if risk.consecutive_losses >= profile.max_consecutive_losses:
        return "Consecutive-loss stop is active until the next UTC day"
    if risk.cooldown_until is not None and now < _aware(risk.cooldown_until):
        return "UTC-day consecutive-loss stop is active"
    if _per_unit_risk(inputs) <= 0 or _per_unit_reward(inputs) <= 0:
        return "Invalid SL/TP ordering"
    return None


DAILY_RISK_BUDGET_REASON = "WAIT: DAILY RISK BUDGET"


def realized_daily_loss(daily_realized_pnl: Decimal) -> Decimal:
    """Profit never expands the fixed UTC-day loss allowance."""
    return max(Decimal(), -daily_realized_pnl)


def remaining_daily_risk_budget(
    risk: ControlledRiskSnapshot,
    profile: ControlledLiveProfile = CONTROLLED_LIVE_V1,
) -> Decimal:
    return max(
        Decimal(),
        profile.daily_max_loss_usdt
        - realized_daily_loss(risk.daily_realized_pnl)
        - risk.open_planned_risk,
    )


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
        f"Estimated slippage: ${preview.estimated_slippage}\n"
        f"SL: {preview.stop_loss}\n"
        f"TP: {preview.take_profit}\n"
        f"Maximum planned loss: ${preview.maximum_planned_loss}\n"
        f"Status: {'AWAITING ADMIN CONFIRMATION' if preview.executable else 'BLOCKED'}\n"
        f"Reason: {preview.reason}"
    )


class ControlledLiveRepository:
    def __init__(
        self,
        session_factory,
        profile: ControlledLiveProfile = CONTROLLED_LIVE_V1,
        instrument: FirstInstrumentSelection = CONTROLLED_LIVE_V1_FIRST_INSTRUMENT,
    ):
        self.session_factory = session_factory
        self.profile = profile
        self.instrument = instrument

    def state(self) -> ControlledLiveStateRecord:
        with self.session_factory() as session:
            state = session.get(ControlledLiveStateRecord, self.profile.name)
            if state is None:
                state = ControlledLiveStateRecord(
                    profile_name=self.profile.name,
                    profile_hash=self.profile.config_hash,
                    first_symbol=self.instrument.symbol,
                    selection_hash=self.instrument.selection_hash,
                    updated_at=datetime.now(UTC),
                )
                session.add(state)
                session.commit()
            self._verify_hash(state.profile_hash)
            self._verify_selection(state.first_symbol, state.selection_hash)
            session.expunge(state)
            return state

    def refresh_loss_baselines(
        self,
        equity: Decimal,
        daily_realized_pnl: Decimal,
        now: datetime | None = None,
    ) -> tuple[Decimal, Decimal]:
        """Persist experiment/day equity anchors without weakening loss gates."""
        current = now or datetime.now(UTC)
        with self.session_factory.begin() as session:
            state = session.get(ControlledLiveStateRecord, self.profile.name)
            if state is None:
                raise ControlledLiveBlocked("Controlled-live persistent state is missing")
            self._verify_hash(state.profile_hash)
            if state.experiment_start_equity is None:
                state.experiment_start_equity = equity
            if state.starting_day_utc != current.date():
                state.starting_day_utc = current.date()
                state.starting_day_equity = equity - daily_realized_pnl
            state.updated_at = current
            return (
                Decimal(state.experiment_start_equity),
                Decimal(state.starting_day_equity or equity),
            )

    def enable_automatic_execution_after_first_validation(self) -> None:
        with self.session_factory.begin() as session:
            state = session.get(ControlledLiveStateRecord, self.profile.name)
            if state is None:
                raise ControlledLiveBlocked("Controlled-live persistent state is missing")
            self._verify_hash(state.profile_hash)
            if (
                not state.first_order_executed
                or state.first_order_in_progress
                or state.kill_switch_active
            ):
                raise ControlledLiveBlocked(
                    "First protected execution must be validated before automatic mode"
                )
            state.automatic_execution_enabled = True
            state.updated_at = datetime.now(UTC)

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
                    selection_hash=preview.selection_hash,
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
            values = json.loads(record.preview_json)
            self._verify_selection(str(values.get("symbol") or ""), record.selection_hash)
            if record.status != "PREVIEWED":
                raise ControlledLiveBlocked("Proposal is not awaiting approval")
            record.status = "APPROVED"
            record.approved_at = datetime.now(UTC)
            record.updated_at = datetime.now(UTC)
            session.commit()

    def cancel(self, proposal_id: str, admin_id: int) -> None:
        with self.session_factory() as session:
            record = session.get(ControlledLiveProposalRecord, proposal_id)
            if record is None or record.admin_telegram_id != admin_id:
                raise ControlledLiveBlocked("Proposal is missing or belongs to another admin")
            self._verify_hash(record.profile_hash)
            values = json.loads(record.preview_json)
            self._verify_selection(str(values.get("symbol") or ""), record.selection_hash)
            if record.status not in {"PREVIEWED", "APPROVED"}:
                raise ControlledLiveBlocked("Proposal can no longer be cancelled")
            record.status = "CANCELLED"
            record.completed_at = datetime.now(UTC)
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
            self._verify_selection(preview.symbol, record.selection_hash)
            self._verify_selection(state.first_symbol, state.selection_hash)
            if state.first_order_in_progress or (
                state.first_order_executed
                and not state.automatic_execution_enabled
            ):
                raise ControlledLiveBlocked(
                    "Mainnet order was already claimed or executed; automatic mode is not validated"
                )
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
                    symbol=(
                        internal_symbol(preview.symbol)
                        if preview.symbol in ALLOWED_SCANNER_SYMBOLS
                        else self.instrument.internal_symbol
                    ),
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

    def claim_ai_submission(self, preview: ManualExecutionPreview, account_id: str) -> None:
        """Reserve an autonomous order without the legacy one-time validation lock."""
        if preview.source != AI_SIGNAL_SOURCE:
            raise ControlledLiveBlocked("AI submission requires the AI source identity")
        now = datetime.now(UTC)
        with self.session_factory() as session:
            record = session.get(ControlledLiveProposalRecord, preview.proposal_id)
            state = session.get(ControlledLiveStateRecord, self.profile.name)
            if record is None or state is None:
                raise ControlledLiveBlocked("Approved AI proposal/state is missing")
            self._verify_hash(record.profile_hash)
            self._verify_selection(preview.symbol, record.selection_hash)
            if record.proposal_hash != preview.proposal_hash or record.status != "APPROVED":
                raise ControlledLiveBlocked("Exact durable AI proposal is required")
            if state.kill_switch_active:
                raise ControlledLiveBlocked("Emergency kill switch is active")
            existing = session.scalar(
                select(ExecutionOrderRecord).where(
                    ExecutionOrderRecord.exchange == "bybit",
                    ExecutionOrderRecord.account_id == account_id,
                    ExecutionOrderRecord.client_order_id == preview.client_order_id,
                )
            )
            if existing is not None:
                raise ControlledLiveBlocked("Duplicate deterministic AI client order ID")
            record.status = "SUBMITTING"
            record.submitted_at = now
            record.updated_at = now
            session.add(
                ExecutionOrderRecord(
                    exchange="bybit",
                    account_id=account_id,
                    client_order_id=preview.client_order_id,
                    symbol=internal_symbol(preview.symbol),
                    side=preview.side,
                    quantity=preview.quantity,
                    request_hash=preview.proposal_hash,
                    status="PENDING",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

    def mark_ai_filled(self, preview: ManualExecutionPreview, fill: LiveFill) -> None:
        self._update_ai_execution(
            preview,
            proposal_status="FILLED_UNPROTECTED",
            ledger_status="SUBMITTED",
            exchange_order_id=fill.order_id,
            position_id=fill.position_id,
            exchange_status="FILLED",
        )

    def mark_ai_protected(self, preview: ManualExecutionPreview) -> None:
        self._update_ai_execution(
            preview, proposal_status="PROTECTED", ledger_status="FILLED_PROTECTED"
        )

    def mark_ai_emergency_closed(
        self, preview: ManualExecutionPreview, error: Exception
    ) -> None:
        self._update_ai_execution(
            preview,
            proposal_status="EMERGENCY_CLOSED",
            ledger_status="EMERGENCY_CLOSED",
            error_code=type(error).__name__,
        )

    def mark_ai_unknown(self, preview: ManualExecutionPreview, error: Exception) -> None:
        self._update_ai_execution(
            preview,
            proposal_status="UNKNOWN",
            ledger_status="UNKNOWN",
            error_code=type(error).__name__,
        )

    def mark_ai_rejected(self, preview: ManualExecutionPreview, error: Exception) -> None:
        self._update_ai_execution(
            preview,
            proposal_status="REJECTED",
            ledger_status="REJECTED",
            error_code=type(error).__name__,
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

    def mark_rejected(self, preview: ManualExecutionPreview, error: Exception) -> None:
        self._update_execution(
            preview,
            proposal_status="REJECTED",
            ledger_status="REJECTED",
            error_code=type(error).__name__,
            release_first_order=True,
        )

    def activate_kill_switch(self) -> None:
        with self.session_factory() as session:
            state = session.get(ControlledLiveStateRecord, self.profile.name)
            if state is None:
                state = ControlledLiveStateRecord(
                    profile_name=self.profile.name,
                    profile_hash=self.profile.config_hash,
                    first_symbol=self.instrument.symbol,
                    selection_hash=self.instrument.selection_hash,
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
        release_first_order: bool = False,
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
            elif release_first_order:
                state.first_order_in_progress = False
                state.updated_at = now
            session.commit()

    def _update_ai_execution(
        self,
        preview: ManualExecutionPreview,
        *,
        proposal_status: str,
        ledger_status: str,
        exchange_order_id: str | None = None,
        position_id: str | None = None,
        exchange_status: str | None = None,
        error_code: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        with self.session_factory.begin() as session:
            record = session.get(ControlledLiveProposalRecord, preview.proposal_id)
            ledger = session.scalar(
                select(ExecutionOrderRecord).where(
                    ExecutionOrderRecord.exchange == "bybit",
                    ExecutionOrderRecord.client_order_id == preview.client_order_id,
                )
            )
            if record is None or ledger is None or record.source != AI_SIGNAL_SOURCE:
                raise RuntimeError("AI execution persistence is incomplete")
            record.status = proposal_status
            record.exchange_order_id = exchange_order_id or record.exchange_order_id
            record.position_id = position_id or record.position_id
            record.error_code = error_code
            if proposal_status in {"PROTECTED", "EMERGENCY_CLOSED", "REJECTED"}:
                record.completed_at = now
            record.updated_at = now
            ledger.status = ledger_status
            ledger.exchange_order_id = exchange_order_id or ledger.exchange_order_id
            ledger.exchange_status = exchange_status or ledger.exchange_status
            ledger.error_code = error_code
            ledger.updated_at = now

    def _verify_hash(self, value: str) -> None:
        if value != self.profile.config_hash:
            raise ControlledLiveBlocked("CONTROLLED_LIVE_V1 profile hash mismatch")

    def _verify_selection(self, symbol: str, selection_hash: str) -> None:
        legacy = (
            symbol == self.instrument.symbol
            and selection_hash == self.instrument.selection_hash
        )
        scanner = (
            symbol in ALLOWED_SCANNER_SYMBOLS
            and selection_hash == scanner_selection_hash(symbol)
        )
        if not legacy and not scanner:
            raise ControlledLiveBlocked("Controlled-live instrument selection mismatch")


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


@dataclass(frozen=True)
class CurrentInstrumentState:
    symbol: str
    status: str
    ask_price: Decimal
    minimum_quantity: Decimal
    quantity_step: Decimal
    minimum_notional: Decimal


@dataclass(frozen=True)
class ControlledProposalReadSnapshot:
    """Read-only exchange facts used to construct (never submit) Phase 5E."""

    symbol: str
    contract_type: str
    status: str
    bid_price: Decimal
    ask_price: Decimal
    tick_size: Decimal
    minimum_quantity: Decimal
    quantity_step: Decimal
    minimum_notional: Decimal
    wallet_balance: Decimal
    equity: Decimal
    available_balance: Decimal
    open_positions: int
    open_order_ids: frozenset[str]
    fills_read: bool
    fetched_at: datetime


class ControlledLiveGateway(Protocol):
    async def current_instrument_state(self, symbol: str) -> CurrentInstrumentState: ...

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
        instrument: FirstInstrumentSelection = CONTROLLED_LIVE_V1_FIRST_INSTRUMENT,
    ) -> None:
        self.repository = repository
        self.gateway = gateway
        self.admin_ids = set(admin_ids)
        self.profile = profile
        self.instrument = instrument

    def preview(
        self,
        admin_id: int,
        inputs: ManualOrderInputs,
        risk: ControlledRiskSnapshot,
        rules: InstrumentRules,
    ) -> ManualExecutionPreview:
        self._require_admin(admin_id)
        preview = build_manual_preview(
            inputs, risk, rules, profile=self.profile, instrument=self.instrument
        )
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
        (gates or ArmingGates.from_environment()).require_all(
            expected_symbol=preview.symbol
        )
        if preview.source not in {MANUAL_SOURCE, FROZEN_SIGNAL_SOURCE}:
            raise ControlledLiveBlocked(
                "Strategy and AI cannot submit directly; only an admin-approved frozen signal may execute"
            )
        if preview.source == FROZEN_SIGNAL_SOURCE:
            self.repository._verify_selection(preview.symbol, preview.selection_hash)
        current = await self.gateway.current_instrument_state(preview.symbol)
        self._validate_current_instrument(preview, current)
        if getattr(self.gateway, "dry_run", False):
            prepare = getattr(self.gateway, "dry_run_market_request", None)
            if prepare is not None:
                await prepare(preview, preview.client_order_id)
            raise ControlledLiveBlocked(
                "DRY_RUN signed and validated the request; mutating HTTP was not sent"
            )
        self.repository.claim_submission(preview, account_id)
        try:
            fill = await self.gateway.submit_market(preview, preview.client_order_id)
        except OrderRejected as error:
            self.repository.mark_rejected(preview, error)
            raise
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
                symbol=preview.symbol,
                stop_loss=preview.stop_loss,
                take_profit=preview.take_profit,
                reduce_only=True,
            )
        except Exception as protection_error:
            try:
                await self.gateway.emergency_close_reduce_only(
                    fill, preview.symbol
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
        snapshot = await self.gateway.snapshot()
        cancelled = 0
        active_symbols = {self.instrument.symbol} | {
            item.symbol for item in snapshot.positions if item.symbol in ALLOWED_SCANNER_SYMBOLS
        }
        for symbol in sorted(active_symbols):
            cancelled += await self.gateway.cancel_pending_orders(symbol)
        closed = 0
        if close_position:
            for position in snapshot.positions:
                if position.symbol not in ALLOWED_SCANNER_SYMBOLS:
                    continue
                fill = LiveFill(
                    order_id="emergency",
                    position_id=position.position_id,
                    filled_quantity=position.quantity,
                    average_price=Decimal(),
                    fee=Decimal(),
                )
                await self.gateway.emergency_close_reduce_only(
                    fill, position.symbol
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
                and item.symbol == preview.symbol
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

    def _validate_current_instrument(
        self,
        preview: ManualExecutionPreview,
        current: CurrentInstrumentState,
    ) -> None:
        if current.symbol != preview.symbol or current.status != "Trading":
            raise ControlledLiveBlocked("Selected USDT perpetual is not currently available")
        if current.quantity_step <= 0 or preview.quantity % current.quantity_step != 0:
            raise ControlledLiveBlocked("Quantity no longer matches the current instrument step")
        if preview.quantity < current.minimum_quantity:
            raise ControlledLiveBlocked("Quantity is below the current instrument minimum")
        actual_minimum = max(
            current.minimum_notional,
            current.minimum_quantity * current.ask_price,
        )
        if actual_minimum < Decimal("5") or actual_minimum > Decimal("10"):
            raise ControlledLiveBlocked(
                "Current minimum order notional is outside the immutable $5-$10 range"
            )
        if preview.quantity * current.ask_price > CONTROLLED_LIVE_V1.max_position_notional:
            raise ControlledLiveBlocked("Current ask price exceeds the first-order notional cap")
