from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import (
    AILiveRuntimeRecord,
    Base,
    ControlledLiveRuntimeRecord,
    ControlledLiveStateRecord,
    ExecutionOrderRecord,
    ShadowCollectorStateRecord,
)
from app.trading.controlled_live import (
    CONTROLLED_LIVE_V1,
    CONTROLLED_LIVE_V1_FIRST_INSTRUMENT,
)
from app.trading.controlled_universe import PROFILE_NAME
from app.trading.self_check import format_self_check_ru, trading_self_check


NOW = datetime(2026, 8, 28, 15, 0, tzinfo=UTC)


def _sessions():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _healthy():
    sessions = _sessions()
    with sessions.begin() as session:
        session.add(
            ControlledLiveStateRecord(
                profile_name=CONTROLLED_LIVE_V1.name,
                profile_hash=CONTROLLED_LIVE_V1.config_hash,
                first_symbol=CONTROLLED_LIVE_V1_FIRST_INSTRUMENT.symbol,
                selection_hash=CONTROLLED_LIVE_V1_FIRST_INSTRUMENT.selection_hash,
                kill_switch_active=False,
                updated_at=NOW,
            )
        )
        session.add(
            ControlledLiveRuntimeRecord(
                profile_name=PROFILE_NAME,
                instance_id="worker-1",
                status="RUNNING",
                started_at=NOW - timedelta(hours=1),
                heartbeat_at=NOW - timedelta(seconds=30),
                dry_run=False,
                live_trading_enabled=True,
                controlled_live_enabled=True,
                manual_first_order_approved=True,
                real_order_execution_enabled=True,
                updated_at=NOW,
            )
        )
        session.add(
            AILiveRuntimeRecord(
                runtime_name="AI_LIVE",
                enabled=True,
                status="RUNNING",
                model="openai/gpt-5.4",
                scan_interval_seconds=300,
                last_scan_at=NOW - timedelta(minutes=1),
                next_scan_at=NOW + timedelta(minutes=4),
                last_market_data_at=NOW - timedelta(minutes=1),
                equity=Decimal("50"),
                available_balance=Decimal("45"),
                open_positions=3,
                open_positions_json="[]",
                open_orders=6,
                total_scans=10,
                last_error=None,
                heartbeat_at=NOW,
                updated_at=NOW,
            )
        )
    return sessions


def test_healthy_production_self_check_passes():
    value = trading_self_check(_healthy(), NOW)
    assert value.passed
    assert value.open_positions == 3
    assert value.open_orders == 6


@pytest.mark.parametrize(
    ("mutation", "failed_field"),
    [
        (lambda worker, ai, state: setattr(worker, "heartbeat_at", NOW - timedelta(minutes=4)), "worker_ok"),
        (lambda worker, ai, state: setattr(ai, "last_market_data_at", NOW - timedelta(minutes=11)), "ai_ok"),
        (lambda worker, ai, state: setattr(ai, "last_error", "provider unavailable"), "ai_ok"),
        (lambda worker, ai, state: setattr(state, "kill_switch_active", True), "kill_switch_safe"),
        (lambda worker, ai, state: setattr(ai, "open_positions", 4), "positions"),
        (lambda worker, ai, state: setattr(state, "profile_hash", "mismatch"), "profile_hash_ok"),
    ],
)
def test_self_check_fails_closed_for_unhealthy_runtime(mutation, failed_field):
    sessions = _healthy()
    with sessions.begin() as session:
        worker = session.get(ControlledLiveRuntimeRecord, PROFILE_NAME)
        ai = session.get(AILiveRuntimeRecord, "AI_LIVE")
        state = session.get(ControlledLiveStateRecord, CONTROLLED_LIVE_V1.name)
        mutation(worker, ai, state)
    value = trading_self_check(sessions, NOW)
    assert not value.passed
    if failed_field == "positions":
        assert value.open_positions == 4
    else:
        assert getattr(value, failed_field) is False


def test_self_check_detects_active_shadow_and_unknown_order():
    sessions = _healthy()
    with sessions.begin() as session:
        session.add(
            ShadowCollectorStateRecord(
                protocol_id="phase4i-prospective",
                instance_id="shadow-1",
                host="host",
                pid=1,
                status="RUNNING",
                started_at=NOW,
                heartbeat_at=NOW,
                last_db_write_at=NOW,
                lease_expires_at=NOW + timedelta(minutes=5),
                restart_count=0,
                updated_at=NOW,
            )
        )
        session.add(
            ExecutionOrderRecord(
                exchange="bybit",
                account_id="main",
                client_order_id="unknown-1",
                symbol="SOL/USDT",
                side="BUY",
                quantity=Decimal("0.1"),
                request_hash="hash",
                status="UNKNOWN",
            )
        )
    value = trading_self_check(sessions, NOW)
    assert not value.passed
    assert value.shadow_collectors == 1
    assert value.unknown_orders == 1


def test_self_check_database_failure_is_reported_without_exception():
    def broken_factory():
        raise RuntimeError("database offline")

    value = trading_self_check(broken_factory, NOW)
    assert not value.passed
    assert not value.database_ok


def test_self_check_formatter_is_explicitly_read_only():
    text = format_self_check_ru(trading_self_check(_healthy(), NOW))
    assert "Итог: <b>PASS</b>" in text
    assert "Margin gate: ISOLATED" in text
    assert "Leverage: 10x" in text
    assert "ордера не создаются" in text


def test_self_check_formatter_shows_sanitized_ai_failure_reason():
    sessions = _healthy()
    with sessions.begin() as session:
        ai = session.get(AILiveRuntimeRecord, "AI_LIVE")
        ai.status = "DEGRADED"
        ai.last_error = "AI HTTP 402: insufficient AI provider credits"
    text = format_self_check_ru(trading_self_check(sessions, NOW))
    assert "Итог: <b>WARNING</b>" in text
    assert "Причина AI: AI HTTP 402: insufficient AI provider credits" in text
