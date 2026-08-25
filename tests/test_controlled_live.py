from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, ControlledLiveProposalRecord, ExecutionOrderRecord
from app.exchanges.models import InstrumentRules, OrderSide
from app.trading.controlled_live import (
    ArmingGates,
    CONTROLLED_LIVE_V1,
    CONTROLLED_LIVE_V1_HASH,
    ControlledLiveBlocked,
    ControlledLiveRepository,
    ControlledRiskSnapshot,
    LiveFill,
    LiveGatewaySnapshot,
    LivePositionSnapshot,
    ManualExecutionService,
    ManualOrderInputs,
    ProtectionFailure,
    build_manual_preview,
    format_preview_ru,
)


def _rules(*, minimum_quantity: str = "0.001", minimum_notional: str = "5"):
    return InstrumentRules(
        tick_size=Decimal("0.1"),
        quantity_step=Decimal("0.001"),
        minimum_quantity=Decimal(minimum_quantity),
        minimum_notional=Decimal(minimum_notional),
        maximum_quantity=Decimal("100"),
        maximum_leverage=Decimal("150"),
    )


def _risk(**overrides):
    values = {
        "equity": Decimal("50"),
        "available_balance": Decimal("50"),
    }
    values.update(overrides)
    return ControlledRiskSnapshot(**values)


def _inputs(**overrides):
    values = {
        "side": OrderSide.BUY,
        "reference_price": Decimal("1000"),
        "stop_loss": Decimal("990"),
        "take_profit": Decimal("1020"),
    }
    values.update(overrides)
    return ManualOrderInputs(**values)


def _repository(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'controlled.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    return engine, sessions, ControlledLiveRepository(sessions)


class FakeGateway:
    def __init__(self, *, protection_fails: bool = False):
        self.protection_fails = protection_fails
        self.submit_calls = 0
        self.protection_calls = []
        self.emergency_closes = []
        self.cancel_calls = 0
        self.snapshot_value = LiveGatewaySnapshot((), frozenset(), frozenset())

    async def submit_market(self, preview, client_order_id):
        self.submit_calls += 1
        return LiveFill(
            "order-1",
            "position-1",
            preview.quantity,
            Decimal("1000"),
            Decimal("0.0022"),
        )

    async def install_native_protection(self, fill, **parameters):
        self.protection_calls.append(parameters)
        if self.protection_fails:
            raise RuntimeError("protection rejected")

    async def emergency_close_reduce_only(self, fill, symbol):
        self.emergency_closes.append((fill.position_id, symbol))

    async def cancel_pending_orders(self, symbol):
        self.cancel_calls += 1
        return 1

    async def snapshot(self):
        return self.snapshot_value


def _prepared_service(tmp_path, gateway=None):
    engine, sessions, repository = _repository(tmp_path)
    gateway = gateway or FakeGateway()
    service = ManualExecutionService(repository, gateway, {42})
    preview = service.preview(42, _inputs(), _risk(), _rules())
    assert preview.executable
    service.approve(42, preview.proposal_hash)
    return engine, sessions, repository, gateway, service, preview


def test_controlled_live_v1_is_frozen_and_exact() -> None:
    assert CONTROLLED_LIVE_V1.name == "CONTROLLED_LIVE_V1"
    assert CONTROLLED_LIVE_V1.config_hash == CONTROLLED_LIVE_V1_HASH
    assert CONTROLLED_LIVE_V1.symbol == "BTC/USDT"
    assert CONTROLLED_LIVE_V1.leverage == 1
    assert CONTROLLED_LIVE_V1.max_positions == 1
    assert CONTROLLED_LIVE_V1.max_trades_per_day == 4
    assert CONTROLLED_LIVE_V1.risk_per_trade_pct == Decimal("0.005")
    assert CONTROLLED_LIVE_V1.daily_loss_limit_pct == Decimal("0.02")
    assert CONTROLLED_LIVE_V1.max_consecutive_losses == 2
    assert CONTROLLED_LIVE_V1.cooldown_minutes == 60
    assert CONTROLLED_LIVE_V1.minimum_risk_reward == 2
    assert CONTROLLED_LIVE_V1.max_position_notional == 10
    assert CONTROLLED_LIVE_V1.first_execution_notional_cap == 5
    assert CONTROLLED_LIVE_V1.trailing_stop is False


def test_current_btc_minimum_quantity_is_incompatible_with_five_dollar_cap() -> None:
    preview = build_manual_preview(
        _inputs(
            reference_price=Decimal("79014.20"),
            stop_loss=Decimal("78224.00"),
            take_profit=Decimal("80594.60"),
        ),
        _risk(),
        _rules(),
    )
    assert not preview.executable
    assert preview.quantity == 0
    assert "minimum quantity" in preview.reason
    rendered = format_preview_ru(preview)
    for field in (
        "Symbol:",
        "Side:",
        "Quantity:",
        "Expected notional:",
        "Leverage:",
        "Expected fee:",
        "SL:",
        "TP:",
        "Maximum planned loss:",
    ):
        assert field in rendered


def test_preview_sizes_from_equity_risk_and_caps_notional() -> None:
    preview = build_manual_preview(_inputs(), _risk(), _rules())
    assert preview.executable
    assert preview.quantity == Decimal("0.005")
    assert preview.expected_notional == Decimal("5.000")
    assert preview.maximum_planned_loss <= Decimal("0.25")
    assert preview.risk_reward_ratio == 2


@pytest.mark.parametrize(
    ("risk", "message"),
    [
        (_risk(open_positions=1), "Maximum open positions"),
        (_risk(trades_today=4), "Maximum trades"),
        (_risk(daily_realized_pnl=Decimal("-1")), "Daily loss"),
        (_risk(consecutive_losses=2), "Consecutive-loss"),
        (
            _risk(cooldown_until=datetime.now(UTC) + timedelta(minutes=1)),
            "cooldown",
        ),
    ],
)
def test_hard_risk_limits_reject_before_approval(risk, message) -> None:
    preview = build_manual_preview(_inputs(), risk, _rules())
    assert not preview.executable
    assert message in preview.reason


@pytest.mark.asyncio
async def test_all_three_arming_gates_are_required_before_gateway_call(tmp_path) -> None:
    _, _, _, gateway, service, preview = _prepared_service(tmp_path)
    for gates in (
        ArmingGates(False, True, True),
        ArmingGates(True, False, True),
        ArmingGates(True, True, False),
    ):
        with pytest.raises(ControlledLiveBlocked, match="gates are disabled"):
            await service.execute_first_order(42, preview, account_id="main", gates=gates)
    assert gateway.submit_calls == 0


@pytest.mark.asyncio
async def test_manual_first_order_installs_native_reduce_only_sl_tp(tmp_path) -> None:
    _, sessions, repository, gateway, service, preview = _prepared_service(tmp_path)
    fill = await service.execute_first_order(
        42,
        preview,
        account_id="main",
        gates=ArmingGates(True, True, True),
    )
    assert fill.order_id == "order-1"
    assert gateway.submit_calls == 1
    assert gateway.protection_calls == [
        {
            "symbol": "BTCUSDT",
            "stop_loss": Decimal("990"),
            "take_profit": Decimal("1020"),
            "reduce_only": True,
        }
    ]
    assert repository.state().first_order_executed is True
    with sessions() as session:
        assert session.query(ControlledLiveProposalRecord).one().status == "PROTECTED"
        assert session.query(ExecutionOrderRecord).one().status == "FILLED_PROTECTED"


@pytest.mark.asyncio
async def test_protection_failure_emergency_closes_reduce_only(tmp_path) -> None:
    gateway = FakeGateway(protection_fails=True)
    _, _, repository, _, service, preview = _prepared_service(tmp_path, gateway)
    with pytest.raises(ProtectionFailure, match="emergency-closed"):
        await service.execute_first_order(
            42,
            preview,
            account_id="main",
            gates=ArmingGates(True, True, True),
        )
    assert gateway.emergency_closes == [("position-1", "BTCUSDT")]
    assert repository.proposal(preview.proposal_id).status == "EMERGENCY_CLOSED"


@pytest.mark.asyncio
async def test_restart_cannot_submit_duplicate_first_order(tmp_path) -> None:
    _, sessions, _, gateway, service, preview = _prepared_service(tmp_path)
    gates = ArmingGates(True, True, True)
    await service.execute_first_order(42, preview, account_id="main", gates=gates)

    restarted = ManualExecutionService(
        ControlledLiveRepository(sessions), gateway, {42}
    )
    with pytest.raises(ControlledLiveBlocked, match="already claimed or executed"):
        await restarted.execute_first_order(
            42, preview, account_id="main", gates=gates
        )
    assert gateway.submit_calls == 1


@pytest.mark.asyncio
async def test_strategy_or_ai_source_cannot_submit_first_mainnet_order(tmp_path) -> None:
    _, _, _, gateway, service, preview = _prepared_service(tmp_path)
    strategy_preview = replace(preview, source="STRATEGY")
    with pytest.raises(ControlledLiveBlocked, match="Strategy and AI"):
        await service.execute_first_order(
            42,
            strategy_preview,
            account_id="main",
            gates=ArmingGates(True, True, True),
        )
    assert gateway.submit_calls == 0


@pytest.mark.asyncio
async def test_emergency_stop_is_admin_only_cancels_and_can_close(tmp_path) -> None:
    _, _, repository, gateway, service, _ = _prepared_service(tmp_path)
    gateway.snapshot_value = LiveGatewaySnapshot(
        (LivePositionSnapshot("position-1", "BTCUSDT", Decimal("0.004")),),
        frozenset({"pending-1"}),
        frozenset(),
    )
    with pytest.raises(ControlledLiveBlocked, match="ADMIN_TELEGRAM_IDS"):
        await service.emergency_stop(7, close_position=True)
    result = await service.emergency_stop(42, close_position=True)
    assert result == {
        "kill_switch": "ACTIVE",
        "cancelled_orders": 1,
        "closed_positions": 1,
    }
    assert repository.state().kill_switch_active is True
    assert gateway.emergency_closes == [("position-1", "BTCUSDT")]


@pytest.mark.asyncio
async def test_reconciliation_matches_fill_position_and_persistent_ledger(tmp_path) -> None:
    _, _, _, gateway, service, preview = _prepared_service(tmp_path)
    await service.execute_first_order(
        42,
        preview,
        account_id="main",
        gates=ArmingGates(True, True, True),
    )
    gateway.snapshot_value = LiveGatewaySnapshot(
        (LivePositionSnapshot("position-1", "BTCUSDT", preview.quantity),),
        frozenset(),
        frozenset({"order-1"}),
    )
    result = await service.reconcile(preview)
    assert result["status"] == "MATCH"
    assert result["fill_match"] is True
    assert result["position_match"] is True
