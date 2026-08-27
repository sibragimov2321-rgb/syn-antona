from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.backtest.core import Candle
from app.db import (
    Base,
    ControlledLiveRuntimeRecord,
    ShadowCandleRecord,
    ShadowQuoteRecord,
)
from app.shadow.engine import PROTOCOL_ID
from app.shadow.market import PublicLiveMarketData
from app.shadow.protocol import canonical_json, verify_existing_lock, warmup_hash
from app.shadow.recovery import recover_after_downtime
from app.shadow.repository import ShadowRepository
from app.shadow.status import system_status, telegram_system_status
from app.shadow.transfer import transfer_runtime
from app.shadow.watchdog import check_health
from app.shadow.warmup_bundle import export_warmup_bundle, load_warmup_bundle
from app.shadow.runner import _update_exchange_health, _wait_for_collector_lease
from app.strategy_lab.phase4g import FROZEN_CONFIG_HASH
from app.trading.controlled_universe import PROFILE_NAME


def _repository(url: str | None = None) -> ShadowRepository:
    options = {"connect_args": {"check_same_thread": False}}
    if url is None:
        options["poolclass"] = StaticPool
    engine = create_engine(url or "sqlite://", **options)
    Base.metadata.create_all(engine)
    return ShadowRepository(sessionmaker(bind=engine, expire_on_commit=False))


def _protocol(locked_at: datetime) -> dict:
    return {
        "id": PROTOCOL_ID,
        "locked_at": locked_at.isoformat(),
        "strategy_version": "phase4g_volatility_expansion_1h_frozen_v1",
        "strategy_config_hash": FROZEN_CONFIG_HASH,
        "source_revision": {"hash": "source"},
        "warmup": {"cutoff": locked_at.replace(minute=0).isoformat(), "data_hash": "warmup"},
        "exchanges": ["binance"],
        "assets": ["BTC/USDT"],
    }


def _save_protocol(repository: ShadowRepository, protocol: dict) -> str:
    protocol_hash = sha256(canonical_json(protocol)).hexdigest()
    repository.create_protocol(
        {
            "id": PROTOCOL_ID,
            "locked_at": datetime.fromisoformat(protocol["locked_at"]),
            "strategy_version": protocol["strategy_version"],
            "config_hash": protocol["strategy_config_hash"],
            "source_hash": protocol["source_revision"]["hash"],
            "warmup_hash": protocol["warmup"]["data_hash"],
            "protocol_hash": protocol_hash,
            "protocol_json": json.dumps(protocol, sort_keys=True),
            "status": "ACTIVE",
        }
    )
    return protocol_hash


class RecoveryMarket:
    def __init__(self, candles):
        self.candles = candles

    async def recovery_candles(self, exchange, symbol, start, end):
        return [candle for candle in self.candles if start <= candle.timestamp < end]


@pytest.mark.asyncio
async def test_downtime_recovery_is_marked_wait_and_idempotent() -> None:
    repository = _repository()
    locked = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
    protocol = _protocol(locked)
    _save_protocol(repository, protocol)
    candle = Candle(
        datetime(2026, 1, 1, 1, tzinfo=UTC),
        Decimal("100"),
        Decimal("101"),
        Decimal("99"),
        Decimal("100"),
        Decimal("10"),
    )
    first = await recover_after_downtime(
        repository,
        RecoveryMarket([candle]),
        protocol,
        through=datetime(2026, 1, 1, 2, tzinfo=UTC),
    )
    second = await recover_after_downtime(
        repository,
        RecoveryMarket([candle]),
        protocol,
        through=datetime(2026, 1, 1, 2, tzinfo=UTC),
    )
    row = repository.latest_candle(PROTOCOL_ID)
    assert first["recovered"] == 1
    assert second["recovered"] == 0
    assert row.recovered_after_downtime is True
    assert repository.decision_counts(PROTOCOL_ID) == {"WAIT": 1, "LONG": 0, "SHORT": 0}
    with repository.session_factory() as session:
        record = session.execute(Base.metadata.tables["shadow_decisions"].select()).mappings().one()
    assert record["observed_bid"] is None
    assert "RECOVERED_AFTER_DOWNTIME=true" in record["risk_reason"]


def test_collector_lease_survives_restart_and_blocks_second_instance() -> None:
    repository = _repository()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    protocol = _protocol(now)
    _save_protocol(repository, protocol)
    assert repository.acquire_collector_lease(PROTOCOL_ID, "a", "host", 1, now, 300) == (True, 0)
    assert repository.acquire_collector_lease(PROTOCOL_ID, "b", "host", 2, now, 300)[0] is False
    repository.release_collector_lease(PROTOCOL_ID, "a", now + timedelta(seconds=1))
    assert repository.acquire_collector_lease(PROTOCOL_ID, "b", "host", 2, now + timedelta(seconds=2), 300) == (True, 1)


@pytest.mark.asyncio
async def test_rolling_deploy_waits_for_lease_without_collector_crash(monkeypatch) -> None:
    repository = _repository()
    now = datetime.now(UTC)
    _save_protocol(repository, _protocol(now))
    assert repository.acquire_collector_lease(
        PROTOCOL_ID, "old", "host", 1, now, 300
    )[0]
    sleeps = 0

    async def release_on_wait(_seconds):
        nonlocal sleeps
        sleeps += 1
        repository.release_collector_lease(
            PROTOCOL_ID, "old", datetime.now(UTC)
        )

    monkeypatch.setattr("app.shadow.runner.asyncio.sleep", release_on_wait)
    restart_count = await _wait_for_collector_lease(
        repository, "new", "host", 2, 300, poll_seconds=0
    )

    assert sleeps == 1
    assert restart_count == 1
    assert repository.collector_state(PROTOCOL_ID).instance_id == "new"


def test_old_shadow_lease_cannot_rearm_execution_status() -> None:
    repository = _repository()
    now = datetime.now(UTC)
    _save_protocol(repository, _protocol(now))
    repository.acquire_collector_lease(PROTOCOL_ID, "active", "host", 1, now, 300)
    repository.record_collector_runtime(
        PROTOCOL_ID,
        "active",
        dry_run=False,
        live_trading_enabled=True,
        controlled_live_enabled=True,
        manual_first_order_approved=True,
        deployment_id="deployment-2",
        replica_id="replica-1",
        now=now,
    )

    status = system_status(repository, now)
    runtime = status["execution_runtime"]
    assert runtime["shadow"] == "DISABLED"
    assert runtime["controlled_live"] == "DISARMED"
    assert runtime["real_order_execution"] == "DISABLED"
    assert status["live_trading"] == "OFF"
    text = telegram_system_status(repository)
    assert "SHADOW: <b>DISABLED</b>" in text
    assert "CONTROLLED LIVE: <b>DISARMED</b>" in text
    assert "REAL ORDER EXECUTION: <b>DISABLED</b>" in text


def test_dedicated_controlled_worker_arms_independently_of_shadow() -> None:
    repository = _repository()
    now = datetime.now(UTC)
    _save_protocol(repository, _protocol(now))
    with repository.session_factory.begin() as session:
        session.add(
            ControlledLiveRuntimeRecord(
                profile_name=PROFILE_NAME,
                instance_id="controlled-1",
                status="RUNNING",
                started_at=now,
                heartbeat_at=now,
                dry_run=False,
                live_trading_enabled=True,
                controlled_live_enabled=True,
                manual_first_order_approved=True,
                real_order_execution_enabled=True,
                deployment_id="deployment-controlled",
                updated_at=now,
            )
        )
    runtime = system_status(repository, now)["execution_runtime"]
    assert runtime["shadow"] == "DISABLED"
    assert runtime["controlled_live"] == "ARMED"
    assert runtime["real_order_execution"] == "ENABLED"


def test_daily_snapshot_is_updated_not_duplicated() -> None:
    repository = _repository()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    _save_protocol(repository, _protocol(now))
    assert repository.save_daily_snapshot(PROTOCOL_ID, now.date(), {"signals": 0}) is True
    assert repository.save_daily_snapshot(PROTOCOL_ID, now.date(), {"signals": 2}) is False
    with repository.session_factory() as session:
        rows = session.execute(Base.metadata.tables["shadow_daily_snapshots"].select()).mappings().all()
    assert len(rows) == 1
    assert json.loads(rows[0]["metrics_json"])["signals"] == 2


def test_sqlite_to_database_transfer_preserves_lock_and_rows(tmp_path) -> None:
    source_path = tmp_path / "source.db"
    target_path = tmp_path / "target.db"
    source_url = f"sqlite:///{source_path.as_posix()}"
    target_url = f"sqlite:///{target_path.as_posix()}"
    source = _repository(source_url)
    target = _repository(target_url)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    protocol = _protocol(now)
    protocol_hash = _save_protocol(source, protocol)
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps(protocol), encoding="utf-8")
    result = transfer_runtime(
        source_url, target_url, lock, require_postgresql=False
    )
    assert result["protocol_hash"] == protocol_hash
    assert result["tables"]["prospective_protocols"]["rows"] == 1
    assert target.protocol(PROTOCOL_ID).locked_at == source.protocol(PROTOCOL_ID).locked_at
    assert target.protocol(PROTOCOL_ID).config_hash == FROZEN_CONFIG_HASH


def test_watchdog_rejects_protocol_hash_mismatch(tmp_path) -> None:
    repository = _repository()
    now = datetime.now(UTC)
    protocol = _protocol(now)
    _save_protocol(repository, protocol)
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps({**protocol, "assets": ["ETH/USDT"]}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="PROTOCOL HASH MISMATCH"):
        check_health(
            repository,
            lock,
            heartbeat_max_age=300,
            quote_max_age=300,
            candle_max_age=7500,
            now=now,
        )


def test_system_status_preserves_live_off() -> None:
    repository = _repository()
    now = datetime.now(UTC)
    _save_protocol(repository, _protocol(now))
    status = system_status(repository, now)
    assert status["protocol"] == "LOCKED"
    assert status["live_trading"] == "OFF"
    assert status["collector_status"] == "DISABLED"


def test_existing_lock_verification_has_no_automatic_creation(tmp_path, monkeypatch) -> None:
    repository = _repository()
    now = datetime.now(UTC)
    protocol = _protocol(now)
    _save_protocol(repository, protocol)
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps(protocol), encoding="utf-8")
    monkeypatch.setattr("app.shadow.protocol.verify_frozen_implementation", lambda: None)
    monkeypatch.setattr(
        "app.shadow.protocol.source_manifest", lambda project_root: {"hash": "source"}
    )
    monkeypatch.setattr("app.shadow.protocol.warmup_hash", lambda warmups: "warmup")
    assert verify_existing_lock(lock, repository, tmp_path, {})["locked_at"] == now.isoformat()
    missing = tmp_path / "missing.json"
    with pytest.raises(RuntimeError, match="PROTOCOL HASH MISMATCH"):
        verify_existing_lock(missing, repository, tmp_path, {})
    assert not missing.exists()


@pytest.mark.asyncio
async def test_stale_exchange_timestamp_blocks_snapshot() -> None:
    old = datetime.now(UTC) - timedelta(minutes=5)

    class Client:
        def fetch_ticker(self, symbol):
            return {"timestamp": int(old.timestamp() * 1000), "last": 100}

        def fetch_order_book(self, symbol, limit):
            return {
                "timestamp": int(old.timestamp() * 1000),
                "bids": [[99, 1]],
                "asks": [[101, 1]],
            }

        def market(self, symbol):
            return {"limits": {}, "precision": {}}

    market = PublicLiveMarketData.__new__(PublicLiveMarketData)
    market.clients = {"binance": Client()}
    market.stale_after = timedelta(seconds=30)
    with pytest.raises(RuntimeError, match="STALE DATA"):
        await market.snapshot("binance", "BTC/USDT")


@pytest.mark.asyncio
async def test_exchange_degrades_then_offline_and_recovers() -> None:
    repository = _repository()
    now = datetime.now(UTC)
    protocol = _protocol(now)
    _save_protocol(repository, protocol)

    class Notifier:
        def __init__(self):
            self.messages = []

        async def system(self, title, message):
            self.messages.append((title, message))

    notifier = Notifier()
    failed = {
        "errors": [
            {"exchange": "binance", "symbol": "BTC/USDT", "error": "RequestTimeout"}
        ]
    }
    for expected in ("DEGRADED", "DEGRADED", "OFFLINE"):
        status = await _update_exchange_health(repository, notifier, protocol, failed, 3)
        assert status["binance"] == expected
    restored = await _update_exchange_health(
        repository, notifier, protocol, {"errors": []}, 3
    )
    assert restored["binance"] == "HEALTHY"
    assert [title for title, _ in notifier.messages] == [
        "EXCHANGE OFFLINE",
        "EXCHANGE RESTORED",
    ]


def test_immutable_warmup_bundle_round_trip(tmp_path) -> None:
    cutoff = datetime(2026, 1, 20, tzinfo=UTC)
    candles = [
        Candle(
            cutoff - timedelta(hours=300 - index),
            Decimal("100"),
            Decimal("101"),
            Decimal("99"),
            Decimal("100"),
            Decimal("10"),
        )
        for index in range(300)
    ]
    protocol = _protocol(cutoff + timedelta(minutes=30))
    protocol["warmup"]["cutoff"] = cutoff.isoformat()
    protocol["warmup"]["data_hash"] = warmup_hash({("binance", "BTC/USDT"): candles})
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps(protocol), encoding="utf-8")
    bundle = tmp_path / "warmup.json.gz"

    class Cache:
        def load(self, exchange, symbol, timeframe, start, end):
            return candles

    exported = export_warmup_bundle(lock, bundle, Cache())
    loaded = load_warmup_bundle(bundle, protocol)
    assert exported["candles"] == 300
    assert loaded[("binance", "BTC/USDT")] == candles


def test_restart_repairs_orphan_candle_without_retroactive_trade() -> None:
    repository = _repository()
    now = datetime.now(UTC).replace(microsecond=0)
    _save_protocol(repository, _protocol(now))
    with repository.session_factory.begin() as session:
        session.add(
            ShadowQuoteRecord(
                protocol_id=PROTOCOL_ID,
                exchange="binance",
                symbol="BTC/USDT",
                bid=Decimal("99"),
                ask=Decimal("101"),
                last=Decimal("100"),
                spread=Decimal("2"),
                spread_pct=Decimal("0.02"),
                orderbook_json="{}",
                exchange_timestamp=now,
                received_at=now,
            )
        )
        session.add(
            ShadowCandleRecord(
                protocol_id=PROTOCOL_ID,
                exchange="binance",
                symbol="BTC/USDT",
                candle_open_time=now - timedelta(hours=1),
                candle_close_time=now,
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=Decimal("10"),
                exchange_timestamp=now,
                received_at=now,
                data_hash="orphan",
            )
        )
    assert repository.repair_orphan_decisions(PROTOCOL_ID, FROZEN_CONFIG_HASH) == 1
    assert repository.repair_orphan_decisions(PROTOCOL_ID, FROZEN_CONFIG_HASH) == 0
    assert repository.decision_counts(PROTOCOL_ID)["WAIT"] == 1
    assert repository.open_trades(PROTOCOL_ID) == []
    with repository.session_factory() as session:
        decision = session.execute(Base.metadata.tables["shadow_decisions"].select()).mappings().one()
    assert decision["observed_bid"] == Decimal("99")
    assert "retroactive shadow execution prohibited" in decision["risk_reason"]
