import asyncio
from dataclasses import replace
from decimal import Decimal
from typing import Protocol
from uuid import uuid4

from app.domain.models import BotState, Decision, RiskProfile, Side, TradeIntent
from app.journal import TradeJournal
from app.signals.engine import MarketFrame, SignalEngine
from app.trading.paper_broker import PaperBroker
from app.trading.positions import PositionManager


class DemoMarketDataProvider(Protocol):
    async def frames(self, symbol: str) -> dict[str, MarketFrame]: ...


class DemoAutotrader:
    """Orchestration loop for DEMO only; a RiskManager approval is mandatory on every entry."""

    def __init__(
        self,
        broker: PaperBroker | None = None,
        journal: TradeJournal | None = None,
        profile: RiskProfile | None = None,
        user_id: int = 0,
    ) -> None:
        self.broker = broker or PaperBroker()
        self.journal = journal or TradeJournal()
        self.profile = profile or RiskProfile()
        self.user_id = user_id
        self.signal_engine = SignalEngine()
        self.position_manager = PositionManager()
        self.state = BotState.PAUSED
        self._seen_setups: set[str] = set()
        self.closed_positions: list = []

    def start(self) -> None:
        if self.state is not BotState.EMERGENCY_STOP:
            self.state = BotState.ACTIVE

    def pause(self) -> None:
        if self.state is not BotState.EMERGENCY_STOP:
            self.state = BotState.PAUSED

    def emergency_stop(self, close_positions: bool = False) -> list:
        self.state = BotState.EMERGENCY_STOP
        if not close_positions:
            return []
        prices = {position.symbol: self.broker.last_price(position.symbol) for position in self.broker.positions}
        closed = self.broker.close_all(prices)
        for item in closed:
            self.journal.record_close(item)
        self.closed_positions.extend(closed)
        return closed

    def process(self, symbol: str, frames: dict[str, MarketFrame]) -> tuple[object, object | None]:
        signal = self.signal_engine.confirm_multi_timeframe(symbol, frames)
        self.journal.record_signal(signal)
        if self.state is not BotState.ACTIVE or signal.decision is Decision.WAIT:
            return signal, None
        if any(position.symbol == symbol for position in self.broker.positions):
            return signal, None
        setup_key = f"{symbol}:{signal.decision}:{signal.proposed_entry}:{signal.proposed_stop_loss}"
        if setup_key in self._seen_setups:
            return signal, None
        side = Side(signal.decision)
        account = self.broker.account()
        intent = TradeIntent(
            trade_id=uuid4().hex,
            user_id=self.user_id,
            symbol=symbol,
            side=side,
            entry=signal.proposed_entry,
            stop_loss=signal.proposed_stop_loss,
            take_profit=signal.proposed_take_profit,
            equity=account.equity,
            available_balance=account.available_balance,
            daily_realized_pnl=Decimal("0"),
            open_positions=len(self.broker.positions),
            consecutive_losses=0,
        )
        try:
            position = self.broker.open_market(intent, self.profile)
        except PermissionError:
            return signal, None
        position = replace(position, trailing_distance=position.initial_risk)
        self.broker.replace_position(position)
        self._seen_setups.add(setup_key)
        self.journal.record_open(position, signal)
        return signal, position

    def on_price(self, symbol: str, price: Decimal) -> list:
        closed = self.position_manager.on_price(self.broker, symbol, price)
        for item in closed:
            self.journal.record_close(item)
        self.closed_positions.extend(closed)
        return closed

    async def run(self, provider: DemoMarketDataProvider, symbols: tuple[str, ...], interval: float = 5.0) -> None:
        while self.state is not BotState.EMERGENCY_STOP:
            if self.state is BotState.ACTIVE:
                for symbol in symbols:
                    frames = await provider.frames(symbol)
                    self.process(symbol, frames)
                    self.on_price(symbol, frames["5M"].price)
            await asyncio.sleep(interval)
