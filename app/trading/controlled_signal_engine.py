"""Dedicated closed-candle signal producer for Controlled Live.

It deliberately owns no Shadow protocol, lease, position, trade, event, or
watchdog state.  The frozen strategy implementation and config hash are reused
unchanged; execution remains behind the existing deterministic proposal/risk
and production-gateway layers.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.backtest.costs import PHASE4E_SPOT_PROFILES
from app.backtest.historical import BybitHistoricalDataProvider
from app.db import ControlledLiveSignalRecord
from app.strategy_lab.phase4e import build_features
from app.strategy_lab.phase4f import EconomicHypothesisStrategy
from app.strategy_lab.phase4g import FROZEN_CONFIG_HASH, frozen_hypothesis
from app.trading.controlled_universe import PROFILE_NAME, SCANNER_CONFIG, internal_symbol


HOUR = timedelta(hours=1)
WARMUP_HOURS = 400


def floor_hour(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


class ControlledLiveSignalEngine:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        provider: BybitHistoricalDataProvider | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.provider = provider or BybitHistoricalDataProvider()

    def latest_open(self, symbol: str) -> datetime | None:
        with self.session_factory() as session:
            return session.scalar(
                select(ControlledLiveSignalRecord.candle_open_time)
                .where(
                    ControlledLiveSignalRecord.profile_name == PROFILE_NAME,
                    ControlledLiveSignalRecord.symbol == internal_symbol(symbol),
                )
                .order_by(ControlledLiveSignalRecord.candle_open_time.desc())
                .limit(1)
            )

    async def cycle(self, now: datetime | None = None) -> dict:
        current = now or datetime.now(UTC)
        closed_end = floor_hour(current)
        target_open = closed_end - HOUR
        results: dict[str, str] = {}
        for symbol in SCANNER_CONFIG.symbols:
            latest = self.latest_open(symbol)
            if latest is not None:
                latest = latest.replace(tzinfo=UTC) if latest.tzinfo is None else latest
            if latest is not None and latest >= target_open:
                results[symbol] = "ALREADY_ANALYZED"
                continue
            try:
                candles = await self.provider.fetch(
                    internal_symbol(symbol),
                    "1h",
                    closed_end - timedelta(hours=WARMUP_HOURS),
                    closed_end,
                )
                candles = [item for item in candles if item.timestamp + HOUR <= current]
                if not candles or candles[-1].timestamp != target_open:
                    raise RuntimeError("latest fully closed 1H candle is unavailable")
                candle = candles[-1]
                features = build_features(candles, candle.timestamp, candle.timestamp + HOUR)
                strategy = EconomicHypothesisStrategy(
                    frozen_hypothesis(), features, PHASE4E_SPOT_PROFILES["bybit"]
                )
                action = strategy(candles) if candle.timestamp in features else None
                decision = action.side.value if action else "WAIT"
                record_id = sha256(
                    f"{PROFILE_NAME}:{symbol}:{candle.timestamp.isoformat()}".encode()
                ).hexdigest()
                values = {
                    "id": record_id,
                    "profile_name": PROFILE_NAME,
                    "strategy_hash": FROZEN_CONFIG_HASH,
                    "symbol": internal_symbol(symbol),
                    "candle_open_time": candle.timestamp,
                    "signal_timestamp": candle.timestamp + HOUR,
                    "decision": decision,
                    "signal_score": action.signal_score if action else 0,
                    "decision_price": candle.close,
                    "stop_loss": action.stop_loss if action else None,
                    "take_profit": action.take_profit if action else None,
                    "risk_status": "ALLOW" if action else "NOT_APPLICABLE",
                    "risk_reason": (
                        "Eligible frozen signal; controlled-live Risk Manager required"
                        if action
                        else "Frozen strategy returned WAIT"
                    ),
                    "context_json": json.dumps(
                        action.context if action else {"reason": "WAIT"},
                        default=str,
                        sort_keys=True,
                    ),
                    "created_at": current,
                }
                with self.session_factory.begin() as session:
                    if session.get(ControlledLiveSignalRecord, record_id) is None:
                        session.add(ControlledLiveSignalRecord(**values))
                results[symbol] = decision
            except Exception as error:
                results[symbol] = f"ERROR: {type(error).__name__}: {error}"
        return results

    async def close(self) -> None:
        close = getattr(self.provider, "close", None)
        if close:
            await close()
