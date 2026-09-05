"""Deterministic, AI-free profit protection for bot-owned real positions."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import defaultdict, deque
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, Protocol

import websockets
from sqlalchemy import select

from app.db import (
    ControlledLiveProposalRecord,
    ExecutionOrderRecord,
    PositionProfitStateRecord,
    PositionProtectionEventRecord,
)
from app.trading.controlled_universe import SCANNER_CONFIG
from app.trading.controlled_live import CONTROLLED_LIVE_V1
from app.trading.execution_store import OrderOutcomeUnknown


logger = logging.getLogger(__name__)
PUBLIC_LINEAR_WS = "wss://stream.bybit.com/v5/public/linear"
BREAK_EVEN_R = Decimal("0.5")
PROFIT_LOCK_R = Decimal("1")
LOCKED_R = Decimal("0.3")
REVERSAL_RETRACE_R = Decimal("0.5")


def D(value: Any) -> Decimal:
    return Decimal(str(value or "0"))


@dataclass(frozen=True)
class ClosedBar:
    opened_at_ms: int
    close: Decimal
    high: Decimal
    low: Decimal


@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    side: str
    quantity: Decimal
    entry_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    position_idx: int
    opened_at: datetime
    entry_client_order_id: str
    entry_fee_usdt: Decimal
    taker_fee_rate: Decimal
    tick_size: Decimal


@dataclass(frozen=True)
class ProtectionDecision:
    action: str
    stop_loss: Decimal | None
    reason: str
    current_net_pnl: Decimal
    current_r: Decimal
    max_favorable_r: Decimal


class ProfitProtectionGateway(Protocol):
    async def read_open_positions(self) -> list[dict[str, Any]]: ...
    async def read_ticker_details(self, symbol: str) -> dict[str, Any]: ...
    async def read_closed_klines(self, symbol: str, *, interval: str, limit: int) -> list[list[Any]]: ...
    async def current_instrument_state(self, symbol: str): ...
    async def account_taker_fee_rate(self, symbol: str) -> Decimal: ...
    async def query_executions(self, *, client_order_id: str, symbol: str) -> list[dict[str, Any]]: ...
    async def manage_native_protection(self, *, symbol: str, stop_loss: Decimal, client_order_id: str) -> None: ...
    async def protective_reduce_only_close(self, *, symbol: str, quantity: Decimal, client_order_id: str) -> dict[str, Any]: ...


def pnl_before_costs(side: str, entry: Decimal, exit_price: Decimal, quantity: Decimal) -> Decimal:
    direction = Decimal("1") if side == "Buy" else Decimal("-1")
    return (exit_price - entry) * quantity * direction


def estimated_close_cost(
    exit_price: Decimal,
    quantity: Decimal,
    taker_fee_rate: Decimal,
    slippage_per_leg: Decimal,
) -> Decimal:
    return exit_price * quantity * (taker_fee_rate + slippage_per_leg)


def net_pnl(
    side: str,
    entry: Decimal,
    executable_exit: Decimal,
    quantity: Decimal,
    entry_fee: Decimal,
    taker_fee_rate: Decimal,
    slippage_per_leg: Decimal,
) -> Decimal:
    return pnl_before_costs(side, entry, executable_exit, quantity) - entry_fee - estimated_close_cost(
        executable_exit, quantity, taker_fee_rate, slippage_per_leg
    )


def initial_risk_usdt(position: PositionSnapshot, slippage_per_leg: Decimal) -> Decimal:
    gross = abs(position.entry_price - position.stop_loss) * position.quantity
    return gross + position.entry_fee_usdt + estimated_close_cost(
        position.stop_loss,
        position.quantity,
        position.taker_fee_rate,
        slippage_per_leg,
    )


def atr(bars: list[ClosedBar], period: int = 14) -> Decimal | None:
    if len(bars) < period + 1:
        return None
    ranges: list[Decimal] = []
    for previous, current in zip(bars[-period - 1 : -1], bars[-period:], strict=True):
        ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return sum(ranges, Decimal()) / Decimal(period)


def ema(values: list[Decimal], period: int) -> Decimal | None:
    if len(values) < period:
        return None
    value = sum(values[:period], Decimal()) / Decimal(period)
    alpha = Decimal("2") / Decimal(period + 1)
    for item in values[period:]:
        value = item * alpha + value * (Decimal("1") - alpha)
    return value


def adverse_momentum_reversal(side: str, bars: list[ClosedBar], current_atr: Decimal) -> bool:
    """Require three confirmed adverse closes plus EMA9/ATR confirmation."""
    if len(bars) < 12 or current_atr <= 0:
        return False
    closes = [item.close for item in bars]
    last = closes[-1]
    average = ema(closes, 9)
    if average is None:
        return False
    last_four = closes[-4:]
    if side == "Buy":
        sequence = all(a > b for a, b in zip(last_four, last_four[1:], strict=False))
        impulse = last_four[0] - last >= current_atr * Decimal("0.5")
        return sequence and last < average and impulse
    sequence = all(a < b for a, b in zip(last_four, last_four[1:], strict=False))
    impulse = last - last_four[0] >= current_atr * Decimal("0.5")
    return sequence and last > average and impulse


def _round_protective(value: Decimal, tick: Decimal, side: str) -> Decimal:
    if tick <= 0:
        return value
    rounding = ROUND_FLOOR if side == "Buy" else ROUND_CEILING
    return (value / tick).to_integral_value(rounding=rounding) * tick


def _tightens(side: str, proposed: Decimal, confirmed: Decimal) -> bool:
    return proposed > confirmed if side == "Buy" else proposed < confirmed


def _stop_for_net_profit(
    position: PositionSnapshot,
    desired_profit: Decimal,
    slippage_per_leg: Decimal,
) -> Decimal:
    # Fee depends on the stop itself. Solve the linear equation instead of
    # iterating, then round conservatively to the exchange tick.
    q = position.quantity
    rate = position.taker_fee_rate + slippage_per_leg
    if position.side == "Buy":
        raw = (desired_profit + position.entry_fee_usdt + q * position.entry_price) / (
            q * (Decimal("1") - rate)
        )
    else:
        raw = (q * position.entry_price - desired_profit - position.entry_fee_usdt) / (
            q * (Decimal("1") + rate)
        )
    return _round_protective(raw, position.tick_size, position.side)


def evaluate_protection(
    position: PositionSnapshot,
    *,
    bid: Decimal,
    ask: Decimal,
    confirmed_stop: Decimal,
    previous_mfe: Decimal,
    stage: str,
    bars: list[ClosedBar],
    slippage_per_leg: Decimal,
) -> ProtectionDecision:
    executable = bid if position.side == "Buy" else ask
    current = net_pnl(
        position.side,
        position.entry_price,
        executable,
        position.quantity,
        position.entry_fee_usdt,
        position.taker_fee_rate,
        slippage_per_leg,
    )
    risk = initial_risk_usdt(position, slippage_per_leg)
    mfe = max(previous_mfe, current)
    current_r = current / risk if risk > 0 else Decimal()
    mfe_r = mfe / risk if risk > 0 else Decimal()
    current_atr = atr(bars)

    if (
        mfe_r >= PROFIT_LOCK_R
        and mfe - current >= risk * REVERSAL_RETRACE_R
        and current > 0
        and current_atr is not None
        and adverse_momentum_reversal(position.side, bars, current_atr)
    ):
        return ProtectionDecision(
            "EARLY_PROFIT_EXIT", None, "MFE retracement with confirmed adverse 5m momentum", current, current_r, mfe_r
        )

    candidate: Decimal | None = None
    action = "NONE"
    reason = "Profit threshold not reached"
    if current_r >= PROFIT_LOCK_R and current_atr is not None:
        locked = _stop_for_net_profit(position, risk * LOCKED_R, slippage_per_leg)
        volatility = current_atr / executable if executable > 0 else Decimal()
        multiplier = Decimal("2") if volatility >= Decimal("0.01") else Decimal("1.5")
        trail = executable - current_atr * multiplier if position.side == "Buy" else executable + current_atr * multiplier
        trail = _round_protective(trail, position.tick_size, position.side)
        candidate = max(locked, trail) if position.side == "Buy" else min(locked, trail)
        action = "PROFIT_LOCK" if stage in {"INITIAL", "BREAK_EVEN"} else "TRAILING_UPDATE"
        reason = f"net PnL reached +1R; ATR trailing multiplier={multiplier}"
    elif current_r >= BREAK_EVEN_R:
        candidate = _stop_for_net_profit(position, Decimal(), slippage_per_leg)
        action = "BREAK_EVEN"
        reason = "net PnL reached +0.5R; stop covers confirmed/estimated costs"

    if candidate is None or not _tightens(position.side, candidate, confirmed_stop):
        return ProtectionDecision("NONE", None, reason, current, current_r, mfe_r)
    # Never submit a stop already through the executable side of the market.
    ceiling = bid - position.tick_size if position.side == "Buy" else ask + position.tick_size
    if position.side == "Buy":
        candidate = min(candidate, ceiling)
    else:
        candidate = max(candidate, ceiling)
    if not _tightens(position.side, candidate, confirmed_stop):
        return ProtectionDecision("NONE", None, "Rounded stop does not tighten protection", current, current_r, mfe_r)
    return ProtectionDecision(action, candidate, reason, current, current_r, mfe_r)


class PositionProfitRepository:
    def __init__(self, session_factory) -> None:
        self._sessions = session_factory

    def owner_for_symbol(self, symbol: str) -> tuple[ControlledLiveProposalRecord, dict[str, Any]] | None:
        with self._sessions() as session:
            candidates = session.scalars(
                select(ControlledLiveProposalRecord)
                .where(ControlledLiveProposalRecord.status == "PROTECTED")
                .order_by(ControlledLiveProposalRecord.completed_at.desc())
            ).all()
            for proposal in candidates:
                preview = json.loads(proposal.preview_json)
                if preview.get("symbol") != symbol:
                    continue
                ledger = session.scalar(
                    select(ExecutionOrderRecord).where(
                        ExecutionOrderRecord.client_order_id == proposal.client_order_id,
                        ExecutionOrderRecord.status == "FILLED_PROTECTED",
                    )
                )
                if ledger is not None:
                    session.expunge(proposal)
                    return proposal, preview
        return None

    def load(self, client_id: str) -> PositionProfitStateRecord | None:
        with self._sessions() as session:
            row = session.get(PositionProfitStateRecord, client_id)
            if row is not None:
                session.expunge(row)
            return row

    def upsert(
        self,
        position: PositionSnapshot,
        current_price: Decimal,
        risk: Decimal,
        exit_cost: Decimal,
        confirmed_stop: Decimal,
    ) -> PositionProfitStateRecord:
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            row = session.get(PositionProfitStateRecord, position.entry_client_order_id)
            if row is None:
                row = PositionProfitStateRecord(
                    entry_client_order_id=position.entry_client_order_id,
                    position_key=f"{position.symbol}:{position.position_idx}",
                    symbol=position.symbol,
                    side=position.side,
                    quantity=position.quantity,
                    entry_price=position.entry_price,
                    initial_stop_loss=position.stop_loss,
                    initial_take_profit=position.take_profit,
                    initial_risk_usdt=risk,
                    entry_fee_usdt=position.entry_fee_usdt,
                    estimated_exit_cost_usdt=exit_cost,
                    current_price=current_price,
                    current_net_pnl=Decimal(),
                    max_favorable_price=current_price,
                    max_favorable_excursion_usdt=Decimal(),
                    max_favorable_r=Decimal(),
                    confirmed_stop_loss=confirmed_stop,
                    stage="INITIAL",
                    opened_at=position.opened_at,
                    last_observed_at=now,
                    updated_at=now,
                )
                session.add(row)
            session.flush()
            session.expunge(row)
            return row

    def observe(self, client_id: str, price: Decimal, decision: ProtectionDecision) -> None:
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            row = session.get(PositionProfitStateRecord, client_id)
            if row is None:
                return
            row.current_price = price
            row.current_net_pnl = decision.current_net_pnl
            if decision.max_favorable_r > Decimal(row.max_favorable_r):
                row.max_favorable_r = decision.max_favorable_r
                row.max_favorable_excursion_usdt = max(
                    Decimal(row.max_favorable_excursion_usdt), decision.current_net_pnl
                )
                row.max_favorable_price = price
            row.last_observed_at = now
            row.updated_at = now

    def begin_event(self, client_id: str, decision: ProtectionDecision, take_profit: Decimal) -> str | None:
        now = datetime.now(UTC)
        event_id = uuid.uuid4().hex
        with self._sessions.begin() as session:
            existing = session.scalar(
                select(PositionProtectionEventRecord).where(
                    PositionProtectionEventRecord.entry_client_order_id == client_id,
                    PositionProtectionEventRecord.action == decision.action,
                    PositionProtectionEventRecord.requested_stop_loss == decision.stop_loss,
                )
            )
            if existing is not None:
                return None
            session.add(
                PositionProtectionEventRecord(
                    event_id=event_id,
                    entry_client_order_id=client_id,
                    action=decision.action,
                    requested_stop_loss=decision.stop_loss,
                    preserved_take_profit=take_profit,
                    status="PENDING",
                    reason=decision.reason,
                    requested_at=now,
                    updated_at=now,
                )
            )
        return event_id

    def finish_event(self, event_id: str, *, status: str, stop_loss: Decimal | None = None) -> None:
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            event = session.get(PositionProtectionEventRecord, event_id)
            if event is None:
                return
            event.status = status
            event.confirmed_at = now if status == "CONFIRMED" else None
            event.updated_at = now
            state = session.get(PositionProfitStateRecord, event.entry_client_order_id)
            if state is not None and status == "CONFIRMED":
                if stop_loss is not None:
                    state.confirmed_stop_loss = stop_loss
                state.stage = event.action
                state.updated_at = now

    def mark_absent_closed(self, open_symbols: set[str]) -> None:
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            rows = session.scalars(
                select(PositionProfitStateRecord).where(PositionProfitStateRecord.closed_at.is_(None))
            ).all()
            for row in rows:
                if row.symbol not in open_symbols:
                    row.closed_at = now
                    row.updated_at = now
                    events = session.scalars(
                        select(PositionProtectionEventRecord).where(
                            PositionProtectionEventRecord.entry_client_order_id
                            == row.entry_client_order_id,
                            PositionProtectionEventRecord.action == "EARLY_PROFIT_EXIT",
                            PositionProtectionEventRecord.status.in_(("PENDING", "UNKNOWN")),
                        )
                    ).all()
                    for event in events:
                        event.status = "CONFIRMED"
                        event.confirmed_at = now
                        event.updated_at = now

    def reconcile_native_stop(
        self, client_id: str, side: str, native_stop: Decimal
    ) -> None:
        """Recover a confirmed update after a process/HTTP response interruption."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            state = session.get(PositionProfitStateRecord, client_id)
            if state is None or native_stop <= 0:
                return
            current = Decimal(state.confirmed_stop_loss)
            tighter = native_stop > current if side == "Buy" else native_stop < current
            if tighter:
                state.confirmed_stop_loss = native_stop
                state.updated_at = now
            events = session.scalars(
                select(PositionProtectionEventRecord).where(
                    PositionProtectionEventRecord.entry_client_order_id == client_id,
                    PositionProtectionEventRecord.requested_stop_loss == native_stop,
                    PositionProtectionEventRecord.status.in_(("PENDING", "UNKNOWN")),
                )
            ).all()
            for event in events:
                event.status = "CONFIRMED"
                event.confirmed_at = now
                event.updated_at = now
                state.stage = event.action


class LocalPositionProfitProtector:
    """Continuously consumes public market data and manages only owned positions."""

    def __init__(self, session_factory, gateway: ProfitProtectionGateway, notifier) -> None:
        self.repository = PositionProfitRepository(session_factory)
        self.gateway = gateway
        self.notifier = notifier
        self.positions: dict[str, PositionSnapshot] = {}
        self.bars: dict[str, deque[ClosedBar]] = defaultdict(lambda: deque(maxlen=64))
        self.quotes: dict[str, tuple[Decimal, Decimal]] = {}
        self._last_sync = 0.0

    async def sync(self) -> None:
        raw_positions = await self.gateway.read_open_positions()
        current: dict[str, PositionSnapshot] = {}
        for raw in raw_positions:
            symbol = str(raw.get("symbol") or "")
            owner = self.repository.owner_for_symbol(symbol)
            if owner is None:
                logger.warning("profit_protector_skips_unowned_position", extra={"symbol": symbol})
                continue
            proposal, preview = owner
            executions = await self.gateway.query_executions(
                client_order_id=proposal.client_order_id, symbol=symbol
            )
            entry_fee = sum((abs(D(item.get("execFee"))) for item in executions), Decimal())
            filled_quantity = sum((D(item.get("execQty")) for item in executions), Decimal())
            filled_value = sum(
                (D(item.get("execQty")) * D(item.get("execPrice")) for item in executions),
                Decimal(),
            )
            fee_rate = await self.gateway.account_taker_fee_rate(symbol)
            instrument = await self.gateway.current_instrument_state(symbol)
            ticker = await self.gateway.read_ticker_details(symbol)
            tick = D(ticker.get("tickSize"))
            entry = D(raw.get("avgPrice")) or D(preview.get("entry"))
            quantity = D(raw.get("size"))
            stop = D(raw.get("stopLoss"))
            target = D(raw.get("takeProfit"))
            fill_average = filled_value / filled_quantity if filled_quantity > 0 else Decimal()
            position_key = f"{symbol}:{int(raw.get('positionIdx') or 0)}"
            proposal_completed = proposal.completed_at or proposal.created_at
            if (
                min(entry, quantity, stop, target, instrument.ask_price, tick, filled_quantity) <= 0
                or quantity > filled_quantity
                or abs(entry - fill_average) > tick
                or (proposal.position_id and proposal.position_id != position_key)
            ):
                logger.warning(
                    "profit_protector_position_ownership_mismatch", extra={"symbol": symbol}
                )
                continue
            if min(entry, quantity, stop, target, instrument.ask_price, instrument.quantity_step) <= 0:
                logger.error("profit_protector_position_missing_native_protection", extra={"symbol": symbol})
                continue
            position = PositionSnapshot(
                symbol=symbol,
                side=str(raw.get("side") or ""),
                quantity=quantity,
                entry_price=entry,
                stop_loss=D(preview.get("stop_loss")) or stop,
                take_profit=target,
                position_idx=int(raw.get("positionIdx") or 0),
                opened_at=proposal_completed,
                entry_client_order_id=proposal.client_order_id,
                entry_fee_usdt=entry_fee,
                taker_fee_rate=fee_rate,
                tick_size=tick,
            )
            price = D(ticker.get("bid1Price") if position.side == "Buy" else ticker.get("ask1Price"))
            risk = initial_risk_usdt(position, CONTROLLED_LIVE_V1.estimated_slippage_per_leg)
            exit_cost = estimated_close_cost(price, quantity, fee_rate, CONTROLLED_LIVE_V1.estimated_slippage_per_leg)
            self.repository.upsert(position, price, risk, exit_cost, stop)
            self.repository.reconcile_native_stop(
                position.entry_client_order_id, position.side, stop
            )
            if not self.bars[symbol]:
                rows = await self.gateway.read_closed_klines(symbol, interval="5", limit=40)
                for row in reversed(rows):
                    self.bars[symbol].append(
                        ClosedBar(int(row[0]), D(row[4]), D(row[2]), D(row[3]))
                    )
            current[symbol] = position
        self.positions = current
        self.repository.mark_absent_closed(set(current))

    async def on_ticker(self, symbol: str, bid: Decimal, ask: Decimal) -> None:
        position = self.positions.get(symbol)
        if position is None or bid <= 0 or ask <= 0 or ask < bid:
            return
        state = self.repository.load(position.entry_client_order_id)
        if state is None:
            return
        decision = evaluate_protection(
            position,
            bid=bid,
            ask=ask,
            confirmed_stop=Decimal(state.confirmed_stop_loss),
            previous_mfe=Decimal(state.max_favorable_excursion_usdt),
            stage=state.stage,
            bars=list(self.bars[symbol]),
            slippage_per_leg=CONTROLLED_LIVE_V1.estimated_slippage_per_leg,
        )
        executable = bid if position.side == "Buy" else ask
        self.repository.observe(position.entry_client_order_id, executable, decision)
        if decision.action == "NONE":
            return
        event_id = self.repository.begin_event(
            position.entry_client_order_id, decision, position.take_profit
        )
        if event_id is None:
            return
        try:
            if decision.action == "EARLY_PROFIT_EXIT":
                await self.gateway.protective_reduce_only_close(
                    symbol=symbol,
                    quantity=position.quantity,
                    client_order_id=position.entry_client_order_id,
                )
                self.repository.finish_event(event_id, status="CONFIRMED")
            else:
                assert decision.stop_loss is not None
                await self.gateway.manage_native_protection(
                    symbol=symbol,
                    stop_loss=decision.stop_loss,
                    client_order_id=position.entry_client_order_id,
                )
                self.repository.finish_event(
                    event_id, status="CONFIRMED", stop_loss=decision.stop_loss
                )
            await self.notifier.profit_protection(
                decision.action,
                symbol,
                decision.stop_loss,
                decision.current_net_pnl,
                decision.current_r,
            )
        except OrderOutcomeUnknown:
            self.repository.finish_event(event_id, status="UNKNOWN")
            raise
        except Exception:
            self.repository.finish_event(event_id, status="FAILED")
            raise

    @staticmethod
    async def _send_bybit_keepalive(ws: Any) -> None:
        """Use Bybit's application heartbeat instead of WebSocket control pings."""
        while True:
            await asyncio.sleep(20)
            await ws.send(json.dumps({"op": "ping"}))

    async def run(self) -> None:
        while True:
            try:
                await self.sync()
                # Subscribe to the immutable universe so a position opened after
                # this socket connects is immediately observed after the next sync.
                symbols = sorted(SCANNER_CONFIG.symbols)
                topics = [f"tickers.{symbol}" for symbol in symbols] + [f"kline.5.{symbol}" for symbol in symbols]
                # Bybit expects an application-level {"op": "ping"}. Railway's
                # network path intermittently drops WebSocket control-pong frames,
                # so relying on the library ping caused healthy streams to reconnect
                # every minute. The application heartbeat still detects a dead socket.
                async with websockets.connect(
                    PUBLIC_LINEAR_WS, ping_interval=None, close_timeout=5
                ) as ws:
                    keepalive = asyncio.create_task(self._send_bybit_keepalive(ws))
                    await ws.send(json.dumps({"op": "subscribe", "args": topics}))
                    try:
                        async for raw_message in ws:
                            message = json.loads(raw_message)
                            topic = str(message.get("topic") or "")
                            data = message.get("data")
                            if topic.startswith("tickers.") and isinstance(data, dict):
                                symbol = topic.split(".")[-1]
                                old_bid, old_ask = self.quotes.get(
                                    symbol, (Decimal(), Decimal())
                                )
                                bid = D(data.get("bid1Price")) or old_bid
                                ask = D(data.get("ask1Price")) or old_ask
                                self.quotes[symbol] = (bid, ask)
                                message_ms = int(message.get("ts") or 0)
                                if (
                                    not message_ms
                                    or int(datetime.now(UTC).timestamp() * 1000) - message_ms
                                    <= 30_000
                                ):
                                    await self.on_ticker(symbol, bid, ask)
                            elif topic.startswith("kline.5.") and isinstance(data, list):
                                symbol = topic.split(".")[-1]
                                for item in data:
                                    if item.get("confirm") is True:
                                        bar = ClosedBar(
                                            int(item["start"]),
                                            D(item["close"]),
                                            D(item["high"]),
                                            D(item["low"]),
                                        )
                                        if (
                                            not self.bars[symbol]
                                            or self.bars[symbol][-1].opened_at_ms
                                            != bar.opened_at_ms
                                        ):
                                            self.bars[symbol].append(bar)
                            now = asyncio.get_running_loop().time()
                            if now - self._last_sync >= 30:
                                await self.sync()
                                self._last_sync = now
                    finally:
                        keepalive.cancel()
                        with suppress(asyncio.CancelledError):
                            await keepalive
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("local_position_profit_protector_reconnecting")
                await asyncio.sleep(5)
