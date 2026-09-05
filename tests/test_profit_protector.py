from datetime import UTC, datetime
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
    closes = [D("103")] * 16 + [D("104"), D("103.6"), D("103.1"), D("102.5")]
    bars = [ClosedBar(i, close, close + D("0.2"), close - D("0.2")) for i, close in enumerate(closes)]
    assert adverse_momentum_reversal("Buy", bars, D("0.8"))
    result = evaluate_protection(
        item,
        bid=D("102.5"),
        ask=D("102.51"),
        confirmed_stop=D("100.5"),
        previous_mfe=D("4"),
        stage="TRAILING_UPDATE",
        bars=bars,
        slippage_per_leg=SLIPPAGE,
    )
    assert result.action == "EARLY_PROFIT_EXIT"


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
