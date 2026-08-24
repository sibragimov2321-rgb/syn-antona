from dataclasses import dataclass
from decimal import Decimal

from app.domain.models import Decision, Signal
from app.market.indicators import IndicatorSnapshot


@dataclass(frozen=True)
class MarketFrame:
    timeframe: str
    price: Decimal
    indicators: IndicatorSnapshot


class SignalEngine:
    """Rules-only signal engine. It never executes an order or accesses exchange credentials."""

    entry_timeframe = "5M"
    required_timeframes = ("4H", "1H", "15M", "5M")

    def analyze(self, symbol: str, frame: MarketFrame) -> Signal:
        indicator = frame.indicators
        if not self._complete(indicator):
            return self._wait(symbol, frame.timeframe, "Insufficient indicator history")

        direction = self._trend_direction(indicator)
        trend_score = self._trend_score(indicator, direction)
        momentum_score = self._momentum_score(indicator, direction)
        volatility_pct = indicator.atr_14 / frame.price
        volatility_score = max(0, min(100, int((Decimal("0.04") - volatility_pct) * 2500)))
        reasons = self._reasons(indicator, direction, volatility_pct)
        score = int(trend_score * Decimal("0.55") + momentum_score * Decimal("0.35") + volatility_score * Decimal("0.10"))

        if direction is None or volatility_pct > Decimal("0.04") or score < 70:
            return Signal(
                symbol, frame.timeframe, Decision.WAIT, score, trend_score, momentum_score,
                volatility_score, tuple(reasons),
            )
        stop_distance = indicator.atr_14 * Decimal("1.5")
        target_distance = stop_distance * Decimal("2")
        stop = frame.price - stop_distance if direction is Decision.LONG else frame.price + stop_distance
        target = frame.price + target_distance if direction is Decision.LONG else frame.price - target_distance
        return Signal(
            symbol, frame.timeframe, direction, score, trend_score, momentum_score,
            volatility_score, tuple(reasons), frame.price, stop, target, Decimal("2"),
        )

    def confirm_multi_timeframe(self, symbol: str, frames: dict[str, MarketFrame]) -> Signal:
        missing = set(self.required_timeframes) - set(frames)
        if missing:
            return self._wait(symbol, self.entry_timeframe, f"Missing timeframes: {', '.join(sorted(missing))}")
        signals = {name: self.analyze(symbol, frames[name]) for name in self.required_timeframes}
        actionable = [signal for signal in signals.values() if signal.decision is not Decision.WAIT]
        decisions = {signal.decision for signal in actionable}
        entry = signals[self.entry_timeframe]
        if len(actionable) < 3 or len(decisions) != 1 or entry.decision is Decision.WAIT:
            return self._wait(symbol, self.entry_timeframe, "Timeframes are contradictory or lack confirmation")
        average_score = sum(signal.signal_score for signal in signals.values()) // len(signals)
        if average_score < 70:
            return self._wait(symbol, self.entry_timeframe, "Multi-timeframe Signal Score below threshold")
        return Signal(
            symbol, self.entry_timeframe, entry.decision, average_score,
            sum(item.trend_score for item in signals.values()) // len(signals),
            sum(item.momentum_score for item in signals.values()) // len(signals),
            sum(item.volatility_score for item in signals.values()) // len(signals),
            ("4H trend, 1H structure, 15M setup and 5M entry agree", *entry.reasons),
            entry.proposed_entry, entry.proposed_stop_loss, entry.proposed_take_profit,
            entry.risk_reward_ratio,
        )

    @staticmethod
    def _complete(indicator: IndicatorSnapshot) -> bool:
        return all((indicator.rsi_14, indicator.ema_9, indicator.ema_21, indicator.ema_50, indicator.atr_14))

    @staticmethod
    def _trend_direction(indicator: IndicatorSnapshot) -> Decision | None:
        if indicator.ema_9 > indicator.ema_21 > indicator.ema_50:
            return Decision.LONG
        if indicator.ema_9 < indicator.ema_21 < indicator.ema_50:
            return Decision.SHORT
        return None

    @staticmethod
    def _trend_score(indicator: IndicatorSnapshot, direction: Decision | None) -> int:
        if direction is None:
            return 20
        separation = abs(indicator.ema_9 - indicator.ema_50) / indicator.ema_50
        return min(100, 70 + int(separation * 1000))

    @staticmethod
    def _momentum_score(indicator: IndicatorSnapshot, direction: Decision | None) -> int:
        if direction is Decision.LONG:
            return 85 if Decimal("52") <= indicator.rsi_14 <= Decimal("72") and indicator.macd > 0 else 45
        if direction is Decision.SHORT:
            return 85 if Decimal("28") <= indicator.rsi_14 <= Decimal("48") and indicator.macd < 0 else 45
        return 20

    @staticmethod
    def _reasons(indicator: IndicatorSnapshot, direction: Decision | None, volatility: Decimal) -> list[str]:
        if direction is None:
            return ["EMA trend is not aligned"]
        label = "bullish" if direction is Decision.LONG else "bearish"
        return [f"EMA 9/21/50 {label} alignment", f"RSI: {indicator.rsi_14:.1f}", f"ATR: {volatility:.2%}"]

    @staticmethod
    def _wait(symbol: str, timeframe: str, reason: str) -> Signal:
        return Signal(symbol, timeframe, Decision.WAIT, 0, 0, 0, 0, (reason,))
