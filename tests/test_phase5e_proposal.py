from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import (
    Base,
    ControlledLiveProposalRecord,
    FirstLiveProposalStateRecord,
    ShadowDecisionRecord,
    ShadowTradeRecord,
)
from app.shadow.engine import PROTOCOL_ID
from app.shadow.notifier import ShadowNotifier
from app.strategy_lab.phase4g import FROZEN_CONFIG_HASH
from app.trading.controlled_live import ControlledLiveRepository, ControlledProposalReadSnapshot
from app.trading.first_live_proposal import (
    FROZEN_SIGNAL_SOURCE,
    FirstControlledLiveProposalCoordinator,
    FirstLiveProposalRepository,
    format_controlled_proposal_ru,
)


def _sessions():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _snapshot(**overrides):
    values = {
        "symbol": "SOLUSDT",
        "contract_type": "LinearPerpetual",
        "status": "Trading",
        "bid_price": Decimal("90"),
        "ask_price": Decimal("90.01"),
        "tick_size": Decimal("0.01"),
        "minimum_quantity": Decimal("0.1"),
        "quantity_step": Decimal("0.1"),
        "minimum_notional": Decimal("5"),
        "wallet_balance": Decimal("50"),
        "equity": Decimal("50"),
        "available_balance": Decimal("50"),
        "open_positions": 0,
        "open_order_ids": frozenset(),
        "fills_read": True,
        "fetched_at": datetime.now(UTC),
    }
    values.update(overrides)
    return ControlledProposalReadSnapshot(**values)


class ReadOnlyGateway:
    dry_run = True

    def __init__(self, snapshot=None):
        self.value = snapshot or _snapshot()
        self.reads = 0

    async def controlled_proposal_snapshot(self, symbol):
        assert symbol == "SOLUSDT"
        self.reads += 1
        return self.value


def _signal(sessions, *, side="LONG", created_at=None, candle=None):
    created_at = created_at or datetime.now(UTC)
    candle = candle or created_at.replace(minute=0, second=0, microsecond=0)
    decision_id = f"decision-{side.lower()}-{int(candle.timestamp())}"
    stop = Decimal("89") if side == "LONG" else Decimal("91")
    target = Decimal("93") if side == "LONG" else Decimal("87")
    with sessions.begin() as session:
        session.add(
            ShadowDecisionRecord(
                id=decision_id,
                protocol_id=PROTOCOL_ID,
                exchange="bybit",
                symbol="SOL/USDT",
                candle_open_time=candle,
                signal_timestamp=candle + timedelta(hours=1),
                decision=side,
                signal_score=90,
                decision_price=Decimal("90"),
                observed_bid=Decimal("89.99"),
                observed_ask=Decimal("90.01"),
                observed_spread=Decimal("0.02"),
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
                symbol="SOL/USDT",
                side=side,
                signal_timestamp=candle + timedelta(hours=1),
                decision_price=Decimal("90"),
                entry_reference=Decimal("90"),
                entry_price=Decimal("90"),
                quantity=Decimal("1"),
                stop_loss=stop,
                take_profit=target,
                leverage=Decimal("1"),
                risk_amount=Decimal("1"),
                expected_fees=Decimal("0.1"),
                entry_fee=Decimal("0.05"),
                observed_spread=Decimal("0.02"),
                entry_spread_cost=Decimal("0.01"),
                entry_slippage_cost=Decimal("0.01"),
                strategy_hash=FROZEN_CONFIG_HASH,
                status="OPEN",
                opened_at=created_at,
            )
        )
    return decision_id


def _setup(start=None):
    sessions = _sessions()
    ControlledLiveRepository(sessions).state()
    repository = FirstLiveProposalRepository(sessions)
    repository.initialize(start or datetime.now(UTC) - timedelta(seconds=1))
    return sessions, repository


@pytest.mark.asyncio
async def test_waits_without_a_new_natural_signal():
    _, repository = _setup()
    gateway = ReadOnlyGateway()
    result = await FirstControlledLiveProposalCoordinator(repository, gateway, {42}).cycle()
    assert result.status == "WAITING_FOR_SIGNAL"
    assert gateway.reads == 0


@pytest.mark.asyncio
async def test_old_signal_is_never_turned_into_a_retrospective_proposal():
    start = datetime(2026, 8, 25, 12, tzinfo=UTC)
    sessions, repository = _setup(start)
    _signal(
        sessions,
        created_at=start - timedelta(seconds=1),
        candle=start - timedelta(hours=1),
    )
    gateway = ReadOnlyGateway()
    result = await FirstControlledLiveProposalCoordinator(repository, gateway, {42}).cycle()
    assert result.status == "WAITING_FOR_SIGNAL"
    assert gateway.reads == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("direction", "expected_side", "entry"),
    (("LONG", "BUY", Decimal("90.01")), ("SHORT", "SELL", Decimal("90"))),
)
async def test_natural_frozen_signal_creates_exact_immutable_preview(
    direction, expected_side, entry
):
    sessions, repository = _setup()
    decision_id = _signal(sessions, side=direction)
    gateway = ReadOnlyGateway()
    coordinator = FirstControlledLiveProposalCoordinator(repository, gateway, {42})

    result = await coordinator.cycle()

    assert result.status == "READY_FOR_USER_APPROVAL"
    assert result.preview is not None
    assert result.preview.source == FROZEN_SIGNAL_SOURCE
    assert result.preview.side == expected_side
    assert result.preview.quantity == Decimal("0.1")
    assert result.preview.expected_notional == entry * Decimal("0.1")
    assert result.preview.expected_notional <= 10
    assert result.preview.leverage == 2
    assert result.preview.maximum_planned_loss <= Decimal("2.50")
    assert result.preview.risk_reward_ratio >= Decimal("1.5")
    state = repository.state()
    assert state.source_decision_id == decision_id
    assert state.status == "READY_FOR_USER_APPROVAL"
    text = format_controlled_proposal_ru(result.preview, result.available_equity)
    for value in (
        "Направление:",
        "Вход",
        "Количество:",
        "Номинал:",
        "Stop Loss:",
        "Take Profit:",
        "Максимальный плановый убыток:",
        "Расчётные комиссии:",
        "Расчётное проскальзывание:",
        "Доступный equity:",
        "одноразового подтверждения администратора",
    ):
        assert value in text

    # A restart observes the same immutable proposal instead of creating another.
    restarted = FirstControlledLiveProposalCoordinator(
        FirstLiveProposalRepository(sessions), gateway, {42}
    )
    again = await restarted.cycle()
    assert again.preview.proposal_id == result.preview.proposal_id
    with sessions() as session:
        assert session.scalar(select(func.count(ControlledLiveProposalRecord.proposal_id))) == 1


@pytest.mark.asyncio
async def test_signal_that_exceeds_risk_is_consumed_without_a_proposal():
    sessions, repository = _setup()
    _signal(sessions)
    gateway = ReadOnlyGateway(_snapshot(equity=Decimal("1"), available_balance=Decimal("1")))
    result = await FirstControlledLiveProposalCoordinator(repository, gateway, {42}).cycle()
    assert result.status == "WAITING_FOR_SIGNAL"
    assert "risk" in result.reason.lower() or "quantity" in result.reason.lower()
    state = repository.state()
    assert state.last_scanned_decision_id is not None
    assert state.proposal_id is None


@pytest.mark.asyncio
async def test_open_exchange_state_blocks_before_a_proposal_is_persisted():
    sessions, repository = _setup()
    _signal(sessions)
    gateway = ReadOnlyGateway(_snapshot(open_positions=1))
    result = await FirstControlledLiveProposalCoordinator(repository, gateway, {42}).cycle()
    assert result.status == "WAITING_FOR_SIGNAL"
    assert "open position" in result.reason
    with sessions() as session:
        assert session.scalar(select(func.count(ControlledLiveProposalRecord.proposal_id))) == 0


@pytest.mark.asyncio
async def test_telegram_consent_state_never_calls_an_execution_method():
    sessions, repository = _setup()
    _signal(sessions)
    gateway = ReadOnlyGateway()
    result = await FirstControlledLiveProposalCoordinator(repository, gateway, {42}).cycle()
    controlled = ControlledLiveRepository(sessions)
    controlled.approve(result.preview.proposal_hash, 42)
    repository.mark_approved_dry_run(result.preview.proposal_id)
    assert repository.state().status == "APPROVED_DRY_RUN"
    assert not hasattr(gateway, "submit_market")


def test_phase5e_state_schema_starts_fail_closed():
    _, repository = _setup()
    state = repository.state()
    assert isinstance(state, FirstLiveProposalStateRecord)
    assert state.status == "WAITING_FOR_SIGNAL"
    assert state.proposal_id is None


@pytest.mark.asyncio
async def test_telegram_proposal_has_exact_admin_buttons():
    class Bot:
        def __init__(self):
            self.calls = []

        async def send_message(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    notifier = ShadowNotifier(None, {42})
    notifier.bot = Bot()
    assert await notifier.controlled_proposal("preview", "p-123", 42)
    markup = notifier.bot.calls[0][1]["reply_markup"]
    buttons = markup.inline_keyboard[0]
    assert [button.text for button in buttons] == [
        "✅ Подтвердить первую сделку",
        "❌ Отменить",
    ]
    assert buttons[0].callback_data == "phase5e:approve:p-123"
    assert buttons[1].callback_data == "phase5e:cancel:p-123"
