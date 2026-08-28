from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import (
    Base,
    FirstLiveProposalStateRecord,
    ShadowCandleRecord,
    ShadowCollectorStateRecord,
    ShadowDecisionRecord,
    ShadowExchangeHealthRecord,
)
from app.shadow.engine import PROTOCOL_ID
from app.shadow.signal_wait_status import (
    BybitWaitAccount,
    SignalWaitStatusRepository,
    SignalWaitStatusService,
    format_signal_wait_status,
    format_wait_reasons,
)
from app.strategy_lab.phase4g import FROZEN_CONFIG_HASH
from app.telegram.runner import dashboard, signal_wait_keyboard
from app.trading.controlled_live import CONTROLLED_LIVE_V1


def _sessions():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


class Reader:
    def __init__(self, *, fails=False):
        self.fails = fails
        self.reads = 0
        self.closed = False

    async def read(self):
        self.reads += 1
        if self.fails:
            raise RuntimeError("private GET unavailable")
        return BybitWaitAccount(Decimal("50.01"), 0, 0)

    async def close(self):
        self.closed = True


def _seed(sessions, now):
    phase_start = now - timedelta(hours=5)
    candle_open = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    candle_close = candle_open + timedelta(hours=1)
    with sessions.begin() as session:
        session.add(
            FirstLiveProposalStateRecord(
                profile_name=CONTROLLED_LIVE_V1.name,
                started_at=phase_start,
                status="WAITING_FOR_SIGNAL",
                created_at=phase_start,
                updated_at=phase_start,
            )
        )
        session.add(
            ShadowCandleRecord(
                protocol_id=PROTOCOL_ID,
                exchange="bybit",
                symbol="SOL/USDT",
                candle_open_time=candle_open,
                candle_close_time=candle_close,
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=Decimal("1000"),
                exchange_timestamp=candle_close + timedelta(seconds=5),
                received_at=candle_close + timedelta(seconds=5),
                data_hash="c" * 64,
            )
        )
        # This pre-Phase-5E decision must never enter the displayed counters.
        session.add(
            _decision(
                "old",
                candle_open - timedelta(hours=2),
                created_at=phase_start - timedelta(seconds=1),
                decision="LONG",
                reason="old decision",
            )
        )
        session.add(
            _decision(
                "wait-1",
                candle_open - timedelta(hours=1),
                created_at=now - timedelta(hours=2),
                decision="WAIT",
                reason="No frozen signal",
            )
        )
        session.add(
            _decision(
                "long-1",
                candle_open - timedelta(minutes=30),
                created_at=now - timedelta(hours=1, minutes=30),
                decision="LONG",
                reason="Approved by deterministic Risk Manager",
            )
        )
        session.add(
            _decision(
                "wait-latest",
                candle_open,
                created_at=now - timedelta(minutes=3),
                decision="WAIT",
                reason="No frozen signal",
            )
        )
        session.add(
            ShadowCollectorStateRecord(
                protocol_id=PROTOCOL_ID,
                instance_id="collector",
                host="test",
                pid=1,
                status="RUNNING",
                started_at=phase_start,
                heartbeat_at=now - timedelta(seconds=10),
                last_db_write_at=now - timedelta(seconds=10),
                lease_expires_at=now + timedelta(minutes=5),
                restart_count=0,
                updated_at=now - timedelta(seconds=10),
            )
        )
        session.add(
            ShadowExchangeHealthRecord(
                protocol_id=PROTOCOL_ID,
                exchange="bybit",
                status="HEALTHY",
                reason="",
                consecutive_failures=0,
                last_success_at=now,
                last_quote_at=now,
                checked_at=now,
                updated_at=now,
            )
        )
    return phase_start, candle_close


def _decision(decision_id, candle_open, *, created_at, decision, reason):
    return ShadowDecisionRecord(
        id=decision_id,
        protocol_id=PROTOCOL_ID,
        exchange="bybit",
        symbol="SOL/USDT",
        candle_open_time=candle_open,
        signal_timestamp=candle_open + timedelta(hours=1),
        decision=decision,
        signal_score=0 if decision == "WAIT" else 90,
        decision_price=Decimal("100"),
        observed_bid=Decimal("99.99"),
        observed_ask=Decimal("100.01"),
        observed_spread=Decimal("0.02"),
        risk_status="NOT_APPLICABLE" if decision == "WAIT" else "ALLOW",
        risk_reason=reason,
        strategy_hash=FROZEN_CONFIG_HASH,
        context_json='{"reason":"WAIT"}' if decision == "WAIT" else "{}",
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_wait_snapshot_uses_only_persisted_closed_candles_and_phase_decisions():
    now = datetime(2026, 8, 26, 12, 3, tzinfo=UTC)
    sessions = _sessions()
    phase_start, candle_close = _seed(sessions, now)
    reader = Reader()
    service = SignalWaitStatusService(SignalWaitStatusRepository(sessions), reader)
    with sessions() as session:
        before = session.scalar(select(func.count(ShadowDecisionRecord.id)))

    snapshot = await service.snapshot(now)

    with sessions() as session:
        after = session.scalar(select(func.count(ShadowDecisionRecord.id)))
    assert before == after  # "check now" did not run the strategy or create a decision.
    assert snapshot.phase_status == "WAITING_FOR_SIGNAL"
    assert snapshot.started_at == phase_start
    assert snapshot.waiting_seconds == 5 * 3600
    assert snapshot.last_closed_candle == candle_close
    assert snapshot.latest_candle_processed is True
    assert snapshot.last_analysis == now - timedelta(minutes=3)
    assert snapshot.analysis_age_seconds == 180
    assert snapshot.decisions == 3
    assert snapshot.wait == 2
    assert snapshot.long == 1
    assert snapshot.short == 0
    assert snapshot.latest_decision == "WAIT"
    assert snapshot.factual_reasons == ("No frozen signal",)
    assert snapshot.equity == Decimal("50.01")
    assert snapshot.open_positions == 0
    assert snapshot.open_orders == 0
    assert snapshot.bybit_connection == "HEALTHY"
    assert snapshot.collector_status == "RUNNING"
    assert reader.reads == 1
    assert not hasattr(reader, "submit_market")
    await service.close()
    assert reader.closed


@pytest.mark.asyncio
async def test_stale_data_stopped_collector_and_failed_private_get_are_fail_closed():
    now = datetime(2026, 8, 26, 12, 3, tzinfo=UTC)
    sessions = _sessions()
    _seed(sessions, now - timedelta(hours=4))
    with sessions.begin() as session:
        collector = session.get(ShadowCollectorStateRecord, PROTOCOL_ID)
        collector.heartbeat_at = now - timedelta(minutes=10)
        health = session.scalar(
            select(ShadowExchangeHealthRecord).where(
                ShadowExchangeHealthRecord.exchange == "bybit"
            )
        )
        health.status = "OFFLINE"
    snapshot = await SignalWaitStatusService(
        SignalWaitStatusRepository(sessions), Reader(fails=True)
    ).snapshot(now)
    assert snapshot.stale_data is True
    assert snapshot.collector_status == "STOPPED"
    assert snapshot.bybit_connection == "OFFLINE"
    assert snapshot.equity is None
    assert snapshot.open_positions is None
    assert snapshot.open_orders is None


@pytest.mark.asyncio
async def test_credential_free_telegram_reads_fresh_account_snapshot_from_postgres():
    now = datetime(2026, 8, 26, 12, 3, tzinfo=UTC)
    sessions = _sessions()
    _seed(sessions, now)
    repository = SignalWaitStatusRepository(sessions)
    shadow_service = SignalWaitStatusService(repository, Reader())
    await shadow_service.snapshot(now)
    await shadow_service.close()

    # Telegram has no Bybit key and no account reader. It receives only the
    # recent GET-only snapshot written by Shadow.
    telegram_snapshot = await SignalWaitStatusService(repository, None).snapshot(
        now + timedelta(seconds=30)
    )
    assert telegram_snapshot.equity == Decimal("50.01")
    assert telegram_snapshot.open_positions == 0
    assert telegram_snapshot.open_orders == 0
    assert telegram_snapshot.bybit_connection == "HEALTHY"


@pytest.mark.asyncio
async def test_unclosed_candle_is_ignored_by_check_now():
    now = datetime(2026, 8, 26, 12, 3, tzinfo=UTC)
    sessions = _sessions()
    _seed(sessions, now)
    open_time = now.replace(minute=0, second=0, microsecond=0)
    with sessions.begin() as session:
        session.add(
            ShadowCandleRecord(
                protocol_id=PROTOCOL_ID,
                exchange="bybit",
                symbol="SOL/USDT",
                candle_open_time=open_time,
                candle_close_time=open_time + timedelta(hours=1),
                open=Decimal("100"), high=Decimal("101"), low=Decimal("99"),
                close=Decimal("100"), volume=Decimal("1000"),
                exchange_timestamp=open_time + timedelta(hours=1),
                received_at=open_time + timedelta(hours=1),
                data_hash="n" * 64,
            )
        )
    snapshot = await SignalWaitStatusService(
        SignalWaitStatusRepository(sessions), Reader()
    ).snapshot(now)
    assert snapshot.last_closed_candle == open_time
    assert snapshot.latest_candle_processed is True


@pytest.mark.asyncio
async def test_formatting_contains_required_status_and_only_factual_wait_reason():
    now = datetime(2026, 8, 26, 12, 3, tzinfo=UTC)
    sessions = _sessions()
    _seed(sessions, now)
    snapshot = await SignalWaitStatusService(
        SignalWaitStatusRepository(sessions), Reader()
    ).snapshot(now)
    status = format_signal_wait_status(snapshot)
    for label in (
        "⏳ <b>ОЖИДАНИЕ СИГНАЛА</b>",
        "ЖДУ LONG/SHORT",
        "SOLUSDT",
        "Volatility Expansion 1H",
        "Последняя закрытая 1H свеча:",
        "Время последнего анализа:",
        "Всего решений после Phase 5E:",
        "Equity Bybit:",
        "Bybit connection: HEALTHY",
        "Shadow Collector: RUNNING",
        "Ожидание первого сигнала:",
    ):
        assert label in status
    reasons = format_wait_reasons(snapshot)
    assert "No frozen signal" in reasons
    assert "volatility expansion" not in reasons.lower()
    assert "MTF" not in reasons


def test_signal_wait_keyboard_is_read_only_and_exact():
    buttons = signal_wait_keyboard().inline_keyboard[0]
    assert [button.text for button in buttons] == [
        "🔄 Проверить сигнал сейчас",
        "📊 Почему WAIT?",
    ]
    assert [button.callback_data for button in buttons] == [
        "controlled:status",
        "controlled:why",
    ]


def test_production_dashboard_contains_only_controlled_live_actions():
    buttons = [
        button
        for row in dashboard().inline_keyboard
        for button in row
    ]
    callbacks = {button.text: button.callback_data for button in buttons}
    assert callbacks["🟢 CONTROLLED LIVE"] == "controlled:status"
    assert callbacks["🧪 Самопроверка"] == "system:selfcheck"
    assert "▶️ Запустить DEMO" not in callbacks
    assert "📊 SHADOW REPORT" not in callbacks
