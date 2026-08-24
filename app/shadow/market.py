import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import perf_counter

import ccxt

from app.backtest.historical import (
    BinanceHistoricalDataProvider,
    BybitHistoricalDataProvider,
    CCXTHistoricalDataProvider,
)
from app.backtest.persistence import CachedHistoricalDataProvider, HistoricalCandleCache
from app.core.config import get_settings
from app.shadow.models import InstrumentConstraints, LiveMarketSnapshot


def _decimal(value, default: str = "0") -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal(default)


def _precision_step(value) -> Decimal:
    if value is None:
        return Decimal("0.00000001")
    parsed = _decimal(value)
    if parsed == 0:
        return Decimal("1")
    return Decimal(1).scaleb(-int(parsed)) if parsed >= 1 else parsed


class PublicLiveMarketData:
    """Public-only market data gateway. It exposes no order method or credentials."""

    def __init__(self, exchanges: tuple[str, ...]) -> None:
        settings = get_settings()
        self.stale_after = timedelta(seconds=settings.shadow_quote_stale_seconds)
        self.live_candle_grace = timedelta(
            seconds=settings.shadow_live_candle_grace_seconds
        )
        self.clients = {
            name: getattr(ccxt, name)(
                {
                    "enableRateLimit": True,
                    "timeout": settings.shadow_api_timeout_ms,
                    "options": {"defaultType": "spot"},
                }
            )
            for name in exchanges
        }
        self.historical = {
            "binance": BinanceHistoricalDataProvider(),
            "bybit": BybitHistoricalDataProvider(),
            "okx": CCXTHistoricalDataProvider("okx"),
            "bitget": CCXTHistoricalDataProvider("bitget"),
        }

    async def health(self) -> dict[str, dict]:
        async def check(name: str, client) -> tuple[str, dict]:
            started = perf_counter()
            try:
                await asyncio.to_thread(client.load_markets)
                server_ms = await asyncio.to_thread(client.fetch_time)
                server_time = datetime.fromtimestamp(server_ms / 1000, UTC)
                return name, {
                    "status": "HEALTHY",
                    "latency_ms": (perf_counter() - started) * 1000,
                    "exchange_timestamp": server_time,
                    "local_receive_timestamp": datetime.now(UTC),
                }
            except Exception as error:
                return name, {
                    "status": "OFFLINE",
                    "latency_ms": (perf_counter() - started) * 1000,
                    "reason": f"{type(error).__name__}: {error}",
                    "local_receive_timestamp": datetime.now(UTC),
                }

        return dict(await asyncio.gather(*(check(name, client) for name, client in self.clients.items())))

    async def warmup(self, exchange: str, symbol: str, start: datetime, end: datetime):
        provider = CachedHistoricalDataProvider(
            self.historical[exchange], HistoricalCandleCache()
        )
        return await provider.fetch(symbol, "1h", start, end)

    async def closed_candles(
        self, exchange: str, symbol: str, start: datetime, end: datetime
    ):
        now = datetime.now(UTC)
        if start < end - timedelta(hours=1) or now - end > self.live_candle_grace:
            raise RuntimeError(
                "RECOVERED_AFTER_DOWNTIME required; historical candles cannot use a "
                "current live quote"
            )
        return await self.historical[exchange].fetch(symbol, "1h", start, end)

    async def recovery_candles(
        self, exchange: str, symbol: str, start: datetime, end: datetime
    ):
        """Historical OHLCV only. Callers must mark every row as recovered."""
        return await self.historical[exchange].fetch(symbol, "1h", start, end)

    async def snapshot(self, exchange: str, symbol: str) -> LiveMarketSnapshot:
        client = self.clients[exchange]
        ticker = await asyncio.to_thread(client.fetch_ticker, symbol)
        orderbook = await asyncio.to_thread(client.fetch_order_book, symbol, 10)
        bids = tuple(
            (_decimal(row[0]), _decimal(row[1])) for row in orderbook.get("bids", [])[:10]
        )
        asks = tuple(
            (_decimal(row[0]), _decimal(row[1])) for row in orderbook.get("asks", [])[:10]
        )
        if not bids or not asks:
            raise RuntimeError(f"{exchange} {symbol} returned an empty order book")
        bid, ask = bids[0][0], asks[0][0]
        if min(bid, ask) <= 0 or bid >= ask:
            raise RuntimeError(f"{exchange} {symbol} returned an invalid bid/ask")
        timestamp_values = [
            value
            for value in (ticker.get("timestamp"), orderbook.get("timestamp"))
            if value is not None
        ]
        if not timestamp_values:
            raise RuntimeError(f"STALE DATA: {exchange} {symbol} has no exchange timestamp")
        timestamp_ms = max(timestamp_values)
        exchange_timestamp = datetime.fromtimestamp(timestamp_ms / 1000, UTC)
        received_at = datetime.now(UTC)
        age = received_at - exchange_timestamp
        if age > self.stale_after:
            raise RuntimeError(
                f"STALE DATA: {exchange} {symbol} quote age {age.total_seconds():.1f}s"
            )
        market = client.market(symbol)
        limits = market.get("limits", {})
        precision = market.get("precision", {})
        return LiveMarketSnapshot(
            exchange=exchange,
            symbol=symbol,
            bid=bid,
            ask=ask,
            last=_decimal(ticker.get("last"), str((bid + ask) / 2)),
            bids=bids,
            asks=asks,
            exchange_timestamp=exchange_timestamp,
            received_at=received_at,
            constraints=InstrumentConstraints(
                quantity_step=_precision_step(precision.get("amount")),
                minimum_quantity=_decimal(limits.get("amount", {}).get("min")),
                minimum_notional=_decimal(limits.get("cost", {}).get("min")),
                price_step=_precision_step(precision.get("price")),
            ),
        )

    async def close(self) -> None:
        for provider in self.historical.values():
            close = getattr(provider, "close", None)
            if close:
                await close()
        for client in self.clients.values():
            await asyncio.to_thread(client.close)
