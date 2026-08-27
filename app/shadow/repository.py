from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db import (
    ProspectiveProtocolRecord,
    SessionLocal,
    ShadowCandleRecord,
    ShadowCollectorStateRecord,
    ShadowDailySnapshotRecord,
    ShadowDecisionRecord,
    ShadowExchangeHealthRecord,
    ShadowQuoteRecord,
    ShadowSystemEventRecord,
    ShadowTradeRecord,
)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class ShadowRepository:
    def __init__(self, session_factory: Callable[[], Session] = SessionLocal) -> None:
        self.session_factory = session_factory

    def protocol(self, protocol_id: str) -> ProspectiveProtocolRecord | None:
        with self.session_factory() as session:
            record = session.get(ProspectiveProtocolRecord, protocol_id)
            if record:
                session.expunge(record)
            return record

    def create_protocol(self, values: dict) -> None:
        with self.session_factory.begin() as session:
            if session.get(ProspectiveProtocolRecord, values["id"]):
                raise RuntimeError("Prospective protocol already exists")
            session.add(ProspectiveProtocolRecord(**values))

    def ping(self) -> datetime:
        with self.session_factory() as session:
            session.execute(text("SELECT 1"))
        return datetime.now(UTC)

    def save_quote(self, protocol_id: str, snapshot) -> None:
        orderbook = {
            "bids": [[str(price), str(size)] for price, size in snapshot.bids],
            "asks": [[str(price), str(size)] for price, size in snapshot.asks],
        }
        with self.session_factory.begin() as session:
            session.add(
                ShadowQuoteRecord(
                    protocol_id=protocol_id,
                    exchange=snapshot.exchange,
                    symbol=snapshot.symbol,
                    bid=snapshot.bid,
                    ask=snapshot.ask,
                    last=snapshot.last,
                    spread=snapshot.spread,
                    spread_pct=snapshot.spread_pct,
                    orderbook_json=json.dumps(orderbook, separators=(",", ":")),
                    exchange_timestamp=snapshot.exchange_timestamp,
                    received_at=snapshot.received_at,
                )
            )

    def save_candle(self, protocol_id: str, observation, data_hash: str) -> bool:
        with self.session_factory.begin() as session:
            exists = session.scalar(
                select(ShadowCandleRecord.id).where(
                    ShadowCandleRecord.protocol_id == protocol_id,
                    ShadowCandleRecord.exchange == observation.exchange,
                    ShadowCandleRecord.symbol == observation.symbol,
                    ShadowCandleRecord.candle_open_time == observation.candle.timestamp,
                )
            )
            if exists:
                return False
            candle = observation.candle
            session.add(
                ShadowCandleRecord(
                    protocol_id=protocol_id,
                    exchange=observation.exchange,
                    symbol=observation.symbol,
                    candle_open_time=candle.timestamp,
                    candle_close_time=observation.close_time,
                    open=candle.open,
                    high=candle.high,
                    low=candle.low,
                    close=candle.close,
                    volume=candle.volume,
                    exchange_timestamp=observation.exchange_timestamp,
                    received_at=observation.received_at,
                    data_hash=data_hash,
                )
            )
            return True

    def record_recovered_candle(
        self,
        protocol_id: str,
        exchange: str,
        symbol: str,
        candle,
        data_hash: str,
        strategy_hash: str,
        recovery_time: datetime,
    ) -> bool:
        """Atomically records OHLCV recovered without a contemporaneous quote.

        A deterministic WAIT is stored in the same transaction. Nullable quote fields make it
        impossible to mistake the recovery record for observed live bid/ask execution data.
        """
        from hashlib import sha256

        close_time = _utc(candle.timestamp) + timedelta(hours=1)
        decision_id = sha256(
            f"{protocol_id}:{exchange}:{symbol}:{_utc(candle.timestamp).isoformat()}".encode()
        ).hexdigest()
        with self.session_factory.begin() as session:
            exists = session.scalar(
                select(ShadowCandleRecord.id).where(
                    ShadowCandleRecord.protocol_id == protocol_id,
                    ShadowCandleRecord.exchange == exchange,
                    ShadowCandleRecord.symbol == symbol,
                    ShadowCandleRecord.candle_open_time == candle.timestamp,
                )
            )
            if exists:
                return False
            session.add(
                ShadowCandleRecord(
                    protocol_id=protocol_id,
                    exchange=exchange,
                    symbol=symbol,
                    candle_open_time=candle.timestamp,
                    candle_close_time=close_time,
                    open=candle.open,
                    high=candle.high,
                    low=candle.low,
                    close=candle.close,
                    volume=candle.volume,
                    exchange_timestamp=close_time,
                    received_at=recovery_time,
                    data_hash=data_hash,
                    recovered_after_downtime=True,
                    recovery_recorded_at=recovery_time,
                )
            )
            session.add(
                ShadowDecisionRecord(
                    id=decision_id,
                    protocol_id=protocol_id,
                    exchange=exchange,
                    symbol=symbol,
                    candle_open_time=candle.timestamp,
                    signal_timestamp=close_time,
                    decision="WAIT",
                    signal_score=0,
                    decision_price=candle.close,
                    observed_bid=None,
                    observed_ask=None,
                    observed_spread=None,
                    risk_status="NOT_APPLICABLE",
                    risk_reason=(
                        "RECOVERED_AFTER_DOWNTIME=true; live bid/ask unavailable; "
                        "shadow signal suppressed"
                    ),
                    strategy_hash=strategy_hash,
                    context_json=json.dumps(
                        {
                            "recovered_after_downtime": True,
                            "live_quote_available": False,
                            "orderbook_available": False,
                        },
                        sort_keys=True,
                    ),
                )
            )
            return True

    def last_candle_open(self, protocol_id: str, exchange: str, symbol: str) -> datetime | None:
        with self.session_factory() as session:
            value = session.scalar(
                select(func.max(ShadowCandleRecord.candle_open_time)).where(
                    ShadowCandleRecord.protocol_id == protocol_id,
                    ShadowCandleRecord.exchange == exchange,
                    ShadowCandleRecord.symbol == symbol,
                )
            )
        return _utc(value) if value else None

    def prospective_candles(self, protocol_id: str, exchange: str, symbol: str):
        with self.session_factory() as session:
            rows = session.scalars(
                select(ShadowCandleRecord)
                .where(
                    ShadowCandleRecord.protocol_id == protocol_id,
                    ShadowCandleRecord.exchange == exchange,
                    ShadowCandleRecord.symbol == symbol,
                )
                .order_by(ShadowCandleRecord.candle_open_time)
            ).all()
            for row in rows:
                session.expunge(row)
            return rows

    def record_decision(self, values: dict, trade: dict | None = None) -> None:
        with self.session_factory.begin() as session:
            session.add(ShadowDecisionRecord(**values))
            if trade:
                first_trade = not session.scalar(
                    select(func.count())
                    .select_from(ShadowTradeRecord)
                    .where(ShadowTradeRecord.protocol_id == values["protocol_id"])
                )
                session.add(ShadowTradeRecord(**trade))
                if first_trade:
                    session.add(
                        ShadowSystemEventRecord(
                            protocol_id=values["protocol_id"],
                            event_type="FIRST_SHADOW_TRADE",
                            exchange=trade["exchange"],
                            severity="INFO",
                            message="First shadow trade opened",
                            details_json=json.dumps(
                                {"trade": trade}, default=str, sort_keys=True
                            ),
                        )
                    )

    def repair_orphan_decisions(
        self, protocol_id: str, strategy_hash: str
    ) -> int:
        repaired = 0
        with self.session_factory.begin() as session:
            decision_exists = select(ShadowDecisionRecord.id).where(
                ShadowDecisionRecord.protocol_id == ShadowCandleRecord.protocol_id,
                ShadowDecisionRecord.exchange == ShadowCandleRecord.exchange,
                ShadowDecisionRecord.symbol == ShadowCandleRecord.symbol,
                ShadowDecisionRecord.candle_open_time
                == ShadowCandleRecord.candle_open_time,
            ).exists()
            candles = session.scalars(
                select(ShadowCandleRecord).where(
                    ShadowCandleRecord.protocol_id == protocol_id,
                    ~decision_exists,
                )
            ).all()
            for candle in candles:
                quote = session.scalar(
                    select(ShadowQuoteRecord)
                    .where(
                        ShadowQuoteRecord.protocol_id == protocol_id,
                        ShadowQuoteRecord.exchange == candle.exchange,
                        ShadowQuoteRecord.symbol == candle.symbol,
                        ShadowQuoteRecord.received_at == candle.received_at,
                    )
                    .limit(1)
                )
                decision_id = sha256(
                    f"{protocol_id}:{candle.exchange}:{candle.symbol}:"
                    f"{_utc(candle.candle_open_time).isoformat()}".encode()
                ).hexdigest()
                session.add(
                    ShadowDecisionRecord(
                        id=decision_id,
                        protocol_id=protocol_id,
                        exchange=candle.exchange,
                        symbol=candle.symbol,
                        candle_open_time=candle.candle_open_time,
                        signal_timestamp=candle.candle_close_time,
                        decision="WAIT",
                        signal_score=0,
                        decision_price=candle.close,
                        observed_bid=quote.bid if quote else None,
                        observed_ask=quote.ask if quote else None,
                        observed_spread=quote.spread if quote else None,
                        risk_status="NOT_APPLICABLE",
                        risk_reason=(
                            "PROCESS_INTERRUPTED_AFTER_CANDLE=true; retroactive shadow "
                            "execution prohibited"
                        ),
                        strategy_hash=strategy_hash,
                        context_json=json.dumps(
                            {
                                "process_interrupted_after_candle": True,
                                "live_quote_recovered_from_journal": bool(quote),
                                "retroactive_trade_allowed": False,
                            },
                            sort_keys=True,
                        ),
                    )
                )
                repaired += 1
        return repaired

    def open_trade(self, protocol_id: str, exchange: str, symbol: str):
        with self.session_factory() as session:
            record = session.scalar(
                select(ShadowTradeRecord).where(
                    ShadowTradeRecord.protocol_id == protocol_id,
                    ShadowTradeRecord.exchange == exchange,
                    ShadowTradeRecord.symbol == symbol,
                    ShadowTradeRecord.status == "OPEN",
                )
            )
            if record:
                session.expunge(record)
            return record

    def open_trades(self, protocol_id: str):
        return self._trades(protocol_id, "OPEN")

    def closed_trades(self, protocol_id: str):
        return self._trades(protocol_id, "CLOSED")

    def _trades(self, protocol_id: str, status: str):
        with self.session_factory() as session:
            rows = session.scalars(
                select(ShadowTradeRecord)
                .where(
                    ShadowTradeRecord.protocol_id == protocol_id,
                    ShadowTradeRecord.status == status,
                )
                .order_by(ShadowTradeRecord.opened_at)
            ).all()
            for row in rows:
                session.expunge(row)
            return rows

    def close_trade(self, trade_id: str, values: dict) -> None:
        with self.session_factory.begin() as session:
            record = session.get(ShadowTradeRecord, trade_id)
            if not record or record.status != "OPEN":
                raise RuntimeError("Shadow trade is not open")
            for key, value in values.items():
                setattr(record, key, value)
            session.add(
                ShadowSystemEventRecord(
                    protocol_id=record.protocol_id,
                    event_type="SHADOW_POSITION_CLOSED",
                    exchange=record.exchange,
                    severity="INFO",
                    message=f"Shadow position {trade_id} closed",
                    details_json=json.dumps(
                        {
                            "trade": {
                                "id": record.id,
                                "exchange": record.exchange,
                                "symbol": record.symbol,
                                "side": record.side,
                            },
                            "values": values,
                        },
                        default=str,
                        sort_keys=True,
                    ),
                )
            )

    def decisions_count(self, protocol_id: str, *, signals_only: bool = False) -> int:
        with self.session_factory() as session:
            statement = select(func.count()).select_from(ShadowDecisionRecord).where(
                ShadowDecisionRecord.protocol_id == protocol_id
            )
            if signals_only:
                statement = statement.where(ShadowDecisionRecord.decision != "WAIT")
            return int(session.scalar(statement) or 0)

    def decision_counts(self, protocol_id: str) -> dict[str, int]:
        with self.session_factory() as session:
            rows = session.execute(
                select(ShadowDecisionRecord.decision, func.count())
                .where(ShadowDecisionRecord.protocol_id == protocol_id)
                .group_by(ShadowDecisionRecord.decision)
            ).all()
        counts = {"WAIT": 0, "LONG": 0, "SHORT": 0}
        counts.update({decision: int(total) for decision, total in rows})
        return counts

    def daily_activity(self, protocol_id: str, day: date) -> dict:
        start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        end = start + timedelta(days=1)
        with self.session_factory() as session:
            decisions = session.execute(
                select(ShadowDecisionRecord.decision, func.count())
                .where(
                    ShadowDecisionRecord.protocol_id == protocol_id,
                    ShadowDecisionRecord.signal_timestamp >= start,
                    ShadowDecisionRecord.signal_timestamp < end,
                )
                .group_by(ShadowDecisionRecord.decision)
            ).all()
            opened = int(
                session.scalar(
                    select(func.count())
                    .select_from(ShadowTradeRecord)
                    .where(
                        ShadowTradeRecord.protocol_id == protocol_id,
                        ShadowTradeRecord.opened_at >= start,
                        ShadowTradeRecord.opened_at < end,
                    )
                )
                or 0
            )
            closed = session.scalars(
                select(ShadowTradeRecord).where(
                    ShadowTradeRecord.protocol_id == protocol_id,
                    ShadowTradeRecord.status == "CLOSED",
                    ShadowTradeRecord.closed_at >= start,
                    ShadowTradeRecord.closed_at < end,
                )
            ).all()
            for row in closed:
                session.expunge(row)
        counts = {"WAIT": 0, "LONG": 0, "SHORT": 0}
        counts.update({decision: int(total) for decision, total in decisions})
        metrics = shadow_metrics(closed)
        metrics.update(
            {
                "signals": counts["LONG"] + counts["SHORT"],
                "wait": counts["WAIT"],
                "long": counts["LONG"],
                "short": counts["SHORT"],
                "trades": opened,
                "closed_trades": len(closed),
                "open_positions": len(self.open_trades(protocol_id)),
            }
        )
        return metrics

    def first_candle(self, protocol_id: str):
        with self.session_factory() as session:
            row = session.scalar(
                select(ShadowCandleRecord)
                .where(ShadowCandleRecord.protocol_id == protocol_id)
                .order_by(ShadowCandleRecord.candle_close_time)
                .limit(1)
            )
            if row:
                session.expunge(row)
            return row

    def first_signal(self, protocol_id: str):
        with self.session_factory() as session:
            row = session.scalar(
                select(ShadowDecisionRecord)
                .where(
                    ShadowDecisionRecord.protocol_id == protocol_id,
                    ShadowDecisionRecord.decision != "WAIT",
                )
                .order_by(ShadowDecisionRecord.signal_timestamp)
                .limit(1)
            )
            if row:
                session.expunge(row)
            return row

    def latest_candle(self, protocol_id: str):
        with self.session_factory() as session:
            row = session.scalar(
                select(ShadowCandleRecord)
                .where(ShadowCandleRecord.protocol_id == protocol_id)
                .order_by(ShadowCandleRecord.candle_close_time.desc())
                .limit(1)
            )
            if row:
                session.expunge(row)
            return row

    def save_daily_snapshot(self, protocol_id: str, day: date, metrics: dict) -> bool:
        timestamp = datetime(day.year, day.month, day.day, tzinfo=UTC)
        with self.session_factory.begin() as session:
            exists = session.scalar(
                select(ShadowDailySnapshotRecord.id).where(
                    ShadowDailySnapshotRecord.protocol_id == protocol_id,
                    ShadowDailySnapshotRecord.snapshot_date == timestamp,
                )
            )
            now = datetime.now(UTC)
            payload = json.dumps(metrics, default=str, sort_keys=True)
            if exists:
                record = session.get(ShadowDailySnapshotRecord, exists)
                record.metrics_json = payload
                record.updated_at = now
                return False
            session.add(
                ShadowDailySnapshotRecord(
                    protocol_id=protocol_id,
                    snapshot_date=timestamp,
                    metrics_json=payload,
                    updated_at=now,
                )
            )
            return True

    def pending_daily_snapshot(self, protocol_id: str, before: date):
        cutoff = datetime(before.year, before.month, before.day, tzinfo=UTC)
        with self.session_factory() as session:
            record = session.scalar(
                select(ShadowDailySnapshotRecord)
                .where(
                    ShadowDailySnapshotRecord.protocol_id == protocol_id,
                    ShadowDailySnapshotRecord.snapshot_date < cutoff,
                    ShadowDailySnapshotRecord.report_sent_at.is_(None),
                )
                .order_by(ShadowDailySnapshotRecord.snapshot_date)
                .limit(1)
            )
            if record:
                session.expunge(record)
            return record

    def mark_daily_snapshot_sent(self, snapshot_id: int, sent_at: datetime) -> None:
        with self.session_factory.begin() as session:
            record = session.get(ShadowDailySnapshotRecord, snapshot_id)
            if record and record.report_sent_at is None:
                record.report_sent_at = sent_at

    def acquire_collector_lease(
        self,
        protocol_id: str,
        instance_id: str,
        host: str,
        pid: int,
        now: datetime,
        lease_seconds: int,
    ) -> tuple[bool, int]:
        with self.session_factory.begin() as session:
            record = session.scalar(
                select(ShadowCollectorStateRecord)
                .where(ShadowCollectorStateRecord.protocol_id == protocol_id)
                .with_for_update()
            )
            expires = now + timedelta(seconds=lease_seconds)
            if not record:
                session.add(
                    ShadowCollectorStateRecord(
                        protocol_id=protocol_id,
                        instance_id=instance_id,
                        host=host,
                        pid=pid,
                        status="STARTING",
                        started_at=now,
                        heartbeat_at=now,
                        last_db_write_at=now,
                        lease_expires_at=expires,
                        restart_count=0,
                        updated_at=now,
                    )
                )
                return True, 0
            lease_expiry = _utc(record.lease_expires_at)
            if record.instance_id != instance_id and lease_expiry > now:
                return False, record.restart_count
            record.instance_id = instance_id
            record.host = host
            record.pid = pid
            record.status = "STARTING"
            record.started_at = now
            record.heartbeat_at = now
            record.last_db_write_at = now
            record.lease_expires_at = expires
            record.restart_count += 1
            record.dry_run = None
            record.live_trading_enabled = None
            record.controlled_live_enabled = None
            record.manual_first_order_approved = None
            record.real_order_execution_enabled = None
            record.last_error = None
            record.updated_at = now
            return True, record.restart_count

    def record_collector_runtime(
        self,
        protocol_id: str,
        instance_id: str,
        *,
        dry_run: bool,
        live_trading_enabled: bool,
        controlled_live_enabled: bool,
        manual_first_order_approved: bool,
        deployment_id: str | None,
        replica_id: str | None,
        now: datetime,
    ) -> None:
        """Persist flags from the process that actually owns the execution lease."""
        with self.session_factory.begin() as session:
            record = session.get(ShadowCollectorStateRecord, protocol_id)
            if not record or record.instance_id != instance_id:
                raise RuntimeError("Collector lease lost before runtime flags were recorded")
            previous_deployment = record.deployment_id
            if deployment_id and previous_deployment != deployment_id:
                start_cause = "RAILWAY_REDEPLOY"
            elif previous_deployment == deployment_id and deployment_id:
                start_cause = "PROCESS_RESTART"
            else:
                start_cause = "RUNTIME_START"
            record.dry_run = dry_run
            record.live_trading_enabled = live_trading_enabled
            record.controlled_live_enabled = controlled_live_enabled
            record.manual_first_order_approved = manual_first_order_approved
            record.real_order_execution_enabled = bool(
                not dry_run
                and live_trading_enabled
                and controlled_live_enabled
                and manual_first_order_approved
            )
            record.deployment_id = deployment_id
            record.replica_id = replica_id
            record.last_start_cause = start_cause
            record.last_db_write_at = now
            record.updated_at = now

    def heartbeat(
        self,
        protocol_id: str,
        instance_id: str,
        status: str,
        now: datetime,
        lease_seconds: int,
        error: str | None = None,
    ) -> None:
        with self.session_factory.begin() as session:
            record = session.get(ShadowCollectorStateRecord, protocol_id)
            if not record or record.instance_id != instance_id:
                raise RuntimeError("Collector lease lost")
            record.status = status
            record.heartbeat_at = now
            record.last_db_write_at = now
            record.lease_expires_at = now + timedelta(seconds=lease_seconds)
            record.last_error = error
            record.updated_at = now

    def release_collector_lease(
        self, protocol_id: str, instance_id: str, now: datetime
    ) -> None:
        with self.session_factory.begin() as session:
            record = session.get(ShadowCollectorStateRecord, protocol_id)
            if record and record.instance_id == instance_id:
                record.status = "STOPPED"
                record.heartbeat_at = now
                record.last_db_write_at = now
                record.lease_expires_at = now
                record.updated_at = now

    def collector_state(self, protocol_id: str):
        with self.session_factory() as session:
            record = session.get(ShadowCollectorStateRecord, protocol_id)
            if record:
                session.expunge(record)
            return record

    def update_exchange_health(
        self,
        protocol_id: str,
        exchange: str,
        status: str,
        reason: str,
        checked_at: datetime,
        last_quote_at: datetime | None,
    ) -> tuple[str | None, str]:
        with self.session_factory.begin() as session:
            record = session.scalar(
                select(ShadowExchangeHealthRecord).where(
                    ShadowExchangeHealthRecord.protocol_id == protocol_id,
                    ShadowExchangeHealthRecord.exchange == exchange,
                )
            )
            previous = record.status if record else None
            if not record:
                record = ShadowExchangeHealthRecord(
                    protocol_id=protocol_id,
                    exchange=exchange,
                    status=status,
                    reason=reason,
                    consecutive_failures=0 if status == "HEALTHY" else 1,
                    last_success_at=checked_at if status == "HEALTHY" else None,
                    last_failure_at=checked_at if status != "HEALTHY" else None,
                    last_quote_at=last_quote_at,
                    checked_at=checked_at,
                    updated_at=checked_at,
                )
                session.add(record)
            else:
                record.status = status
                record.reason = reason
                record.consecutive_failures = (
                    0 if status == "HEALTHY" else record.consecutive_failures + 1
                )
                if status == "HEALTHY":
                    record.last_success_at = checked_at
                else:
                    record.last_failure_at = checked_at
                record.last_quote_at = last_quote_at or record.last_quote_at
                record.checked_at = checked_at
                record.updated_at = checked_at
            return previous, status

    def exchange_health(self, protocol_id: str) -> dict[str, object]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(ShadowExchangeHealthRecord).where(
                    ShadowExchangeHealthRecord.protocol_id == protocol_id
                )
            ).all()
            for row in rows:
                session.expunge(row)
        return {row.exchange: row for row in rows}

    def record_system_event(
        self,
        protocol_id: str,
        event_type: str,
        severity: str,
        message: str,
        *,
        exchange: str | None = None,
        details: dict | None = None,
        dedupe_since: datetime | None = None,
    ) -> tuple[int, bool]:
        with self.session_factory.begin() as session:
            if dedupe_since:
                existing = session.scalar(
                    select(ShadowSystemEventRecord.id)
                    .where(
                        ShadowSystemEventRecord.protocol_id == protocol_id,
                        ShadowSystemEventRecord.event_type == event_type,
                        ShadowSystemEventRecord.exchange == exchange,
                        ShadowSystemEventRecord.created_at >= dedupe_since,
                    )
                    .limit(1)
                )
                if existing:
                    return existing, False
            record = ShadowSystemEventRecord(
                protocol_id=protocol_id,
                event_type=event_type,
                exchange=exchange,
                severity=severity,
                message=message,
                details_json=json.dumps(details or {}, default=str, sort_keys=True),
            )
            session.add(record)
            session.flush()
            return record.id, True

    def mark_event_alerted(self, event_id: int, alerted_at: datetime) -> None:
        with self.session_factory.begin() as session:
            record = session.get(ShadowSystemEventRecord, event_id)
            if record:
                record.alerted_at = alerted_at

    def pending_trade_alerts(self, protocol_id: str):
        with self.session_factory() as session:
            rows = session.scalars(
                select(ShadowSystemEventRecord)
                .where(
                    ShadowSystemEventRecord.protocol_id == protocol_id,
                    ShadowSystemEventRecord.event_type.in_(
                        ("FIRST_SHADOW_TRADE", "SHADOW_POSITION_CLOSED")
                    ),
                    ShadowSystemEventRecord.alerted_at.is_(None),
                )
                .order_by(ShadowSystemEventRecord.created_at, ShadowSystemEventRecord.id)
            ).all()
            for row in rows:
                session.expunge(row)
            return rows

    def latest_quote_times(self, protocol_id: str) -> dict[str, datetime]:
        with self.session_factory() as session:
            rows = session.execute(
                select(ShadowQuoteRecord.exchange, func.max(ShadowQuoteRecord.received_at))
                .where(ShadowQuoteRecord.protocol_id == protocol_id)
                .group_by(ShadowQuoteRecord.exchange)
            ).all()
        return {exchange: _utc(timestamp) for exchange, timestamp in rows}


def shadow_metrics(trades) -> dict:
    pnls = [Decimal(trade.realized_pnl) for trade in trades]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    net = sum(pnls, Decimal())
    peak = equity = Decimal("1000")
    max_drawdown = Decimal()
    for value in pnls:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": Decimal(len(wins) * 100) / len(trades) if trades else Decimal(),
        "gross_pnl": sum((Decimal(trade.gross_pnl) for trade in trades), Decimal()),
        "fees": sum((Decimal(trade.entry_fee) + Decimal(trade.exit_fee) for trade in trades), Decimal()),
        "spread_cost": sum((Decimal(trade.entry_spread_cost) + Decimal(trade.exit_spread_cost) for trade in trades), Decimal()),
        "slippage": sum((Decimal(trade.entry_slippage_cost) + Decimal(trade.exit_slippage_cost) for trade in trades), Decimal()),
        "net_pnl": net,
        "net_pf": sum(wins, Decimal()) / abs(sum(losses, Decimal())) if losses else Decimal("Infinity") if wins else Decimal(),
        "expectancy": net / len(trades) if trades else Decimal(),
        "max_drawdown": max_drawdown,
    }
