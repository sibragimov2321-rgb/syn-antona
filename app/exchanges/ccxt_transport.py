from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.backtest.core import Candle
from app.exchanges.base import ExchangeError
from app.exchanges.models import (
    ExchangeBalance,
    ExchangeOrder,
    ExchangePosition,
    ExchangeTrade,
    InstrumentRules,
    OrderBook,
    OrderRequest,
    Ticker,
    TradingPermissions,
)


def _decimal(value: Any, default: str = "0") -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal(default)


def _timestamp(value: Any) -> datetime:
    milliseconds = int(value) if value is not None else int(datetime.now(UTC).timestamp() * 1000)
    return datetime.fromtimestamp(milliseconds / 1000, UTC)


class CcxtTransport:
    """Concrete async transport; production writes remain blocked by ExchangeAdapter."""

    EXCHANGE_IDS = {"gate.io": "gate", "gateio": "gate"}

    def __init__(self, exchange: str, api_key: str | None = None, secret: str | None = None, passphrase: str | None = None, sandbox: bool = False, permissions: TradingPermissions | None = None) -> None:
        try:
            import ccxt.async_support as ccxt
        except ImportError as error:
            raise RuntimeError('CCXT is required: install the project with `python -m pip install -e .`') from error
        exchange_id = self.EXCHANGE_IDS.get(exchange.lower(), exchange.lower())
        try:
            exchange_type = getattr(ccxt, exchange_id)
        except AttributeError as error:
            raise ValueError(f"CCXT does not support exchange {exchange}") from error
        options = {"defaultType": "swap"}
        configuration = {"enableRateLimit": False, "options": options}
        if api_key:
            configuration["apiKey"] = api_key
        if secret:
            configuration["secret"] = secret
        if passphrase:
            configuration["password"] = passphrase
        self.client = exchange_type(configuration)
        self.exchange = exchange.lower().replace("gate.io", "gateio")
        self.exchange_id = exchange_id
        self.sandbox = sandbox
        self.permissions = permissions or TradingPermissions()
        self.websocket_connected = False
        if sandbox:
            self.client.set_sandbox_mode(True)

    async def connect(self) -> None:
        try:
            await self.client.load_markets()
        except Exception as error:
            raise ExchangeError(f"{self.exchange} connection failed: {error}") from error

    async def close(self) -> None:
        await self.client.close()

    async def call(self, operation: str, **parameters: Any) -> Any:
        handler = getattr(self, f"_{operation}", None)
        if handler is None:
            raise ExchangeError(f"CCXT transport does not implement {operation}")
        try:
            return await handler(**parameters)
        except ExchangeError:
            raise
        except Exception as error:
            raise ExchangeError(f"{self.exchange} {operation} failed: {error}") from error

    async def _get_server_time(self) -> datetime:
        value = await self.client.fetch_time()
        return _timestamp(value)

    async def _get_balance(self) -> ExchangeBalance:
        value = await self.client.fetch_balance()
        total = value.get("total", {}).get("USDT")
        free = value.get("free", {}).get("USDT")
        return ExchangeBalance(_decimal(total), _decimal(free), "USDT")

    async def _get_symbols(self, market_type=None) -> list[str]:
        await self.client.load_markets()
        return [market["id"] for market in self.client.markets.values() if market.get("active", True)]

    async def _get_ticker(self, symbol: str, market_type=None) -> Ticker:
        unified = self._unified_symbol(symbol)
        value = await self.client.fetch_ticker(unified)
        return Ticker(self.exchange, self._internal_symbol(unified), _decimal(value.get("bid"), str(value.get("last", 0))), _decimal(value.get("ask"), str(value.get("last", 0))), _decimal(value.get("last")), _timestamp(value.get("timestamp")))

    async def _get_orderbook(self, symbol: str, market_type=None, depth: int = 20) -> OrderBook:
        unified = self._unified_symbol(symbol)
        value = await self.client.fetch_order_book(unified, depth)
        bids = tuple((_decimal(price), _decimal(size)) for price, size, *_ in value.get("bids", []))
        asks = tuple((_decimal(price), _decimal(size)) for price, size, *_ in value.get("asks", []))
        return OrderBook(self.exchange, self._internal_symbol(unified), bids, asks, _timestamp(value.get("timestamp")))

    async def _get_ohlcv(self, symbol: str, timeframe: str, start: datetime | None = None, end: datetime | None = None, limit: int = 200, market_type=None) -> list[Candle]:
        unified = self._unified_symbol(symbol)
        since = int(start.timestamp() * 1000) if start else None
        end_ms = int(end.timestamp() * 1000) if end else None
        rows: list[list] = []
        while True:
            page = await self.client.fetch_ohlcv(unified, timeframe, since=since, limit=min(limit, 1000))
            if not page:
                break
            rows.extend(page)
            next_since = int(page[-1][0]) + 1
            if since is None or next_since <= since or len(rows) >= limit or (end_ms is not None and next_since >= end_ms):
                break
            since = next_since
        selected = [row for row in rows if end_ms is None or int(row[0]) < end_ms][:limit]
        return [Candle(_timestamp(row[0]), _decimal(row[1]), _decimal(row[2]), _decimal(row[3]), _decimal(row[4]), _decimal(row[5])) for row in selected]

    async def _get_funding_rate(self, symbol: str) -> Decimal:
        value = await self.client.fetch_funding_rate(self._unified_symbol(symbol))
        return _decimal(value.get("fundingRate"))

    async def _get_open_interest(self, symbol: str) -> Decimal:
        value = await self.client.fetch_open_interest(self._unified_symbol(symbol))
        return _decimal(value.get("openInterestAmount") or value.get("openInterestValue"))

    async def _get_positions(self) -> list[ExchangePosition]:
        values = await self.client.fetch_positions()
        return [self._position(value) for value in values if _decimal(value.get("contracts")) != 0]

    async def _get_open_orders(self, symbol: str | None = None) -> list[ExchangeOrder]:
        values = await self.client.fetch_open_orders(self._unified_symbol(symbol) if symbol else None)
        return [self._order(value) for value in values]

    async def _create_order(self, request: OrderRequest, exchange_symbol: str) -> ExchangeOrder:
        symbol = self._unified_symbol(exchange_symbol)
        parameters = {"reduceOnly": request.reduce_only}
        if request.client_order_id:
            parameters["clientOrderId"] = request.client_order_id
        value = await self.client.create_order(symbol, request.order_type.value.lower(), request.side.value.lower(), float(request.quantity), float(request.price) if request.price is not None else None, parameters)
        return self._order(value, request.account_id)

    async def _cancel_order(self, order_id: str, symbol: str) -> ExchangeOrder:
        return self._order(await self.client.cancel_order(order_id, self._unified_symbol(symbol)))

    async def _cancel_all_orders(self, symbol: str | None = None) -> list[ExchangeOrder]:
        values = await self.client.cancel_all_orders(self._unified_symbol(symbol) if symbol else None)
        return [self._order(value) for value in values]

    async def _close_position(self, position_id: str, symbol: str) -> ExchangeOrder:
        position = await self._find_position(position_id, symbol)
        side = "sell" if position.side.upper() == "LONG" else "buy"
        value = await self.client.create_order(self._unified_symbol(symbol), "market", side, float(position.quantity), None, {"reduceOnly": True})
        return self._order(value, position.account_id)

    async def _set_leverage(self, symbol: str, leverage: Decimal) -> None:
        await self.client.set_leverage(float(leverage), self._unified_symbol(symbol))

    async def _set_stop_loss(self, position_id: str, symbol: str, price: Decimal) -> None:
        await self._protect(position_id, symbol, price, "stopLossPrice")

    async def _set_take_profit(self, position_id: str, symbol: str, price: Decimal) -> None:
        await self._protect(position_id, symbol, price, "takeProfitPrice")

    async def _get_order_status(self, order_id: str, symbol: str) -> ExchangeOrder:
        return self._order(await self.client.fetch_order(order_id, self._unified_symbol(symbol)))

    async def _get_trade_history(self, symbol: str | None = None, limit: int = 100) -> list[ExchangeTrade]:
        values = await self.client.fetch_my_trades(self._unified_symbol(symbol) if symbol else None, limit=limit)
        return [ExchangeTrade(self.exchange, str(value.get("account") or "default"), str(value.get("id")), self._internal_symbol(value.get("symbol", "")), str(value.get("side", "")).upper(), _decimal(value.get("amount")), _decimal(value.get("price")), _decimal((value.get("fee") or {}).get("cost")), _timestamp(value.get("timestamp"))) for value in values]

    async def _get_instrument_rules(self, symbol: str, market_type=None) -> InstrumentRules:
        market = self.client.market(self._unified_symbol(symbol))
        limits = market.get("limits") or {}
        amount = limits.get("amount") or {}
        cost = limits.get("cost") or {}
        leverage = limits.get("leverage") or {}
        precision = market.get("precision") or {}
        return InstrumentRules(
            self._step(precision.get("price")),
            self._step(precision.get("amount")),
            _decimal(amount.get("min"), "0"),
            _decimal(cost.get("min"), "0"),
            _decimal(amount.get("max")) if amount.get("max") is not None else None,
            _decimal(leverage.get("max"), "1"),
        )

    async def _get_permissions(self) -> TradingPermissions:
        return self.permissions

    async def _protect(self, position_id: str, symbol: str, price: Decimal, parameter: str) -> None:
        position = await self._find_position(position_id, symbol)
        side = "sell" if position.side.upper() == "LONG" else "buy"
        await self.client.create_order(self._unified_symbol(symbol), "market", side, float(position.quantity), None, {parameter: float(price), "reduceOnly": True})

    async def _find_position(self, position_id: str, symbol: str) -> ExchangePosition:
        for position in await self._get_positions():
            if position.symbol == self._internal_symbol(self._unified_symbol(symbol)) and (position.position_id == position_id or not position_id):
                return position
        raise ExchangeError("Position not found on its original exchange")

    def _unified_symbol(self, symbol: str) -> str:
        if symbol in self.client.markets:
            return symbol
        matches = self.client.markets_by_id.get(symbol)
        if matches:
            market = matches[0] if isinstance(matches, list) else matches
            return market["symbol"]
        return symbol

    @staticmethod
    def _internal_symbol(symbol: str) -> str:
        return symbol.split(":")[0].replace("XBT", "BTC")

    @staticmethod
    def _step(value: Any) -> Decimal:
        if value is None:
            return Decimal("0.00000001")
        numeric = _decimal(value)
        return numeric if numeric > 0 else Decimal("0.00000001")

    def _order(self, value: dict, account_id: str = "default") -> ExchangeOrder:
        client_order_id = value.get("clientOrderId") or (value.get("info") or {}).get("orderLinkId")
        return ExchangeOrder(self.exchange, account_id, str(value.get("id")), self._internal_symbol(value.get("symbol", "")), str(value.get("status", "unknown")).upper(), _decimal(value.get("amount")), _decimal(value.get("filled")), _decimal(value.get("average")) if value.get("average") is not None else None, str(client_order_id) if client_order_id else None)

    def _position(self, value: dict) -> ExchangePosition:
        return ExchangePosition(self.exchange, str(value.get("account") or "default"), str(value.get("id") or value.get("symbol")), self._internal_symbol(value.get("symbol", "")), str(value.get("side", "")).upper(), _decimal(value.get("contracts")), _decimal(value.get("entryPrice")), _decimal(value.get("leverage"), "1"), _decimal(value.get("unrealizedPnl")), str((value.get("info") or {}).get("strategyVersion", "unknown")))


def create_ccxt_transport(exchange: str, **configuration: Any) -> CcxtTransport:
    return CcxtTransport(exchange, **configuration)
