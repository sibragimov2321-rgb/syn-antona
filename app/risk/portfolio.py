from dataclasses import dataclass
from decimal import Decimal

from app.exchanges.models import HealthStatus
from app.exchanges.portfolio import ManagedPosition


@dataclass(frozen=True)
class ExchangeRiskLimits:
    max_exposure_pct: Decimal = Decimal("0.25")
    max_positions: int = 2
    max_daily_loss_pct: Decimal = Decimal("0.02")


@dataclass(frozen=True)
class GlobalRiskLimits:
    max_total_exposure_pct: Decimal = Decimal("0.50")
    max_total_leverage: Decimal = Decimal("2")
    max_open_risk_pct: Decimal = Decimal("0.02")
    max_daily_loss_pct: Decimal = Decimal("0.02")
    max_correlated_exposure_pct: Decimal = Decimal("0.30")


@dataclass(frozen=True)
class ProposedExposure:
    exchange: str
    account_id: str
    symbol: str
    side: str
    notional: Decimal
    leverage: Decimal
    open_risk: Decimal


@dataclass(frozen=True)
class PortfolioRiskDecision:
    approved: bool
    reason: str


class GlobalPortfolioRiskManager:
    CORRELATION_GROUPS = {
        "BTC": "CRYPTO_MAJOR",
        "ETH": "CRYPTO_MAJOR",
    }

    def approve(self, proposal: ProposedExposure, positions: tuple[ManagedPosition, ...], equity_by_account: dict[tuple[str, str], Decimal], daily_pnl_by_account: dict[tuple[str, str], Decimal], health: HealthStatus, exchange_limits: ExchangeRiskLimits | None = None, global_limits: GlobalRiskLimits | None = None) -> PortfolioRiskDecision:
        exchange_limits = exchange_limits or ExchangeRiskLimits()
        global_limits = global_limits or GlobalRiskLimits()
        if health is HealthStatus.UNAVAILABLE:
            return PortfolioRiskDecision(False, "Exchange is UNAVAILABLE")
        account_key = (proposal.exchange, proposal.account_id)
        account_equity = equity_by_account.get(account_key, Decimal())
        total_equity = sum(equity_by_account.values(), Decimal())
        if account_equity <= 0 or total_equity <= 0:
            return PortfolioRiskDecision(False, "Equity must be positive")
        account_positions = [position for position in positions if (position.exchange, position.account_id) == account_key]
        account_exposure = sum((position.notional for position in account_positions), Decimal()) + proposal.notional
        if len(account_positions) >= exchange_limits.max_positions:
            return PortfolioRiskDecision(False, "Exchange maximum positions reached")
        if account_exposure > account_equity * exchange_limits.max_exposure_pct:
            return PortfolioRiskDecision(False, "Exchange exposure limit exceeded")
        if daily_pnl_by_account.get(account_key, Decimal()) <= -(account_equity * exchange_limits.max_daily_loss_pct):
            return PortfolioRiskDecision(False, "Exchange daily loss limit reached")
        global_daily_pnl = sum(daily_pnl_by_account.values(), Decimal())
        if global_daily_pnl <= -(total_equity * global_limits.max_daily_loss_pct):
            return PortfolioRiskDecision(False, "Global daily loss limit reached")
        total_exposure = sum((position.notional for position in positions), Decimal()) + proposal.notional
        if total_exposure > total_equity * global_limits.max_total_exposure_pct:
            return PortfolioRiskDecision(False, "Global portfolio exposure limit exceeded")
        leveraged_exposure = sum((position.notional * position.leverage for position in positions), Decimal()) + proposal.notional * proposal.leverage
        if leveraged_exposure / total_equity > global_limits.max_total_leverage:
            return PortfolioRiskDecision(False, "Global leverage limit exceeded")
        open_risk = sum((position.open_risk for position in positions), Decimal()) + proposal.open_risk
        if open_risk > total_equity * global_limits.max_open_risk_pct:
            return PortfolioRiskDecision(False, "Global open risk limit exceeded")
        if self._correlated_exposure(proposal, positions) > total_equity * global_limits.max_correlated_exposure_pct:
            return PortfolioRiskDecision(False, "Correlated exposure limit exceeded")
        return PortfolioRiskDecision(True, "ALLOW")

    def _correlated_exposure(self, proposal: ProposedExposure, positions: tuple[ManagedPosition, ...]) -> Decimal:
        group = self.CORRELATION_GROUPS.get(proposal.symbol.split("/")[0], proposal.symbol)
        exposure = proposal.notional
        for position in positions:
            position_group = self.CORRELATION_GROUPS.get(position.symbol.split("/")[0], position.symbol)
            if position_group == group:
                exposure += position.notional
        return exposure
