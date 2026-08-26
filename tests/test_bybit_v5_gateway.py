import hashlib
import hmac
import json
import time
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, ExecutionOrderRecord
from app.exchanges.bybit_v5_gateway import (
    BybitOrderRejected,
    BybitV5Http,
    BybitV5OrderGateway,
    DryRunBlocked,
    GuardSnapshot,
    ProductionMutationGuard,
)
from app.core.config import get_settings
from app.exchanges.models import InstrumentRules, OrderSide
from app.trading.controlled_live import (
    ArmingGates,
    ControlledLiveBlocked,
    ControlledLiveRepository,
    ControlledRiskSnapshot,
    ManualExecutionService,
    ManualOrderInputs,
    ProtectionFailure,
    ReconciliationRequired,
)
from app.trading.execution_store import DuplicateOrderError
import app.exchanges.bybit_v5_gateway as gateway_module


class AllowAuthorizer:
    def __init__(self):
        self.calls = []

    async def authorize(self, **values):
        self.calls.append(values)
        return GuardSnapshot(Decimal("90"), 0, Decimal("50"), Decimal())


class MockBybitVenue:
    def __init__(
        self,
        *,
        order_status="Filled",
        fill_quantity="0.1",
        reject_create=False,
        timeout_create=False,
        protection_failure=False,
        existing_duplicate=False,
        pending_order=False,
    ):
        self.order_status = order_status
        self.fill_quantity = fill_quantity
        self.reject_create = reject_create
        self.timeout_create = timeout_create
        self.protection_failure = protection_failure
        self.created = existing_duplicate
        self.open_position = existing_duplicate
        self.pending_order = pending_order
        self.client_id = "clv1-existing"
        self.side = "Buy"
        self.posts = []
        self.cancelled = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = dict(request.url.params)
        if request.method == "POST":
            assert request.headers.get("X-BAPI-SIGN")
            payload = json.loads(request.content)
            self.posts.append((path, payload))
            if path == "/v5/position/set-leverage":
                return _ok({})
            if path == "/v5/position/trading-stop":
                if self.protection_failure:
                    return _error(10001, "protection rejected")
                return _ok({})
            if path == "/v5/order/cancel":
                self.cancelled = True
                self.pending_order = False
                return _ok({"orderId": payload["orderId"], "orderLinkId": payload["orderLinkId"]})
            if path == "/v5/order/create":
                if payload.get("reduceOnly"):
                    self.open_position = False
                    return _ok({"orderId": "close-1", "orderLinkId": payload["orderLinkId"]})
                self.client_id = payload["orderLinkId"]
                self.side = payload["side"]
                if self.reject_create:
                    return _error(10001, "order rejected")
                self.created = True
                self.open_position = True
                if self.timeout_create:
                    self.timeout_create = False
                    raise httpx.ReadTimeout("accepted but response lost", request=request)
                return _ok({"orderId": "order-1", "orderLinkId": self.client_id})

        if path == "/v5/market/instruments-info":
            return _ok(
                {
                    "list": [
                        {
                            "symbol": "SOLUSDT",
                            "status": "Trading",
                            "contractType": "LinearPerpetual",
                            "lotSizeFilter": {
                                "minOrderQty": "0.1",
                                "qtyStep": "0.1",
                                "minNotionalValue": "5",
                            },
                            "priceFilter": {"tickSize": "0.01"},
                        }
                    ]
                }
            )
        if path == "/v5/market/tickers":
            return _ok(
                {
                    "list": [
                        {
                            "symbol": "SOLUSDT",
                            "bid1Price": "89.99",
                            "ask1Price": "90",
                            "turnover24h": "100000000",
                        }
                    ]
                }
            )
        if path in {"/v5/order/realtime", "/v5/order/history"}:
            if self.pending_order and "orderId" not in query and "orderLinkId" not in query:
                return _ok(
                    {
                        "list": [
                            {
                                "orderId": "pending-1",
                                "orderLinkId": "clv1-existing",
                                "orderStatus": "New",
                            }
                        ]
                    }
                )
            if not self.created:
                return _ok({"list": []})
            return _ok(
                {
                    "list": [
                        {
                            "orderId": "order-1",
                            "orderLinkId": self.client_id,
                            "orderStatus": self.order_status,
                            "cumExecQty": self.fill_quantity,
                            "avgPrice": "90",
                            "positionIdx": 0,
                        }
                    ]
                }
            )
        if path == "/v5/execution/list":
            return _ok(
                {
                    "list": [
                        {
                            "orderId": "order-1",
                            "orderLinkId": self.client_id,
                            "execId": "fill-1",
                            "execQty": self.fill_quantity,
                            "execPrice": "90",
                            "execFee": "0.005",
                        }
                    ]
                    if self.created
                    else []
                }
            )
        if path == "/v5/user/query-api":
            return _ok(
                {
                    "readOnly": 0,
                    "permissions": {
                        "ContractTrade": ["Order", "Position"],
                        "Wallet": [],
                    },
                }
            )
        if path == "/v5/account/wallet-balance":
            return _ok(
                {
                    "list": [
                        {
                            "totalEquity": "50",
                            "totalAvailableBalance": "50",
                            "coin": [{"coin": "USDT", "walletBalance": "50"}],
                        }
                    ]
                }
            )
        if path == "/v5/position/list":
            return _ok(
                {
                    "list": [
                        {
                            "symbol": "SOLUSDT",
                            "side": self.side,
                            "size": self.fill_quantity,
                            "avgPrice": "90",
                            "positionIdx": 0,
                        }
                    ]
                    if self.open_position
                    else []
                }
            )
        return _ok({"list": []})


def _ok(result):
    return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": result})


def _error(code, message):
    return httpx.Response(200, json={"retCode": code, "retMsg": message, "result": {}})


def _preview(side=OrderSide.BUY):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    repository = ControlledLiveRepository(sessions)
    service = ManualExecutionService(repository, None, {42})
    preview = service.preview(
        42,
        ManualOrderInputs(side, Decimal("90"), Decimal("89") if side is OrderSide.BUY else Decimal("91"), Decimal("92") if side is OrderSide.BUY else Decimal("88")),
        ControlledRiskSnapshot(Decimal("50"), Decimal("50")),
        InstrumentRules(
            Decimal("0.01"),
            Decimal("0.1"),
            Decimal("0.1"),
            Decimal("5"),
            Decimal("100"),
            Decimal("150"),
        ),
    )
    service.approve(42, preview.proposal_hash)
    return engine, sessions, repository, preview


def _gateway(venue, *, dry_run=False):
    http = BybitV5Http("key", "secret", transport=httpx.MockTransport(venue))
    return BybitV5OrderGateway(http, AllowAuthorizer(), dry_run=dry_run), http


@pytest.mark.asyncio
@pytest.mark.parametrize(("side", "expected"), [(OrderSide.BUY, "Buy"), (OrderSide.SELL, "Sell")])
async def test_market_long_and_short_fill(side, expected):
    venue = MockBybitVenue()
    gateway, _ = _gateway(venue)
    _, _, _, preview = _preview(side)
    fill = await gateway.submit_market(preview, preview.client_order_id)
    assert fill.filled_quantity == Decimal("0.1")
    assert fill.average_price == Decimal("90")
    assert fill.fee == Decimal("0.005")
    create = next(payload for path, payload in venue.posts if path == "/v5/order/create")
    assert create["side"] == expected
    assert create["qty"] == "0.1"
    assert create["reduceOnly"] is False
    assert len(create["orderLinkId"]) <= 36
    actions = [item["action"] for item in gateway._authorizer.calls]
    assert actions == ["SET_LEVERAGE", "CREATE"]
    await gateway.close()


@pytest.mark.asyncio
async def test_rejected_order_is_explicit_not_fill():
    venue = MockBybitVenue(reject_create=True)
    gateway, _ = _gateway(venue)
    _, _, _, preview = _preview()
    with pytest.raises(BybitOrderRejected):
        await gateway.submit_market(preview, preview.client_order_id)
    await gateway.close()


@pytest.mark.asyncio
async def test_rejected_order_is_persisted_as_rejected_not_unknown():
    venue = MockBybitVenue(reject_create=True)
    gateway, _ = _gateway(venue)
    _, sessions, repository, preview = _preview()
    service = ManualExecutionService(repository, gateway, {42})
    with pytest.raises(BybitOrderRejected):
        await service.execute_first_order(
            42,
            preview,
            account_id="main",
            gates=ArmingGates(True, True, True, "SOLUSDT"),
        )
    assert repository.proposal(preview.proposal_id).status == "REJECTED"
    assert repository.state().first_order_in_progress is False
    with sessions() as session:
        assert session.query(ExecutionOrderRecord).one().status == "REJECTED"
    await gateway.close()


@pytest.mark.asyncio
async def test_partial_fill_is_preserved_for_native_protection():
    venue = MockBybitVenue(order_status="PartiallyFilledCanceled", fill_quantity="0.04")
    gateway, _ = _gateway(venue)
    _, _, _, preview = _preview()
    fill = await gateway.submit_market(preview, preview.client_order_id)
    assert fill.filled_quantity == Decimal("0.04")
    assert fill.average_price == Decimal("90")
    await gateway.close()


@pytest.mark.asyncio
async def test_timeout_is_unknown_then_reconciled_and_never_resent():
    venue = MockBybitVenue(timeout_create=True)
    gateway, _ = _gateway(venue)
    _, sessions, repository, preview = _preview()
    service = ManualExecutionService(repository, gateway, {42})
    with pytest.raises(ReconciliationRequired):
        await service.execute_first_order(
            42,
            preview,
            account_id="main",
            gates=ArmingGates(True, True, True, "SOLUSDT"),
        )
    assert repository.proposal(preview.proposal_id).status == "UNKNOWN"
    with sessions() as session:
        assert session.query(ExecutionOrderRecord).one().status == "UNKNOWN"
    reconciled = await gateway.reconcile_client_order_id(preview.client_order_id)
    assert reconciled["status"] == "MATCH"
    assert reconciled["safe_to_retry"] is False
    with pytest.raises(ControlledLiveBlocked, match="already claimed"):
        await service.execute_first_order(
            42,
            preview,
            account_id="main",
            gates=ArmingGates(True, True, True, "SOLUSDT"),
        )
    creates = [payload for path, payload in venue.posts if path == "/v5/order/create"]
    assert len(creates) == 1
    await gateway.close()


@pytest.mark.asyncio
async def test_duplicate_client_id_is_blocked_before_create():
    venue = MockBybitVenue(existing_duplicate=True)
    gateway, _ = _gateway(venue)
    _, _, _, preview = _preview()
    venue.client_id = preview.client_order_id
    with pytest.raises(DuplicateOrderError):
        await gateway.submit_market(preview, preview.client_order_id)
    assert venue.posts == []
    await gateway.close()


@pytest.mark.asyncio
async def test_exchange_native_tp_sl_success():
    venue = MockBybitVenue(existing_duplicate=True)
    gateway, _ = _gateway(venue)
    await gateway.set_tp_sl(
        symbol="SOLUSDT",
        stop_loss=Decimal("89"),
        take_profit=Decimal("92"),
        client_order_id="clv1-existing",
        reduce_only=True,
    )
    payload = next(payload for path, payload in venue.posts if path == "/v5/position/trading-stop")
    assert payload["tpslMode"] == "Full"
    assert payload["tpOrderType"] == "Market"
    assert payload["slOrderType"] == "Market"
    await gateway.close()


@pytest.mark.asyncio
async def test_tp_sl_failure_emergency_closes_reduce_only():
    venue = MockBybitVenue(protection_failure=True)
    gateway, _ = _gateway(venue)
    _, _, repository, preview = _preview()
    service = ManualExecutionService(repository, gateway, {42})
    with pytest.raises(ProtectionFailure, match="emergency-closed"):
        await service.execute_first_order(
            42,
            preview,
            account_id="main",
            gates=ArmingGates(True, True, True, "SOLUSDT"),
        )
    close = [
        payload
        for path, payload in venue.posts
        if path == "/v5/order/create" and payload.get("reduceOnly")
    ]
    assert len(close) == 1
    assert close[0]["closeOnTrigger"] is True
    assert venue.open_position is False
    await gateway.close()


@pytest.mark.asyncio
async def test_restart_reads_open_position_without_creating_order():
    venue = MockBybitVenue(existing_duplicate=True)
    first, _ = _gateway(venue)
    snapshot = await first.snapshot()
    await first.close()
    restarted, _ = _gateway(venue)
    restored = await restarted.snapshot()
    assert snapshot.positions == restored.positions
    assert len(restored.positions) == 1
    assert venue.posts == []
    await restarted.close()


@pytest.mark.asyncio
async def test_cancel_pending_order():
    venue = MockBybitVenue(pending_order=True)
    gateway, _ = _gateway(venue)
    assert await gateway.cancel_pending_orders("SOLUSDT") == 1
    assert venue.cancelled is True
    payload = next(payload for path, payload in venue.posts if path == "/v5/order/cancel")
    assert payload["orderId"] == "pending-1"
    await gateway.close()


@pytest.mark.asyncio
async def test_dry_run_signs_exact_payload_and_sends_no_post():
    venue = MockBybitVenue()
    gateway, _ = _gateway(venue, dry_run=True)
    _, _, _, preview = _preview()
    report = await gateway.dry_run_market_request(preview, preview.client_order_id)
    assert report["signature_valid"] is True
    assert report["payload"] == {
        "category": "linear",
        "symbol": "SOLUSDT",
        "side": "Buy",
        "orderType": "Market",
        "qty": "0.1",
        "timeInForce": "IOC",
        "positionIdx": 0,
        "reduceOnly": False,
        "closeOnTrigger": False,
        "orderLinkId": preview.client_order_id,
    }
    assert report["sent"] is False
    assert venue.posts == []
    with pytest.raises(DryRunBlocked):
        await gateway.submit_market(preview, preview.client_order_id)
    assert not any(path == "/v5/order/create" for path, _ in venue.posts)
    await gateway.close()


@pytest.mark.asyncio
async def test_service_dry_run_never_claims_ledger_or_changes_approval():
    venue = MockBybitVenue()
    gateway, _ = _gateway(venue, dry_run=True)
    _, sessions, repository, preview = _preview()
    service = ManualExecutionService(repository, gateway, {42})
    with pytest.raises(ControlledLiveBlocked, match="DRY_RUN signed"):
        await service.execute_first_order(
            42,
            preview,
            account_id="main",
            gates=ArmingGates(True, True, True, "SOLUSDT"),
        )
    assert repository.proposal(preview.proposal_id).status == "APPROVED"
    with sessions() as session:
        assert session.query(ExecutionOrderRecord).count() == 0
    assert not any(path == "/v5/order/create" for path, _ in venue.posts)
    await gateway.close()


@pytest.mark.asyncio
async def test_production_guard_validates_full_controlled_snapshot(monkeypatch):
    venue = MockBybitVenue()
    http = BybitV5Http("key", "secret", transport=httpx.MockTransport(venue))
    _, sessions, _, preview = _preview()
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("CONTROLLED_LIVE_ENABLED", "true")
    monkeypatch.setenv("MANUAL_FIRST_ORDER_APPROVED", "true")
    monkeypatch.setenv("CONTROLLED_LIVE_V1_FIRST_SYMBOL", "SOLUSDT")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "[42]")
    get_settings.cache_clear()
    snapshot = await ProductionMutationGuard(sessions, http).authorize(
        action="CREATE",
        symbol="SOLUSDT",
        quantity=Decimal("0.1"),
        client_order_id=preview.client_order_id,
    )
    assert snapshot == GuardSnapshot(Decimal("90"), 0, Decimal("50"), Decimal())
    get_settings.cache_clear()
    await http.close()


@pytest.mark.asyncio
async def test_production_guard_blocks_before_any_http_when_gates_false(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'guard.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    calls = []

    def handler(request):
        calls.append(request)
        return _ok({})

    http = BybitV5Http("key", "secret", transport=httpx.MockTransport(handler))
    guard = ProductionMutationGuard(sessions, http)
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("CONTROLLED_LIVE_ENABLED", "false")
    monkeypatch.setenv("MANUAL_FIRST_ORDER_APPROVED", "false")
    monkeypatch.setenv("CONTROLLED_LIVE_V1_FIRST_SYMBOL", "SOLUSDT")
    with pytest.raises(ControlledLiveBlocked, match="gates are disabled"):
        await guard.authorize(
            action="CREATE",
            symbol="SOLUSDT",
            quantity=Decimal("0.1"),
            client_order_id="clv1-never-sent",
        )
    assert calls == []
    await http.close()


def test_client_order_id_is_deterministic_and_bybit_compatible():
    _, _, _, preview = _preview()
    assert preview.client_order_id == preview.client_order_id
    assert len(preview.client_order_id) == 36


@pytest.mark.asyncio
async def test_post_signature_matches_official_v5_formula(monkeypatch):
    monkeypatch.setattr(gateway_module.time, "time", lambda: 1_658_385_579.423)
    http = BybitV5Http("public-key", "private-secret", transport=httpx.MockTransport(lambda request: _ok({})))
    payload = {"category": "linear", "symbol": "SOLUSDT", "qty": "0.1"}
    mutation, body, headers = http.sign_post("/v5/order/create", payload)
    plain = f"{mutation.timestamp_ms}public-key5000{body}"
    expected = hmac.new(b"private-secret", plain.encode(), hashlib.sha256).hexdigest()
    assert headers["X-BAPI-SIGN"] == expected
    assert mutation.signature_valid is True
    assert "private-secret" not in repr(http)
    await http.close()


@pytest.mark.asyncio
async def test_private_get_signs_the_exact_query_order_sent_over_http(monkeypatch):
    monkeypatch.setattr(gateway_module.time, "time", lambda: 1_658_385_579.423)

    def handler(request):
        query = request.url.query.decode()
        assert query == "category=linear&limit=1&symbol=SOLUSDT"
        plain = f"{request.headers['X-BAPI-TIMESTAMP']}public-key5000{query}"
        expected = hmac.new(
            b"private-secret", plain.encode(), hashlib.sha256
        ).hexdigest()
        assert request.headers["X-BAPI-SIGN"] == expected
        return _ok({"list": []})

    http = BybitV5Http(
        "public-key",
        "private-secret",
        transport=httpx.MockTransport(handler),
    )
    await http.private_get(
        "/v5/execution/list",
        {"category": "linear", "symbol": "SOLUSDT", "limit": 1},
    )
    await http.close()


@pytest.mark.asyncio
async def test_controlled_proposal_snapshot_is_read_only_and_complete():
    venue = MockBybitVenue()
    gateway, _ = _gateway(venue, dry_run=True)
    snapshot = await gateway.controlled_proposal_snapshot("SOLUSDT")
    assert snapshot.bid_price == Decimal("89.99")
    assert snapshot.ask_price == Decimal("90")
    assert snapshot.wallet_balance == Decimal("50")
    assert snapshot.equity == Decimal("50")
    assert snapshot.open_positions == 0
    assert snapshot.open_order_ids == frozenset()
    assert snapshot.fills_read is True
    assert venue.posts == []
    await gateway.close()


class GuardHttp:
    def __init__(self, *, positions=None, bid="89.99", turnover="100000000", executions=None):
        self.positions = positions or []
        self.bid = bid
        self.turnover = turnover
        self.executions = executions or []

    async def public_get(self, path, params):
        if path.endswith("instruments-info"):
            return {
                "list": [{
                    "status": "Trading",
                    "contractType": "LinearPerpetual",
                    "lotSizeFilter": {
                        "minOrderQty": "0.1",
                        "qtyStep": "0.1",
                        "minNotionalValue": "5",
                    },
                }]
            }
        return {
            "list": [{
                "bid1Price": self.bid,
                "ask1Price": "90",
                "lastPrice": "90",
                "turnover24h": self.turnover,
            }]
        }

    async def private_get(self, path, params):
        if path == "/v5/user/query-api":
            return {
                "readOnly": 0,
                "permissions": {
                    "ContractTrade": ["Order", "Position"],
                    "Wallet": [],
                },
            }
        if path == "/v5/position/list":
            return {"list": self.positions}
        if path == "/v5/order/realtime":
            return {"list": []}
        if path == "/v5/account/wallet-balance":
            return {"list": [{"totalEquity": "50"}]}
        if path == "/v5/execution/list":
            return {"list": self.executions}
        raise AssertionError(path)


def _armed_environment(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("CONTROLLED_LIVE_ENABLED", "true")
    monkeypatch.setenv("MANUAL_FIRST_ORDER_APPROVED", "true")
    monkeypatch.setenv("CONTROLLED_LIVE_V1_FIRST_SYMBOL", "SOLUSDT")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "[42]")
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_production_guard_counts_positions_across_entire_unified_account(monkeypatch):
    _, sessions, _, preview = _preview()
    _armed_environment(monkeypatch)
    http = GuardHttp(positions=[{"symbol": "XRPUSDT", "size": "1"}])
    with pytest.raises(ControlledLiveBlocked, match="Maximum open positions"):
        await ProductionMutationGuard(sessions, http).authorize(
            action="CREATE",
            symbol="SOLUSDT",
            quantity=Decimal("0.1"),
            client_order_id=preview.client_order_id,
        )
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_production_guard_rechecks_spread_and_liquidity_before_http(monkeypatch):
    _, sessions, _, preview = _preview()
    _armed_environment(monkeypatch)
    with pytest.raises(ControlledLiveBlocked, match="spread"):
        await ProductionMutationGuard(sessions, GuardHttp(bid="89")).authorize(
            action="CREATE",
            symbol="SOLUSDT",
            quantity=Decimal("0.1"),
            client_order_id=preview.client_order_id,
        )
    with pytest.raises(ControlledLiveBlocked, match="turnover"):
        await ProductionMutationGuard(sessions, GuardHttp(turnover="1000")).authorize(
            action="CREATE",
            symbol="SOLUSDT",
            quantity=Decimal("0.1"),
            client_order_id=preview.client_order_id,
        )
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_production_guard_enforces_consecutive_losses(monkeypatch):
    _, sessions, _, preview = _preview()
    _armed_environment(monkeypatch)
    now_ms = int(time.time() * 1000)
    executions = [
        {
            "orderId": f"loss-{index}",
            "execPnl": "-0.10",
            "execFee": "0.01",
            "execTime": str(now_ms - index * 1000),
        }
        for index in range(2)
    ]
    with pytest.raises(ControlledLiveBlocked, match="Consecutive-loss"):
        await ProductionMutationGuard(
            sessions, GuardHttp(executions=executions)
        ).authorize(
            action="CREATE",
            symbol="SOLUSDT",
            quantity=Decimal("0.1"),
            client_order_id=preview.client_order_id,
        )
    get_settings.cache_clear()
