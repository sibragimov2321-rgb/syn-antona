"""Strict GET-only Bybit V5 Mainnet private preflight.

This module deliberately has no order, transfer, withdrawal, or account mutation endpoints.
Credentials and signatures are never included in reports or exception messages.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import hashlib
import hmac
import json
import os
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import create_engine, inspect, text

from app.exchanges.adapters import BybitAdapter
from app.exchanges.base import LiveTradingDisabledError
from app.exchanges.models import MarketType, OrderRequest, OrderSide, OrderType


MAINNET_BASE_URL = "https://api.bytick.com"
APPROVED_MAINNET_BASE_URLS = frozenset(
    {"https://api.bybit.com", "https://api.bytick.com"}
)
RECV_WINDOW_MS = 5_000
PRIVATE_GET_PATHS = frozenset(
    {
        "/v5/user/query-api",
        "/v5/account/info",
        "/v5/account/wallet-balance",
        "/v5/position/list",
        "/v5/order/realtime",
        "/v5/order/history",
        "/v5/execution/list",
    }
)
PUBLIC_GET_PATHS = frozenset(
    {
        "/v5/market/time",
        "/v5/market/instruments-info",
        "/v5/market/tickers",
    }
)


class ReadOnlyViolation(PermissionError):
    pass


class BybitReadError(RuntimeError):
    def __init__(self, path: str, code: int | str, message: str) -> None:
        safe_message = " ".join(str(message).replace("\r", " ").replace("\n", " ").split())[:160]
        super().__init__(f"GET {path} failed: code={code}, message={safe_message}")
        self.path = path
        self.code = code
        self.safe_message = safe_message


@dataclass(frozen=True)
class ReadResult:
    result: dict[str, Any]
    latency_ms: Decimal


class BybitMainnetReadOnlyClient:
    """A small signed client whose allowlist contains GET endpoints only."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = MAINNET_BASE_URL,
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("BYBIT_API_KEY and BYBIT_API_SECRET must be set")
        normalized_base_url = base_url.rstrip("/")
        if normalized_base_url not in APPROVED_MAINNET_BASE_URLS:
            raise ValueError("Bybit base URL is not an approved official Mainnet endpoint")
        self._api_key = api_key
        self._api_secret = api_secret
        self._server_offset_ms = 0
        self._http = httpx.AsyncClient(
            base_url=normalized_base_url,
            timeout=timeout_seconds,
            transport=transport,
            headers={"User-Agent": "syn-antona-readonly-preflight/1.0"},
        )

    def __repr__(self) -> str:
        return "BybitMainnetReadOnlyClient(api_key=***, api_secret=***)"

    async def close(self) -> None:
        await self._http.aclose()

    async def public_get(self, path: str, params: dict[str, Any] | None = None) -> ReadResult:
        self._require_allowed(path, private=False)
        return await self._get(path, params or {}, headers=None)

    async def private_get(self, path: str, params: dict[str, Any] | None = None) -> ReadResult:
        self._require_allowed(path, private=True)
        parameters = _clean_params(params or {})
        query = urlencode(sorted(parameters.items()))
        timestamp_ms = int(time.time() * 1000) + self._server_offset_ms
        payload = f"{timestamp_ms}{self._api_key}{RECV_WINDOW_MS}{query}"
        signature = hmac.new(
            self._api_secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        headers = {
            "X-BAPI-API-KEY": self._api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-TIMESTAMP": str(timestamp_ms),
            "X-BAPI-RECV-WINDOW": str(RECV_WINDOW_MS),
        }
        return await self._get(path, parameters, headers=headers)

    async def synchronize_time(self) -> ReadResult:
        response = await self.public_get("/v5/market/time")
        server_ms = _server_time_ms(response.result)
        self._server_offset_ms = server_ms - int(time.time() * 1000)
        return response

    async def _get(
        self,
        path: str,
        params: dict[str, Any],
        headers: dict[str, str] | None,
    ) -> ReadResult:
        started = time.perf_counter()
        try:
            response = await self._http.get(path, params=params, headers=headers)
            latency = Decimal(str((time.perf_counter() - started) * 1000))
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as error:
            raise BybitReadError(
                path, f"HTTP_{error.response.status_code}", "HTTPStatusError"
            ) from error
        except (httpx.HTTPError, ValueError) as error:
            raise BybitReadError(path, "HTTP", type(error).__name__) from error
        code = payload.get("retCode")
        if code != 0:
            raise BybitReadError(path, code, payload.get("retMsg", "Bybit rejected request"))
        result = payload.get("result")
        if not isinstance(result, dict):
            raise BybitReadError(path, "SCHEMA", "result is not an object")
        return ReadResult(result, latency)

    @staticmethod
    def _require_allowed(path: str, *, private: bool) -> None:
        allowlist = PRIVATE_GET_PATHS if private else PUBLIC_GET_PATHS
        if path not in allowlist:
            raise ReadOnlyViolation(f"Endpoint is not on the GET-only allowlist: {path}")


def _clean_params(params: dict[str, Any]) -> dict[str, str]:
    return {str(key): str(value) for key, value in params.items() if value is not None}


def _server_time_ms(result: dict[str, Any]) -> int:
    if result.get("timeNano"):
        return int(result["timeNano"]) // 1_000_000
    if result.get("timeSecond"):
        return int(result["timeSecond"]) * 1000
    raise BybitReadError("/v5/market/time", "SCHEMA", "server time missing")


def _decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal()
    return Decimal(str(value))


def _unified_status(value: Any) -> str:
    return {
        1: "CLASSIC",
        3: "UTA_1_0",
        4: "UTA_1_0_PRO",
        5: "UTA_2_0",
        6: "UTA_2_0_PRO",
    }.get(int(value or 0), f"UNKNOWN_{value}")


def permission_summary(api_info: dict[str, Any]) -> dict[str, Any]:
    permissions = api_info.get("permissions") or {}
    contract = set(permissions.get("ContractTrade") or [])
    spot = set(permissions.get("Spot") or [])
    wallet = set(permissions.get("Wallet") or [])
    options = set(permissions.get("Options") or [])
    derivatives = set(permissions.get("Derivatives") or [])
    trade = bool(
        contract.intersection({"Order", "Position"})
        or "SpotTrade" in spot
        or "OptionsTrade" in options
        or "DerivativesTrade" in derivatives
    )
    transfer_tokens = {"AccountTransfer", "SubMemberTransfer", "SubMemberTransferList"}
    return {
        "read": "YES",
        "trade": "YES" if trade else "NO",
        "withdraw": "YES" if "Withdraw" in wallet else "NO",
        "transfer": "YES" if wallet.intersection(transfer_tokens) else "NO",
        "read_only_key": bool(int(api_info.get("readOnly", 0))),
        "spot": "YES" if "SpotTrade" in spot else "NO",
        "usdt_perpetual_linear": "YES"
        if contract.intersection({"Order", "Position"}) or "DerivativesTrade" in derivatives
        else "NO",
        "usdc_derivatives": "YES"
        if "OptionsTrade" in options or "DerivativesTrade" in derivatives
        else "NO",
    }


def _safe_positions(result: dict[str, Any]) -> list[dict[str, str]]:
    positions = []
    for item in result.get("list") or []:
        if _decimal(item.get("size")) <= 0:
            continue
        positions.append(
            {
                "symbol": str(item.get("symbol", "")),
                "side": str(item.get("side", "")),
                "size": str(item.get("size", "0")),
                "entry_price": str(item.get("avgPrice") or item.get("entryPrice") or "0"),
                "leverage": str(item.get("leverage") or "0"),
                "unrealized_pnl": str(item.get("unrealisedPnl") or "0"),
            }
        )
    return positions


def _safe_wallet(result: dict[str, Any]) -> dict[str, str]:
    accounts = result.get("list") or []
    account = accounts[0] if accounts else {}
    usdt = next(
        (coin for coin in account.get("coin") or [] if coin.get("coin") == "USDT"),
        {},
    )
    return {
        "total_equity": str(account.get("totalEquity") or "0"),
        "available_balance": str(account.get("totalAvailableBalance") or "0"),
        "wallet_balance": str(account.get("totalWalletBalance") or "0"),
        "unrealized_pnl": str(account.get("totalPerpUPL") or "0"),
        "usdt_wallet_balance": str(usdt.get("walletBalance") or "0"),
        "usdt_available_to_withdraw": str(
            usdt.get("availableToWithdraw") or account.get("totalAvailableBalance") or "0"
        ),
        "usdt_unrealized_pnl": str(usdt.get("unrealisedPnl") or "0"),
    }


def _safe_orders(result: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "order_id": str(item.get("orderId") or ""),
            "client_order_id": str(item.get("orderLinkId") or ""),
            "symbol": str(item.get("symbol") or ""),
            "status": str(item.get("orderStatus") or ""),
        }
        for item in result.get("list") or []
    ]


def _instrument(instrument: dict[str, Any], ticker: dict[str, Any]) -> dict[str, str]:
    lot = instrument.get("lotSizeFilter") or {}
    price = instrument.get("priceFilter") or {}
    leverage = instrument.get("leverageFilter") or {}
    return {
        "symbol": "BTCUSDT",
        "tick_size": str(price.get("tickSize") or ""),
        "quantity_step": str(lot.get("qtyStep") or ""),
        "minimum_quantity": str(lot.get("minOrderQty") or ""),
        "minimum_notional": str(lot.get("minNotionalValue") or ""),
        "maximum_leverage": str(leverage.get("maxLeverage") or ""),
        "mark_price": str(ticker.get("markPrice") or ""),
        "index_price": str(ticker.get("indexPrice") or ""),
        "last_price": str(ticker.get("lastPrice") or ""),
        "funding_rate": str(ticker.get("fundingRate") or ""),
        "next_funding_timestamp": str(ticker.get("nextFundingTime") or ""),
    }


class _MainnetProbeTransport:
    sandbox = False
    websocket_connected = False

    def __init__(self) -> None:
        self.calls = 0

    async def connect(self) -> None:
        return None

    async def call(self, operation: str, **parameters):
        self.calls += 1
        raise AssertionError("Mainnet transport must not be reached")


async def mainnet_order_gate() -> str:
    probe = _MainnetProbeTransport()
    adapter = BybitAdapter(probe)
    request = OrderRequest(
        "readonly-preflight",
        "BTC/USDT",
        MarketType.PERPETUAL,
        OrderSide.BUY,
        OrderType.MARKET,
        Decimal("0.001"),
        leverage=Decimal("1"),
        client_order_id="readonly-probe-never-sent",
    )
    try:
        await adapter.create_order(request)
    except LiveTradingDisabledError:
        if probe.calls == 0:
            return "BLOCKED"
    raise RuntimeError("Mainnet order submission gate did not block before transport")


def _normalize_database_url(value: str) -> str:
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+psycopg://", 1)
    if value.startswith("postgres://"):
        return value.replace("postgres://", "postgresql+psycopg://", 1)
    return value


def reconciliation_dry_run(
    open_orders: list[dict[str, str]],
    recent_orders: list[dict[str, str]],
    positions: list[dict[str, str]],
) -> dict[str, Any]:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        return {"status": "DATABASE_UNAVAILABLE", "reason": "DATABASE_URL is not set"}
    try:
        engine = create_engine(
            _normalize_database_url(database_url),
            pool_pre_ping=True,
            connect_args={"connect_timeout": 5}
            if database_url.startswith(("postgres://", "postgresql://"))
            else {},
        )
        if not inspect(engine).has_table("execution_orders"):
            engine.dispose()
            return {"status": "LEDGER_NOT_MIGRATED", "reason": "execution_orders missing"}
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT client_order_id, exchange_order_id, status "
                    "FROM execution_orders WHERE exchange = :exchange"
                ),
                {"exchange": "bybit"},
            ).mappings().all()
        engine.dispose()
    except Exception as error:
        return {"status": "DATABASE_UNAVAILABLE", "reason": type(error).__name__}

    exchange_ids = {item["order_id"] for item in open_orders if item["order_id"]}
    exchange_client_ids = {
        item["client_order_id"] for item in open_orders if item["client_order_id"]
    }
    recent_ids = {item["order_id"] for item in recent_orders if item["order_id"]}
    recent_client_ids = {
        item["client_order_id"] for item in recent_orders if item["client_order_id"]
    }
    local_active = [row for row in rows if row["status"] in {"PENDING", "SUBMITTED", "UNKNOWN"}]
    local_ids = {str(row["exchange_order_id"]) for row in local_active if row["exchange_order_id"]}
    local_client_ids = {str(row["client_order_id"]) for row in local_active}
    exchange_only = [
        item
        for item in open_orders
        if item["order_id"] not in local_ids
        and item["client_order_id"] not in local_client_ids
    ]
    unresolved_local = [
        row
        for row in local_active
        if str(row["exchange_order_id"] or "") not in exchange_ids | recent_ids
        and str(row["client_order_id"]) not in exchange_client_ids | recent_client_ids
    ]
    mismatches = {
        "exchange_only_open_orders": len(exchange_only),
        "unresolved_local_orders": len(unresolved_local),
        "open_positions_without_local_active_order": len(positions) if not local_active else 0,
    }
    return {
        "status": "MATCH" if not any(mismatches.values()) else "MISMATCH",
        "local_ledger_rows": len(rows),
        "local_active_rows": len(local_active),
        **mismatches,
    }


async def build_report() -> dict[str, Any]:
    api_key = os.getenv("BYBIT_API_KEY")
    api_secret = os.getenv("BYBIT_API_SECRET")
    live_enabled = os.getenv("LIVE_TRADING_ENABLED", "false").strip().lower() == "true"
    report: dict[str, Any] = {
        "checked_at": datetime.now(UTC).isoformat(),
        "environment": {
            "BYBIT_API_KEY": "SET" if api_key else "NOT SET",
            "BYBIT_API_SECRET": "SET" if api_secret else "NOT SET",
            "LIVE_TRADING_ENABLED": str(live_enabled).lower(),
        },
        "mainnet_order_submission_gate": await mainnet_order_gate(),
        "kill_switch": "SAFE" if not live_enabled else "UNSAFE",
    }
    if not api_key or not api_secret or live_enabled:
        report["private_api_auth"] = "FAIL"
        report["status"] = "FAIL"
        report["reason"] = "Required credentials missing or live trading flag is unsafe"
        return report

    client = BybitMainnetReadOnlyClient(api_key, api_secret)
    try:
        server_time = await client.synchronize_time()
        report["server_time"] = {
            "utc": datetime.fromtimestamp(_server_time_ms(server_time.result) / 1000, UTC).isoformat(),
            "latency_ms": str(server_time.latency_ms.quantize(Decimal("0.01"))),
        }
        try:
            api_info_read = await client.private_get("/v5/user/query-api")
        except BybitReadError as error:
            report["private_api_auth"] = "FAIL"
            report["status"] = "FAIL"
            report["reason"] = str(error)
            return report
        report["private_api_auth"] = "PASS"
        report["private_api_latency_ms"] = str(
            api_info_read.latency_ms.quantize(Decimal("0.01"))
        )
        api_info = api_info_read.result
        permissions = permission_summary(api_info)
        report["permissions"] = permissions
        report["account_scope"] = (
            "SUBACCOUNT"
            if str(api_info.get("parentUid") or "0") not in {"", "0"}
            else "MAIN_OR_UNVERIFIED"
        )

        account_read = await client.private_get("/v5/account/info")
        account = account_read.result
        report["account"] = {
            "account_type": "UNIFIED"
            if int(account.get("unifiedMarginStatus") or 0) > 1
            else "CLASSIC",
            "unified_trading_status": _unified_status(account.get("unifiedMarginStatus")),
            "margin_mode": str(account.get("marginMode") or "UNKNOWN"),
            "wallet_categories_read": ["UNIFIED"],
        }

        wallet_read = await client.private_get(
            "/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": "USDT"}
        )
        report["balance"] = _safe_wallet(wallet_read.result)

        positions_read = await client.private_get(
            "/v5/position/list", {"category": "linear", "settleCoin": "USDT", "limit": 200}
        )
        positions = _safe_positions(positions_read.result)
        report["open_positions_count"] = len(positions)
        report["open_positions"] = positions

        open_orders_read = await client.private_get(
            "/v5/order/realtime",
            {"category": "linear", "settleCoin": "USDT", "openOnly": 0, "limit": 50},
        )
        open_orders = _safe_orders(open_orders_read.result)
        report["open_orders_count"] = len(open_orders)

        try:
            conditional_read = await client.private_get(
                "/v5/order/realtime",
                {
                    "category": "linear",
                    "settleCoin": "USDT",
                    "orderFilter": "StopOrder",
                    "openOnly": 0,
                    "limit": 50,
                },
            )
            conditional_orders = _safe_orders(conditional_read.result)
            report["conditional_orders_count"] = len(conditional_orders)
        except BybitReadError as error:
            conditional_orders = []
            report["conditional_orders_count"] = "READ_ERROR"
            report["conditional_orders_error"] = str(error)

        recent_read = await client.private_get(
            "/v5/order/history", {"category": "linear", "limit": 20}
        )
        recent_orders = _safe_orders(recent_read.result)
        report["recent_order_status_read"] = "PASS"
        report["recent_orders_count"] = len(recent_orders)

        await client.private_get("/v5/execution/list", {"category": "linear", "limit": 1})
        report["fills_read"] = "PASS"

        instruments_read = await client.public_get(
            "/v5/market/instruments-info", {"category": "linear", "symbol": "BTCUSDT"}
        )
        tickers_read = await client.public_get(
            "/v5/market/tickers", {"category": "linear", "symbol": "BTCUSDT"}
        )
        instrument_list = instruments_read.result.get("list") or []
        ticker_list = tickers_read.result.get("list") or []
        if not instrument_list or not ticker_list:
            raise BybitReadError("BTCUSDT instrument", "SCHEMA", "instrument or ticker missing")
        report["btc_usdt_linear"] = _instrument(instrument_list[0], ticker_list[0])
        report["market_capabilities"] = {
            "spot": permissions["spot"],
            "usdt_perpetual_linear": permissions["usdt_perpetual_linear"],
            "usdc_derivatives": permissions["usdc_derivatives"],
            "selection": "NONE",
        }
        report["reconciliation"] = reconciliation_dry_run(
            open_orders + conditional_orders, recent_orders, positions
        )
        report["status"] = "PASS" if report["reconciliation"]["status"] == "MATCH" else "FAIL"
        return report
    except BybitReadError as error:
        report["status"] = "FAIL"
        report["reason"] = str(error)
        return report
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Strict read-only Bybit Mainnet private preflight")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    report = asyncio.run(build_report())
    encoded = json.dumps(report, indent=2, default=str, sort_keys=True)
    if arguments.output:
        arguments.output.write_text(encoded, encoding="utf-8")
    print(encoded, flush=True)
    raise SystemExit(0 if report.get("status") == "PASS" else 1)


if __name__ == "__main__":
    main()
