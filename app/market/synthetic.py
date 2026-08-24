from decimal import Decimal

from app.market.indicators import IndicatorSnapshot
from app.signals.engine import MarketFrame


class SyntheticDemoData:
    """Explicitly synthetic data source for safely demonstrating DEMO lifecycle behaviour."""

    def __init__(self) -> None:
        self._ticks: dict[str, int] = {}

    async def frames(self, symbol: str) -> dict[str, MarketFrame]:
        tick = self._ticks.get(symbol, 0) + 1
        self._ticks[symbol] = tick
        price = Decimal("100") + Decimal(tick)
        snapshot = IndicatorSnapshot(
            rsi_14=Decimal("60"), ema_9=price + Decimal("10"), ema_21=price + Decimal("5"),
            ema_50=price, macd=Decimal("2"), macd_signal=Decimal("1"), atr_14=Decimal("1"),
            bollinger_upper=price + Decimal("3"), bollinger_lower=price - Decimal("3"),
        )
        return {timeframe: MarketFrame(timeframe, price, snapshot) for timeframe in ("4H", "1H", "15M", "5M")}
