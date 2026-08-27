"""Prospective multi-symbol scanner for the frozen Phase 4G strategy.

The scanner consumes only decisions already persisted by ``ProspectiveShadowEngine``.
It cannot invoke the strategy, Risk Manager, or any mutating Bybit endpoint.  The
separate proposal coordinator performs a fresh GET-only validation and stores at
most one admin-review preview.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from html import escape
import json
import os
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import (
    ControlledLiveStateRecord,
    ControlledLiveProposalRecord,
    ExecutionOrderRecord,
    FirstLiveProposalStateRecord,
    MultiSymbolScannerInstrumentRecord,
    MultiSymbolScannerStateRecord,
    ShadowDecisionRecord,
    ShadowCollectorStateRecord,
    ShadowTradeRecord,
    SignalWaitRuntimeRecord,
)
from app.exchanges.bybit_readonly import BybitMainnetReadOnlyClient
from app.exchanges.bybit_v5_gateway import _open_positions_planned_risk
from app.exchanges.models import InstrumentRules, OrderSide
from app.shadow.engine import PROTOCOL_ID
from app.shadow.status import execution_runtime_status
from app.strategy_lab.phase4g import FROZEN_CONFIG_HASH, FROZEN_VERSION
from app.trading.controlled_live import (
    CONTROLLED_LIVE_V1,
    ControlledLiveRepository,
    ControlledRiskSnapshot,
    FirstInstrumentSelection,
    ManualExecutionPreview,
    ManualOrderInputs,
    build_manual_preview,
)
from app.trading.controlled_universe import (
    FROZEN_SIGNAL_SOURCE,
    PROFILE_NAME,
    SCANNER_CONFIG,
    internal_symbol as _internal_symbol,
    scanner_selection_hash,
)
from app.trading.first_live_proposal import (
    CandidateRejected,
    FirstLiveProposalRepository,
    FrozenSignalCandidate,
    ProposalCycleResult,
    _native_levels,
)


INSTRUMENT_MAX_AGE = timedelta(minutes=5)


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value or "0"))


def _bool(value: Any) -> bool:
    return value is True or str(value).strip().lower() == "true"


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _ceil_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return Decimal()
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


if (
    SCANNER_CONFIG.base_profile_hash != CONTROLLED_LIVE_V1.config_hash
    or SCANNER_CONFIG.frozen_strategy != FROZEN_VERSION
    or SCANNER_CONFIG.frozen_strategy_hash != FROZEN_CONFIG_HASH
    or SCANNER_CONFIG.maximum_spread_pct != Decimal("0.002")
):
    raise RuntimeError("Scanner identity does not match frozen strategy/risk profile")
SCANNER_INTERNAL_SYMBOLS = tuple(_internal_symbol(item) for item in SCANNER_CONFIG.symbols)


@dataclass(frozen=True)
class ScannerInstrument:
    symbol: str
    internal_symbol: str
    enabled: bool
    exclusion_reason: str
    status: str
    contract_type: str
    bid: Decimal
    ask: Decimal
    tick_size: Decimal
    minimum_quantity: Decimal
    quantity_step: Decimal
    minimum_notional: Decimal
    actual_minimum_quantity: Decimal
    actual_minimum_notional: Decimal
    spread_pct: Decimal
    turnover_24h: Decimal
    maximum_leverage: Decimal
    checked_at: datetime


@dataclass(frozen=True)
class ScannerAccount:
    equity: Decimal
    available_balance: Decimal
    open_positions: int
    open_order_ids: frozenset[str]
    fills_read: bool
    trades_today: int
    daily_realized_pnl: Decimal
    consecutive_losses: int
    cooldown_until: datetime | None
    open_planned_risk: Decimal = Decimal()
    open_position_symbols: frozenset[str] = frozenset()
    blocking_open_order_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ScannerReadSnapshot:
    instruments: dict[str, ScannerInstrument]
    account: ScannerAccount
    fetched_at: datetime


class BybitMultiSymbolReadOnlyReader:
    """GET-only Mainnet reader; its transport has no mutation allowlist."""

    def __init__(self, client: BybitMainnetReadOnlyClient) -> None:
        self.client = client

    @classmethod
    def from_environment(cls) -> BybitMultiSymbolReadOnlyReader:
        return cls(
            BybitMainnetReadOnlyClient(
                os.getenv("BYBIT_API_KEY", ""),
                os.getenv("BYBIT_API_SECRET", ""),
            )
        )

    async def read(self) -> ScannerReadSnapshot:
        await self.client.synchronize_time()
        day_start_ms = int(
            datetime.now(UTC)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
            * 1000
        )
        public = await asyncio.gather(
            *(
                asyncio.gather(
                    self.client.public_get(
                        "/v5/market/instruments-info",
                        {"category": "linear", "symbol": symbol},
                    ),
                    self.client.public_get(
                        "/v5/market/tickers",
                        {"category": "linear", "symbol": symbol},
                    ),
                )
                for symbol in SCANNER_CONFIG.symbols
            )
        )
        wallet, positions, orders, fills = await asyncio.gather(
            self.client.private_get(
                "/v5/account/wallet-balance",
                {"accountType": "UNIFIED", "coin": "USDT"},
            ),
            self.client.private_get(
                "/v5/position/list", {"category": "linear", "settleCoin": "USDT"}
            ),
            self.client.private_get(
                "/v5/order/realtime",
                {"category": "linear", "settleCoin": "USDT", "openOnly": 0, "limit": 50},
            ),
            self.client.private_get(
                "/v5/execution/list",
                {"category": "linear", "startTime": day_start_ms, "limit": 100},
            ),
        )
        execution_rows = list(fills.result.get("list") or [])
        cursor = str(fills.result.get("nextPageCursor") or "")
        seen_cursors: set[str] = set()
        while cursor:
            if cursor in seen_cursors or len(seen_cursors) >= 49:
                raise RuntimeError("Bybit execution pagination is incomplete")
            seen_cursors.add(cursor)
            page = await self.client.private_get(
                "/v5/execution/list",
                {
                    "category": "linear",
                    "startTime": day_start_ms,
                    "limit": 100,
                    "cursor": cursor,
                },
            )
            execution_rows.extend(page.result.get("list") or [])
            cursor = str(page.result.get("nextPageCursor") or "")
        now = datetime.now(UTC)
        instruments: dict[str, ScannerInstrument] = {}
        for symbol, (instrument_result, ticker_result) in zip(
            SCANNER_CONFIG.symbols, public, strict=True
        ):
            instrument = (instrument_result.result.get("list") or [None])[0]
            ticker = (ticker_result.result.get("list") or [None])[0]
            if not instrument or not ticker:
                instruments[symbol] = _unavailable_instrument(symbol, now)
                continue
            instruments[symbol] = _parse_instrument(symbol, instrument, ticker, now)

        accounts = wallet.result.get("list") or []
        account = accounts[0] if accounts else {}
        executions = execution_rows
        day_start = datetime(now.year, now.month, now.day, tzinfo=UTC)
        daily = [
            item
            for item in executions
            if _execution_time(item) is not None
            and _execution_time(item) >= day_start
        ]
        order_results: dict[str, Decimal] = {}
        order_times: dict[str, datetime] = {}
        for item in daily:
            order_id = str(item.get("orderId") or item.get("execId") or "")
            if not order_id:
                continue
            order_results[order_id] = order_results.get(order_id, Decimal()) + (
                _decimal(item.get("execPnl")) - abs(_decimal(item.get("execFee")))
            )
            execution_time = _execution_time(item)
            if execution_time:
                order_times[order_id] = max(order_times.get(order_id, execution_time), execution_time)
        ordered_results = sorted(
            order_results.items(), key=lambda item: order_times[item[0]], reverse=True
        )
        losses = 0
        latest_loss_time = None
        for order_id, pnl in ordered_results:
            if pnl < 0:
                losses += 1
                latest_loss_time = latest_loss_time or order_times[order_id]
            else:
                break
        position_rows = [
            item
            for item in positions.result.get("list") or []
            if _decimal(item.get("size")) > 0
        ]
        order_rows = orders.result.get("list") or []
        return ScannerReadSnapshot(
            instruments,
            ScannerAccount(
                equity=_decimal(account.get("totalEquity")),
                available_balance=_decimal(account.get("totalAvailableBalance")),
                open_positions=len(position_rows),
                open_order_ids=frozenset(
                    str(item.get("orderId"))
                    for item in order_rows
                    if item.get("orderId")
                ),
                fills_read=isinstance(fills.result.get("list"), list),
                trades_today=len(order_results),
                daily_realized_pnl=sum(order_results.values(), Decimal()),
                consecutive_losses=losses,
                cooldown_until=(
                    day_start + timedelta(days=1)
                    if latest_loss_time
                    and losses >= CONTROLLED_LIVE_V1.max_consecutive_losses
                    and CONTROLLED_LIVE_V1.consecutive_loss_stop_until_next_utc_day
                    else None
                ),
                open_planned_risk=_open_positions_planned_risk(position_rows),
                open_position_symbols=frozenset(
                    str(item.get("symbol"))
                    for item in position_rows
                    if item.get("symbol")
                ),
                blocking_open_order_ids=frozenset(
                    str(item.get("orderId"))
                    for item in order_rows
                    if item.get("orderId") and not _bool(item.get("reduceOnly"))
                ),
            ),
            now,
        )

    async def close(self) -> None:
        await self.client.close()


def _execution_time(item: dict[str, Any]) -> datetime | None:
    raw = item.get("execTime")
    if raw in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(raw) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


def _parse_instrument(
    symbol: str, instrument: dict[str, Any], ticker: dict[str, Any], now: datetime
) -> ScannerInstrument:
    lot = instrument.get("lotSizeFilter") or {}
    price_filter = instrument.get("priceFilter") or {}
    bid = _decimal(ticker.get("bid1Price"))
    ask = _decimal(ticker.get("ask1Price"))
    step = _decimal(lot.get("qtyStep"))
    minimum_quantity = _decimal(lot.get("minOrderQty"))
    minimum_notional = _decimal(lot.get("minNotionalValue"))
    actual_quantity = max(
        minimum_quantity,
        _ceil_step(minimum_notional / ask, step) if ask > 0 else Decimal(),
    )
    actual_notional = actual_quantity * ask
    midpoint = (bid + ask) / 2
    spread_pct = (ask - bid) / midpoint if midpoint > 0 and ask >= bid else Decimal("Infinity")
    turnover = _decimal(ticker.get("turnover24h"))
    maximum_leverage = _decimal(
        (instrument.get("leverageFilter") or {}).get("maxLeverage")
    )
    status = str(instrument.get("status") or "")
    contract_type = str(instrument.get("contractType") or "")
    reasons = []
    if status != "Trading" or contract_type != "LinearPerpetual":
        reasons.append("Linear Perpetual недоступен")
    if bid <= 0 or ask <= 0 or ask < bid or step <= 0:
        reasons.append("некорректные instrument/quote данные")
    if actual_notional > SCANNER_CONFIG.maximum_actual_minimum_notional:
        reasons.append(
            f"минимальный фактический ордер ${actual_notional} превышает $10"
        )
    if turnover < SCANNER_CONFIG.minimum_turnover_24h:
        reasons.append(
            f"оборот 24ч ${turnover} ниже ${SCANNER_CONFIG.minimum_turnover_24h}"
        )
    if spread_pct > SCANNER_CONFIG.maximum_spread_pct:
        reasons.append(
            f"spread {spread_pct:.6%} выше лимита {SCANNER_CONFIG.maximum_spread_pct:.2%}"
        )
    return ScannerInstrument(
        symbol,
        _internal_symbol(symbol),
        not reasons,
        "; ".join(reasons),
        status,
        contract_type,
        bid,
        ask,
        _decimal(price_filter.get("tickSize")),
        minimum_quantity,
        step,
        minimum_notional,
        actual_quantity,
        actual_notional,
        spread_pct,
        turnover,
        maximum_leverage,
        now,
    )


def _unavailable_instrument(symbol: str, now: datetime) -> ScannerInstrument:
    return ScannerInstrument(
        symbol,
        _internal_symbol(symbol),
        False,
        "Bybit instrument/ticker временно недоступен",
        "UNKNOWN",
        "UNKNOWN",
        *(Decimal() for _ in range(11)),
        now,
    )


class MultiSymbolScannerRepository:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self.session_factory = session_factory

    def initialize(self, now: datetime | None = None) -> MultiSymbolScannerStateRecord:
        current = now or datetime.now(UTC)
        with self.session_factory.begin() as session:
            record = session.get(MultiSymbolScannerStateRecord, PROFILE_NAME)
            if record is None:
                record = MultiSymbolScannerStateRecord(
                    profile_name=PROFILE_NAME,
                    config_hash=SCANNER_CONFIG.config_hash,
                    started_at=current,
                    status="RUNNING",
                    updated_at=current,
                )
                session.add(record)
            elif record.config_hash != SCANNER_CONFIG.config_hash:
                raise RuntimeError("MULTI-SYMBOL SCANNER HASH MISMATCH")
        return self.state()

    def state(self) -> MultiSymbolScannerStateRecord:
        with self.session_factory() as session:
            record = session.get(MultiSymbolScannerStateRecord, PROFILE_NAME)
            if record is None:
                raise RuntimeError("Multi-symbol scanner state is not initialized")
            if record.config_hash != SCANNER_CONFIG.config_hash:
                raise RuntimeError("MULTI-SYMBOL SCANNER HASH MISMATCH")
            session.expunge(record)
            return record

    def save_market_snapshot(self, snapshot: ScannerReadSnapshot) -> None:
        with self.session_factory.begin() as session:
            state = session.get(MultiSymbolScannerStateRecord, PROFILE_NAME)
            if state is None or state.config_hash != SCANNER_CONFIG.config_hash:
                raise RuntimeError("MULTI-SYMBOL SCANNER HASH MISMATCH")
            state.status = "RUNNING"
            state.last_error = None
            state.updated_at = snapshot.fetched_at
            account = session.get(SignalWaitRuntimeRecord, CONTROLLED_LIVE_V1.name)
            if account is None:
                account = SignalWaitRuntimeRecord(
                    profile_name=CONTROLLED_LIVE_V1.name,
                    updated_at=snapshot.fetched_at,
                )
                session.add(account)
            account.equity = snapshot.account.equity
            account.open_positions = snapshot.account.open_positions
            account.open_orders = len(snapshot.account.open_order_ids)
            account.trades_today = snapshot.account.trades_today
            account.daily_realized_pnl = snapshot.account.daily_realized_pnl
            account.open_planned_risk = snapshot.account.open_planned_risk
            account.account_checked_at = snapshot.fetched_at
            account.account_error = None
            account.updated_at = snapshot.fetched_at
            for item in snapshot.instruments.values():
                record = session.get(
                    MultiSymbolScannerInstrumentRecord, (PROFILE_NAME, item.symbol)
                )
                if record is None:
                    record = MultiSymbolScannerInstrumentRecord(
                        profile_name=PROFILE_NAME,
                        symbol=item.symbol,
                        internal_symbol=item.internal_symbol,
                        instrument_status=item.status,
                        contract_type=item.contract_type,
                        bid_price=item.bid,
                        ask_price=item.ask,
                        tick_size=item.tick_size,
                        minimum_quantity=item.minimum_quantity,
                        quantity_step=item.quantity_step,
                        minimum_notional=item.minimum_notional,
                        actual_minimum_quantity=item.actual_minimum_quantity,
                        actual_minimum_notional=item.actual_minimum_notional,
                        spread_pct=item.spread_pct,
                        turnover_24h=item.turnover_24h,
                        checked_at=item.checked_at,
                        updated_at=snapshot.fetched_at,
                    )
                    session.add(record)
                record.enabled = item.enabled
                record.exclusion_reason = item.exclusion_reason
                record.instrument_status = item.status
                record.contract_type = item.contract_type
                record.bid_price = item.bid
                record.ask_price = item.ask
                record.tick_size = item.tick_size
                record.minimum_quantity = item.minimum_quantity
                record.quantity_step = item.quantity_step
                record.minimum_notional = item.minimum_notional
                record.actual_minimum_quantity = item.actual_minimum_quantity
                record.actual_minimum_notional = item.actual_minimum_notional
                record.spread_pct = item.spread_pct
                record.turnover_24h = item.turnover_24h
                record.checked_at = item.checked_at
                record.updated_at = snapshot.fetched_at

    def mark_transient_error(self, reason: str) -> None:
        with self.session_factory.begin() as session:
            state = session.get(MultiSymbolScannerStateRecord, PROFILE_NAME)
            if state is None or state.config_hash != SCANNER_CONFIG.config_hash:
                raise RuntimeError("MULTI-SYMBOL SCANNER HASH MISMATCH")
            state.status = "DEGRADED"
            state.last_error = reason[:1000]
            state.updated_at = datetime.now(UTC)

    def instruments(self) -> dict[str, MultiSymbolScannerInstrumentRecord]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(MultiSymbolScannerInstrumentRecord).where(
                    MultiSymbolScannerInstrumentRecord.profile_name == PROFILE_NAME
                )
            ).all()
            for row in rows:
                session.expunge(row)
            return {row.symbol: row for row in rows}

    def next_candidate_batch(self) -> tuple[FrozenSignalCandidate, ...]:
        state = self.state()
        with self.session_factory() as session:
            phase = session.get(FirstLiveProposalStateRecord, CONTROLLED_LIVE_V1.name)
            if (
                phase is None
                or phase.proposal_id
                or phase.status not in {"WAITING_FOR_SIGNAL", "AUTO_RUNNING"}
            ):
                return ()
            filters = [
                ShadowDecisionRecord.protocol_id == PROTOCOL_ID,
                ShadowDecisionRecord.exchange == "bybit",
                ShadowDecisionRecord.symbol.in_(SCANNER_INTERNAL_SYMBOLS),
                ShadowDecisionRecord.decision.in_(("LONG", "SHORT")),
                ShadowDecisionRecord.risk_status == "ALLOW",
                ShadowDecisionRecord.signal_score >= CONTROLLED_LIVE_V1.signal_threshold,
                ShadowDecisionRecord.strategy_hash == FROZEN_CONFIG_HASH,
                ShadowDecisionRecord.created_at > state.started_at,
            ]
            if state.last_scanned_candle_open is not None:
                filters.append(
                    ShadowDecisionRecord.candle_open_time
                    > state.last_scanned_candle_open
                )
            first_candle = session.scalar(
                select(ShadowDecisionRecord.candle_open_time)
                .where(*filters)
                .order_by(ShadowDecisionRecord.candle_open_time)
                .limit(1)
            )
            if first_candle is None:
                return ()
            rows = session.execute(
                select(ShadowDecisionRecord, ShadowTradeRecord)
                .join(
                    ShadowTradeRecord,
                    ShadowTradeRecord.decision_id == ShadowDecisionRecord.id,
                )
                .where(*filters, ShadowDecisionRecord.candle_open_time == first_candle)
                .order_by(ShadowDecisionRecord.symbol)
            ).all()
            result = []
            for decision, trade in rows:
                session.expunge(decision)
                session.expunge(trade)
                result.append(FrozenSignalCandidate(decision, trade))
            return tuple(result)

    def mark_batch_scanned(self, candle_open: datetime, reason: str = "") -> None:
        with self.session_factory.begin() as session:
            state = session.get(MultiSymbolScannerStateRecord, PROFILE_NAME)
            if state is None or state.config_hash != SCANNER_CONFIG.config_hash:
                raise RuntimeError("MULTI-SYMBOL SCANNER HASH MISMATCH")
            state.last_scanned_candle_open = candle_open
            state.last_error = reason[:1000] or None
            state.updated_at = datetime.now(UTC)

    def mark_ready(self, candle_open: datetime) -> None:
        with self.session_factory.begin() as session:
            state = session.get(MultiSymbolScannerStateRecord, PROFILE_NAME)
            if state is None or state.config_hash != SCANNER_CONFIG.config_hash:
                raise RuntimeError("MULTI-SYMBOL SCANNER HASH MISMATCH")
            state.last_scanned_candle_open = candle_open
            state.status = "READY_FOR_USER_APPROVAL"
            state.last_error = None
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

    def unresolved_execution_attempts(self) -> int:
        with self.session_factory() as session:
            return int(
                session.scalar(
                    select(func.count(ExecutionOrderRecord.id)).where(
                        ExecutionOrderRecord.exchange == "bybit",
                        ExecutionOrderRecord.status.in_(
                            ("PENDING", "SUBMITTED", "UNKNOWN")
                        ),
                    )
                )
                or 0
            )


@dataclass(frozen=True)
class RankedPreview:
    candidate: FrozenSignalCandidate
    instrument: ScannerInstrument
    preview: ManualExecutionPreview
    cost_ratio: Decimal


class MultiSymbolFirstProposalCoordinator:
    def __init__(
        self,
        scanner_repository: MultiSymbolScannerRepository,
        phase_repository: FirstLiveProposalRepository,
        reader: BybitMultiSymbolReadOnlyReader,
        admin_ids: set[int],
    ) -> None:
        self.scanner_repository = scanner_repository
        self.phase_repository = phase_repository
        self.reader = reader
        self.admin_ids = set(admin_ids)

    async def cycle(self) -> ProposalCycleResult:
        self.scanner_repository.initialize()
        phase = self.phase_repository.initialize()
        snapshot = None
        if phase.proposal_id:
            if phase.status in {"FIRST_EXECUTION_VALIDATED", "AUTO_POSITION_OPEN"}:
                try:
                    snapshot = await self.reader.read()
                except Exception:
                    self.scanner_repository.mark_transient_error(
                        "Read-only Bybit scanner unavailable during position rollover"
                    )
                    return ProposalCycleResult(
                        phase.status,
                        "Cannot verify prior position closure; automatic scan remains blocked",
                    )
                if (
                    snapshot.account.open_positions >= CONTROLLED_LIVE_V1.max_positions
                    or snapshot.account.blocking_open_order_ids
                ):
                    return ProposalCycleResult(
                        phase.status,
                        "Position capacity is full or an entry order remains active",
                    )
                self.phase_repository.release_position_slot_for_automatic_scan()
                phase = self.phase_repository.state()
            else:
                record = self.phase_repository.proposal(phase.proposal_id)
                from app.trading.first_live_proposal import preview_from_record

                preview = preview_from_record(record) if record else None
                return ProposalCycleResult(
                    phase.status,
                    "Immutable proposal already exists",
                    preview,
                    Decimal(phase.available_equity or 0),
                    record.admin_telegram_id if record else None,
                    notify=bool(record and phase.notified_at is None),
                )
        if not self.admin_ids:
            return ProposalCycleResult("WAITING_FOR_SIGNAL", "Admin is not configured")
        if snapshot is None:
            try:
                snapshot = await self.reader.read()
            except Exception as error:
                self.scanner_repository.mark_transient_error(
                    f"{type(error).__name__}: read-only Bybit scanner unavailable"
                )
                return ProposalCycleResult(
                    "WAITING_FOR_SIGNAL",
                    "Read-only scanner temporarily unavailable; no signal was consumed",
                )
        self.scanner_repository.save_market_snapshot(snapshot)
        experiment_start_equity, starting_day_equity = ControlledLiveRepository(
            self.scanner_repository.session_factory
        ).refresh_loss_baselines(
            snapshot.account.equity,
            snapshot.account.daily_realized_pnl,
            snapshot.fetched_at,
        )
        candidates = self.scanner_repository.next_candidate_batch()
        if not candidates:
            return ProposalCycleResult("WAITING_FOR_SIGNAL", "No new admissible frozen signal")
        if self.scanner_repository.unresolved_execution_attempts():
            return ProposalCycleResult(
                "WAITING_FOR_SIGNAL",
                "Local execution ledger contains an unresolved order",
            )
        automatic = ControlledLiveRepository(
            self.scanner_repository.session_factory
        ).state().automatic_execution_enabled
        ranked: list[RankedPreview] = []
        rejections = []
        for candidate in candidates:
            symbol = candidate.decision.symbol.replace("/", "")
            instrument = snapshot.instruments.get(symbol)
            if instrument is None or not instrument.enabled:
                rejections.append(
                    f"{symbol}: {instrument.exclusion_reason if instrument else 'no instrument snapshot'}"
                )
                continue
            try:
                preview = self._build(
                    candidate,
                    instrument,
                    snapshot.account,
                    experiment_start_equity,
                    starting_day_equity,
                )
            except CandidateRejected as error:
                rejections.append(f"{symbol}: {error}")
                continue
            cost_ratio = (
                preview.expected_fee / preview.expected_notional + instrument.spread_pct
                if preview.expected_notional > 0
                else Decimal("Infinity")
            )
            ranked.append(RankedPreview(candidate, instrument, preview, cost_ratio))
        candle_open = candidates[0].decision.candle_open_time
        if not ranked:
            self.scanner_repository.mark_batch_scanned(candle_open, "; ".join(rejections))
            return ProposalCycleResult(
                "WAITING_FOR_SIGNAL", "; ".join(rejections) or "No admissible candidate"
            )
        winner = sorted(
            ranked,
            key=lambda item: (
                -int(item.candidate.decision.signal_score),
                item.cost_ratio,
                -item.instrument.turnover_24h,
                item.instrument.symbol,
            ),
        )[0]
        admin_id = min(self.admin_ids)
        created = self.phase_repository.save_ready(
            winner.candidate,
            winner.preview,
            admin_id,
            snapshot.account.equity,
            automatic=automatic,
        )
        if created:
            self.scanner_repository.mark_ready(candle_open)
        return ProposalCycleResult(
            "APPROVED_FOR_EXECUTION" if automatic else "READY_FOR_USER_APPROVAL",
            (
                f"Natural {winner.instrument.symbol} signal passed automatic controlled-live gates"
                if automatic
                else f"Natural {winner.instrument.symbol} signal won deterministic ranking"
            ),
            winner.preview,
            snapshot.account.equity,
            admin_id,
            notify=created and not automatic,
        )

    def _build(
        self,
        candidate: FrozenSignalCandidate,
        instrument: ScannerInstrument,
        account: ScannerAccount,
        experiment_start_equity: Decimal,
        starting_day_equity: Decimal,
    ) -> ManualExecutionPreview:
        if candidate.decision.strategy_hash != FROZEN_CONFIG_HASH:
            raise CandidateRejected("Frozen strategy hash mismatch")
        if candidate.decision.risk_status != "ALLOW":
            raise CandidateRejected("Deterministic Risk Manager did not ALLOW")
        if candidate.decision.signal_score < CONTROLLED_LIVE_V1.signal_threshold:
            raise CandidateRejected("Signal score is below the controlled-live threshold")
        if account.open_positions >= CONTROLLED_LIVE_V1.max_positions:
            raise CandidateRejected("Maximum open positions reached")
        if instrument.symbol in account.open_position_symbols:
            raise CandidateRejected("Bybit already has a position for this symbol")
        if account.blocking_open_order_ids:
            raise CandidateRejected("Bybit already has a pending entry order")
        if not account.fills_read:
            raise CandidateRejected("Bybit fills read/reconciliation failed")
        if datetime.now(UTC) - instrument.checked_at > INSTRUMENT_MAX_AGE:
            raise CandidateRejected("STALE DATA: instrument snapshot is older than five minutes")
        side = OrderSide.BUY if candidate.decision.decision == "LONG" else OrderSide.SELL
        entry = instrument.ask if side is OrderSide.BUY else instrument.bid
        stop, target = _native_levels(
            side,
            entry,
            Decimal(candidate.trade.stop_loss),
            Decimal(candidate.trade.take_profit),
            instrument.tick_size,
        )
        selection = FirstInstrumentSelection(
            instrument.symbol,
            instrument.internal_symbol,
            "bybit",
            "USDT_PERPETUAL",
            CONTROLLED_LIVE_V1.config_hash,
            SCANNER_CONFIG.maximum_order_notional,
            scanner_selection_hash(instrument.symbol),
        )
        preview = build_manual_preview(
            ManualOrderInputs(side, entry, stop, target),
            ControlledRiskSnapshot(
                equity=account.equity,
                available_balance=account.available_balance,
                open_positions=account.open_positions,
                trades_today=account.trades_today,
                daily_realized_pnl=account.daily_realized_pnl,
                consecutive_losses=account.consecutive_losses,
                cooldown_until=account.cooldown_until,
                starting_day_equity=starting_day_equity,
                experiment_start_equity=experiment_start_equity,
                open_planned_risk=account.open_planned_risk,
            ),
            InstrumentRules(
                instrument.tick_size,
                instrument.quantity_step,
                instrument.minimum_quantity,
                instrument.minimum_notional,
                maximum_leverage=instrument.maximum_leverage,
            ),
            instrument=selection,
        )
        from dataclasses import replace

        preview = replace(
            preview,
            source=FROZEN_SIGNAL_SOURCE,
            signal_score=int(candidate.decision.signal_score),
        )
        if not preview.executable:
            raise CandidateRejected(preview.reason)
        if preview.expected_notional > SCANNER_CONFIG.maximum_order_notional:
            raise CandidateRejected("Controlled-live $10 notional cap exceeded")
        return preview

    async def close(self) -> None:
        await self.reader.close()


@dataclass(frozen=True)
class ScannerSymbolStatus:
    symbol: str
    status: str
    signal_score: int
    reason: str
    candle_close: datetime | None
    analyzed_at: datetime | None


@dataclass(frozen=True)
class MultiSymbolScannerStatus:
    config_hash: str
    started_at: datetime | None
    symbols: tuple[ScannerSymbolStatus, ...]
    last_closed_candle: datetime | None
    analyses_today: int
    long_candidates: int
    short_candidates: int
    risk_rejects: int
    best_candidate: str
    closest_symbol: str
    closest_score: int
    score_gap: int
    last_analysis: datetime | None
    equity: Decimal | None
    open_positions: int | None
    open_orders: int | None
    trades_today: int | None
    daily_realized_pnl: Decimal | None
    open_planned_risk: Decimal | None
    remaining_daily_loss: Decimal | None
    remaining_experiment_loss: Decimal | None
    shadow_runtime: str
    controlled_live_runtime: str
    real_order_execution_runtime: str
    runtime_dry_run: bool | None
    runtime_live_trading_enabled: bool | None
    runtime_controlled_live_enabled: bool | None


def scanner_status(session_factory: Callable[[], Session], now: datetime | None = None) -> MultiSymbolScannerStatus:
    current = now or datetime.now(UTC)
    today = datetime(current.year, current.month, current.day, tzinfo=UTC)
    repository = MultiSymbolScannerRepository(session_factory)
    try:
        state = repository.state()
    except RuntimeError:
        state = None
    instruments = repository.instruments()
    statuses = []
    latest_close = None
    last_analysis = None
    with session_factory() as session:
        daily_filters = [
            ShadowDecisionRecord.protocol_id == PROTOCOL_ID,
            ShadowDecisionRecord.exchange == "bybit",
            ShadowDecisionRecord.symbol.in_(SCANNER_INTERNAL_SYMBOLS),
            ShadowDecisionRecord.created_at >= today,
        ]
        analyses = int(session.scalar(select(func.count()).select_from(ShadowDecisionRecord).where(*daily_filters)) or 0)
        long_candidates = int(session.scalar(select(func.count()).select_from(ShadowDecisionRecord).where(*daily_filters, ShadowDecisionRecord.decision == "LONG", ShadowDecisionRecord.risk_status == "ALLOW")) or 0)
        short_candidates = int(session.scalar(select(func.count()).select_from(ShadowDecisionRecord).where(*daily_filters, ShadowDecisionRecord.decision == "SHORT", ShadowDecisionRecord.risk_status == "ALLOW")) or 0)
        rejects = int(session.scalar(select(func.count()).select_from(ShadowDecisionRecord).where(*daily_filters, ShadowDecisionRecord.decision.in_(("LONG", "SHORT")), ShadowDecisionRecord.risk_status == "REJECT")) or 0)
        best_rows = []
        for symbol, internal in zip(SCANNER_CONFIG.symbols, SCANNER_INTERNAL_SYMBOLS, strict=True):
            decision = session.scalar(
                select(ShadowDecisionRecord)
                .where(
                    ShadowDecisionRecord.protocol_id == PROTOCOL_ID,
                    ShadowDecisionRecord.exchange == "bybit",
                    ShadowDecisionRecord.symbol == internal,
                )
                .order_by(ShadowDecisionRecord.candle_open_time.desc())
                .limit(1)
            )
            instrument = instruments.get(symbol)
            if instrument is not None and not instrument.enabled:
                status = "ИСКЛЮЧЕН"
                reason = instrument.exclusion_reason
                score = 0
            elif decision is None:
                status, reason, score = "НЕТ ДАННЫХ", "решений ещё нет", 0
            elif decision.decision in {"LONG", "SHORT"} and decision.risk_status == "REJECT":
                status, reason, score = "RISK REJECT", decision.risk_reason, int(decision.signal_score)
            else:
                status = decision.decision
                reason = _persisted_decision_reason(decision)
                score = int(decision.signal_score)
            close_time = _aware(decision.signal_timestamp) if decision else None
            analyzed_at = _aware(decision.created_at) if decision else None
            if close_time and (latest_close is None or close_time > latest_close):
                latest_close = close_time
            if analyzed_at and (last_analysis is None or analyzed_at > last_analysis):
                last_analysis = analyzed_at
            statuses.append(
                ScannerSymbolStatus(
                    symbol, status, score, reason, close_time, analyzed_at
                )
            )
            if (
                decision is not None
                and instrument is not None
                and instrument.enabled
                and decision.decision in {"LONG", "SHORT"}
                and decision.risk_status == "ALLOW"
            ):
                best_rows.append((decision, instrument))
        if best_rows:
            best_decision, best_instrument = sorted(
                best_rows,
                key=lambda item: (
                    -int(item[0].signal_score),
                    Decimal(item[1].spread_pct),
                    -Decimal(item[1].turnover_24h),
                    item[1].symbol,
                ),
            )[0]
            best = f"{best_instrument.symbol} {best_decision.decision} / score {best_decision.signal_score}"
        else:
            best = "НЕТ"
        proposal = session.scalar(
            select(ControlledLiveProposalRecord)
            .where(ControlledLiveProposalRecord.source == FROZEN_SIGNAL_SOURCE)
            .order_by(ControlledLiveProposalRecord.created_at.desc())
            .limit(1)
        )
        if proposal is not None:
            preview = json.loads(proposal.preview_json)
            best = f"{preview.get('symbol')} {'LONG' if preview.get('side') == 'BUY' else 'SHORT'} / READY"
        eligible = [
            item
            for item in statuses
            if item.status not in {"ИСКЛЮЧЕН", "НЕТ ДАННЫХ"}
        ]
        closest_score = max((item.signal_score for item in eligible), default=0)
        closest_symbols = tuple(
            item.symbol for item in eligible if item.signal_score == closest_score
        )
        runtime_account = session.get(
            SignalWaitRuntimeRecord, CONTROLLED_LIVE_V1.name
        )
        controlled = session.get(ControlledLiveStateRecord, CONTROLLED_LIVE_V1.name)
        collector = session.get(ShadowCollectorStateRecord, PROTOCOL_ID)
        execution_runtime = execution_runtime_status(collector, current)
        equity = (
            Decimal(runtime_account.equity)
            if runtime_account and runtime_account.equity is not None
            else None
        )
        positions = (
            int(runtime_account.open_positions)
            if runtime_account and runtime_account.open_positions is not None
            else None
        )
        orders = (
            int(runtime_account.open_orders)
            if runtime_account and runtime_account.open_orders is not None
            else None
        )
        trades_today = (
            int(runtime_account.trades_today)
            if runtime_account and runtime_account.trades_today is not None
            else None
        )
        daily_pnl = (
            Decimal(runtime_account.daily_realized_pnl)
            if runtime_account and runtime_account.daily_realized_pnl is not None
            else None
        )
        open_planned_risk = (
            Decimal(runtime_account.open_planned_risk)
            if runtime_account and runtime_account.open_planned_risk is not None
            else None
        )
        experiment_start_equity = (
            Decimal(controlled.experiment_start_equity)
            if controlled and controlled.experiment_start_equity is not None
            else None
        )
        remaining_daily = (
            max(
                Decimal(),
                CONTROLLED_LIVE_V1.daily_max_loss_usdt
                - max(Decimal(), -daily_pnl)
                - open_planned_risk,
            )
            if daily_pnl is not None and open_planned_risk is not None
            else None
        )
        remaining_experiment = (
            max(
                Decimal(),
                CONTROLLED_LIVE_V1.total_experiment_loss_limit
                - max(Decimal(), experiment_start_equity - equity),
            )
            if experiment_start_equity is not None and equity is not None
            else None
        )
    return MultiSymbolScannerStatus(
        SCANNER_CONFIG.config_hash,
        _aware(state.started_at) if state else None,
        tuple(statuses),
        latest_close,
        analyses,
        long_candidates,
        short_candidates,
        rejects,
        best,
        " / ".join(closest_symbols) if closest_symbols else "НЕТ",
        closest_score,
        max(0, CONTROLLED_LIVE_V1.signal_threshold - closest_score)
        if closest_symbols
        else CONTROLLED_LIVE_V1.signal_threshold,
        last_analysis,
        equity,
        positions,
        orders,
        trades_today,
        daily_pnl,
        open_planned_risk,
        remaining_daily,
        remaining_experiment,
        execution_runtime["shadow"],
        execution_runtime["controlled_live"],
        execution_runtime["real_order_execution"],
        execution_runtime["dry_run"],
        execution_runtime["live_trading_enabled"],
        execution_runtime["controlled_live_enabled"],
    )


def format_scanner_status_ru(status: MultiSymbolScannerStatus) -> str:
    lines = [
        "🟢 <b>CONTROLLED LIVE STATUS</b>",
        "",
        f"SHADOW: <b>{status.shadow_runtime}</b>",
        f"CONTROLLED LIVE: <b>{status.controlled_live_runtime}</b>",
        "REAL ORDER EXECUTION: "
        f"<b>{status.real_order_execution_runtime}</b>",
        "",
        "Фактические flags execution-сервиса:",
        f"DRY_RUN={_status_flag(status.runtime_dry_run)}",
        "LIVE_TRADING_ENABLED="
        f"{_status_flag(status.runtime_live_trading_enabled)}",
        "CONTROLLED_LIVE_ENABLED="
        f"{_status_flag(status.runtime_controlled_live_enabled)}",
        "",
        "Активные настройки:",
        f"Threshold: {CONTROLLED_LIVE_V1.signal_threshold}",
        f"Риск: {_compact_decimal(CONTROLLED_LIVE_V1.risk_per_trade_pct * 100)}% equity",
        f"Плечо: {_compact_decimal(CONTROLLED_LIVE_V1.leverage)}x",
        "Минимальный R/R: 1:"
        + _compact_decimal(CONTROLLED_LIVE_V1.minimum_risk_reward),
        "",
        "⏳ <b>MULTI-SYMBOL SIGNAL SCANNER</b>",
        "",
        "Последний анализ: "
        + (status.last_analysis.strftime("%Y-%m-%d %H:%M:%S UTC") if status.last_analysis else "НЕТ"),
    ]
    for item in status.symbols:
        short = item.symbol.removesuffix("USDT")
        lines.append(
            f"{short} — score {item.signal_score} — {item.status}"
        )
        lines.append(
            f"  Причина pipeline: {escape(item.reason or 'не сохранена')}"
        )
    lines.extend(
        [
            "",
            "Последняя закрытая 1H candle: "
            + (status.last_closed_candle.strftime("%Y-%m-%d %H:%M UTC") if status.last_closed_candle else "НЕТ"),
            f"Анализов сегодня: {status.analyses_today}",
            f"LONG candidates: {status.long_candidates}",
            f"SHORT candidates: {status.short_candidates}",
            f"Risk Manager rejects: {status.risk_rejects}",
            f"Лучший текущий candidate: {status.best_candidate}",
            f"Ближе всего к threshold 70: {status.closest_symbol} "
            f"(score {status.closest_score})",
            f"Не хватает до входа: {status.score_gap} score",
            "",
            f"Текущая equity: {_status_money(status.equity)}",
            f"Позиции: {_status_number(status.open_positions)} / {CONTROLLED_LIVE_V1.max_positions}",
            f"Открытые orders: {_status_number(status.open_orders)}",
            f"Сделок сегодня: {_status_number(status.trades_today)} (без лимита)",
            f"Realized PnL сегодня: {_status_money(status.daily_realized_pnl)}",
            "Риск открытых позиций до SL: "
            + _status_money(status.open_planned_risk),
            "Остаток дневного risk budget из $5: "
            + _status_money(status.remaining_daily_loss),
            "Остаток общего лимита эксперимента $10: "
            + _status_money(status.remaining_experiment_loss),
            f"Scanner hash: <code>{status.config_hash[:12]}…</code>",
        ]
    )
    return "\n".join(lines)


def format_scanner_wait_reasons_ru(status: MultiSymbolScannerStatus) -> str:
    lines = [
        "📊 <b>ПОЧЕМУ WAIT?</b>",
        "",
        "Только фактические причины, сохранённые текущим pipeline:",
    ]
    waiting = [
        item
        for item in status.symbols
        if item.status in {"WAIT", "RISK REJECT", "ИСКЛЮЧЕН", "НЕТ ДАННЫХ"}
    ]
    if not waiting:
        lines.append("Последнее состояние не содержит WAIT/reject.")
    else:
        for item in waiting:
            lines.append(
                f"• {item.symbol}: {item.status}, score {item.signal_score} — "
                f"{escape(item.reason or 'причина не сохранена')}"
            )
    lines.extend(
        [
            "",
            "Диагностические причины не дополняются предположениями Telegram-бота.",
        ]
    )
    return "\n".join(lines)


def _persisted_decision_reason(decision: ShadowDecisionRecord) -> str:
    reasons: list[str] = []
    if decision.risk_reason:
        reasons.append(str(decision.risk_reason))
    try:
        context = json.loads(decision.context_json or "{}")
    except (TypeError, ValueError):
        context = {}
    context_reason = context.get("reason")
    if context_reason and str(context_reason) not in {"WAIT", *reasons}:
        reasons.append(str(context_reason))
    return "; ".join(reasons) or "pipeline не сохранил причину"


def _status_money(value: Decimal | None) -> str:
    return f"{value.quantize(Decimal('0.0001'))} USDT" if value is not None else "НЕДОСТУПНО"


def _status_number(value: int | None) -> str:
    return str(value) if value is not None else "НЕДОСТУПНО"


def _status_flag(value: bool | None) -> str:
    if value is None:
        return "UNKNOWN"
    return "true" if value else "false"


def _compact_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")
