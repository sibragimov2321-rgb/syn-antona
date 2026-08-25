"""Fail-closed Bybit V5 gateway for the one controlled SOLUSDT validation order.

The default is DRY_RUN.  Only the explicit mutation allowlist in this module can
issue POST requests, and every POST is preceded by the production safety guard.
No strategy or AI component imports this gateway.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db import (
    ControlledLiveProposalRecord,
    ControlledLiveStateRecord,
    ExecutionOrderRecord,
)
from app.exchanges.bybit_readonly import permission_summary
from app.trading.controlled_live import (
    ArmingGates,
    CONTROLLED_LIVE_V1,
    CONTROLLED_LIVE_V1_FIRST_INSTRUMENT,
    ControlledLiveBlocked,
    CurrentInstrumentState,
    LiveFill,
    LiveGatewaySnapshot,
    LivePositionSnapshot,
    ManualExecutionPreview,
)
from app.trading.execution_store import (
    DuplicateOrderError,
    OrderOutcomeUnknown,
    OrderRejected,
)


MAINNET_BASE_URL = "https://api.bytick.com"
RECV_WINDOW_MS = 5_000
APPROVAL_TTL = timedelta(minutes=10)
ALLOWED_SYMBOL = "SOLUSDT"
ALLOWED_QUANTITY = Decimal("0.1")
MAX_NOTIONAL = Decimal("10")
MUTATING_PATHS = frozenset(
    {
        "/v5/order/create",
        "/v5/order/cancel",
        "/v5/position/set-leverage",
        "/v5/position/trading-stop",
    }
)


class BybitGatewayError(RuntimeError):
    pass


class BybitOrderRejected(OrderRejected):
    pass


class DryRunBlocked(ControlledLiveBlocked):
    pass


@dataclass(frozen=True)
class SignedMutation:
    method: str
    path: str
    payload: dict[str, Any]
    timestamp_ms: int
    recv_window_ms: int
    signature_valid: bool

    def safe_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "path": self.path,
            "payload": self.payload,
            "timestamp_ms": self.timestamp_ms,
            "recv_window_ms": self.recv_window_ms,
            "signature_valid": self.signature_valid,
            "sent": False,
        }


@dataclass(frozen=True)
class GuardSnapshot:
    ask_price: Decimal
    open_positions: int
    equity: Decimal
    daily_realized_pnl: Decimal


class MutationAuthorizer(Protocol):
    async def authorize(
        self,
        *,
        action: str,
        symbol: str,
        quantity: Decimal,
        client_order_id: str,
        risk_reducing: bool = False,
    ) -> GuardSnapshot: ...


class BybitV5Http:
    """Signed V5 transport. Mutation methods remain private to the gateway."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        base_url: str = MAINNET_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 12.0,
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("BYBIT_API_KEY and BYBIT_API_SECRET must be set")
        if base_url.rstrip("/") not in {
            "https://api.bybit.com",
            "https://api.bytick.com",
        }:
            raise ValueError("Only official Bybit Mainnet endpoints are allowed")
        self._api_key = api_key
        self._api_secret = api_secret
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            transport=transport,
            timeout=timeout_seconds,
            headers={"User-Agent": "syn-antona-controlled-live-v1/1.0"},
        )

    def __repr__(self) -> str:
        return "BybitV5Http(api_key=***, api_secret=***)"

    async def close(self) -> None:
        await self._http.aclose()

    async def public_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        return await self._request("GET", path, params=params)

    async def private_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        clean = _clean(params)
        query = urlencode(sorted(clean.items()))
        timestamp = int(time.time() * 1000)
        signature = self._sign(f"{timestamp}{self._api_key}{RECV_WINDOW_MS}{query}")
        return await self._request(
            "GET",
            path,
            params=clean,
            headers=self._headers(timestamp, signature),
        )

    def sign_post(self, path: str, payload: dict[str, Any]) -> tuple[SignedMutation, str, dict[str, str]]:
        if path not in MUTATING_PATHS:
            raise ControlledLiveBlocked("Mutation endpoint is not allowlisted")
        timestamp = int(time.time() * 1000)
        body = _json_body(payload)
        signature = self._sign(f"{timestamp}{self._api_key}{RECV_WINDOW_MS}{body}")
        validation = hmac.compare_digest(
            signature,
            hmac.new(
                self._api_secret.encode(),
                f"{timestamp}{self._api_key}{RECV_WINDOW_MS}{body}".encode(),
                hashlib.sha256,
            ).hexdigest(),
        )
        return (
            SignedMutation("POST", path, payload, timestamp, RECV_WINDOW_MS, validation),
            body,
            self._headers(timestamp, signature) | {"Content-Type": "application/json"},
        )

    async def post_signed(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        _, body, headers = self.sign_post(path, payload)
        try:
            return await self._request("POST", path, content=body, headers=headers)
        except httpx.TimeoutException as error:
            raise OrderOutcomeUnknown(
                "Bybit submission timed out; reconcile by orderLinkId before any retry"
            ) from error

    def _sign(self, value: str) -> str:
        return hmac.new(self._api_secret.encode(), value.encode(), hashlib.sha256).hexdigest()

    def _headers(self, timestamp: int, signature: str) -> dict[str, str]:
        return {
            "X-BAPI-API-KEY": self._api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-TIMESTAMP": str(timestamp),
            "X-BAPI-RECV-WINDOW": str(RECV_WINDOW_MS),
        }

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = await self._http.request(method, path, **kwargs)
        response.raise_for_status()
        payload = response.json()
        if payload.get("retCode") != 0:
            message = str(payload.get("retMsg") or "Bybit rejected request")
            message = message.replace(self._api_key, "***").replace(self._api_secret, "***")
            error = BybitOrderRejected if method == "POST" else BybitGatewayError
            raise error(f"{method} {path} rejected: code={payload.get('retCode')} {message[:120]}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise BybitGatewayError(f"{method} {path} returned an invalid result")
        return result


class ProductionMutationGuard:
    """Re-checks all independent controls before every private POST."""

    def __init__(self, session_factory, http: BybitV5Http) -> None:
        self._sessions = session_factory
        self._http = http

    async def authorize(
        self,
        *,
        action: str,
        symbol: str,
        quantity: Decimal,
        client_order_id: str,
        risk_reducing: bool = False,
    ) -> GuardSnapshot:
        gates = ArmingGates.from_environment()
        gates.require_all(expected_symbol=ALLOWED_SYMBOL)
        if os.getenv("DRY_RUN", "true").strip().lower() != "false" and action != "DRY_RUN":
            raise DryRunBlocked("DRY_RUN=true blocks mutating HTTP before transport")
        if symbol != ALLOWED_SYMBOL:
            raise ControlledLiveBlocked("Only SOLUSDT is allowed")
        if not risk_reducing and quantity != ALLOWED_QUANTITY:
            raise ControlledLiveBlocked("First controlled order quantity must equal 0.1 SOL")
        if risk_reducing and (quantity <= 0 or quantity > ALLOWED_QUANTITY):
            raise ControlledLiveBlocked("Risk-reducing quantity is outside the controlled position")

        proposal, state, attempts_today = self._persistent_checks(client_order_id)
        if state.kill_switch_active and not risk_reducing:
            raise ControlledLiveBlocked("Emergency kill switch is active")
        # The durable claim for the current request already exists by the time
        # the gateway is reached, so allow that row to be the fourth attempt.
        if attempts_today > CONTROLLED_LIVE_V1.max_trades_per_day and not risk_reducing:
            raise ControlledLiveBlocked("Maximum trades per UTC day reached")
        if proposal.admin_telegram_id not in get_settings().admin_telegram_ids:
            raise ControlledLiveBlocked("Persistent approval does not belong to an active admin")

        permissions = permission_summary(await self._http.private_get("/v5/user/query-api", {}))
        if permissions["read"] != "YES" or permissions["trade"] != "YES":
            raise ControlledLiveBlocked("Bybit API Read and Trade permissions are required")
        if permissions["withdraw"] != "NO" or permissions["transfer"] != "NO":
            raise ControlledLiveBlocked("Withdraw and Transfer permissions must be disabled")

        instrument, ask = await self._instrument_and_ask(symbol)
        lot = instrument.get("lotSizeFilter") or {}
        minimum_quantity = _decimal(lot.get("minOrderQty"))
        step = _decimal(lot.get("qtyStep"))
        minimum_notional = _decimal(lot.get("minNotionalValue"))
        if instrument.get("status") != "Trading" or instrument.get("contractType") != "LinearPerpetual":
            raise ControlledLiveBlocked("SOLUSDT LinearPerpetual is not Trading")
        if step <= 0 or quantity % step or quantity < minimum_quantity:
            raise ControlledLiveBlocked("Quantity violates current Bybit instrument limits")
        notional = quantity * ask
        if notional < minimum_notional or notional > MAX_NOTIONAL:
            raise ControlledLiveBlocked("Current order notional is outside $5-$10")

        positions = await self._http.private_get(
            "/v5/position/list", {"category": "linear", "symbol": symbol}
        )
        open_positions = sum(_decimal(item.get("size")) > 0 for item in positions.get("list") or [])
        allowed_positions = 1 if risk_reducing or action == "PROTECTION" else 0
        if open_positions > allowed_positions:
            raise ControlledLiveBlocked("Maximum open positions reached")

        wallet = await self._http.private_get(
            "/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": "USDT"}
        )
        accounts = wallet.get("list") or []
        equity = _decimal(accounts[0].get("totalEquity")) if accounts else Decimal()
        executions = await self._http.private_get(
            "/v5/execution/list",
            {
                "category": "linear",
                "symbol": symbol,
                "startTime": _utc_day_start_ms(),
                "limit": 100,
            },
        )
        daily_pnl = sum(
            (
                _decimal(item.get("execPnl")) - abs(_decimal(item.get("execFee")))
                for item in executions.get("list") or []
            ),
            Decimal(),
        )
        if not risk_reducing:
            if equity <= 0:
                raise ControlledLiveBlocked("Account equity must be positive")
            if daily_pnl <= -(equity * CONTROLLED_LIVE_V1.daily_loss_limit_pct):
                raise ControlledLiveBlocked("Daily loss limit reached")
        return GuardSnapshot(ask, open_positions, equity, daily_pnl)

    def _persistent_checks(
        self, client_order_id: str
    ) -> tuple[ControlledLiveProposalRecord, ControlledLiveStateRecord, int]:
        with self._sessions() as session:
            proposal = session.scalar(
                select(ControlledLiveProposalRecord).where(
                    ControlledLiveProposalRecord.client_order_id == client_order_id
                )
            )
            state = session.get(ControlledLiveStateRecord, CONTROLLED_LIVE_V1.name)
            if proposal is None or state is None:
                raise ControlledLiveBlocked("Persistent admin approval/state is missing")
            approved_at = _aware(proposal.approved_at)
            if approved_at is None or datetime.now(UTC) - approved_at > APPROVAL_TTL:
                raise ControlledLiveBlocked("Admin approval is missing or expired")
            if proposal.status not in {
                "APPROVED",
                "SUBMITTING",
                "FILLED_UNPROTECTED",
                "PROTECTED",
                "UNKNOWN",
            }:
                raise ControlledLiveBlocked("Proposal is not in an executable state")
            if proposal.profile_hash != CONTROLLED_LIVE_V1.config_hash:
                raise ControlledLiveBlocked("Controlled-live profile hash mismatch")
            if proposal.selection_hash != CONTROLLED_LIVE_V1_FIRST_INSTRUMENT.selection_hash:
                raise ControlledLiveBlocked("Controlled-live instrument hash mismatch")
            start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            attempts_today = session.scalar(
                select(func.count(ExecutionOrderRecord.id)).where(
                    ExecutionOrderRecord.exchange == "bybit",
                    ExecutionOrderRecord.created_at >= start,
                    ExecutionOrderRecord.client_order_id.not_like("%-close%"),
                )
            ) or 0
            session.expunge(proposal)
            session.expunge(state)
            return proposal, state, int(attempts_today)

    async def _instrument_and_ask(self, symbol: str) -> tuple[dict[str, Any], Decimal]:
        instrument_result = await self._http.public_get(
            "/v5/market/instruments-info", {"category": "linear", "symbol": symbol}
        )
        ticker_result = await self._http.public_get(
            "/v5/market/tickers", {"category": "linear", "symbol": symbol}
        )
        instruments = instrument_result.get("list") or []
        tickers = ticker_result.get("list") or []
        if not instruments or not tickers:
            raise ControlledLiveBlocked("Fresh SOLUSDT quote/instrument data is unavailable")
        ask = _decimal(tickers[0].get("ask1Price") or tickers[0].get("lastPrice"))
        if ask <= 0:
            raise ControlledLiveBlocked("Fresh SOLUSDT ask is unavailable")
        return instruments[0], ask


class BybitV5OrderGateway:
    """ControlledLiveGateway implementation backed by signed Bybit V5 HTTP."""

    def __init__(
        self,
        http: BybitV5Http,
        authorizer: MutationAuthorizer,
        *,
        dry_run: bool = True,
        order_poll_attempts: int = 3,
    ) -> None:
        self._http = http
        self._authorizer = authorizer
        self.dry_run = dry_run
        self._order_poll_attempts = order_poll_attempts
        self.last_dry_run: SignedMutation | None = None
        self._order_context: dict[str, str] = {}

    @classmethod
    def from_environment(cls, session_factory) -> BybitV5OrderGateway:
        http = BybitV5Http(
            os.getenv("BYBIT_API_KEY", ""),
            os.getenv("BYBIT_API_SECRET", ""),
        )
        return cls(
            http,
            ProductionMutationGuard(session_factory, http),
            dry_run=os.getenv("DRY_RUN", "true").strip().lower() != "false",
        )

    async def close(self) -> None:
        await self._http.close()

    async def current_instrument_state(self, symbol: str) -> CurrentInstrumentState:
        self._require_symbol(symbol)
        instruments = await self._http.public_get(
            "/v5/market/instruments-info", {"category": "linear", "symbol": symbol}
        )
        tickers = await self._http.public_get(
            "/v5/market/tickers", {"category": "linear", "symbol": symbol}
        )
        instrument = (instruments.get("list") or [None])[0]
        ticker = (tickers.get("list") or [None])[0]
        if not instrument or not ticker:
            raise BybitGatewayError("SOLUSDT instrument/ticker is unavailable")
        lot = instrument.get("lotSizeFilter") or {}
        return CurrentInstrumentState(
            symbol,
            str(instrument.get("status")),
            _decimal(ticker.get("ask1Price") or ticker.get("lastPrice")),
            _decimal(lot.get("minOrderQty")),
            _decimal(lot.get("qtyStep")),
            _decimal(lot.get("minNotionalValue")),
        )

    async def dry_run_market_request(
        self, preview: ManualExecutionPreview, client_order_id: str
    ) -> dict[str, Any]:
        await self._authorizer.authorize(
            action="DRY_RUN",
            symbol=preview.symbol,
            quantity=preview.quantity,
            client_order_id=client_order_id,
        )
        mutation, _, _ = self._http.sign_post(
            "/v5/order/create", self._market_payload(preview, client_order_id)
        )
        self.last_dry_run = mutation
        return mutation.safe_dict()

    async def submit_market(
        self, preview: ManualExecutionPreview, client_order_id: str
    ) -> LiveFill:
        self._validate_preview(preview, client_order_id)
        existing = await self.query_order(client_order_id=client_order_id)
        if existing is not None:
            raise DuplicateOrderError("Bybit already has this deterministic client order ID")
        if self.dry_run:
            await self.dry_run_market_request(preview, client_order_id)
            raise DryRunBlocked("DRY_RUN signed the request but did not send it")
        await self.set_leverage(preview.symbol, Decimal("1"), client_order_id)
        result = await self._mutate(
            "CREATE",
            "/v5/order/create",
            self._market_payload(preview, client_order_id),
            symbol=preview.symbol,
            quantity=preview.quantity,
            client_order_id=client_order_id,
        )
        order_id = str(result.get("orderId") or "")
        if not order_id:
            raise OrderOutcomeUnknown("Bybit create response omitted orderId; reconciliation required")
        self._order_context[order_id] = client_order_id
        order = None
        for _ in range(self._order_poll_attempts):
            order = await self.query_order(order_id=order_id, client_order_id=client_order_id)
            if order and str(order.get("orderStatus")) not in {"New", "Created", "Untriggered"}:
                break
            await asyncio.sleep(0.25)
        if order is None:
            raise OrderOutcomeUnknown("Order is not visible yet; reconciliation required")
        status = str(order.get("orderStatus") or "")
        if status in {"Rejected", "Deactivated"}:
            raise BybitOrderRejected(f"Bybit order status is {status}")
        fills = await self.query_executions(order_id=order_id, client_order_id=client_order_id)
        filled_quantity = _decimal(order.get("cumExecQty")) or sum(
            (_decimal(item.get("execQty")) for item in fills), Decimal()
        )
        if filled_quantity <= 0:
            raise OrderOutcomeUnknown("No fill confirmed; reconciliation required")
        value = sum(
            (_decimal(item.get("execQty")) * _decimal(item.get("execPrice")) for item in fills),
            Decimal(),
        )
        average_price = (
            value / filled_quantity if value > 0 else _decimal(order.get("avgPrice"))
        )
        fee = sum((abs(_decimal(item.get("execFee"))) for item in fills), Decimal())
        position_idx = str(order.get("positionIdx") or "0")
        return LiveFill(
            order_id,
            f"{preview.symbol}:{position_idx}",
            filled_quantity,
            average_price,
            fee,
        )

    async def query_order(
        self,
        *,
        order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not order_id and not client_order_id:
            raise ValueError("order_id or client_order_id is required")
        params = {"category": "linear", "symbol": ALLOWED_SYMBOL}
        if order_id:
            params["orderId"] = order_id
        if client_order_id:
            params["orderLinkId"] = client_order_id
        for path in ("/v5/order/realtime", "/v5/order/history"):
            result = await self._http.private_get(path, params)
            items = result.get("list") or []
            if items:
                return items[0]
        return None

    async def query_executions(
        self,
        *,
        order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> list[dict[str, Any]]:
        params = {"category": "linear", "symbol": ALLOWED_SYMBOL, "limit": 100}
        if order_id:
            params["orderId"] = order_id
        if client_order_id:
            params["orderLinkId"] = client_order_id
        result = await self._http.private_get("/v5/execution/list", params)
        return list(result.get("list") or [])

    async def cancel_order(
        self,
        *,
        order_id: str,
        client_order_id: str,
        symbol: str = ALLOWED_SYMBOL,
        risk_reducing: bool = False,
    ) -> dict[str, Any]:
        return await self._mutate(
            "CANCEL",
            "/v5/order/cancel",
            {
                "category": "linear",
                "symbol": symbol,
                "orderId": order_id,
                "orderLinkId": client_order_id,
            },
            symbol=symbol,
            quantity=ALLOWED_QUANTITY,
            client_order_id=client_order_id,
            risk_reducing=risk_reducing,
        )

    async def read_position(self, symbol: str = ALLOWED_SYMBOL) -> dict[str, Any] | None:
        self._require_symbol(symbol)
        result = await self._http.private_get(
            "/v5/position/list", {"category": "linear", "symbol": symbol}
        )
        return next(
            (item for item in result.get("list") or [] if _decimal(item.get("size")) > 0),
            None,
        )

    async def set_leverage(
        self, symbol: str, leverage: Decimal, client_order_id: str
    ) -> None:
        if leverage != Decimal("1"):
            raise ControlledLiveBlocked("Only 1x leverage is allowed")
        await self._mutate(
            "SET_LEVERAGE",
            "/v5/position/set-leverage",
            {
                "category": "linear",
                "symbol": symbol,
                "buyLeverage": "1",
                "sellLeverage": "1",
            },
            symbol=symbol,
            quantity=ALLOWED_QUANTITY,
            client_order_id=client_order_id,
        )

    async def set_tp_sl(
        self,
        *,
        symbol: str,
        stop_loss: Decimal,
        take_profit: Decimal,
        client_order_id: str,
        reduce_only: bool,
    ) -> None:
        if not reduce_only or stop_loss <= 0 or take_profit <= 0:
            raise ControlledLiveBlocked("Full-position reduce-only TP/SL is required")
        await self._mutate(
            "PROTECTION",
            "/v5/position/trading-stop",
            {
                "category": "linear",
                "symbol": symbol,
                "tpslMode": "Full",
                "positionIdx": 0,
                "takeProfit": _number(take_profit),
                "stopLoss": _number(stop_loss),
                "tpOrderType": "Market",
                "slOrderType": "Market",
            },
            symbol=symbol,
            quantity=ALLOWED_QUANTITY,
            client_order_id=client_order_id,
            risk_reducing=True,
        )

    async def install_native_protection(
        self,
        fill: LiveFill,
        *,
        symbol: str,
        stop_loss: Decimal,
        take_profit: Decimal,
        reduce_only: bool,
    ) -> None:
        client_order_id = await self._client_id_for_order(fill.order_id)
        await self.set_tp_sl(
            symbol=symbol,
            stop_loss=stop_loss,
            take_profit=take_profit,
            client_order_id=client_order_id,
            reduce_only=reduce_only,
        )

    async def reduce_only_close(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        client_order_id: str,
    ) -> dict[str, Any]:
        position = await self.read_position(symbol)
        if position is None:
            return {"alreadyClosed": True}
        side = "Sell" if str(position.get("side")) == "Buy" else "Buy"
        close_link = _derived_link_id(client_order_id, "close")
        return await self._mutate(
            "REDUCE_ONLY_CLOSE",
            "/v5/order/create",
            {
                "category": "linear",
                "symbol": symbol,
                "side": side,
                "orderType": "Market",
                "qty": _number(quantity),
                "timeInForce": "IOC",
                "positionIdx": int(position.get("positionIdx") or 0),
                "reduceOnly": True,
                "closeOnTrigger": True,
                "orderLinkId": close_link,
            },
            symbol=symbol,
            quantity=quantity,
            client_order_id=client_order_id,
            risk_reducing=True,
        )

    async def emergency_close_reduce_only(self, fill: LiveFill, symbol: str) -> None:
        client_order_id = await self._client_id_for_order(fill.order_id)
        await self.reduce_only_close(
            symbol=symbol,
            quantity=fill.filled_quantity,
            client_order_id=client_order_id,
        )

    async def cancel_pending_orders(self, symbol: str) -> int:
        self._require_symbol(symbol)
        result = await self._http.private_get(
            "/v5/order/realtime",
            {"category": "linear", "symbol": symbol, "openOnly": 0, "limit": 50},
        )
        cancelled = 0
        for item in result.get("list") or []:
            order_id = str(item.get("orderId") or "")
            client_id = str(item.get("orderLinkId") or "")
            if not order_id or not client_id:
                continue
            await self.cancel_order(
                order_id=order_id,
                client_order_id=client_id,
                symbol=symbol,
                risk_reducing=True,
            )
            cancelled += 1
        return cancelled

    async def reconcile_client_order_id(self, client_order_id: str) -> dict[str, Any]:
        order = await self.query_order(client_order_id=client_order_id)
        fills = await self.query_executions(client_order_id=client_order_id)
        position = await self.read_position(ALLOWED_SYMBOL)
        return {
            "status": "MATCH" if order or fills else "NOT_FOUND",
            "order": order,
            "fills": fills,
            "position": position,
            "safe_to_retry": False,
        }

    async def snapshot(self) -> LiveGatewaySnapshot:
        positions_result = await self._http.private_get(
            "/v5/position/list", {"category": "linear", "symbol": ALLOWED_SYMBOL}
        )
        orders_result = await self._http.private_get(
            "/v5/order/realtime",
            {"category": "linear", "symbol": ALLOWED_SYMBOL, "openOnly": 0, "limit": 50},
        )
        fills_result = await self._http.private_get(
            "/v5/execution/list", {"category": "linear", "symbol": ALLOWED_SYMBOL, "limit": 100}
        )
        positions = tuple(
            LivePositionSnapshot(
                f"{ALLOWED_SYMBOL}:{item.get('positionIdx') or 0}",
                ALLOWED_SYMBOL,
                _decimal(item.get("size")),
            )
            for item in positions_result.get("list") or []
            if _decimal(item.get("size")) > 0
        )
        return LiveGatewaySnapshot(
            positions,
            frozenset(
                str(item.get("orderId"))
                for item in orders_result.get("list") or []
                if item.get("orderId")
            ),
            frozenset(
                str(item.get("orderId"))
                for item in fills_result.get("list") or []
                if item.get("orderId")
            ),
        )

    async def _mutate(
        self,
        action: str,
        path: str,
        payload: dict[str, Any],
        *,
        symbol: str,
        quantity: Decimal,
        client_order_id: str,
        risk_reducing: bool = False,
    ) -> dict[str, Any]:
        await self._authorizer.authorize(
            action=action,
            symbol=symbol,
            quantity=quantity,
            client_order_id=client_order_id,
            risk_reducing=risk_reducing,
        )
        mutation, _, _ = self._http.sign_post(path, payload)
        if self.dry_run:
            self.last_dry_run = mutation
            raise DryRunBlocked("DRY_RUN signed the request but did not send it")
        return await self._http.post_signed(path, payload)

    async def _client_id_for_order(self, order_id: str) -> str:
        if order_id in self._order_context:
            return self._order_context[order_id]
        order = await self.query_order(order_id=order_id)
        client_id = str((order or {}).get("orderLinkId") or "")
        if not client_id:
            raise ControlledLiveBlocked("Cannot resolve approved client order ID")
        self._order_context[order_id] = client_id
        return client_id

    @staticmethod
    def _market_payload(
        preview: ManualExecutionPreview, client_order_id: str
    ) -> dict[str, Any]:
        return {
            "category": "linear",
            "symbol": ALLOWED_SYMBOL,
            "side": "Buy" if preview.side == "BUY" else "Sell",
            "orderType": "Market",
            "qty": "0.1",
            "timeInForce": "IOC",
            "positionIdx": 0,
            "reduceOnly": False,
            "closeOnTrigger": False,
            "orderLinkId": client_order_id,
        }

    @staticmethod
    def _validate_preview(preview: ManualExecutionPreview, client_order_id: str) -> None:
        if (
            preview.symbol != ALLOWED_SYMBOL
            or preview.quantity != ALLOWED_QUANTITY
            or preview.leverage != Decimal("1")
            or preview.expected_notional > MAX_NOTIONAL
            or preview.side not in {"BUY", "SELL"}
            or not preview.executable
        ):
            raise ControlledLiveBlocked("Preview violates CONTROLLED_LIVE_V1 production limits")
        if client_order_id != preview.client_order_id or len(client_order_id) > 36:
            raise ControlledLiveBlocked("Invalid deterministic Bybit client order ID")

    @staticmethod
    def _require_symbol(symbol: str) -> None:
        if symbol != ALLOWED_SYMBOL:
            raise ControlledLiveBlocked("Only SOLUSDT LinearPerpetual is allowed")


def _clean(values: dict[str, Any]) -> dict[str, str]:
    return {str(key): _number(value) for key, value in values.items() if value is not None}


def _number(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _json_body(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value or "0"))


def _utc_day_start_ms() -> int:
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _derived_link_id(client_order_id: str, suffix: str) -> str:
    digest = hashlib.sha256(f"{client_order_id}:{suffix}".encode()).hexdigest()
    return f"clv1-{suffix[:5]}-{digest[:24]}"[:36]
