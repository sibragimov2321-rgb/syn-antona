import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from app.db import SessionLocal, Trade, TradeEvent
from app.domain.models import ClosedPosition, Position, Signal


@dataclass(frozen=True)
class JournalRecord:
    timestamp: datetime
    event_type: str
    symbol: str
    trade_id: str | None
    payload: dict


class TradeJournal:
    """Audit journal. WAIT decisions are first-class events, not discarded signals."""

    def __init__(self, persistent: bool = False) -> None:
        self._records: list[JournalRecord] = []
        self._persistent = persistent

    @property
    def records(self) -> tuple[JournalRecord, ...]:
        return tuple(self._records)

    def record_signal(self, signal: Signal, indicators: dict[str, str] | None = None) -> None:
        payload = {
            "decision": signal.decision,
            "signal_score": signal.signal_score,
            "trend_score": signal.trend_score,
            "momentum_score": signal.momentum_score,
            "volatility_score": signal.volatility_score,
            "reasons": signal.reasons,
            "entry": signal.proposed_entry,
            "stop_loss": signal.proposed_stop_loss,
            "take_profit": signal.proposed_take_profit,
            "risk_reward": signal.risk_reward_ratio,
            "indicators": indicators or {},
        }
        self._record("SIGNAL", signal.symbol, None, payload)

    def record_open(self, position: Position, signal: Signal) -> None:
        payload = {
            "side": position.side,
            "signal_score": signal.signal_score,
            "reasons": signal.reasons,
            "entry": position.entry_price,
            "stop_loss": position.stop_loss,
            "take_profit": position.take_profit,
            "size": position.quantity,
            "leverage": position.leverage,
            "fees": position.entry_fee,
            "slippage": position.entry_price - (signal.proposed_entry or position.entry_price),
            "exchange": position.exchange,
            "account_id": position.account_id,
            "position_id": position.position_id,
            "strategy_version": position.strategy_version,
        }
        self._record("POSITION_OPENED", position.symbol, position.trade_id, payload)
        if self._persistent:
            with SessionLocal.begin() as session:
                session.add(Trade(
                    trade_id=position.trade_id, user_id=position.user_id, mode="DEMO", symbol=position.symbol,
                    side=position.side, entry_price=position.entry_price, quantity=position.quantity,
                    stop_loss=position.stop_loss, take_profit=position.take_profit, status="OPEN",
                    exchange=position.exchange, account_id=position.account_id,
                    position_id=position.position_id, strategy_version=position.strategy_version,
                ))

    def record_close(self, closed: ClosedPosition) -> None:
        payload = {
            "exit": closed.exit_price,
            "exit_fee": closed.exit_fee,
            "pnl": closed.realized_pnl,
            "exit_reason": closed.reason,
        }
        self._record("POSITION_CLOSED", closed.position.symbol, closed.position.trade_id, payload)
        if self._persistent:
            with SessionLocal.begin() as session:
                trade = session.scalar(select(Trade).where(Trade.trade_id == closed.position.trade_id))
                if trade:
                    trade.status = "CLOSED"

    def _record(self, event_type: str, symbol: str, trade_id: str | None, payload: dict) -> None:
        serializable = json.loads(json.dumps(payload, default=str))
        record = JournalRecord(datetime.now(UTC), event_type, symbol, trade_id, serializable)
        self._records.append(record)
        if self._persistent:
            with SessionLocal.begin() as session:
                session.add(TradeEvent(trade_id=trade_id, event_type=event_type, payload=json.dumps({"symbol": symbol, **serializable})))
