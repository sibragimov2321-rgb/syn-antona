from datetime import UTC, datetime
from decimal import Decimal

from app.ai.models import MarketContext, Trend
from app.domain.models import Signal
from app.signals.engine import MarketFrame


class MarketContextBuilder:
    """Compacts validated market facts; it deliberately excludes credentials and user data."""
    def build(self, symbol: str, frames: dict[str, MarketFrame], technical: Signal, open_positions: int, spread: Decimal = Decimal()) -> MarketContext:
        entry = frames["5M"]
        indicator = entry.indicators
        def trend(frame: MarketFrame) -> Trend:
            i = frame.indicators
            return Trend.BULLISH if i.ema_9 > i.ema_21 > i.ema_50 else Trend.BEARISH if i.ema_9 < i.ema_21 < i.ema_50 else Trend.NEUTRAL
        return MarketContext(symbol=symbol, current_price=entry.price, timestamp=datetime.now(UTC).isoformat(), trends={name: trend(frame) for name, frame in frames.items()}, rsi=indicator.rsi_14, macd=indicator.macd, ema_9=indicator.ema_9, ema_21=indicator.ema_21, ema_50=indicator.ema_50, atr=indicator.atr_14, volatility=indicator.atr_14/entry.price, spread=spread, technical_decision=technical.decision, technical_score=technical.signal_score, open_positions=open_positions, risk_summary="Risk Manager approval is mandatory")
