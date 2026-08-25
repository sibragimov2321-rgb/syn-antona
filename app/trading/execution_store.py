"""Durable order-attempt ledger used before any private exchange mutation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.exc import IntegrityError

from app.db import ExecutionOrderRecord
from app.exchanges.models import ExchangeOrder, OrderRequest


class DuplicateOrderError(PermissionError):
    pass


class ClientOrderIdCollision(PermissionError):
    pass


class OrderOutcomeUnknown(RuntimeError):
    """The venue may have accepted the order; automatic retry is forbidden."""


@dataclass(frozen=True)
class OrderAttempt:
    exchange: str
    account_id: str
    client_order_id: str
    request_hash: str
    status: str
    exchange_order_id: str | None = None


class ExecutionOrderStore(Protocol):
    persistent: bool

    def claim(self, exchange: str, request: OrderRequest) -> OrderAttempt: ...

    def mark_order(self, exchange: str, request: OrderRequest, order: ExchangeOrder) -> None: ...

    def mark_unknown(self, exchange: str, request: OrderRequest, error_code: str) -> None: ...


def request_hash(request: OrderRequest) -> str:
    payload = {
        "account_id": request.account_id,
        "client_order_id": request.client_order_id,
        "leverage": str(request.leverage),
        "market_type": request.market_type.value,
        "order_type": request.order_type.value,
        "price": str(request.price) if request.price is not None else None,
        "quantity": str(request.quantity),
        "reduce_only": request.reduce_only,
        "side": request.side.value,
        "symbol": request.symbol,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class InMemoryExecutionOrderStore:
    persistent = False

    def __init__(self) -> None:
        self._attempts: dict[tuple[str, str, str], OrderAttempt] = {}

    def claim(self, exchange: str, request: OrderRequest) -> OrderAttempt:
        key, digest = self._key(exchange, request), request_hash(request)
        existing = self._attempts.get(key)
        if existing:
            _raise_existing(existing, digest)
        attempt = OrderAttempt(exchange, request.account_id, key[2], digest, "PENDING")
        self._attempts[key] = attempt
        return attempt

    def mark_order(self, exchange: str, request: OrderRequest, order: ExchangeOrder) -> None:
        key = self._key(exchange, request)
        prior = self._attempts[key]
        self._attempts[key] = OrderAttempt(
            prior.exchange,
            prior.account_id,
            prior.client_order_id,
            prior.request_hash,
            "SUBMITTED",
            order.order_id,
        )

    def mark_unknown(self, exchange: str, request: OrderRequest, error_code: str) -> None:
        key = self._key(exchange, request)
        prior = self._attempts[key]
        self._attempts[key] = OrderAttempt(
            prior.exchange,
            prior.account_id,
            prior.client_order_id,
            prior.request_hash,
            "UNKNOWN",
        )

    @staticmethod
    def _key(exchange: str, request: OrderRequest) -> tuple[str, str, str]:
        if not request.client_order_id:
            raise PermissionError("A deterministic client_order_id is mandatory")
        return exchange, request.account_id, request.client_order_id


class SqlExecutionOrderStore:
    persistent = True

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def claim(self, exchange: str, request: OrderRequest) -> OrderAttempt:
        client_order_id = _required_client_order_id(request)
        digest = request_hash(request)
        with self.session_factory() as session:
            existing = self._find(session, exchange, request.account_id, client_order_id)
            if existing:
                _raise_existing(_attempt(existing), digest)
            record = ExecutionOrderRecord(
                exchange=exchange,
                account_id=request.account_id,
                client_order_id=client_order_id,
                symbol=request.symbol,
                side=request.side.value,
                quantity=request.quantity,
                request_hash=digest,
                status="PENDING",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            session.add(record)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = self._find(session, exchange, request.account_id, client_order_id)
                if existing:
                    _raise_existing(_attempt(existing), digest)
                raise
            return _attempt(record)

    def mark_order(self, exchange: str, request: OrderRequest, order: ExchangeOrder) -> None:
        self._update(
            exchange,
            request,
            status="SUBMITTED",
            exchange_order_id=order.order_id,
            exchange_status=order.status,
            error_code=None,
        )

    def mark_unknown(self, exchange: str, request: OrderRequest, error_code: str) -> None:
        self._update(exchange, request, status="UNKNOWN", error_code=error_code[:64])

    def _update(self, exchange: str, request: OrderRequest, **values) -> None:
        client_order_id = _required_client_order_id(request)
        with self.session_factory() as session:
            record = self._find(session, exchange, request.account_id, client_order_id)
            if record is None:
                raise RuntimeError("Execution ledger claim is missing")
            for key, value in values.items():
                setattr(record, key, value)
            record.updated_at = datetime.now(UTC)
            session.commit()

    @staticmethod
    def _find(session, exchange: str, account_id: str, client_order_id: str):
        return session.query(ExecutionOrderRecord).filter_by(
            exchange=exchange,
            account_id=account_id,
            client_order_id=client_order_id,
        ).one_or_none()


def _required_client_order_id(request: OrderRequest) -> str:
    if not request.client_order_id:
        raise PermissionError("A deterministic client_order_id is mandatory")
    return request.client_order_id


def _attempt(record: ExecutionOrderRecord) -> OrderAttempt:
    return OrderAttempt(
        record.exchange,
        record.account_id,
        record.client_order_id,
        record.request_hash,
        record.status,
        record.exchange_order_id,
    )


def _raise_existing(existing: OrderAttempt, digest: str) -> None:
    if existing.request_hash != digest:
        raise ClientOrderIdCollision("client_order_id was reused with a different order payload")
    if existing.status == "UNKNOWN":
        raise OrderOutcomeUnknown(
            "Previous order outcome is unknown; reconcile with the exchange before any retry"
        )
    raise DuplicateOrderError("Duplicate client_order_id")
