from datetime import UTC, datetime
from dataclasses import replace
from decimal import Decimal
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import (
    Base,
    ControlledLiveProposalRecord,
    ExecutionOrderRecord,
    PositionProfitStateRecord,
    PositionProtectionEventRecord,
)
from app.trading.profit_protector import (
    ClosedBar,
    PositionProfitRepository,
    PositionSnapshot,
    LocalPositionProfitProtector,
    adverse_momentum_reversal,
    evaluate_protection,
    initial_risk_usdt,
    net_pnl,
)
import app.trading.profit_protector as protector_module
from app.shadow.notifier import ShadowNotifier


D = Decimal
SLIPPAGE = D("0.0002")
HUNDRED = D("100")


def position(side: str = "Buy") -> PositionSnapshot:
    return PositionSnapshot(
        symbol="SOLUSDT",
        side=side,
        quantity=D("1"),
        entry_price=D("100"),
        stop_loss=D("98") if side == "Buy" else D("102"),
        take_profit=D("104") if side == "Buy" else D("96"),
        position_idx=0,
        opened_at=datetime.now(UTC),
        entry_client_order_id="owned-entry",
        entry_fee_usdt=D("0.06"),
        taker_fee_rate=D("0.0006"),
        tick_size=D("0.01"),
    )


def flat_bars(price: Decimal = HUNDRED) -> list[ClosedBar]:
    return [ClosedBar(i, price, price + D("0.4"), price - D("0.4")) for i in range(20)]


def test_long_break_even_activates_at_half_r_and_covers_costs() -> None:
    item = position("Buy")
    risk = initial_risk_usdt(item, SLIPPAGE)
    # Find an executable bid just above +0.5R without relying on Bybit ROI%.
    bid = item.entry_price + (risk * D("0.55") + D("0.2")) / item.quantity
    result = evaluate_protection(
        item,
        bid=bid,
        ask=bid + D("0.01"),
        confirmed_stop=item.stop_loss,
        previous_mfe=D("0"),
        stage="INITIAL",
        bars=flat_bars(),
        slippage_per_leg=SLIPPAGE,
    )
    assert result.action == "BREAK_EVEN"
    assert result.stop_loss is not None and result.stop_loss > item.entry_price
    assert net_pnl(
        "Buy", item.entry_price, result.stop_loss, item.quantity,
        item.entry_fee_usdt, item.taker_fee_rate, SLIPPAGE,
    ) >= D("-0.01")


def test_short_break_even_activates_and_only_tightens() -> None:
    item = position("Sell")
    risk = initial_risk_usdt(item, SLIPPAGE)
    ask = item.entry_price - (risk * D("0.6") + D("0.2"))
    result = evaluate_protection(
        item,
        bid=ask - D("0.01"),
        ask=ask,
        confirmed_stop=item.stop_loss,
        previous_mfe=D("0"),
        stage="INITIAL",
        bars=flat_bars(),
        slippage_per_leg=SLIPPAGE,
    )
    assert result.action == "BREAK_EVEN"
    assert result.stop_loss is not None
    assert item.entry_price > result.stop_loss < item.stop_loss


def test_one_r_locks_at_least_point_three_r_after_costs() -> None:
    item = position("Buy")
    risk = initial_risk_usdt(item, SLIPPAGE)
    result = evaluate_protection(
        item,
        bid=D("103"),
        ask=D("103.01"),
        confirmed_stop=D("100.10"),
        previous_mfe=D("0"),
        stage="BREAK_EVEN",
        bars=flat_bars(D("102")),
        slippage_per_leg=SLIPPAGE,
    )
    assert result.action == "PROFIT_LOCK"
    assert result.stop_loss is not None
    protected = net_pnl(
        "Buy", item.entry_price, result.stop_loss, item.quantity,
        item.entry_fee_usdt, item.taker_fee_rate, SLIPPAGE,
    )
    assert protected >= risk * D("0.3") - D("0.02")


def test_trailing_update_never_moves_stop_backward() -> None:
    item = position("Buy")
    result = evaluate_protection(
        item,
        bid=D("104"),
        ask=D("104.01"),
        confirmed_stop=D("103.50"),
        previous_mfe=D("4"),
        stage="TRAILING_UPDATE",
        bars=flat_bars(D("103")),
        slippage_per_leg=SLIPPAGE,
    )
    assert result.action == "NONE" or result.stop_loss > D("103.50")


def test_early_exit_requires_profit_retracement_and_confirmed_reversal() -> None:
    item = position("Buy")
    closes = [D("103")] * 16 + [D("104"), D("103.4"), D("102.5"), D("101.8")]
    bars = [ClosedBar(i, close, close + D("0.2"), close - D("0.2")) for i, close in enumerate(closes)]
    assert adverse_momentum_reversal("Buy", bars, D("0.8"))
    result = evaluate_protection(
        item,
        bid=D("101.8"),
        ask=D("101.81"),
        confirmed_stop=D("100.5"),
        previous_mfe=D("4"),
        stage="TRAILING_UPDATE",
        bars=bars,
        slippage_per_leg=SLIPPAGE,
    )
    assert result.action == "EARLY_PROFIT_EXIT"


@pytest.mark.parametrize(
    ("side", "bid", "ask"),
    [
        ("Buy", D("100.70"), D("100.71")),
        ("Sell", D("99.29"), D("99.30")),
    ],
)
def test_profit_watch_activates_after_costs_and_point_two_r(
    side: str, bid: Decimal, ask: Decimal
) -> None:
    result = evaluate_protection(
        position(side),
        bid=bid,
        ask=ask,
        confirmed_stop=position(side).stop_loss,
        previous_mfe=D("0"),
        stage="INITIAL",
        bars=flat_bars(),
        slippage_per_leg=SLIPPAGE,
    )
    assert result.action == "PROFIT_WATCH"
    assert result.current_net_pnl > D("0")
    assert result.max_favorable_excursion_usdt == result.current_net_pnl
    assert result.giveback_pct == D("0")


def test_profit_watch_does_nothing_when_net_gain_is_only_a_few_cents() -> None:
    item = position("Buy")
    result = evaluate_protection(
        item,
        bid=D("100.15"),
        ask=D("100.16"),
        confirmed_stop=item.stop_loss,
        previous_mfe=D("0"),
        stage="INITIAL",
        bars=flat_bars(),
        slippage_per_leg=SLIPPAGE,
    )
    assert result.action == "NONE"


@pytest.mark.parametrize(
    ("side", "bid", "ask"),
    [
        ("Buy", D("100.79"), D("100.80")),
        ("Sell", D("99.21"), D("99.22")),
    ],
)
def test_thirty_five_percent_mfe_giveback_tightens_stop_for_long_and_short(
    side: str, bid: Decimal, ask: Decimal
) -> None:
    item = position(side)
    result = evaluate_protection(
        item,
        bid=bid,
        ask=ask,
        confirmed_stop=item.stop_loss,
        previous_mfe=D("1.0"),
        stage="PROFIT_WATCH",
        bars=flat_bars(),
        slippage_per_leg=SLIPPAGE,
    )
    assert result.action == "MFE_PROFIT_PROTECT"
    assert result.stop_loss is not None
    assert result.giveback_pct >= D("35")
    protected_net = net_pnl(
        side,
        item.entry_price,
        result.stop_loss,
        item.quantity,
        item.entry_fee_usdt,
        item.taker_fee_rate,
        SLIPPAGE,
    )
    assert protected_net > D("0.30")


def test_fifty_percent_giveback_without_momentum_reversal_does_not_close() -> None:
    item = position("Buy")
    result = evaluate_protection(
        item,
        bid=D("101.8"),
        ask=D("101.81"),
        confirmed_stop=D("100.20"),
        previous_mfe=D("4"),
        stage="MFE_PROFIT_PROTECT",
        bars=flat_bars(D("101.8")),
        slippage_per_leg=SLIPPAGE,
    )
    assert result.action != "EARLY_PROFIT_EXIT"


def test_reversal_does_not_close_for_dust_profit_after_costs() -> None:
    item = position("Buy")
    closes = [D("101")] * 16 + [D("102"), D("101.4"), D("100.8"), D("100.2")]
    bars = [
        ClosedBar(i, close, close + D("0.2"), close - D("0.2"))
        for i, close in enumerate(closes)
    ]
    result = evaluate_protection(
        item,
        bid=D("100.18"),
        ask=D("100.19"),
        confirmed_stop=D("100.10"),
        previous_mfe=D("1"),
        stage="PROFIT_WATCH",
        bars=bars,
        slippage_per_leg=SLIPPAGE,
    )
    assert result.giveback_pct >= D("50")
    assert result.current_net_pnl > D("0")
    assert result.action == "NONE"


def test_watch_peak_still_protects_after_current_profit_falls_below_point_two_r() -> None:
    item = position("Buy")
    result = evaluate_protection(
        item,
        bid=D("100.46"),
        ask=D("100.47"),
        confirmed_stop=item.stop_loss,
        previous_mfe=D("0.50"),
        stage="PROFIT_WATCH",
        bars=flat_bars(),
        slippage_per_leg=SLIPPAGE,
    )
    assert result.current_r < D("0.20")
    assert result.giveback_pct >= D("35")
    assert result.action == "MFE_PROFIT_PROTECT"
    assert result.stop_loss is not None and result.stop_loss > item.entry_price


@pytest.mark.asyncio
async def test_profit_watch_notification_contains_required_diagnostics() -> None:
    class Bot:
        def __init__(self) -> None:
            self.calls = []

        async def send_message(self, *args, **kwargs) -> None:
            self.calls.append((args, kwargs))

    notifier = ShadowNotifier(None, {42})
    notifier.bot = Bot()
    assert await notifier.profit_protection(
        "PROFIT_WATCH",
        "SOLUSDT",
        None,
        D("0.52"),
        D("0.24"),
        D("0.80"),
        D("35"),
    )
    text = notifier.bot.calls[0][0][1]
    assert "👀 <b>PROFIT WATCH</b>" in text
    assert "Current PnL: $0.5200" in text
    assert "MFE: $0.8000" in text
    assert "Giveback: 35.0%" in text
    assert "Action: PROFIT_WATCH" in text


def test_state_survives_repository_restart(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'protector.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    item = position()
    risk = initial_risk_usdt(item, SLIPPAGE)
    first = PositionProfitRepository(sessions)
    first.upsert(item, D("101"), risk, D("0.08"), D("99"))
    second = PositionProfitRepository(sessions)
    restored = second.load(item.entry_client_order_id)
    assert isinstance(restored, PositionProfitStateRecord)
    assert Decimal(restored.initial_risk_usdt) == risk
    assert restored.stage == "INITIAL"
    assert Decimal(restored.confirmed_stop_loss) == D("99")


def test_new_entry_can_reuse_closed_bybit_position_slot(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'reused-position-slot.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    repository = PositionProfitRepository(sessions)
    previous = position()
    risk = initial_risk_usdt(previous, SLIPPAGE)
    repository.upsert(previous, D("101"), risk, D("0.08"), D("99"))

    current = replace(
        previous,
        entry_client_order_id="new-owned-entry",
        opened_at=datetime.now(UTC),
    )
    repository.upsert(current, D("100.5"), risk, D("0.08"), D("99"))

    with sessions() as session:
        old_state = session.get(PositionProfitStateRecord, previous.entry_client_order_id)
        new_state = session.get(PositionProfitStateRecord, current.entry_client_order_id)
        assert old_state is not None and old_state.closed_at is not None
        assert new_state is not None and new_state.closed_at is None
        assert old_state.position_key == new_state.position_key == "SOLUSDT:0"


class FakeProtectionGateway:
    def __init__(self) -> None:
        self.stop = D("98")
        self.managed: list[Decimal] = []

    async def read_open_positions(self):
        return [{
            "symbol": "SOLUSDT", "side": "Buy", "size": "1", "avgPrice": "100",
            "stopLoss": str(self.stop), "takeProfit": "104", "positionIdx": 0,
        }]

    async def query_executions(self, **_):
        return [{"execFee": "0.06", "execQty": "1", "execPrice": "100"}]

    async def account_taker_fee_rate(self, _symbol):
        return D("0.0006")

    async def current_instrument_state(self, _symbol):
        return SimpleNamespace(ask_price=D("100.01"), quantity_step=D("0.1"))

    async def read_ticker_details(self, _symbol):
        return {"bid1Price": "101.5", "ask1Price": "101.51", "tickSize": "0.01"}

    async def read_closed_klines(self, _symbol, **_):
        return [
            [str(i), "100", "100.4", "99.6", "100"] for i in range(20, 0, -1)
        ]

    async def manage_native_protection(self, *, stop_loss, **_):
        self.stop = stop_loss
        self.managed.append(stop_loss)

    async def protective_reduce_only_close(self, **_):
        raise AssertionError("early exit was not expected")


class FakeNotifier:
    def __init__(self) -> None:
        self.events = []

    async def profit_protection(self, *args):
        self.events.append(args)
        return True


@pytest.mark.asyncio
async def test_local_monitor_is_ai_free_idempotent_and_restart_safe(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'monitor.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.now(UTC)
    preview = {
        "symbol": "SOLUSDT", "quantity": "1", "entry": "100",
        "stop_loss": "98", "take_profit": "104",
    }
    with sessions.begin() as session:
        session.add(ControlledLiveProposalRecord(
            proposal_id="proposal", proposal_hash="p" * 64, profile_name="profile",
            profile_hash="h" * 64, selection_hash="s" * 64, admin_telegram_id=42,
            source="AI_AUTONOMOUS_V1", preview_json=json.dumps(preview),
            status="PROTECTED", client_order_id="owned-entry", created_at=now,
            approved_at=now, submitted_at=now, completed_at=now, updated_at=now,
        ))
        session.add(ExecutionOrderRecord(
            exchange="bybit", account_id="main", client_order_id="owned-entry",
            symbol="SOLUSDT", side="BUY", quantity=D("1"), request_hash="r" * 64,
            status="FILLED_PROTECTED", created_at=now, updated_at=now,
        ))
    gateway = FakeProtectionGateway()
    notifier = FakeNotifier()
    monitor = LocalPositionProfitProtector(sessions, gateway, notifier)
    await monitor.sync()
    await monitor.on_ticker("SOLUSDT", D("101.5"), D("101.51"))
    assert len(gateway.managed) == 1
    assert notifier.events[0][0] == "BREAK_EVEN"
    with sessions() as session:
        assert session.query(PositionProtectionEventRecord).filter_by(status="CONFIRMED").count() == 1

    restarted = LocalPositionProfitProtector(sessions, gateway, notifier)
    await restarted.sync()
    await restarted.on_ticker("SOLUSDT", D("101.5"), D("101.51"))
    assert len(gateway.managed) == 1
    assert len(notifier.events) == 1


@pytest.mark.asyncio
async def test_profit_watch_is_notification_only_and_restart_idempotent(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'watch.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.now(UTC)
    preview = {
        "symbol": "SOLUSDT", "quantity": "1", "entry": "100",
        "stop_loss": "98", "take_profit": "104",
    }
    with sessions.begin() as session:
        session.add(ControlledLiveProposalRecord(
            proposal_id="proposal-watch", proposal_hash="w" * 64,
            profile_name="profile", profile_hash="h" * 64,
            selection_hash="s" * 64, admin_telegram_id=42,
            source="AI_AUTONOMOUS_V1", preview_json=json.dumps(preview),
            status="PROTECTED", client_order_id="owned-entry", created_at=now,
            approved_at=now, submitted_at=now, completed_at=now, updated_at=now,
        ))
        session.add(ExecutionOrderRecord(
            exchange="bybit", account_id="main", client_order_id="owned-entry",
            symbol="SOLUSDT", side="BUY", quantity=D("1"), request_hash="r" * 64,
            status="FILLED_PROTECTED", created_at=now, updated_at=now,
        ))
    gateway = FakeProtectionGateway()
    notifier = FakeNotifier()
    monitor = LocalPositionProfitProtector(sessions, gateway, notifier)
    await monitor.sync()
    await monitor.on_ticker("SOLUSDT", D("100.70"), D("100.71"))
    assert gateway.managed == []
    assert len(notifier.events) == 1
    assert notifier.events[0][0] == "PROFIT_WATCH"

    restarted = LocalPositionProfitProtector(sessions, gateway, notifier)
    await restarted.sync()
    await restarted.on_ticker("SOLUSDT", D("100.70"), D("100.71"))
    assert gateway.managed == []
    assert len(notifier.events) == 1


@pytest.mark.asyncio
async def test_websocket_failure_enters_bounded_reconnect_path(tmp_path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'reconnect.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monitor = LocalPositionProfitProtector(sessions, FakeProtectionGateway(), FakeNotifier())
    monitor.sync = AsyncMock()

    class FailedConnection:
        async def __aenter__(self):
            raise RuntimeError("socket unavailable")

        async def __aexit__(self, *_):
            return False

    async def stop_after_failure(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(protector_module.websockets, "connect", lambda *_a, **_k: FailedConnection())
    monkeypatch.setattr(protector_module.asyncio, "sleep", stop_after_failure)
    with pytest.raises(asyncio.CancelledError):
        await monitor.run()
    monitor.sync.assert_awaited_once()


@pytest.mark.asyncio
async def test_bybit_keepalive_uses_application_ping(monkeypatch) -> None:
    sent: list[str] = []

    class FakeWebSocket:
        async def send(self, payload: str) -> None:
            sent.append(payload)

    sleeps = 0

    async def one_ping_then_stop(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(protector_module.asyncio, "sleep", one_ping_then_stop)
    with pytest.raises(asyncio.CancelledError):
        await LocalPositionProfitProtector._send_bybit_keepalive(FakeWebSocket())
    assert sent == [json.dumps({"op": "ping"})]
