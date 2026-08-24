from decimal import ROUND_DOWN, Decimal

from app.domain.models import RiskDecision, RiskProfile, Side, TradeIntent


class RiskManager:
    """The mandatory and deterministic authorization point for every trade."""

    def approve(self, intent: TradeIntent, profile: RiskProfile) -> RiskDecision:
        if intent.equity <= 0 or intent.entry <= 0:
            return RiskDecision(False, "Invalid equity or entry price")
        if intent.open_positions >= profile.max_concurrent_positions:
            return RiskDecision(False, "Maximum simultaneous positions reached")
        if intent.requested_leverage > profile.max_leverage:
            return RiskDecision(False, "Requested leverage exceeds configured maximum")
        if intent.consecutive_losses >= profile.max_consecutive_losses:
            return RiskDecision(False, "Cooldown required after consecutive losses")
        if intent.cooldown_active:
            return RiskDecision(False, "Cooldown after losses is active")
        if intent.volatility_pct > profile.max_volatility_pct:
            return RiskDecision(False, "Volatility filter rejected trade")
        if intent.spread_pct > profile.max_spread_pct:
            return RiskDecision(False, "Spread filter rejected trade")
        if intent.daily_realized_pnl <= -(intent.equity * profile.max_daily_loss_pct):
            return RiskDecision(False, "Daily loss limit reached; bot must be paused")

        per_unit_risk = self._per_unit_risk(intent)
        if per_unit_risk <= 0:
            return RiskDecision(False, "Stop loss is invalid for trade direction")
        reward = self._per_unit_reward(intent)
        if reward / per_unit_risk < profile.min_risk_reward:
            return RiskDecision(False, "Minimum risk/reward requirement is not met")

        risk_amount = (intent.equity * profile.risk_per_trade_pct).quantize(Decimal("0.01"))
        risk_quantity = risk_amount / per_unit_risk
        notional_quantity = profile.max_position_notional / intent.entry
        leverage_quantity = (intent.equity * intent.requested_leverage) / intent.entry
        balance_quantity = (
            (intent.available_balance * intent.requested_leverage) / intent.entry
            if intent.available_balance is not None
            else leverage_quantity
        )
        quantity = min(risk_quantity, notional_quantity, leverage_quantity, balance_quantity)
        quantity = quantity.quantize(Decimal("0.000001"), ROUND_DOWN)
        if quantity <= 0:
            return RiskDecision(False, "Calculated position size is zero")
        return RiskDecision(True, "Approved", quantity, risk_amount)

    @staticmethod
    def _per_unit_risk(intent: TradeIntent) -> Decimal:
        return (
            intent.entry - intent.stop_loss
            if intent.side is Side.LONG
            else intent.stop_loss - intent.entry
        )

    @staticmethod
    def _per_unit_reward(intent: TradeIntent) -> Decimal:
        return (
            intent.take_profit - intent.entry
            if intent.side is Side.LONG
            else intent.entry - intent.take_profit
        )
