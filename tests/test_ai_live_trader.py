from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai.live_trader import (
    AI_CONFIDENCE_THRESHOLD,
    AI_LEVERAGE,
    AI_POSITION_NOTIONAL,
    AIAutonomousExecutionService,
    AIBatchDecision,
    AIDecision,
    AILiveRepository,
    AIMarketSnapshot,
    AIMarketDataReader,
    BybitFeeRateSnapshot,
    RecentClosedPosition,
    ClosedCandle,
    build_ai_preview,
    build_ai_prompt,
    require_same_symbol_cooldown,
    require_trend_confirmation,
    save_ai_proposal,
)
from app.core.config import Settings, get_settings
from app.db import (
    Base,
    BybitFeeRateCacheRecord,
    ControlledLiveStateRecord,
    ExecutionOrderRecord,
)
from app.exchanges.models import OrderSide
from app.exchanges.bybit_v5_gateway import BybitGatewayError, ProductionMutationGuard
from app.trading.controlled_live import (
    CONTROLLED_LIVE_V1,
    ControlledLiveBlocked,
    ControlledLiveRepository,
    LiveFill,
    LiveGatewaySnapshot,
    LivePositionSnapshot,
)
from app.trading.controlled_universe import SCANNER_CONFIG
from app.trading.multi_symbol_scanner import (
    ScannerAccount,
    ScannerInstrument,
    ScannerReadSnapshot,
    _parse_instrument,
)


def _sessions():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _instrument(symbol: str = "XRPUSDT", *, price: str = "1") -> ScannerInstrument:
    now = datetime.now(UTC)
    value = Decimal(price)
    return ScannerInstrument(
        symbol=symbol,
        internal_symbol=f"{symbol[:-4]}/USDT",
        enabled=True,
        exclusion_reason="",
        status="Trading",
        contract_type="LinearPerpetual",
        bid=value,
        ask=value,
        tick_size=Decimal("0.0001"),
        minimum_quantity=Decimal("1"),
        quantity_step=Decimal("1"),
        minimum_notional=Decimal("5"),
        actual_minimum_quantity=Decimal("5"),
        actual_minimum_notional=Decimal("5"),
        spread_pct=Decimal("0.0001"),
        turnover_24h=Decimal("100000000"),
        maximum_leverage=Decimal("50"),
        checked_at=now,
    )


def test_ai_decision_schema_and_fixed_notional_preview() -> None:
    decision = AIDecision(
        symbol="XRPUSDT",
        action="LONG",
        confidence=81,
        stop_loss=0.98,
        take_profit=1.04,
        reason="volatility and momentum agree",
    )
    preview = build_ai_preview("scan-1", decision, _instrument(), Decimal("0.001"))
    assert AI_CONFIDENCE_THRESHOLD == 75
    assert preview.side == OrderSide.BUY.value
    assert preview.leverage == AI_LEVERAGE == Decimal("10")
    assert preview.expected_notional == AI_POSITION_NOTIONAL == Decimal("15")
    assert preview.quantity == Decimal("15")
    assert preview.stop_loss < Decimal("1") < preview.take_profit
    assert preview.client_order_id == build_ai_preview(
        "scan-1", decision, _instrument(), Decimal("0.001")
    ).client_order_id


def test_ai_short_and_invalid_levels_fail_closed() -> None:
    short = AIDecision(
        symbol="XRPUSDT",
        action="SHORT",
        confidence=75,
        stop_loss=1.02,
        take_profit=0.96,
        reason="downside momentum",
    )
    preview = build_ai_preview("scan-2", short, _instrument(), Decimal("0.001"))
    assert preview.side == OrderSide.SELL.value
    assert preview.take_profit < Decimal("1") < preview.stop_loss
    invalid = short.model_copy(update={"stop_loss": 0.99})
    with pytest.raises(ControlledLiveBlocked, match="ordering"):
        build_ai_preview("scan-3", invalid, _instrument(), Decimal("0.001"))


def test_ai_confidence_74_is_blocked() -> None:
    decision = AIDecision(
        symbol="XRPUSDT",
        action="LONG",
        confidence=74,
        stop_loss=0.98,
        take_profit=1.04,
        reason="below corrected confidence gate",
    )
    with pytest.raises(ControlledLiveBlocked, match="threshold"):
        build_ai_preview("scan-confidence", decision, _instrument(), Decimal("0.001"))


def test_net_rr_and_real_fee_are_enforced() -> None:
    weak = AIDecision(
        symbol="XRPUSDT",
        action="LONG",
        confidence=80,
        stop_loss=0.98,
        take_profit=1.03,
        reason="gross rr is not net rr",
    )
    with pytest.raises(ControlledLiveBlocked, match="NET R/R"):
        build_ai_preview("scan-net-rr", weak, _instrument(), Decimal("0.001"))

    strong = weak.model_copy(update={"take_profit": 1.04})
    preview = build_ai_preview(
        "scan-real-fee", strong, _instrument(), Decimal("0.001")
    )
    assert preview.taker_fee_rate == Decimal("0.001")
    assert preview.expected_fee == preview.quantity * (
        Decimal("1") + preview.take_profit
    ) * Decimal("0.001")
    assert preview.risk_reward_ratio >= Decimal("1.5")
    assert preview.expected_net_edge > 0


def _trend_rows(*, bullish: bool) -> tuple[ClosedCandle, ...]:
    now = datetime(2026, 8, 30, tzinfo=UTC)
    closes = [
        Decimal("1") + (Decimal(index) / Decimal("1000")) * (1 if bullish else -1)
        for index in range(80)
    ]
    return tuple(
        ClosedCandle(
            now + timedelta(minutes=index),
            now + timedelta(minutes=index + 1),
            close,
            close + Decimal("0.001"),
            close - Decimal("0.001"),
            close,
            Decimal("100"),
        )
        for index, close in enumerate(closes)
    )


def test_countertrend_and_same_symbol_cooldown_block() -> None:
    short = AIDecision(
        symbol="XRPUSDT", action="SHORT", confidence=80,
        stop_loss=1.02, take_profit=0.96, reason="countertrend",
    )
    bullish = _trend_rows(bullish=True)
    with pytest.raises(ControlledLiveBlocked, match="bullish 15m.*1h"):
        require_trend_confirmation(short, {"15m": bullish, "1h": bullish})

    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    recent_sl = RecentClosedPosition(
        "XRPUSDT", "SHORT", now - timedelta(minutes=59), "SL"
    )
    with pytest.raises(ControlledLiveBlocked, match="60m cooldown"):
        require_same_symbol_cooldown(short, (recent_sl,), now)
    recent_tp = RecentClosedPosition(
        "XRPUSDT", "LONG", now - timedelta(minutes=59), "TP"
    )
    with pytest.raises(ControlledLiveBlocked, match="Opposite"):
        require_same_symbol_cooldown(short, (recent_tp,), now)
    require_same_symbol_cooldown(
        short,
        (RecentClosedPosition("XRPUSDT", "LONG", now - timedelta(minutes=60), "TP"),),
        now,
    )


def test_ai_batch_rejects_duplicate_or_unknown_symbols() -> None:
    values = {
        "action": "WAIT",
        "confidence": 20,
        "stop_loss": None,
        "take_profit": None,
        "reason": "no setup",
    }
    with pytest.raises(ValueError):
        AIBatchDecision.model_validate(
            {"decisions": [{"symbol": "XRPUSDT", **values}] * 2}
        )
    with pytest.raises(ValueError):
        AIBatchDecision.model_validate(
            {"decisions": [{"symbol": "BTCUSDT", **values}]}
        )


def test_ai_batch_strict_schema_requires_nullable_trade_levels() -> None:
    decision_schema = AIBatchDecision.model_json_schema()["$defs"]["AIDecision"]
    assert set(decision_schema["required"]) == set(decision_schema["properties"])

    wait = AIBatchDecision.model_validate(
        {
            "decisions": [
                {
                    "symbol": "SOLUSDT",
                    "action": "WAIT",
                    "confidence": 0,
                    "stop_loss": None,
                    "take_profit": None,
                    "reason": "No setup",
                }
            ]
        }
    )
    assert wait.decisions[0].stop_loss is None


def test_ai_scan_is_idempotent_and_decisions_persist() -> None:
    sessions = _sessions()
    settings = Settings()
    repository = AILiveRepository(sessions)
    repository.initialize(settings)
    scheduled = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    scan_id = repository.begin_scan(scheduled, "test-model")
    assert scan_id
    assert repository.begin_scan(scheduled, "test-model") is None


def _market() -> AIMarketSnapshot:
    now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    instruments = {symbol: _instrument(symbol) for symbol in SCANNER_CONFIG.symbols}
    account = ScannerAccount(
        equity=Decimal("50"),
        available_balance=Decimal("50"),
        open_positions=0,
        open_order_ids=frozenset(),
        fills_read=True,
        trades_today=0,
        daily_realized_pnl=Decimal(),
        consecutive_losses=0,
        cooldown_until=None,
    )
    rows = tuple(
        ClosedCandle(
            now - timedelta(minutes=5 * (80 - index)),
            now - timedelta(minutes=5 * (79 - index)),
            Decimal("1"),
            Decimal("1.01"),
            Decimal("0.99"),
            Decimal("1") + Decimal(index) / Decimal("10000"),
            Decimal("1000"),
        )
        for index in range(80)
    )
    candles = {
        symbol: {"5m": rows, "15m": rows, "1h": rows}
        for symbol in SCANNER_CONFIG.symbols
    }
    return AIMarketSnapshot(
        ScannerReadSnapshot(instruments, account, now), candles, (), now
    )


def test_prompt_contains_every_symbol_and_closed_indicators() -> None:
    prompt, digest = build_ai_prompt(_market())
    assert len(digest) == 64
    assert '"closed_candles_only":true' in prompt
    for symbol in SCANNER_CONFIG.symbols:
        assert symbol in prompt
    assert '"rsi_14"' in prompt
    assert '"macd_signal"' in prompt
    payload = json.loads(prompt)
    assert sum(
        len(frame["candles"])
        for symbol in payload["symbols"]
        for frame in symbol["timeframes"].values()
    ) == 8 * 3 * 5
    assert all(
        frame["trend"] in {"BULLISH", "BEARISH", "NEUTRAL"}
        for symbol in payload["symbols"]
        for frame in symbol["timeframes"].values()
    )


def test_prompt_uses_verified_real_fees_and_unchanged_net_cost_formula():
    market = _market()
    rates = {symbol: BybitFeeRateSnapshot(
        symbol, Decimal("0.0004"), Decimal("0.001"), market.fetched_at,
    ) for symbol in SCANNER_CONFIG.symbols}
    prompt, _ = build_ai_prompt(market, rates)
    data = json.loads(prompt)
    assert data["constraints"]["minimum_net_rr"] == "1.5"
    assert data["constraints"]["confidence_threshold"] == 75
    assert data["constraints"]["leverage"] == "10"
    assert data["constraints"]["position_notional_usdt"] == "15"
    assert data["constraints"]["maximum_open_positions"] == 3
    assert data["constraints"]["slippage_per_leg"] == str(CONTROLLED_LIVE_V1.estimated_slippage_per_leg)
    assert data["cost_formula_per_unit"]["target_cost"] == "(entry+TP)*(f+s)+(ask-bid)"
    assert data["cost_formula_per_unit"]["stop_cost"] == "(entry+SL)*(f+s)+(ask-bid)"
    assert "return WAIT" in data["task"]
    for item in data["symbols"]:
        assert item["taker_fee_rate"] == "0.001"
        assert item["fee_verified_at"] == market.fetched_at.isoformat()
        assert item["tick_size"] == str(market.scanner.instruments[item["symbol"]].tick_size)


def _sol_limits(*, price="101.48", cap=None, turnover="100000000", min_notional="5"):
    metadata = {
        "status": "Trading", "contractType": "LinearPerpetual",
        "lotSizeFilter": {"minOrderQty": "0.1", "qtyStep": "0.1", "minNotionalValue": min_notional},
        "priceFilter": {"tickSize": "0.01"}, "leverageFilter": {"maxLeverage": "50"},
    }
    ticker = {"bid1Price": str(Decimal(price)-Decimal("0.01")), "ask1Price": price,
              "turnover24h": turnover}
    kwargs = {} if cap is None else {"maximum_actual_minimum_notional": cap}
    return _parse_instrument("SOLUSDT", metadata, ticker, datetime.now(UTC), **kwargs)


@pytest.mark.parametrize("direction,stop,target", [("LONG", 99, 107), ("SHORT", 104, 95)])
def test_sol_minimum_10148_passes_ai_15_cap_and_quantity_rounding(direction, stop, target):
    old = _sol_limits()
    assert not old.enabled  # The independent archived/frozen path is unchanged.
    instrument = _sol_limits(cap=AI_POSITION_NOTIONAL)
    assert instrument.enabled
    assert instrument.actual_minimum_notional == Decimal("10.148")
    decision = AIDecision(symbol="SOLUSDT", action=direction, confidence=80,
                          stop_loss=stop, take_profit=target, reason="fixture")
    preview = build_ai_preview("sol-15", decision, instrument, Decimal("0.001"))
    assert preview.quantity == Decimal("0.1")
    assert preview.quantity % instrument.quantity_step == 0
    assert preview.expected_notional <= Decimal("15")
    assert preview.risk_reward_ratio >= Decimal("1.5")
    assert preview.leverage == Decimal("10")


@pytest.mark.parametrize("kwargs", [
    {"price": "151"}, {"turnover": "1"}, {"min_notional": "16"},
])
def test_ai_15_cap_does_not_bypass_exchange_limits_or_liquidity(kwargs):
    instrument = _sol_limits(cap=AI_POSITION_NOTIONAL, **kwargs)
    assert not instrument.enabled
    decision = AIDecision(symbol="SOLUSDT", action="LONG", confidence=80,
                          stop_loss=99, take_profit=107, reason="fixture")
    with pytest.raises(ControlledLiveBlocked):
        build_ai_preview("sol-reject", decision, instrument, Decimal("0.001"))


def test_production_ai_reader_passes_15_cap_to_shared_reader(monkeypatch):
    from app.trading.multi_symbol_scanner import BybitMultiSymbolReadOnlyReader
    from unittest.mock import Mock
    factory = Mock()
    monkeypatch.setattr(BybitMultiSymbolReadOnlyReader, "from_environment", factory)
    reader = AIMarketDataReader.from_environment()
    factory.assert_called_once_with(maximum_actual_minimum_notional=Decimal("15"))
    assert reader.reader is factory.return_value


class _Gateway:
    dry_run = False

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.fill = LiveFill(
            "bybit-order-1", "XRPUSDT:0", Decimal("15"), Decimal("1"), Decimal("0.01")
        )

    async def submit_market(self, preview, client_order_id):
        if self.fail:
            raise RuntimeError("submission timeout")
        return self.fill

    async def install_native_protection(self, *args, **kwargs):
        return None

    async def verify_native_protection(self, symbol, stop_loss, take_profit):
        return True

    async def emergency_close_reduce_only(self, fill, symbol):
        return None

    async def snapshot(self):
        return LiveGatewaySnapshot(
            (
                LivePositionSnapshot(
                    self.fill.position_id, "XRPUSDT", self.fill.filled_quantity
                ),
            ),
            frozenset(),
            frozenset({self.fill.order_id}),
        )


@pytest.mark.asyncio
async def test_ai_execution_records_fill_protection_and_reconciliation() -> None:
    sessions = _sessions()
    controlled = ControlledLiveRepository(sessions)
    controlled.state()
    decision = AIDecision(
        symbol="XRPUSDT",
        action="LONG",
        confidence=80,
        stop_loss=0.98,
        take_profit=1.04,
        reason="test",
    )
    preview = build_ai_preview("scan-ok", decision, _instrument(), Decimal("0.001"))
    save_ai_proposal(controlled, preview, 123)
    fill = await AIAutonomousExecutionService(controlled, _Gateway()).execute(
        preview, account_id="account"
    )
    assert fill.order_id == "bybit-order-1"
    with sessions() as session:
        ledger = session.scalar(select(ExecutionOrderRecord))
        assert ledger.status == "FILLED_PROTECTED"


@pytest.mark.asyncio
async def test_ai_unknown_activates_kill_switch_and_never_retries() -> None:
    sessions = _sessions()
    controlled = ControlledLiveRepository(sessions)
    controlled.state()
    decision = AIDecision(
        symbol="XRPUSDT",
        action="LONG",
        confidence=80,
        stop_loss=0.98,
        take_profit=1.04,
        reason="test",
    )
    preview = build_ai_preview("scan-timeout", decision, _instrument(), Decimal("0.001"))
    save_ai_proposal(controlled, preview, 123)
    gateway = _Gateway(fail=True)
    with pytest.raises(RuntimeError, match="UNKNOWN"):
        await AIAutonomousExecutionService(controlled, gateway).execute(
            preview, account_id="account"
        )
    with sessions() as session:
        state = session.get(ControlledLiveStateRecord, CONTROLLED_LIVE_V1.name)
        ledger = session.scalar(select(ExecutionOrderRecord))
        assert state.kill_switch_active
        assert ledger.status == "UNKNOWN"


class _GuardHttp:
    async def public_get(self, path, params):
        if path.endswith("instruments-info"):
            return {
                "list": [{
                    "status": "Trading",
                    "contractType": "LinearPerpetual",
                    "lotSizeFilter": {
                        "minOrderQty": "1",
                        "qtyStep": "1",
                        "minNotionalValue": "5",
                    },
                }]
            }
        return {
            "list": [{
                "bid1Price": "0.9999",
                "ask1Price": "1",
                "turnover24h": "100000000",
            }]
        }

    async def private_get(self, path, params):
        if path == "/v5/user/query-api":
            return {
                "readOnly": 0,
                "permissions": {"ContractTrade": ["Order", "Position"], "Wallet": []},
            }
        if path == "/v5/account/info":
            return {"marginMode": "ISOLATED_MARGIN"}
        if path == "/v5/account/fee-rate":
            return {
                "list": [{
                    "symbol": params.get("symbol", "XRPUSDT"),
                    "makerFeeRate": "0.0004",
                    "takerFeeRate": "0.001",
                }]
            }
        if path == "/v5/position/list":
            return {"list": []}
        if path == "/v5/order/realtime":
            return {"list": []}
        if path == "/v5/account/wallet-balance":
            return {"list": [{"totalEquity": "50", "totalAvailableBalance": "50"}]}
        if path == "/v5/execution/list":
            return {"list": [{"orderId": "old-loss", "execPnl": "-5", "execFee": "0"}]}
        raise AssertionError(path)


class _HighFeeGuardHttp(_GuardHttp):
    async def private_get(self, path, params):
        if path == "/v5/account/fee-rate":
            return {
                "list": [{
                    "symbol": params.get("symbol", "XRPUSDT"),
                    "makerFeeRate": "0.0004",
                    "takerFeeRate": "0.003",
                }]
            }
        return await super().private_get(path, params)


class _UnavailableFeeGuardHttp(_GuardHttp):
    async def private_get(self, path, params):
        if path == "/v5/account/fee-rate":
            raise BybitGatewayError("fee API unavailable")
        return await super().private_get(path, params)


def _arm_ai(monkeypatch, enabled: str = "true") -> None:
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("CONTROLLED_LIVE_ENABLED", "true")
    monkeypatch.setenv("MANUAL_FIRST_ORDER_APPROVED", "true")
    monkeypatch.setenv("CONTROLLED_LIVE_V1_FIRST_SYMBOL", "MULTI_SYMBOL_SCANNER")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "[123]")
    monkeypatch.setenv("AI_TRADING_ENABLED", enabled)
    monkeypatch.setenv("AI_API_KEY", "test-secret")
    monkeypatch.setenv("AI_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("AI_MODEL", "test-model")
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_production_guard_allows_exact_15_ai_notional_and_ignores_old_loss_gate(
    monkeypatch,
) -> None:
    _arm_ai(monkeypatch)
    sessions = _sessions()
    controlled = ControlledLiveRepository(sessions)
    controlled.state()
    preview = build_ai_preview(
        "guard-ai",
        AIDecision(
            symbol="XRPUSDT",
            action="LONG",
            confidence=80,
            stop_loss=0.98,
            take_profit=1.04,
            reason="test",
        ),
        _instrument(),
        Decimal("0.001"),
    )
    save_ai_proposal(controlled, preview, 123)
    result = await ProductionMutationGuard(sessions, _GuardHttp()).authorize(
        action="CREATE",
        symbol="XRPUSDT",
        quantity=Decimal("15"),
        client_order_id=preview.client_order_id,
    )
    assert result.equity == Decimal("50")
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_production_guard_blocks_ai_source_when_ai_flag_is_false(monkeypatch) -> None:
    _arm_ai(monkeypatch)
    sessions = _sessions()
    controlled = ControlledLiveRepository(sessions)
    controlled.state()
    preview = build_ai_preview(
        "guard-disabled",
        AIDecision(
            symbol="XRPUSDT",
            action="LONG",
            confidence=80,
            stop_loss=0.98,
            take_profit=1.04,
            reason="test",
        ),
        _instrument(),
        Decimal("0.001"),
    )
    save_ai_proposal(controlled, preview, 123)
    monkeypatch.setenv("AI_TRADING_ENABLED", "false")
    get_settings.cache_clear()
    with pytest.raises(ControlledLiveBlocked, match="AI_TRADING_ENABLED=false"):
        await ProductionMutationGuard(sessions, _GuardHttp()).authorize(
            action="CREATE",
            symbol="XRPUSDT",
            quantity=Decimal("15"),
            client_order_id=preview.client_order_id,
        )
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_production_guard_recalculates_fee_and_net_rr_before_http(monkeypatch) -> None:
    _arm_ai(monkeypatch)
    sessions = _sessions()
    controlled = ControlledLiveRepository(sessions)
    controlled.state()
    preview = build_ai_preview(
        "guard-high-fee",
        AIDecision(
            symbol="XRPUSDT", action="LONG", confidence=80,
            stop_loss=0.98, take_profit=1.04, reason="test",
        ),
        _instrument(),
        Decimal("0.001"),
    )
    save_ai_proposal(controlled, preview, 123)
    with pytest.raises(ControlledLiveBlocked, match="BLOCKED_BEFORE_HTTP.*NET R/R"):
        await ProductionMutationGuard(sessions, _HighFeeGuardHttp()).authorize(
            action="CREATE",
            symbol="XRPUSDT",
            quantity=Decimal("15"),
            client_order_id=preview.client_order_id,
        )
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_fee_api_fails_closed_or_uses_fresh_persisted_cache(monkeypatch) -> None:
    _arm_ai(monkeypatch)
    sessions = _sessions()
    controlled = ControlledLiveRepository(sessions)
    controlled.state()
    preview = build_ai_preview(
        "guard-fee-cache",
        AIDecision(
            symbol="XRPUSDT", action="LONG", confidence=80,
            stop_loss=0.98, take_profit=1.04, reason="test",
        ),
        _instrument(),
        Decimal("0.001"),
    )
    save_ai_proposal(controlled, preview, 123)
    guard = ProductionMutationGuard(sessions, _UnavailableFeeGuardHttp())
    with pytest.raises(ControlledLiveBlocked, match="no confirmed fee cache"):
        await guard.authorize(
            action="CREATE", symbol="XRPUSDT", quantity=Decimal("15"),
            client_order_id=preview.client_order_id,
        )
    with sessions.begin() as session:
        session.add(
            BybitFeeRateCacheRecord(
                symbol="XRPUSDT",
                maker_fee_rate=Decimal("0.0004"),
                taker_fee_rate=Decimal("0.001"),
                verified_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    result = await guard.authorize(
        action="CREATE", symbol="XRPUSDT", quantity=Decimal("15"),
        client_order_id=preview.client_order_id,
    )
    assert result.equity == Decimal("50")
    get_settings.cache_clear()
