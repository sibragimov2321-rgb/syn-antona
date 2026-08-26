from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import (
    Base,
    ControlledLiveProposalRecord,
    ExecutionOrderRecord,
    ShadowDecisionRecord,
    ShadowTradeRecord,
)
from app.exchanges.bybit_v5_gateway import BybitV5Http, BybitV5OrderGateway, GuardSnapshot
from app.shadow.engine import PROTOCOL_ID
from app.shadow.runner import _controlled_execution_cycle
from app.core.config import get_settings
from app.strategy_lab.phase4g import FROZEN_CONFIG_HASH
from app.trading.controlled_live import (
    ArmingGates,
    CONTROLLED_LIVE_V1,
    ControlledLiveRepository,
    ControlledRiskSnapshot,
    CurrentInstrumentState,
    FirstInstrumentSelection,
    LiveFill,
    LiveGatewaySnapshot,
    LivePositionSnapshot,
    ManualExecutionService,
    ManualOrderInputs,
    build_manual_preview,
)
from app.exchanges.models import InstrumentRules, OrderSide
from app.trading.controlled_universe import (
    FROZEN_SIGNAL_SOURCE,
    SCANNER_CONFIG,
    scanner_selection_hash,
)
from app.trading.first_live_proposal import FirstLiveProposalRepository
from app.trading.multi_symbol_scanner import (
    MultiSymbolFirstProposalCoordinator,
    MultiSymbolScannerRepository,
    ScannerAccount,
    ScannerInstrument,
    ScannerReadSnapshot,
    format_scanner_status_ru,
    scanner_status,
)


def _sessions():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _instrument(
    symbol: str,
    *,
    bid: str,
    ask: str,
    step: str,
    minimum_quantity: str,
    tick: str,
    turnover: str = "100000000",
    enabled: bool = True,
    reason: str = "",
) -> ScannerInstrument:
    bid_value = Decimal(bid)
    ask_value = Decimal(ask)
    midpoint = (bid_value + ask_value) / 2
    minimum = Decimal(minimum_quantity)
    return ScannerInstrument(
        symbol=symbol,
        internal_symbol=f"{symbol[:-4]}/USDT",
        enabled=enabled,
        exclusion_reason=reason,
        status="Trading",
        contract_type="LinearPerpetual",
        bid=bid_value,
        ask=ask_value,
        tick_size=Decimal(tick),
        minimum_quantity=minimum,
        quantity_step=Decimal(step),
        minimum_notional=Decimal("5"),
        actual_minimum_quantity=minimum,
        actual_minimum_notional=max(Decimal("5"), minimum * ask_value),
        spread_pct=(ask_value - bid_value) / midpoint,
        turnover_24h=Decimal(turnover),
        checked_at=datetime.now(UTC),
    )


def _market_snapshot(*instruments: ScannerInstrument) -> ScannerReadSnapshot:
    now = datetime.now(UTC)
    return ScannerReadSnapshot(
        {item.symbol: item for item in instruments},
        ScannerAccount(
            equity=Decimal("50"),
            available_balance=Decimal("50"),
            open_positions=0,
            open_order_ids=frozenset(),
            fills_read=True,
            trades_today=0,
            daily_realized_pnl=Decimal(),
            consecutive_losses=0,
            cooldown_until=None,
        ),
        now,
    )


class Reader:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.reads = 0
        self.closed = False

    async def read(self):
        self.reads += 1
        return self.snapshot

    async def close(self):
        self.closed = True


class FailingReader(Reader):
    async def read(self):
        self.reads += 1
        raise RuntimeError("temporary timeout")


def _signal(sessions, symbol, side, score, candle, created_at, stop, target):
    decision_id = f"{symbol}-{side}-{score}"
    price = Decimal("90") if symbol == "SOLUSDT" else Decimal("1.4")
    internal = f"{symbol[:-4]}/USDT"
    with sessions.begin() as session:
        session.add(
            ShadowDecisionRecord(
                id=decision_id,
                protocol_id=PROTOCOL_ID,
                exchange="bybit",
                symbol=internal,
                candle_open_time=candle,
                signal_timestamp=candle + timedelta(hours=1),
                decision=side,
                signal_score=score,
                decision_price=price,
                observed_bid=price,
                observed_ask=price,
                observed_spread=Decimal("0.001"),
                risk_status="ALLOW",
                risk_reason="Approved by deterministic Risk Manager",
                strategy_hash=FROZEN_CONFIG_HASH,
                context_json="{}",
                created_at=created_at,
            )
        )
        session.add(
            ShadowTradeRecord(
                id=f"shadow-{decision_id}",
                decision_id=decision_id,
                protocol_id=PROTOCOL_ID,
                exchange="bybit",
                symbol=internal,
                side=side,
                signal_timestamp=candle + timedelta(hours=1),
                decision_price=price,
                entry_reference=price,
                entry_price=price,
                quantity=Decimal("1"),
                stop_loss=Decimal(stop),
                take_profit=Decimal(target),
                leverage=Decimal("1"),
                risk_amount=Decimal("0.1"),
                expected_fees=Decimal("0.01"),
                entry_fee=Decimal("0.005"),
                observed_spread=Decimal("0.001"),
                entry_spread_cost=Decimal("0.001"),
                entry_slippage_cost=Decimal("0.001"),
                strategy_hash=FROZEN_CONFIG_HASH,
                status="OPEN",
                opened_at=created_at,
            )
        )


def _setup():
    sessions = _sessions()
    ControlledLiveRepository(sessions).state()
    phase = FirstLiveProposalRepository(sessions)
    phase.initialize(datetime.now(UTC) - timedelta(minutes=2))
    scanner = MultiSymbolScannerRepository(sessions)
    scanner.initialize(datetime.now(UTC) - timedelta(minutes=1))
    return sessions, phase, scanner


@pytest.mark.asyncio
async def test_simultaneous_candidates_use_frozen_deterministic_ranking():
    sessions, phase, scanner = _setup()
    candle = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    created = datetime.now(UTC)
    _signal(sessions, "SOLUSDT", "LONG", 85, candle, created, "89", "93")
    _signal(sessions, "XRPUSDT", "LONG", 92, candle, created, "1.35", "1.55")
    reader = Reader(
        _market_snapshot(
            _instrument("SOLUSDT", bid="89.99", ask="90", step="0.1", minimum_quantity="0.1", tick="0.01"),
            _instrument("XRPUSDT", bid="1.3999", ask="1.4", step="0.1", minimum_quantity="0.1", tick="0.0001"),
        )
    )
    coordinator = MultiSymbolFirstProposalCoordinator(scanner, phase, reader, {42})

    result = await coordinator.cycle()

    assert result.status == "READY_FOR_USER_APPROVAL"
    assert result.preview.symbol == "XRPUSDT"
    assert result.preview.selection_hash == scanner_selection_hash("XRPUSDT")
    assert result.preview.expected_notional <= Decimal("10")
    assert result.preview.maximum_planned_loss <= Decimal("0.25")
    assert result.preview.risk_reward_ratio >= Decimal("2")
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(ControlledLiveProposalRecord)) == 1
    restarted = MultiSymbolFirstProposalCoordinator(
        MultiSymbolScannerRepository(sessions),
        FirstLiveProposalRepository(sessions),
        reader,
        {42},
    )
    again = await restarted.cycle()
    assert again.preview.proposal_id == result.preview.proposal_id


class ExecutionGateway:
    dry_run = False

    def __init__(self):
        self.protected = False
        self.fill = None

    async def current_instrument_state(self, symbol):
        assert symbol == "XRPUSDT"
        return CurrentInstrumentState(
            symbol,
            "Trading",
            Decimal("1.4"),
            Decimal("0.1"),
            Decimal("0.1"),
            Decimal("5"),
        )

    async def submit_market(self, preview, client_order_id):
        self.fill = LiveFill(
            "xrp-order-1",
            "XRPUSDT:0",
            preview.quantity,
            Decimal("1.4"),
            Decimal("0.004"),
        )
        return self.fill

    async def install_native_protection(self, fill, **values):
        assert values["symbol"] == "XRPUSDT"
        assert values["reduce_only"] is True
        self.protected = True

    async def emergency_close_reduce_only(self, fill, symbol):
        raise AssertionError("emergency close should not be needed")

    async def cancel_pending_orders(self, symbol):
        return 0

    async def snapshot(self):
        return LiveGatewaySnapshot(
            (
                LivePositionSnapshot(
                    "XRPUSDT:0", "XRPUSDT", self.fill.filled_quantity
                ),
            ),
            frozenset(),
            frozenset({"xrp-order-1"}),
        )


@pytest.mark.asyncio
async def test_admin_approved_multi_symbol_execution_protects_and_recovers_after_restart():
    sessions, phase, scanner = _setup()
    candle = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    _signal(sessions, "XRPUSDT", "SHORT", 92, candle, datetime.now(UTC), "1.45", "1.25")
    reader = Reader(
        _market_snapshot(
            _instrument("XRPUSDT", bid="1.4", ask="1.4001", step="0.1", minimum_quantity="0.1", tick="0.0001")
        )
    )
    result = await MultiSymbolFirstProposalCoordinator(scanner, phase, reader, {42}).cycle()
    preview = result.preview
    repository = ControlledLiveRepository(sessions)
    repository.approve(preview.proposal_hash, 42)
    gateway = ExecutionGateway()
    service = ManualExecutionService(repository, gateway, {42})

    fill = await service.execute_first_order(
        42,
        preview,
        account_id="main",
        gates=ArmingGates(True, True, True, "XRPUSDT"),
    )

    assert fill.order_id == "xrp-order-1"
    assert gateway.protected
    restarted = ManualExecutionService(ControlledLiveRepository(sessions), gateway, {42})
    assert (await restarted.reconcile(preview))["status"] == "MATCH"
    with sessions() as session:
        ledger = session.scalar(select(ExecutionOrderRecord))
        assert ledger.symbol == "XRP/USDT"
        assert ledger.status == "FILLED_PROTECTED"


class Notifier:
    def __init__(self):
        self.messages = []

    async def system(self, title, message):
        self.messages.append((title, message))
        return True


class TimeoutGateway(ExecutionGateway):
    async def submit_market(self, preview, client_order_id):
        raise TimeoutError("submission outcome unknown")


@pytest.mark.asyncio
async def test_worker_executes_only_after_arming_then_requires_restart_reconciliation(
    monkeypatch,
):
    sessions, phase, scanner = _setup()
    candle = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    _signal(sessions, "XRPUSDT", "LONG", 92, candle, datetime.now(UTC), "1.35", "1.55")
    result = await MultiSymbolFirstProposalCoordinator(
        scanner,
        phase,
        Reader(
            _market_snapshot(
                _instrument("XRPUSDT", bid="1.3999", ask="1.4", step="0.1", minimum_quantity="0.1", tick="0.0001")
            )
        ),
        {42},
    ).cycle()
    controlled = ControlledLiveRepository(sessions)
    controlled.approve(result.preview.proposal_hash, 42)
    phase.mark_approved_for_execution(result.preview.proposal_id)
    gateway = ExecutionGateway()
    notifier = Notifier()

    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("CONTROLLED_LIVE_ENABLED", "false")
    monkeypatch.setenv("MANUAL_FIRST_ORDER_APPROVED", "false")
    get_settings.cache_clear()
    await _controlled_execution_cycle(
        phase, controlled, gateway, notifier, {42}, datetime.now(UTC)
    )
    assert gateway.fill is None
    assert phase.state().status == "APPROVED_FOR_EXECUTION"

    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("CONTROLLED_LIVE_ENABLED", "true")
    monkeypatch.setenv("MANUAL_FIRST_ORDER_APPROVED", "true")
    monkeypatch.setenv("CONTROLLED_LIVE_V1_FIRST_SYMBOL", "XRPUSDT")
    get_settings.cache_clear()
    process_started = datetime.now(UTC) - timedelta(minutes=1)
    await _controlled_execution_cycle(
        phase, controlled, gateway, notifier, {42}, process_started
    )
    assert gateway.protected
    assert phase.state().status == "EXECUTED_AWAITING_RESTART_VALIDATION"

    await _controlled_execution_cycle(
        phase,
        controlled,
        gateway,
        notifier,
        {42},
        datetime.now(UTC) + timedelta(seconds=1),
    )
    assert phase.state().status == "FIRST_EXECUTION_VALIDATED"
    assert not controlled.state().kill_switch_active
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_unknown_first_execution_activates_persistent_kill_switch(monkeypatch):
    sessions, phase, scanner = _setup()
    candle = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    _signal(sessions, "XRPUSDT", "LONG", 92, candle, datetime.now(UTC), "1.35", "1.55")
    result = await MultiSymbolFirstProposalCoordinator(
        scanner,
        phase,
        Reader(
            _market_snapshot(
                _instrument("XRPUSDT", bid="1.3999", ask="1.4", step="0.1", minimum_quantity="0.1", tick="0.0001")
            )
        ),
        {42},
    ).cycle()
    controlled = ControlledLiveRepository(sessions)
    controlled.approve(result.preview.proposal_hash, 42)
    phase.mark_approved_for_execution(result.preview.proposal_id)
    for name, value in {
        "DRY_RUN": "false",
        "LIVE_TRADING_ENABLED": "true",
        "CONTROLLED_LIVE_ENABLED": "true",
        "MANUAL_FIRST_ORDER_APPROVED": "true",
        "CONTROLLED_LIVE_V1_FIRST_SYMBOL": "XRPUSDT",
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()

    await _controlled_execution_cycle(
        phase,
        controlled,
        TimeoutGateway(),
        Notifier(),
        {42},
        datetime.now(UTC) - timedelta(minutes=1),
    )

    assert controlled.state().kill_switch_active
    assert phase.state().status == "HALTED_EXECUTION_FAILURE"
    with sessions() as session:
        assert session.scalar(select(ExecutionOrderRecord)).status == "UNKNOWN"
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_excluded_instrument_cannot_create_proposal():
    sessions, phase, scanner = _setup()
    candle = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    _signal(sessions, "AVAXUSDT", "LONG", 95, candle, datetime.now(UTC), "7", "9")
    reader = Reader(
        _market_snapshot(
            _instrument(
                "AVAXUSDT",
                bid="7.38",
                ask="7.39",
                step="0.1",
                minimum_quantity="0.1",
                tick="0.001",
                enabled=False,
                reason="оборот 24ч ниже $25000000",
            )
        )
    )
    result = await MultiSymbolFirstProposalCoordinator(scanner, phase, reader, {42}).cycle()
    assert result.status == "WAITING_FOR_SIGNAL"
    assert "оборот" in result.reason
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(ControlledLiveProposalRecord)) == 0


@pytest.mark.asyncio
async def test_scanner_read_failure_is_fail_closed_and_does_not_consume_signal():
    sessions, phase, scanner = _setup()
    candle = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    _signal(sessions, "SOLUSDT", "LONG", 90, candle, datetime.now(UTC), "89", "93")
    result = await MultiSymbolFirstProposalCoordinator(
        scanner, phase, FailingReader(_market_snapshot()), {42}
    ).cycle()
    assert result.status == "WAITING_FOR_SIGNAL"
    assert "temporarily unavailable" in result.reason
    assert scanner.state().status == "DEGRADED"
    assert scanner.state().last_scanned_candle_open is None
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(ControlledLiveProposalRecord)) == 0


def test_status_lists_exact_allowlist_and_uses_persisted_decisions_only():
    sessions, _, scanner = _setup()
    snapshot = _market_snapshot(
        *[
            _instrument(
                symbol,
                bid="1",
                ask="1.001",
                step="0.1",
                minimum_quantity="5",
                tick="0.001",
            )
            for symbol in SCANNER_CONFIG.symbols
        ]
    )
    scanner.save_market_snapshot(snapshot)
    value = scanner_status(sessions)
    text = format_scanner_status_ru(value)
    assert "MULTI-SYMBOL SIGNAL SCANNER" in text
    for symbol in SCANNER_CONFIG.symbols:
        assert symbol.removesuffix("USDT") + " —" in text
    assert value.analyses_today == 0


class AllowAuthorizer:
    async def authorize(self, **values):
        return GuardSnapshot(Decimal("1.4"), 0, Decimal("50"), Decimal())


@pytest.mark.asyncio
async def test_multi_symbol_gateway_dry_run_builds_payload_but_sends_no_post():
    selection = FirstInstrumentSelection(
        "XRPUSDT",
        "XRP/USDT",
        "bybit",
        "USDT_PERPETUAL",
        CONTROLLED_LIVE_V1.config_hash,
        Decimal("10"),
        scanner_selection_hash("XRPUSDT"),
    )
    preview = build_manual_preview(
        ManualOrderInputs(OrderSide.BUY, Decimal("1.4"), Decimal("1.35"), Decimal("1.55")),
        ControlledRiskSnapshot(Decimal("50"), Decimal("50")),
        InstrumentRules(Decimal("0.0001"), Decimal("0.1"), Decimal("0.1"), Decimal("5")),
        instrument=selection,
    )
    preview = replace(preview, source=FROZEN_SIGNAL_SOURCE)
    http = BybitV5Http("key", "secret")
    gateway = BybitV5OrderGateway(http, AllowAuthorizer(), dry_run=True)
    try:
        result = await gateway.dry_run_market_request(preview, preview.client_order_id)
    finally:
        await gateway.close()
    assert result["sent"] is False
    assert result["payload"]["symbol"] == "XRPUSDT"
    assert Decimal(result["payload"]["qty"]) == preview.quantity
