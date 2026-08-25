import asyncio
import json
from urllib.parse import parse_qs

import httpx
import pytest

from app.exchanges.bybit_readonly import (
    BybitMainnetReadOnlyClient,
    ReadOnlyViolation,
    mainnet_order_gate,
    permission_summary,
)
from app.service_runner import command_for_role


def test_client_repr_and_errors_never_expose_credentials() -> None:
    client = BybitMainnetReadOnlyClient("visible-key", "super-secret", transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"retCode": 0, "result": {}})))
    try:
        rendered = repr(client)
        assert "visible-key" not in rendered
        assert "super-secret" not in rendered
    finally:
        asyncio.run(client.close())


def test_client_rejects_non_official_mainnet_endpoint() -> None:
    with pytest.raises(ValueError, match="official Mainnet"):
        BybitMainnetReadOnlyClient(
            "key", "secret", base_url="https://example.invalid"
        )


@pytest.mark.asyncio
async def test_allowlist_rejects_every_mutating_endpoint_before_http() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    client = BybitMainnetReadOnlyClient("key", "secret", transport=httpx.MockTransport(handler))
    try:
        for path in (
            "/v5/order/create",
            "/v5/order/amend",
            "/v5/order/cancel",
            "/v5/order/cancel-all",
            "/v5/position/set-leverage",
            "/v5/position/trading-stop",
            "/v5/asset/transfer/inter-transfer",
            "/v5/asset/withdraw/create",
        ):
            with pytest.raises(ReadOnlyViolation):
                await client.private_get(path)
        assert calls == 0
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_signed_request_is_get_and_report_response_excludes_key_fields() -> None:
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["method"] = request.method
        observed["path"] = request.url.path
        observed["query"] = parse_qs(request.url.query.decode())
        observed["has_auth"] = all(
            name in request.headers
            for name in ("X-BAPI-API-KEY", "X-BAPI-SIGN", "X-BAPI-TIMESTAMP")
        )
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "result": {"apiKey": "must-not-be-reported", "secret": "", "readOnly": 0},
            },
        )

    client = BybitMainnetReadOnlyClient("key", "secret", transport=httpx.MockTransport(handler))
    try:
        result = await client.private_get("/v5/user/query-api", {"b": 2, "a": 1})
        assert observed == {
            "method": "GET",
            "path": "/v5/user/query-api",
            "query": {"a": ["1"], "b": ["2"]},
            "has_auth": True,
        }
        safe = permission_summary(result.result)
        assert "must-not-be-reported" not in json.dumps(safe)
    finally:
        await client.close()


def test_permission_mapping_is_exact_and_conservative() -> None:
    mapped = permission_summary(
        {
            "readOnly": 0,
            "permissions": {
                "ContractTrade": ["Order", "Position"],
                "Spot": ["SpotTrade"],
                "Wallet": [],
                "Options": [],
                "Derivatives": ["DerivativesTrade"],
            },
        }
    )
    assert mapped["read"] == "YES"
    assert mapped["trade"] == "YES"
    assert mapped["withdraw"] == "NO"
    assert mapped["transfer"] == "NO"


@pytest.mark.asyncio
async def test_mainnet_adapter_gate_blocks_before_transport() -> None:
    assert await mainnet_order_gate() == "BLOCKED"


def test_railway_readonly_service_role() -> None:
    assert command_for_role("bybit_preflight")[-1] == "app.exchanges.bybit_readonly_worker"
