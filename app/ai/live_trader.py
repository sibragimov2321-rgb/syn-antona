"""Five-minute multi-symbol AI decision loop for the existing Bybit gateway.

The AI is an untrusted decision source.  It can only create a durable proposal;
all exchange mutations still pass through the production gateway guard.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_DOWN, Decimal
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.ai.service import OpenAICompatibleProvider
from app.core.config import Settings
from app.db import (
    AILiveDecisionRecord,
    AILiveRuntimeRecord,
    AILiveScanRecord,
    BybitFeeRateCacheRecord,
    ControlledLiveProposalRecord,
)
from app.exchanges.models import OrderSide
from app.trading.execution_store import OrderRejected
from app.market.indicators import ema, snapshot as indicator_snapshot
from app.trading.controlled_live import (
    CONTROLLED_LIVE_V1,
    ControlledLiveBlocked,
    ControlledLiveRepository,
    ManualExecutionPreview,
)
from app.trading.live_costs import (
    estimate_live_costs,
    require_cost_aware_edge,
    validate_taker_fee_rate,
)
from app.trading.controlled_universe import (
    AI_SIGNAL_SOURCE,
    ALLOWED_SCANNER_SYMBOLS,
    SCANNER_CONFIG,
    scanner_selection_hash,
)
from app.trading.multi_symbol_scanner import (
    BybitMultiSymbolReadOnlyReader,
    ScannerInstrument,
    ScannerReadSnapshot,
)


AI_RUNTIME_NAME = "AI_LIVE"
AI_POSITION_NOTIONAL = Decimal("15")
AI_LEVERAGE = Decimal("10")
AI_MAX_POSITIONS = 3
AI_CONFIDENCE_THRESHOLD = 75
SAME_SYMBOL_COOLDOWN = timedelta(minutes=60)
FEE_CACHE_MAX_AGE = timedelta(hours=24)
TIMEFRAMES = {"5m": ("5", 5), "15m": ("15", 15), "1h": ("60", 60)}
MAX_LEVEL_DISTANCE_PCT = Decimal("0.10")
MIN_LEVEL_DISTANCE_PCT = Decimal("0.001")


class AIDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    action: Literal["LONG", "SHORT", "WAIT"]
    confidence: int = Field(ge=0, le=100)
    # Strict OpenAI-compatible JSON Schema requires every declared property to
    # be listed as required. WAIT decisions carry explicit nulls; trade
    # decisions still require numeric levels in the validator below.
    stop_loss: float | None
    take_profit: float | None
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def levels_for_trade(self) -> AIDecision:
        if self.action != "WAIT" and (
            self.stop_loss is None or self.take_profit is None
        ):
            raise ValueError("LONG/SHORT requires stop_loss and take_profit")
        return self


class AIBatchDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: list[AIDecision]

    @model_validator(mode="after")
    def unique_allowed_symbols(self) -> AIBatchDecision:
        symbols = [item.symbol for item in self.decisions]
        if len(symbols) != len(set(symbols)):
            raise ValueError("AI returned duplicate symbols")
        unknown = set(symbols) - ALLOWED_SCANNER_SYMBOLS
        if unknown:
            raise ValueError("AI returned symbols outside the production allowlist")
        return self


@dataclass(frozen=True)
class ClosedCandle:
    opened_at: datetime
    closed_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class AIMarketSnapshot:
    scanner: ScannerReadSnapshot
    candles: dict[str, dict[str, tuple[ClosedCandle, ...]]]
    positions: tuple[dict[str, Any], ...]
    fetched_at: datetime


@dataclass(frozen=True)
class BybitFeeRateSnapshot:
    symbol: str
    maker_fee_rate: Decimal
    taker_fee_rate: Decimal
    verified_at: datetime


@dataclass(frozen=True)
class RecentClosedPosition:
    symbol: str
    direction: Literal["LONG", "SHORT"]
    closed_at: datetime
    exit_reason: Literal["SL", "TP", "OTHER"]


@dataclass(frozen=True)
class AILiveStatus:
    enabled: bool
    status: str
    model: str
    last_scan_at: datetime | None
    next_scan_at: datetime | None
    equity: Decimal | None
    available_balance: Decimal | None
    open_positions: int
    open_orders: int
    total_scans: int
    last_error: str | None
    decisions: tuple[dict[str, Any], ...]
    positions: tuple[dict[str, Any], ...]


class AIMarketDataReader:
    def __init__(self, reader: BybitMultiSymbolReadOnlyReader) -> None:
        self.reader = reader

    @classmethod
    def from_environment(cls) -> AIMarketDataReader:
        return cls(BybitMultiSymbolReadOnlyReader.from_environment())

    async def read(self, now: datetime | None = None) -> AIMarketSnapshot:
        current = now or datetime.now(UTC)
        scanner = await self.reader.read()
        jobs = [
            self._candles(symbol, name, interval, minutes, current)
            for symbol in SCANNER_CONFIG.symbols
            for name, (interval, minutes) in TIMEFRAMES.items()
        ]
        results = await asyncio.gather(*jobs)
        candles: dict[str, dict[str, tuple[ClosedCandle, ...]]] = {
            symbol: {} for symbol in SCANNER_CONFIG.symbols
        }
        for symbol, timeframe, rows in results:
            candles[symbol][timeframe] = rows
        positions_response = await self.reader.client.private_get(
            "/v5/position/list", {"category": "linear", "settleCoin": "USDT"}
        )
        positions = tuple(
            {
                "symbol": str(item.get("symbol") or ""),
                "side": str(item.get("side") or ""),
                "size": str(item.get("size") or "0"),
                "entry_price": str(item.get("avgPrice") or "0"),
                "mark_price": str(item.get("markPrice") or "0"),
                "leverage": str(item.get("leverage") or "0"),
                "unrealized_pnl": str(item.get("unrealisedPnl") or "0"),
                "stop_loss": str(item.get("stopLoss") or "0"),
                "take_profit": str(item.get("takeProfit") or "0"),
            }
            for item in positions_response.result.get("list") or []
            if Decimal(str(item.get("size") or "0")) > 0
        )
        return AIMarketSnapshot(scanner, candles, positions, current)

    async def _candles(
        self,
        symbol: str,
        timeframe: str,
        interval: str,
        minutes: int,
        now: datetime,
    ) -> tuple[str, str, tuple[ClosedCandle, ...]]:
        response = await self.reader.client.public_get(
            "/v5/market/kline",
            {"category": "linear", "symbol": symbol, "interval": interval, "limit": 200},
        )
        duration = timedelta(minutes=minutes)
        rows: list[ClosedCandle] = []
        for item in response.result.get("list") or []:
            opened = datetime.fromtimestamp(int(item[0]) / 1000, tz=UTC)
            closed = opened + duration
            if closed > now:
                continue
            rows.append(
                ClosedCandle(
                    opened,
                    closed,
                    Decimal(str(item[1])),
                    Decimal(str(item[2])),
                    Decimal(str(item[3])),
                    Decimal(str(item[4])),
                    Decimal(str(item[5])),
                )
            )
        rows.sort(key=lambda item: item.opened_at)
        if len(rows) < 60:
            raise RuntimeError(f"{symbol} {timeframe} closed-candle warm-up is incomplete")
        if now - rows[-1].closed_at > duration + timedelta(minutes=2):
            raise RuntimeError(f"{symbol} {timeframe} market data is stale")
        return symbol, timeframe, tuple(rows)

    async def close(self) -> None:
        await self.reader.close()

    async def fee_rates(self, now: datetime | None = None) -> dict[str, BybitFeeRateSnapshot]:
        verified = (now or datetime.now(UTC)).astimezone(UTC)
        response = await self.reader.client.private_get(
            "/v5/account/fee-rate", {"category": "linear"}
        )
        rates: dict[str, BybitFeeRateSnapshot] = {}
        for item in response.result.get("list") or []:
            symbol = str(item.get("symbol") or "")
            if symbol not in ALLOWED_SCANNER_SYMBOLS:
                continue
            taker = validate_taker_fee_rate(Decimal(str(item.get("takerFeeRate") or "0")))
            maker = Decimal(str(item.get("makerFeeRate") or "0"))
            rates[symbol] = BybitFeeRateSnapshot(symbol, maker, taker, verified)
        missing = ALLOWED_SCANNER_SYMBOLS - set(rates)
        if missing:
            raise RuntimeError("Bybit fee API omitted allowed symbols")
        return rates

    async def recent_closes(self) -> tuple[RecentClosedPosition, ...]:
        closed_response, executions_response = await asyncio.gather(
            self.reader.client.private_get(
                "/v5/position/closed-pnl", {"category": "linear", "limit": 100}
            ),
            self.reader.client.private_get(
                "/v5/execution/list", {"category": "linear", "limit": 100}
            ),
        )
        execution_by_order = {
            str(item.get("orderId") or ""): item
            for item in executions_response.result.get("list") or []
        }
        rows: list[RecentClosedPosition] = []
        for item in closed_response.result.get("list") or []:
            symbol = str(item.get("symbol") or "")
            if symbol not in ALLOWED_SCANNER_SYMBOLS:
                continue
            execution = execution_by_order.get(str(item.get("orderId") or ""), {})
            marker = (
                str(execution.get("stopOrderType") or "")
                + str(execution.get("createType") or "")
            ).upper()
            reason: Literal["SL", "TP", "OTHER"] = "OTHER"
            if "STOPLOSS" in marker:
                reason = "SL"
            elif "TAKEPROFIT" in marker:
                reason = "TP"
            rows.append(
                RecentClosedPosition(
                    symbol,
                    "LONG" if str(item.get("side")) == "Sell" else "SHORT",
                    datetime.fromtimestamp(int(item["updatedTime"]) / 1000, tz=UTC),
                    reason,
                )
            )
        return tuple(sorted(rows, key=lambda item: item.closed_at))


class AILiveRepository:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def initialize(self, settings: Settings) -> None:
        now = datetime.now(UTC)
        with self.session_factory.begin() as session:
            runtime = session.get(AILiveRuntimeRecord, AI_RUNTIME_NAME)
            if runtime is None:
                runtime = AILiveRuntimeRecord(runtime_name=AI_RUNTIME_NAME)
                session.add(runtime)
            runtime.enabled = settings.ai_trading_enabled
            runtime.status = "STARTING" if settings.ai_trading_enabled else "DISABLED"
            runtime.model = settings.ai_model if settings.ai_trading_enabled else "NOT_CONFIGURED"
            runtime.scan_interval_seconds = settings.ai_scan_interval_seconds
            runtime.heartbeat_at = now
            runtime.updated_at = now

    def save_fee_rates(self, rates: dict[str, BybitFeeRateSnapshot]) -> None:
        now = datetime.now(UTC)
        with self.session_factory.begin() as session:
            for symbol, snapshot in rates.items():
                record = session.get(BybitFeeRateCacheRecord, symbol)
                if record is None:
                    record = BybitFeeRateCacheRecord(
                        symbol=symbol,
                        maker_fee_rate=snapshot.maker_fee_rate,
                        taker_fee_rate=snapshot.taker_fee_rate,
                        verified_at=snapshot.verified_at,
                        updated_at=now,
                    )
                    session.add(record)
                else:
                    record.maker_fee_rate = snapshot.maker_fee_rate
                    record.taker_fee_rate = snapshot.taker_fee_rate
                    record.verified_at = snapshot.verified_at
                    record.updated_at = now

    def cached_fee_rates(
        self, now: datetime | None = None
    ) -> dict[str, BybitFeeRateSnapshot]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        with self.session_factory() as session:
            rows = session.scalars(select(BybitFeeRateCacheRecord)).all()
            snapshots: dict[str, BybitFeeRateSnapshot] = {}
            for row in rows:
                verified = row.verified_at
                if verified.tzinfo is None:
                    verified = verified.replace(tzinfo=UTC)
                else:
                    verified = verified.astimezone(UTC)
                if current - verified > FEE_CACHE_MAX_AGE:
                    continue
                snapshots[row.symbol] = BybitFeeRateSnapshot(
                    row.symbol,
                    Decimal(row.maker_fee_rate),
                    validate_taker_fee_rate(Decimal(row.taker_fee_rate)),
                    verified,
                )
            return snapshots

    def begin_scan(self, scheduled_at: datetime, model: str) -> str | None:
        scan_id = sha256(f"{AI_RUNTIME_NAME}:{scheduled_at.isoformat()}".encode()).hexdigest()
        now = datetime.now(UTC)
        try:
            with self.session_factory.begin() as session:
                if session.get(AILiveScanRecord, scan_id) is not None:
                    return None
                session.add(
                    AILiveScanRecord(
                        id=scan_id,
                        scheduled_at=scheduled_at,
                        started_at=now,
                        model=model,
                        status="RUNNING",
                        created_at=now,
                    )
                )
            return scan_id
        except IntegrityError:
            return None

    def complete_scan(
        self,
        scan_id: str,
        request_hash: str,
        result: AIBatchDecision,
        market: AIMarketSnapshot,
        next_scan_at: datetime,
    ) -> None:
        now = datetime.now(UTC)
        account_json = json.dumps(
            {
                "equity": str(market.scanner.account.equity),
                "available_balance": str(market.scanner.account.available_balance),
                "open_positions": market.positions,
                "open_orders": sorted(market.scanner.account.open_order_ids),
            },
            sort_keys=True,
        )
        with self.session_factory.begin() as session:
            scan = session.get(AILiveScanRecord, scan_id)
            runtime = session.get(AILiveRuntimeRecord, AI_RUNTIME_NAME)
            if scan is None or runtime is None:
                raise RuntimeError("AI scan/runtime persistence is missing")
            scan.completed_at = now
            scan.request_hash = request_hash
            scan.status = "COMPLETED"
            scan.account_json = account_json
            for decision in result.decisions:
                decision_id = sha256(f"{scan_id}:{decision.symbol}".encode()).hexdigest()
                session.add(
                    AILiveDecisionRecord(
                        id=decision_id,
                        scan_id=scan_id,
                        symbol=decision.symbol,
                        action=decision.action,
                        confidence=decision.confidence,
                        stop_loss=(
                            Decimal(str(decision.stop_loss))
                            if decision.stop_loss is not None
                            else None
                        ),
                        take_profit=(
                            Decimal(str(decision.take_profit))
                            if decision.take_profit is not None
                            else None
                        ),
                        reason=decision.reason,
                        disposition=(
                            "CANDIDATE"
                            if decision.action != "WAIT"
                            and decision.confidence >= AI_CONFIDENCE_THRESHOLD
                            else "WAIT"
                        ),
                        created_at=now,
                        updated_at=now,
                    )
                )
            runtime.status = "RUNNING"
            runtime.last_scan_at = now
            runtime.next_scan_at = next_scan_at
            runtime.last_market_data_at = market.fetched_at
            runtime.equity = market.scanner.account.equity
            runtime.available_balance = market.scanner.account.available_balance
            runtime.open_positions = market.scanner.account.open_positions
            runtime.open_positions_json = json.dumps(market.positions, sort_keys=True)
            runtime.open_orders = len(market.scanner.account.open_order_ids)
            runtime.total_scans += 1
            runtime.last_error = None
            runtime.heartbeat_at = now
            runtime.updated_at = now

    def fail_scan(self, scan_id: str | None, error: Exception) -> None:
        now = datetime.now(UTC)
        message = f"{type(error).__name__}: {error}"[:1000]
        with self.session_factory.begin() as session:
            if scan_id:
                scan = session.get(AILiveScanRecord, scan_id)
                if scan is not None:
                    scan.completed_at = now
                    scan.status = "FAILED"
                    scan.error_code = type(error).__name__
                    scan.error_message = message
            runtime = session.get(AILiveRuntimeRecord, AI_RUNTIME_NAME)
            if runtime is not None:
                runtime.status = "DEGRADED"
                runtime.last_error = message
                runtime.heartbeat_at = now
                runtime.updated_at = now

    def heartbeat(self) -> None:
        now = datetime.now(UTC)
        with self.session_factory.begin() as session:
            runtime = session.get(AILiveRuntimeRecord, AI_RUNTIME_NAME)
            if runtime is not None:
                runtime.heartbeat_at = now
                runtime.updated_at = now

    def set_disposition(
        self,
        scan_id: str,
        symbol: str,
        disposition: str,
        *,
        proposal_id: str | None = None,
        error: Exception | None = None,
    ) -> None:
        with self.session_factory.begin() as session:
            record = session.scalar(
                select(AILiveDecisionRecord).where(
                    AILiveDecisionRecord.scan_id == scan_id,
                    AILiveDecisionRecord.symbol == symbol,
                )
            )
            if record is None:
                raise RuntimeError("AI decision record is missing")
            record.disposition = disposition
            record.proposal_id = proposal_id or record.proposal_id
            record.error_code = type(error).__name__ if error else None
            record.updated_at = datetime.now(UTC)

    def status(self) -> AILiveStatus:
        with self.session_factory() as session:
            runtime = session.get(AILiveRuntimeRecord, AI_RUNTIME_NAME)
            if runtime is None:
                return AILiveStatus(
                    False, "NOT_DEPLOYED", "NOT_CONFIGURED", None, None,
                    None, None, 0, 0, 0, None, (), (),
                )
            latest_scan = session.scalar(
                select(AILiveScanRecord)
                .where(AILiveScanRecord.status == "COMPLETED")
                .order_by(AILiveScanRecord.completed_at.desc())
                .limit(1)
            )
            decisions: tuple[dict[str, Any], ...] = ()
            if latest_scan is not None:
                rows = session.scalars(
                    select(AILiveDecisionRecord)
                    .where(AILiveDecisionRecord.scan_id == latest_scan.id)
                    .order_by(AILiveDecisionRecord.symbol)
                ).all()
                decisions = tuple(
                    {
                        "symbol": item.symbol,
                        "action": item.action,
                        "confidence": item.confidence,
                        "reason": item.reason,
                        "disposition": item.disposition,
                    }
                    for item in rows
                )
            try:
                positions = tuple(json.loads(runtime.open_positions_json or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                positions = ()
            return AILiveStatus(
                runtime.enabled,
                runtime.status,
                runtime.model,
                runtime.last_scan_at,
                runtime.next_scan_at,
                Decimal(runtime.equity) if runtime.equity is not None else None,
                (
                    Decimal(runtime.available_balance)
                    if runtime.available_balance is not None
                    else None
                ),
                runtime.open_positions,
                runtime.open_orders,
                runtime.total_scans,
                runtime.last_error,
                decisions,
                positions,
            )


def build_ai_prompt(market: AIMarketSnapshot) -> tuple[str, str]:
    symbols: list[dict[str, Any]] = []
    for symbol in SCANNER_CONFIG.symbols:
        instrument = market.scanner.instruments[symbol]
        frames = {
            timeframe: _frame_payload(rows)
            for timeframe, rows in market.candles[symbol].items()
        }
        symbols.append(
            {
                "symbol": symbol,
                "bid": str(instrument.bid),
                "ask": str(instrument.ask),
                "spread_pct": str(instrument.spread_pct),
                "turnover_24h": str(instrument.turnover_24h),
                "timeframes": frames,
            }
        )
    payload = {
        "task": (
            "Compare every symbol and return exactly one LONG, SHORT, or WAIT decision "
            "per symbol. Confidence is 0-100. LONG/SHORT must include concrete stop_loss "
            "and take_profit around the fresh bid/ask. Do not invent symbols."
        ),
        "constraints": {
            "confidence_threshold": AI_CONFIDENCE_THRESHOLD,
            "position_notional_usdt": str(AI_POSITION_NOTIONAL),
            "leverage": str(AI_LEVERAGE),
            "maximum_open_positions": AI_MAX_POSITIONS,
            "closed_candles_only": True,
        },
        "account": {
            "equity": str(market.scanner.account.equity),
            "available_balance": str(market.scanner.account.available_balance),
            "positions": market.positions,
            "open_orders": sorted(market.scanner.account.open_order_ids),
        },
        "symbols": symbols,
    }
    prompt = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return prompt, sha256(prompt.encode()).hexdigest()


def _frame_payload(rows: tuple[ClosedCandle, ...]) -> dict[str, Any]:
    highs = [item.high for item in rows]
    lows = [item.low for item in rows]
    closes = [item.close for item in rows]
    volumes = [item.volume for item in rows]
    indicators = indicator_snapshot(highs, lows, closes)
    macd_signal = _macd_signal(closes)
    returns = [
        (closes[index] / closes[index - 1] - 1)
        for index in range(max(1, len(closes) - 20), len(closes))
        if closes[index - 1] > 0
    ]
    mean_return = sum(returns, Decimal()) / Decimal(len(returns)) if returns else Decimal()
    variance = (
        sum((item - mean_return) ** 2 for item in returns) / Decimal(len(returns))
        if returns
        else Decimal()
    )
    latest = closes[-1]
    moves = {
        str(count): str(latest / closes[-1 - count] - 1)
        for count in (1, 3, 12)
        if len(closes) > count and closes[-1 - count] > 0
    }
    return {
        "last_closed_at": rows[-1].closed_at.isoformat(),
        "indicators": {
            "rsi_14": _value(indicators.rsi_14),
            "ema_9": _value(indicators.ema_9),
            "ema_21": _value(indicators.ema_21),
            "ema_50": _value(indicators.ema_50),
            "macd": _value(indicators.macd),
            "macd_signal": _value(macd_signal),
            "atr_14": _value(indicators.atr_14),
            "bollinger_upper": _value(indicators.bollinger_upper),
            "bollinger_lower": _value(indicators.bollinger_lower),
            "realized_volatility_20": str(variance.sqrt()) if variance >= 0 else "0",
            "relative_volume_20": str(
                volumes[-1] / (sum(volumes[-20:]) / Decimal(20))
                if len(volumes) >= 20 and sum(volumes[-20:]) > 0
                else Decimal()
            ),
            "price_moves": moves,
        },
        "candles": [
            [
                item.opened_at.isoformat(),
                str(item.open),
                str(item.high),
                str(item.low),
                str(item.close),
                str(item.volume),
            ]
            for item in rows[-40:]
        ],
    }


def _macd_signal(closes: list[Decimal]) -> Decimal | None:
    if len(closes) < 35:
        return None
    macd_series: list[Decimal] = []
    for end in range(26, len(closes) + 1):
        fast = ema(closes[:end], 12)
        slow = ema(closes[:end], 26)
        if fast is not None and slow is not None:
            macd_series.append(fast - slow)
    return ema(macd_series, 9)


def _value(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def build_ai_preview(
    scan_id: str,
    decision: AIDecision,
    instrument: ScannerInstrument,
    taker_fee_rate: Decimal,
) -> ManualExecutionPreview:
    if decision.symbol != instrument.symbol or not instrument.enabled:
        raise ControlledLiveBlocked(instrument.exclusion_reason or "Instrument is disabled")
    if decision.action == "WAIT" or decision.confidence < AI_CONFIDENCE_THRESHOLD:
        raise ControlledLiveBlocked("AI decision is below the execution threshold")
    side = OrderSide.BUY if decision.action == "LONG" else OrderSide.SELL
    entry = instrument.ask if side is OrderSide.BUY else instrument.bid
    if entry <= 0 or instrument.quantity_step <= 0 or instrument.tick_size <= 0:
        raise ControlledLiveBlocked("Fresh instrument price/precision is invalid")
    quantity = (
        (AI_POSITION_NOTIONAL / entry) / instrument.quantity_step
    ).to_integral_value(rounding=ROUND_DOWN) * instrument.quantity_step
    notional = quantity * entry
    if quantity < instrument.minimum_quantity or notional < instrument.minimum_notional:
        raise ControlledLiveBlocked("$15 does not meet current Bybit quantity/notional limits")
    if notional <= 0 or notional > AI_POSITION_NOTIONAL:
        raise ControlledLiveBlocked("AI position notional exceeds $15")
    raw_stop = Decimal(str(decision.stop_loss))
    raw_target = Decimal(str(decision.take_profit))
    stop, target = _validated_levels(
        side, entry, raw_stop, raw_target, instrument.tick_size
    )
    try:
        costs = estimate_live_costs(
            side=side,
            quantity=quantity,
            entry=entry,
            stop=stop,
            target=target,
            bid=instrument.bid,
            ask=instrument.ask,
            taker_fee_rate=taker_fee_rate,
            slippage_per_leg=CONTROLLED_LIVE_V1.estimated_slippage_per_leg,
        )
        require_cost_aware_edge(costs)
    except ValueError as error:
        raise ControlledLiveBlocked(str(error)) from error
    proposal_id = sha256(
        f"{AI_SIGNAL_SOURCE}:{scan_id}:{decision.symbol}:{decision.action}".encode()
    ).hexdigest()[:32]
    return ManualExecutionPreview(
        proposal_id=proposal_id,
        profile_name=CONTROLLED_LIVE_V1.name,
        profile_hash=CONTROLLED_LIVE_V1.config_hash,
        selection_hash=scanner_selection_hash(decision.symbol),
        source=AI_SIGNAL_SOURCE,
        signal_score=decision.confidence,
        symbol=decision.symbol,
        side=side.value,
        quantity=quantity,
        expected_notional=notional,
        leverage=AI_LEVERAGE,
        expected_fee=costs.entry_fee + costs.target_exit_fee,
        estimated_slippage=costs.target_slippage,
        stop_loss=stop,
        take_profit=target,
        maximum_planned_loss=costs.net_risk,
        risk_reward_ratio=costs.net_rr,
        executable=True,
        reason=decision.reason,
        taker_fee_rate=costs.taker_fee_rate,
        estimated_spread=costs.spread_cost,
        expected_net_edge=costs.net_reward,
    )


def _frame_trend(rows: tuple[ClosedCandle, ...]) -> Literal["BULLISH", "BEARISH", "MIXED"]:
    closes = [item.close for item in rows]
    ema_21 = ema(closes, 21)
    ema_50 = ema(closes, 50)
    if ema_21 is None or ema_50 is None or len(closes) < 4 or closes[-4] <= 0:
        return "MIXED"
    momentum = closes[-1] / closes[-4] - 1
    if closes[-1] < ema_21 < ema_50 and momentum < 0:
        return "BEARISH"
    if closes[-1] > ema_21 > ema_50 and momentum > 0:
        return "BULLISH"
    return "MIXED"


def require_trend_confirmation(
    decision: AIDecision, frames: dict[str, tuple[ClosedCandle, ...]]
) -> None:
    higher = (_frame_trend(frames["15m"]), _frame_trend(frames["1h"]))
    if decision.action == "LONG" and higher == ("BEARISH", "BEARISH"):
        raise ControlledLiveBlocked("LONG blocked by bearish 15m + 1h EMA/momentum trend")
    if decision.action == "SHORT" and higher == ("BULLISH", "BULLISH"):
        raise ControlledLiveBlocked("SHORT blocked by bullish 15m + 1h EMA/momentum trend")


def require_same_symbol_cooldown(
    decision: AIDecision,
    recent_closes: tuple[RecentClosedPosition, ...],
    now: datetime,
) -> None:
    latest = next(
        (
            item
            for item in reversed(recent_closes)
            if item.symbol == decision.symbol and item.closed_at <= now
        ),
        None,
    )
    if latest is None or now - latest.closed_at >= SAME_SYMBOL_COOLDOWN:
        return
    if latest.exit_reason == "SL":
        raise ControlledLiveBlocked("Same-symbol 60m cooldown after Stop Loss")
    if latest.direction != decision.action:
        raise ControlledLiveBlocked("Opposite same-symbol entry is blocked for 60 minutes")


def _validated_levels(
    side: OrderSide,
    entry: Decimal,
    stop: Decimal,
    target: Decimal,
    tick: Decimal,
) -> tuple[Decimal, Decimal]:
    if side is OrderSide.BUY:
        stop = (stop / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
        target = (target / tick).to_integral_value(rounding=ROUND_CEILING) * tick
        valid = stop < entry < target
    else:
        stop = (stop / tick).to_integral_value(rounding=ROUND_CEILING) * tick
        target = (target / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
        valid = target < entry < stop
    if not valid:
        raise ControlledLiveBlocked("AI SL/TP ordering is invalid for the selected side")
    minimum = max(entry * MIN_LEVEL_DISTANCE_PCT, tick * 2)
    if abs(entry - stop) < minimum or abs(target - entry) < minimum:
        raise ControlledLiveBlocked("AI SL/TP distance is too small")
    if (
        abs(entry - stop) > entry * MAX_LEVEL_DISTANCE_PCT
        or abs(target - entry) > entry * MAX_LEVEL_DISTANCE_PCT
    ):
        raise ControlledLiveBlocked("AI SL/TP distance exceeds the technical safety bound")
    return stop, target


def save_ai_proposal(
    repository: ControlledLiveRepository,
    preview: ManualExecutionPreview,
    audit_admin_id: int,
) -> None:
    now = datetime.now(UTC)
    repository.state()
    try:
        with repository.session_factory.begin() as session:
            if session.get(ControlledLiveProposalRecord, preview.proposal_id) is not None:
                return
            session.add(
                ControlledLiveProposalRecord(
                    proposal_id=preview.proposal_id,
                    proposal_hash=preview.proposal_hash,
                    profile_name=preview.profile_name,
                    profile_hash=preview.profile_hash,
                    selection_hash=preview.selection_hash,
                    admin_telegram_id=audit_admin_id,
                    source=AI_SIGNAL_SOURCE,
                    preview_json=json.dumps(preview.safe_dict(), sort_keys=True),
                    status="APPROVED",
                    client_order_id=preview.client_order_id,
                    approved_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
    except IntegrityError as error:
        raise ControlledLiveBlocked("Duplicate deterministic AI proposal") from error


def format_ai_live_status_ru(status: AILiveStatus) -> str:
    execution = "ENABLED" if status.enabled and status.status == "RUNNING" else "DISABLED"
    lines = [
        "🤖 <b>AI LIVE TRADING</b>",
        "",
        f"🟢 Bot: <b>{status.status}</b>",
        f"💰 Real trading: <b>{execution}</b>",
        f"🤖 AI: <b>{'CONNECTED' if status.status == 'RUNNING' else status.status}</b>",
        f"AI model: {status.model}",
        f"💵 Equity: {_money(status.equity)}",
        f"Available: {_money(status.available_balance)}",
        f"📊 Open positions: {status.open_positions} / {AI_MAX_POSITIONS}",
        f"Open orders: {status.open_orders}",
        "🧱 Margin: ISOLATED",
        f"⚡ Leverage: {AI_LEVERAGE}x",
        f"💰 Position size: ~${AI_POSITION_NOTIONAL}",
        f"AI confidence threshold: {AI_CONFIDENCE_THRESHOLD}",
        "NET R/R gate: ≥ 1.5 after fees/spread/slippage",
        "Same-symbol reversal/SL cooldown: 60m",
        "15m + 1h countertrend filter: ACTIVE",
        "Scan interval: 5m",
        f"Last scan: {_time(status.last_scan_at)}",
        f"Total scans: {status.total_scans}",
        "",
        "🤖 <b>AI АНАЛИЗ</b>",
    ]
    if status.decisions:
        lines.extend(
            f"{item['symbol']} — {item['action']} — {item['confidence']}% — "
            f"{item['disposition']}"
            for item in status.decisions
        )
    else:
        lines.append("Решений пока нет.")
    if status.last_error:
        lines.extend(("", f"⚠️ {status.last_error}"))
    return "\n".join(lines)


def format_ai_positions_ru(status: AILiveStatus) -> str:
    lines = ["📈 <b>РЕАЛЬНЫЕ BYBIT ПОЗИЦИИ</b>", ""]
    if not status.positions:
        lines.append("Открытых позиций нет.")
    for item in status.positions:
        lines.extend(
            (
                f"{item.get('symbol')} — {item.get('side')}",
                f"Entry: {item.get('entry_price')}",
                f"Current: {item.get('mark_price')}",
                f"Size: {item.get('size')}",
                f"Leverage: {item.get('leverage')}x",
                f"SL: {item.get('stop_loss')}",
                f"TP: {item.get('take_profit')}",
                f"Unrealized PnL: {item.get('unrealized_pnl')}",
                "",
            )
        )
    return "\n".join(lines)


def _money(value: Decimal | None) -> str:
    return f"${value.quantize(Decimal('0.0001'))}" if value is not None else "N/A"


def _time(value: datetime | None) -> str:
    if value is None:
        return "N/A"
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat()


class AIAutonomousExecutionService:
    """Executes only a validated durable AI preview through the guarded gateway."""

    def __init__(self, repository: ControlledLiveRepository, gateway: Any) -> None:
        self.repository = repository
        self.gateway = gateway

    async def execute(
        self, preview: ManualExecutionPreview, *, account_id: str
    ) -> Any:
        if preview.source != AI_SIGNAL_SOURCE:
            raise ControlledLiveBlocked("Autonomous executor accepts only AI proposals")
        self.repository.claim_ai_submission(preview, account_id)
        try:
            fill = await self.gateway.submit_market(preview, preview.client_order_id)
        except ControlledLiveBlocked as error:
            self.repository.mark_ai_rejected(preview, error)
            raise
        except OrderRejected as error:
            self.repository.mark_ai_rejected(preview, error)
            raise
        except Exception as error:
            self.repository.mark_ai_unknown(preview, error)
            self.repository.activate_kill_switch()
            raise RuntimeError(
                "AI order outcome UNKNOWN; kill switch activated and retry forbidden"
            ) from error
        if fill.filled_quantity <= 0:
            error = RuntimeError("Bybit returned no confirmed fill")
            self.repository.mark_ai_unknown(preview, error)
            self.repository.activate_kill_switch()
            raise error
        self.repository.mark_ai_filled(preview, fill)
        try:
            await self.gateway.install_native_protection(
                fill,
                symbol=preview.symbol,
                stop_loss=preview.stop_loss,
                take_profit=preview.take_profit,
                reduce_only=True,
            )
            protected = await self.gateway.verify_native_protection(
                preview.symbol, preview.stop_loss, preview.take_profit
            )
            if not protected:
                raise RuntimeError("Bybit did not confirm native SL and TP")
        except Exception as protection_error:
            try:
                await self.gateway.emergency_close_reduce_only(fill, preview.symbol)
            except Exception as close_error:
                self.repository.mark_ai_unknown(preview, close_error)
                self.repository.activate_kill_switch()
                raise RuntimeError(
                    "UNPROTECTED POSITION: emergency reduce-only close failed"
                ) from close_error
            self.repository.mark_ai_emergency_closed(preview, protection_error)
            self.repository.activate_kill_switch()
            raise RuntimeError(
                "Native SL/TP failed; position emergency-closed and kill switch activated"
            ) from protection_error
        snapshot = await self.gateway.snapshot()
        fill_match = fill.order_id in snapshot.fill_order_ids
        position_match = any(
            item.position_id == fill.position_id
            and item.symbol == preview.symbol
            and item.quantity == fill.filled_quantity
            for item in snapshot.positions
        )
        if not fill_match or not position_match:
            error = RuntimeError("Post-protection Bybit reconciliation mismatch")
            self.repository.mark_ai_unknown(preview, error)
            self.repository.activate_kill_switch()
            raise error
        self.repository.mark_ai_protected(preview)
        return fill


class AIAutonomousTrader:
    def __init__(
        self,
        settings: Settings,
        session_factory,
        provider: OpenAICompatibleProvider,
        market_reader: AIMarketDataReader,
        gateway: Any,
        notifier: Any,
    ) -> None:
        self.settings = settings
        self.repository = AILiveRepository(session_factory)
        self.controlled = ControlledLiveRepository(session_factory)
        self.provider = provider
        self.market_reader = market_reader
        self.executor = AIAutonomousExecutionService(self.controlled, gateway)
        self.notifier = notifier

    async def cycle(self, now: datetime | None = None) -> dict[str, Any]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        scheduled = current.replace(
            minute=(current.minute // 5) * 5, second=0, microsecond=0
        )
        scan_id = self.repository.begin_scan(scheduled, self.settings.ai_model)
        if scan_id is None:
            self.repository.heartbeat()
            return {"status": "ALREADY_SCANNED", "scheduled_at": scheduled.isoformat()}
        try:
            market = await self.market_reader.read(current)
            prompt, request_hash = build_ai_prompt(market)
            raw = await self.provider.complete_json(prompt, AIBatchDecision)
            result = AIBatchDecision.model_validate(raw)
            if {item.symbol for item in result.decisions} != set(SCANNER_CONFIG.symbols):
                raise ValueError("AI must return exactly one decision for every symbol")
            fee_rates = await self._fee_rates(current)
            recent_closes = await self.market_reader.recent_closes()
            next_scan = scheduled + timedelta(seconds=self.settings.ai_scan_interval_seconds)
            self.repository.complete_scan(scan_id, request_hash, result, market, next_scan)
            executions = await self._execute_candidates(
                scan_id, result, market, fee_rates, recent_closes
            )
            return {
                "status": "COMPLETED",
                "scan_id": scan_id,
                "decisions": {
                    item.symbol: f"{item.action}:{item.confidence}"
                    for item in result.decisions
                },
                "executions": executions,
            }
        except Exception as error:
            self.repository.fail_scan(scan_id, error)
            return {
                "status": "FAILED",
                "scan_id": scan_id,
                "error": f"{type(error).__name__}: {error}",
            }

    async def _execute_candidates(
        self,
        scan_id: str,
        result: AIBatchDecision,
        market: AIMarketSnapshot,
        fee_rates: dict[str, BybitFeeRateSnapshot],
        recent_closes: tuple[RecentClosedPosition, ...],
    ) -> list[dict[str, Any]]:
        candidates = [
            item
            for item in result.decisions
            if item.action != "WAIT" and item.confidence >= AI_CONFIDENCE_THRESHOLD
        ]
        candidates.sort(
            key=lambda item: (
                -item.confidence,
                market.scanner.instruments[item.symbol].spread_pct,
                -market.scanner.instruments[item.symbol].turnover_24h,
                item.symbol,
            )
        )
        open_symbols = set(market.scanner.account.open_position_symbols)
        free_slots = max(0, AI_MAX_POSITIONS - market.scanner.account.open_positions)
        outcomes: list[dict[str, Any]] = []
        audit_admin = min(self.settings.admin_telegram_ids)
        for decision in candidates:
            if free_slots <= 0:
                self.repository.set_disposition(
                    scan_id, decision.symbol, "BLOCKED_MAX_POSITIONS"
                )
                continue
            if decision.symbol in open_symbols:
                self.repository.set_disposition(
                    scan_id, decision.symbol, "BLOCKED_EXISTING_POSITION"
                )
                continue
            try:
                require_same_symbol_cooldown(decision, recent_closes, market.fetched_at)
                require_trend_confirmation(decision, market.candles[decision.symbol])
                preview = build_ai_preview(
                    scan_id,
                    decision,
                    market.scanner.instruments[decision.symbol],
                    fee_rates[decision.symbol].taker_fee_rate,
                )
                if (
                    preview.expected_notional / AI_LEVERAGE + preview.expected_fee
                    > market.scanner.account.available_balance
                ):
                    raise ControlledLiveBlocked("Insufficient available balance")
                save_ai_proposal(self.controlled, preview, audit_admin)
                self.repository.set_disposition(
                    scan_id,
                    decision.symbol,
                    "SUBMITTING",
                    proposal_id=preview.proposal_id,
                )
                fill = await self.executor.execute(
                    preview, account_id="bybit-mainnet-unified"
                )
                self.repository.set_disposition(
                    scan_id,
                    decision.symbol,
                    "FILLED_PROTECTED",
                    proposal_id=preview.proposal_id,
                )
                await self.notifier.ai_order_opened(preview, fill)
                free_slots -= 1
                open_symbols.add(decision.symbol)
                outcomes.append(
                    {
                        "symbol": decision.symbol,
                        "status": "FILLED_PROTECTED",
                        "order_id": fill.order_id,
                    }
                )
            except ControlledLiveBlocked as error:
                self.repository.set_disposition(
                    scan_id, decision.symbol, "BLOCKED", error=error
                )
                outcomes.append(
                    {"symbol": decision.symbol, "status": "BLOCKED", "reason": str(error)}
                )
            except Exception as error:
                self.repository.set_disposition(
                    scan_id, decision.symbol, "FAILED_CLOSED", error=error
                )
                outcomes.append(
                    {
                        "symbol": decision.symbol,
                        "status": "FAILED_CLOSED",
                        "reason": str(error),
                    }
                )
                break
        return outcomes

    async def _fee_rates(
        self, now: datetime
    ) -> dict[str, BybitFeeRateSnapshot]:
        try:
            rates = await self.market_reader.fee_rates(now)
            self.repository.save_fee_rates(rates)
            return rates
        except Exception as error:
            cached = self.repository.cached_fee_rates(now)
            missing = ALLOWED_SCANNER_SYMBOLS - set(cached)
            if missing:
                raise ControlledLiveBlocked(
                    "Bybit fee API unavailable and no fresh persisted fee cache exists"
                ) from error
            return cached

    async def close(self) -> None:
        await self.market_reader.close()
