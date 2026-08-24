from datetime import datetime

from app.backtest.core import Candle, validate_candles
from app.backtest.historical import HistoricalDataError
from app.backtest.persistence import TIMEFRAME_SECONDS
from app.exchanges.base import ExchangeAdapter
from app.exchanges.models import MarketType


class AdapterHistoricalDataProvider:
    """Makes any registered adapter available to the existing historical cache/orchestrator."""

    def __init__(self, adapter: ExchangeAdapter, market_type: MarketType = MarketType.PERPETUAL) -> None:
        self.adapter = adapter
        self.market_type = market_type
        self.name = adapter.name

    async def fetch(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[Candle]:
        candles: list[Candle] = []
        cursor = start
        while cursor < end:
            page = await self.adapter.get_ohlcv(symbol, timeframe, cursor, end, limit=1000, market_type=self.market_type)
            if not page:
                break
            candles.extend(page)
            next_cursor = page[-1].timestamp.timestamp() + TIMEFRAME_SECONDS[timeframe]
            cursor = datetime.fromtimestamp(next_cursor, page[-1].timestamp.tzinfo)
            if len(page) < 1000:
                break
        candles = sorted({candle.timestamp: candle for candle in candles if start <= candle.timestamp < end}.values(), key=lambda candle: candle.timestamp)
        if errors := validate_candles(candles):
            raise HistoricalDataError("; ".join(errors))
        return candles
