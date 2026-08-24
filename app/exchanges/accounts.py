import json
from dataclasses import asdict, dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from app.core.security import SecretBox, mask_secret
from app.db import ExchangeAccountRecord, ExchangeHealthRecord, SessionLocal
from app.exchanges.base import ExchangeAdapter, ExchangeError
from app.exchanges.adapters import ADAPTER_TYPES
from app.exchanges.models import AccountStatus, HealthStatus, TradingPermissions


@dataclass(frozen=True)
class ExchangeCredentials:
    api_key: str
    secret: str
    passphrase: str | None = None


@dataclass(frozen=True)
class ExchangeAccountView:
    id: str
    user_id: int
    exchange: str
    account_name: str
    masked_api_key: str
    permissions: TradingPermissions
    status: AccountStatus
    sandbox: bool
    last_health_check: datetime | None


class UnsafeExchangePermissions(ExchangeError):
    pass


class ExchangeAccountManager:
    def __init__(self, encryption_key: str, session_factory: sessionmaker = SessionLocal) -> None:
        self.secret_box = SecretBox(encryption_key)
        self.session_factory = session_factory

    def add_account(self, user_id: int, exchange: str, account_name: str, credentials: ExchangeCredentials, sandbox: bool = True) -> ExchangeAccountView:
        if exchange.lower() not in ADAPTER_TYPES:
            raise ValueError(f"Unsupported exchange: {exchange}")
        if not credentials.api_key or not credentials.secret:
            raise ValueError("API key and secret are required")
        record = ExchangeAccountRecord(
            id=uuid4().hex,
            user_id=user_id,
            exchange=exchange.lower(),
            account_name=account_name.strip(),
            encrypted_api_key=self.secret_box.encrypt(credentials.api_key),
            encrypted_secret=self.secret_box.encrypt(credentials.secret),
            encrypted_passphrase=self.secret_box.encrypt(credentials.passphrase) if credentials.passphrase else None,
            permissions_json=json.dumps(asdict(TradingPermissions())),
            account_status=AccountStatus.DISCONNECTED,
            sandbox=int(sandbox),
        )
        with self.session_factory() as session:
            session.add(record)
            session.commit()
            session.refresh(record)
            return self._view(record)

    def list_accounts(self, user_id: int) -> list[ExchangeAccountView]:
        with self.session_factory() as session:
            records = session.query(ExchangeAccountRecord).filter_by(user_id=user_id).order_by(ExchangeAccountRecord.exchange, ExchangeAccountRecord.account_name).all()
            return [self._view(record) for record in records]

    def remove_account(self, user_id: int, account_id: str) -> None:
        with self.session_factory() as session:
            record = self._owned(session, user_id, account_id)
            session.delete(record)
            session.commit()

    def credentials_for(self, user_id: int, account_id: str) -> ExchangeCredentials:
        with self.session_factory() as session:
            record = self._owned(session, user_id, account_id)
            return ExchangeCredentials(
                self.secret_box.decrypt(record.encrypted_api_key),
                self.secret_box.decrypt(record.encrypted_secret),
                self.secret_box.decrypt(record.encrypted_passphrase) if record.encrypted_passphrase else None,
            )

    async def verify_account(self, user_id: int, account_id: str, adapter: ExchangeAdapter) -> ExchangeAccountView:
        permissions = await adapter.get_permissions()
        report = await adapter.health_check()
        status = AccountStatus.CONNECTED if report.status is HealthStatus.HEALTHY else AccountStatus.WARNING
        if permissions.withdrawal:
            status = AccountStatus.WARNING
        with self.session_factory() as session:
            record = self._owned(session, user_id, account_id)
            if record.exchange != adapter.name:
                raise ValueError("Adapter exchange does not match account")
            record.permissions_json = json.dumps(asdict(permissions))
            record.account_status = status
            record.last_health_check = report.checked_at
            session.add(ExchangeHealthRecord(account_id=record.id, status=report.status, latency_ms=report.latency_ms, details_json=json.dumps(asdict(report), default=str), checked_at=report.checked_at))
            session.commit()
            session.refresh(record)
            return self._view(record)

    def assert_autotrading_safe(self, user_id: int, account_id: str) -> None:
        with self.session_factory() as session:
            record = self._owned(session, user_id, account_id)
            permissions = TradingPermissions(**json.loads(record.permissions_json))
            if permissions.withdrawal:
                raise UnsafeExchangePermissions("Withdrawal permission detected; autotrading is forbidden")
            if not permissions.trade:
                raise UnsafeExchangePermissions("Trading permission is not enabled")
            if record.account_status != AccountStatus.CONNECTED:
                raise UnsafeExchangePermissions("Exchange account is not healthy and connected")
            if not record.sandbox:
                raise UnsafeExchangePermissions("Production exchange execution remains disabled")

    def _view(self, record: ExchangeAccountRecord) -> ExchangeAccountView:
        api_key = self.secret_box.decrypt(record.encrypted_api_key)
        return ExchangeAccountView(record.id, record.user_id, record.exchange, record.account_name, mask_secret(api_key), TradingPermissions(**json.loads(record.permissions_json)), AccountStatus(record.account_status), bool(record.sandbox), record.last_health_check)

    @staticmethod
    def _owned(session: Session, user_id: int, account_id: str) -> ExchangeAccountRecord:
        record = session.get(ExchangeAccountRecord, account_id)
        if record is None or record.user_id != user_id:
            raise KeyError(account_id)
        return record
