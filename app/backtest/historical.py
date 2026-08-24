import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import ccxt
import httpx

from app.backtest.core import Candle, validate_candles

INTERVALS = {"5m": "5", "15m": "15", "1h": "60", "4h": "240", "1d": "D"}
TIMEFRAME_MILLISECONDS = {
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


class HistoricalDataError(RuntimeError): pass


class PublicHistoricalProvider:
    name = "public"
    async def fetch(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[Candle]: raise NotImplementedError
    async def _get(self, url: str, params: dict) -> dict:
        for attempt in range(4):
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    response = await client.get(url, params=params)
                if response.status_code == 429: raise HistoricalDataError("rate limited")
                response.raise_for_status(); return response.json()
            except (httpx.HTTPError, HistoricalDataError) as error:
                if attempt == 3: raise HistoricalDataError("public historical data request failed") from error
                await asyncio.sleep(.25 * 2**attempt)
        raise HistoricalDataError("unreachable")


class BybitHistoricalDataProvider(PublicHistoricalProvider):
    name = "bybit"
    async def fetch(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[Candle]:
        if timeframe not in INTERVALS: raise ValueError("unsupported timeframe")
        start_ms, cursor_end = int(start.timestamp()*1000), int(end.timestamp()*1000); rows=[]
        while start_ms < cursor_end:
            data=await self._get("https://api.bybit.com/v5/market/kline",{"category":"spot","symbol":symbol.replace("/", ""),"interval":INTERVALS[timeframe],"start":start_ms,"end":cursor_end,"limit":1000})
            page=data.get("result",{}).get("list",[])
            if not page: break
            rows.extend(page); oldest=min(int(row[0]) for row in page)
            if oldest <= start_ms: break
            cursor_end=oldest-1
            if len(page)<1000: break
        candles=[Candle(datetime.fromtimestamp(int(r[0])/1000,UTC),Decimal(r[1]),Decimal(r[2]),Decimal(r[3]),Decimal(r[4]),Decimal(r[5])) for r in rows]
        return _dedupe(candles)


class BinanceHistoricalDataProvider(PublicHistoricalProvider):
    name = "binance"
    async def fetch(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[Candle]:
        if timeframe not in INTERVALS: raise ValueError("unsupported timeframe")
        interval=timeframe; cursor=int(start.timestamp()*1000); end_ms=int(end.timestamp()*1000); rows=[]
        while cursor < end_ms:
            data=await self._get("https://api.binance.com/api/v3/klines",{"symbol":symbol.replace("/", ""),"interval":interval,"startTime":cursor,"endTime":end_ms,"limit":1000})
            if not data: break
            rows.extend(data); cursor=int(data[-1][0])+1
            if len(data)<1000: break
        return _dedupe([Candle(datetime.fromtimestamp(int(r[0])/1000,UTC),Decimal(r[1]),Decimal(r[2]),Decimal(r[3]),Decimal(r[4]),Decimal(r[5])) for r in rows])


class CCXTHistoricalDataProvider(PublicHistoricalProvider):
    """Read-only spot OHLCV provider for venues without a dedicated downloader.

    CCXT's built-in rate limiter and symbol mapping are used only for public
    market data. No credentials are accepted and no mutating method is exposed.
    """

    supported = {"okx", "bitget"}

    def __init__(self, exchange: str, client=None) -> None:
        if exchange not in self.supported:
            raise ValueError(f"unsupported CCXT historical exchange: {exchange}")
        self.name = exchange
        self.page_limit = 200 if exchange == "bitget" else 1000
        exchange_type = getattr(ccxt, exchange)
        self.client = client or exchange_type(
            {"enableRateLimit": True, "options": {"defaultType": "spot"}}
        )

    async def fetch(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[Candle]:
        if timeframe not in INTERVALS:
            raise ValueError("unsupported timeframe")
        cursor = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        rows = []
        try:
            while cursor < end_ms:
                page = await asyncio.to_thread(
                    self.client.fetch_ohlcv,
                    symbol,
                    timeframe,
                    cursor,
                    self.page_limit,
                )
                if not page:
                    break
                accepted = [row for row in page if cursor <= int(row[0]) < end_ms]
                rows.extend(accepted)
                next_cursor = int(page[-1][0]) + TIMEFRAME_MILLISECONDS[timeframe]
                if next_cursor <= cursor:
                    break
                cursor = next_cursor
        except ccxt.BaseError as error:
            raise HistoricalDataError(
                f"{self.name} public historical data request failed for {symbol}"
            ) from error
        return _dedupe(
            [
                Candle(
                    datetime.fromtimestamp(int(row[0]) / 1000, UTC),
                    Decimal(str(row[1])),
                    Decimal(str(row[2])),
                    Decimal(str(row[3])),
                    Decimal(str(row[4])),
                    Decimal(str(row[5])),
                )
                for row in rows
            ]
        )

    async def close(self) -> None:
        close = getattr(self.client, "close", None)
        if close:
            await asyncio.to_thread(close)


def _dedupe(candles: list[Candle]) -> list[Candle]:
    ordered=sorted({c.timestamp:c for c in candles}.values(),key=lambda c:c.timestamp)
    if errors:=validate_candles(ordered): raise HistoricalDataError("; ".join(errors))
    return ordered
