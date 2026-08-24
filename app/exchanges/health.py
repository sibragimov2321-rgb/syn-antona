from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from app.exchanges.base import ExchangeAdapter
from app.exchanges.models import HealthReport, HealthStatus


class ExchangeHealthMonitor:
    def __init__(self, stale_after_seconds: Decimal = Decimal("30"), max_order_errors: int = 3) -> None:
        self.stale_after_seconds = stale_after_seconds
        self.max_order_errors = max_order_errors
        self._last_market_data: dict[str, datetime] = {}
        self._order_errors: dict[str, int] = {}
        self._reports: dict[str, HealthReport] = {}

    def market_data_received(self, exchange: str, timestamp: datetime | None = None) -> None:
        self._last_market_data[exchange] = timestamp or datetime.now(UTC)

    def order_error(self, exchange: str) -> None:
        self._order_errors[exchange] = self._order_errors.get(exchange, 0) + 1

    def order_success(self, exchange: str) -> None:
        self._order_errors[exchange] = 0

    async def check(self, adapter: ExchangeAdapter) -> HealthReport:
        report = await adapter.health_check()
        now = datetime.now(UTC)
        last_data = self._last_market_data.get(adapter.name)
        age = Decimal(str((now - last_data).total_seconds())) if last_data else None
        reasons = list(report.reasons)
        status = report.status
        if age is not None and age > self.stale_after_seconds:
            reasons.append("market data is stale")
            status = HealthStatus.DEGRADED if status is HealthStatus.HEALTHY else status
        errors = self._order_errors.get(adapter.name, 0)
        if errors >= self.max_order_errors:
            reasons.append("order error threshold exceeded")
            status = HealthStatus.UNAVAILABLE
        if report.rate_limited and status is HealthStatus.HEALTHY:
            reasons.append("exchange is rate limited")
            status = HealthStatus.DEGRADED
        updated = replace(report, status=status, market_data_age_seconds=age, order_errors=errors, reasons=tuple(reasons))
        self._reports[adapter.name] = updated
        return updated

    def latest(self, exchange: str) -> HealthReport | None:
        return self._reports.get(exchange)

    def can_open_new_position(self, exchange: str) -> bool:
        report = self.latest(exchange)
        return bool(report and report.status is not HealthStatus.UNAVAILABLE)
