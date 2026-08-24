from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from hashlib import sha256
import json

from app.backtest.core import Candle
from app.backtest.costs import PHASE4E_SPOT_PROFILES
from app.backtest.orchestrator import LOW_RISK
from app.domain.models import Side, TradeIntent
from app.risk.manager import RiskManager
from app.shadow.models import ClosedCandleObservation
from app.shadow.repository import ShadowRepository, shadow_metrics
from app.strategy_lab.phase4e import build_features
from app.strategy_lab.phase4f import EconomicHypothesisStrategy, IMPACT_PER_LEG
from app.strategy_lab.phase4g import FROZEN_CONFIG_HASH, frozen_hypothesis

PROTOCOL_ID = "PHASE4I_PROSPECTIVE_LOCK"
HOUR = timedelta(hours=1)


def floor_hour(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def candle_hash(exchange: str, symbol: str, candle: Candle) -> str:
    payload = [
        exchange,
        symbol,
        candle.timestamp.isoformat(),
        str(candle.open),
        str(candle.high),
        str(candle.low),
        str(candle.close),
        str(candle.volume),
    ]
    return sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def _floor_quantity(quantity: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("Quantity step must be positive")
    return (quantity / step).to_integral_value(rounding=ROUND_DOWN) * step


class ProspectiveShadowEngine:
    def __init__(
        self,
        protocol: dict,
        repository: ShadowRepository,
        market,
        warmups: dict[tuple[str, str], list[Candle]],
        notifier,
    ) -> None:
        if protocol["strategy_config_hash"] != FROZEN_CONFIG_HASH:
            raise RuntimeError("Frozen strategy hash mismatch")
        self.protocol = protocol
        self.repository = repository
        self.market = market
        self.histories = warmups
        self.notifier = notifier
        self.risk = RiskManager()
        self.locked_at = datetime.fromisoformat(protocol["locked_at"])
        for exchange, symbol in list(self.histories):
            for row in repository.prospective_candles(PROTOCOL_ID, exchange, symbol):
                self.histories[(exchange, symbol)].append(
                    Candle(
                        row.candle_open_time.replace(tzinfo=UTC) if row.candle_open_time.tzinfo is None else row.candle_open_time,
                        Decimal(row.open),
                        Decimal(row.high),
                        Decimal(row.low),
                        Decimal(row.close),
                        Decimal(row.volume),
                    )
                )

    async def cycle(self) -> dict:
        errors = []

        async def process_exchange(exchange: str) -> None:
            for symbol in self.protocol["assets"]:
                try:
                    snapshot = await self.market.snapshot(exchange, symbol)
                    self.repository.save_quote(PROTOCOL_ID, snapshot)
                    await self._track_position(snapshot)
                    await self._process_candles(snapshot)
                except Exception as error:
                    errors.append(
                        {
                            "exchange": exchange,
                            "symbol": symbol,
                            "error": f"{type(error).__name__}: {error}",
                        }
                    )

        import asyncio

        await asyncio.gather(
            *(process_exchange(exchange) for exchange in self.protocol["exchanges"])
        )
        metrics = shadow_metrics(self.repository.closed_trades(PROTOCOL_ID))
        metrics["decisions"] = self.repository.decisions_count(PROTOCOL_ID)
        metrics["signals"] = self.repository.decisions_count(
            PROTOCOL_ID, signals_only=True
        )
        metrics["open_positions"] = len(self.repository.open_trades(PROTOCOL_ID))
        self.repository.save_daily_snapshot(PROTOCOL_ID, datetime.now(UTC).date(), metrics)
        return {"errors": errors, "metrics": metrics}

    async def _process_candles(self, snapshot) -> None:
        last = self.repository.last_candle_open(
            PROTOCOL_ID, snapshot.exchange, snapshot.symbol
        )
        start = last + HOUR if last else floor_hour(self.locked_at)
        end = floor_hour(snapshot.exchange_timestamp)
        if start >= end:
            return
        candles = await self.market.closed_candles(
            snapshot.exchange, snapshot.symbol, start, end
        )
        for candle in candles:
            close_time = candle.timestamp + HOUR
            if close_time <= self.locked_at or close_time > snapshot.exchange_timestamp:
                continue
            observation = ClosedCandleObservation(
                snapshot.exchange,
                snapshot.symbol,
                candle,
                close_time,
                snapshot.exchange_timestamp,
                snapshot.received_at,
            )
            if not self.repository.save_candle(
                PROTOCOL_ID,
                observation,
                candle_hash(snapshot.exchange, snapshot.symbol, candle),
            ):
                continue
            self.histories[(snapshot.exchange, snapshot.symbol)].append(candle)
            await self._decide(snapshot, candle)

    async def _decide(self, snapshot, candle: Candle) -> None:
        history = self.histories[(snapshot.exchange, snapshot.symbol)]
        features = build_features(history, candle.timestamp, candle.timestamp + HOUR)
        feature = features.get(candle.timestamp)
        strategy = EconomicHypothesisStrategy(
            frozen_hypothesis(),
            features,
            PHASE4E_SPOT_PROFILES[snapshot.exchange],
        )
        action = strategy(history) if feature else None
        decision = action.side.value if action else "WAIT"
        decision_id = sha256(
            f"{PROTOCOL_ID}:{snapshot.exchange}:{snapshot.symbol}:{candle.timestamp.isoformat()}".encode()
        ).hexdigest()
        decision_values = {
            "id": decision_id,
            "protocol_id": PROTOCOL_ID,
            "exchange": snapshot.exchange,
            "symbol": snapshot.symbol,
            "candle_open_time": candle.timestamp,
            "signal_timestamp": candle.timestamp + HOUR,
            "decision": decision,
            "signal_score": action.signal_score if action else 0,
            "decision_price": candle.close,
            "observed_bid": snapshot.bid,
            "observed_ask": snapshot.ask,
            "observed_spread": snapshot.spread,
            "risk_status": "NOT_APPLICABLE",
            "risk_reason": "No frozen signal",
            "strategy_hash": FROZEN_CONFIG_HASH,
            "context_json": json.dumps(
                action.context if action else {"reason": "WAIT"},
                default=str,
                sort_keys=True,
            ),
        }
        if not action:
            self.repository.record_decision(decision_values)
            return
        trade = self._shadow_order(snapshot, action, decision_id, candle)
        if trade is None:
            decision_values["risk_status"] = "REJECT"
            decision_values["risk_reason"] = self._last_risk_reason
            self.repository.record_decision(decision_values)
            return
        decision_values["risk_status"] = "ALLOW"
        decision_values["risk_reason"] = "Approved by deterministic Risk Manager"
        self.repository.record_decision(decision_values, trade)
        await self.notifier.opened(trade)

    def _shadow_order(self, snapshot, action, decision_id: str, candle: Candle):
        existing = self.repository.open_trade(
            PROTOCOL_ID, snapshot.exchange, snapshot.symbol
        )
        if existing:
            self._last_risk_reason = "Existing shadow position for exchange/asset"
            return None
        profile = PHASE4E_SPOT_PROFILES[snapshot.exchange]
        adverse = profile.slippage + IMPACT_PER_LEG
        top = snapshot.ask if action.side is Side.LONG else snapshot.bid
        entry = top * (
            Decimal("1") + adverse
            if action.side is Side.LONG
            else Decimal("1") - adverse
        )
        closed = self.repository.closed_trades(PROTOCOL_ID)
        selected = [
            trade
            for trade in closed
            if trade.exchange == snapshot.exchange and trade.symbol == snapshot.symbol
        ]
        balance = Decimal("1000") + sum(
            (Decimal(trade.realized_pnl) for trade in selected), Decimal()
        )
        today_pnl = sum(
            (
                Decimal(trade.realized_pnl)
                for trade in selected
                if trade.closed_at
                and (
                    trade.closed_at.replace(tzinfo=UTC)
                    if trade.closed_at.tzinfo is None
                    else trade.closed_at
                ).date()
                == snapshot.received_at.date()
            ),
            Decimal(),
        )
        losses = 0
        last_loss = None
        for trade in reversed(selected):
            if Decimal(trade.realized_pnl) < 0:
                losses += 1
                last_loss = last_loss or trade.closed_at
            else:
                break
        cooldown = bool(
            last_loss
            and snapshot.received_at
            < (last_loss.replace(tzinfo=UTC) if last_loss.tzinfo is None else last_loss)
            + timedelta(minutes=LOW_RISK.cooldown_minutes)
        )
        intent = TradeIntent(
            decision_id,
            0,
            snapshot.symbol,
            action.side,
            entry,
            action.stop_loss,
            action.take_profit,
            balance,
            today_pnl,
            0,
            losses,
            Decimal("1"),
            balance,
            action.volatility_pct,
            snapshot.spread_pct,
            cooldown,
        )
        approval = self.risk.approve(intent, LOW_RISK)
        if not approval.approved:
            self._last_risk_reason = approval.reason
            return None
        constraints = snapshot.constraints
        quantity = _floor_quantity(approval.quantity, constraints.quantity_step)
        if quantity < constraints.minimum_quantity or entry * quantity < constraints.minimum_notional:
            self._last_risk_reason = "Exchange minimum order/precision rejected shadow quantity"
            return None
        midpoint = snapshot.midpoint
        entry_spread = abs(top - midpoint) * quantity
        entry_slippage = abs(entry - top) * quantity
        entry_fee = entry * quantity * profile.taker_fee
        expected_fees = entry_fee + action.take_profit * quantity * profile.taker_fee
        return {
            "id": f"shadow-{decision_id}",
            "decision_id": decision_id,
            "protocol_id": PROTOCOL_ID,
            "exchange": snapshot.exchange,
            "symbol": snapshot.symbol,
            "side": action.side.value,
            "signal_timestamp": candle.timestamp + HOUR,
            "decision_price": candle.close,
            "entry_reference": midpoint,
            "entry_price": entry,
            "quantity": quantity,
            "stop_loss": action.stop_loss,
            "take_profit": action.take_profit,
            "leverage": Decimal("1"),
            "risk_amount": abs(entry - action.stop_loss) * quantity,
            "expected_fees": expected_fees,
            "entry_fee": entry_fee,
            "observed_spread": snapshot.spread,
            "entry_spread_cost": entry_spread,
            "entry_slippage_cost": entry_slippage,
            "strategy_hash": FROZEN_CONFIG_HASH,
            "status": "OPEN",
            "opened_at": snapshot.received_at,
        }

    async def _track_position(self, snapshot) -> None:
        trade = self.repository.open_trade(
            PROTOCOL_ID, snapshot.exchange, snapshot.symbol
        )
        if not trade:
            return
        side = Side(trade.side)
        reference = snapshot.bid if side is Side.LONG else snapshot.ask
        reason = None
        if side is Side.LONG:
            reason = "STOP_LOSS" if reference <= trade.stop_loss else "TAKE_PROFIT" if reference >= trade.take_profit else None
        else:
            reason = "STOP_LOSS" if reference >= trade.stop_loss else "TAKE_PROFIT" if reference <= trade.take_profit else None
        if not reason:
            return
        profile = PHASE4E_SPOT_PROFILES[snapshot.exchange]
        adverse = profile.slippage + IMPACT_PER_LEG
        exit_price = reference * (
            Decimal("1") - adverse if side is Side.LONG else Decimal("1") + adverse
        )
        quantity = Decimal(trade.quantity)
        direction = Decimal("1") if side is Side.LONG else Decimal("-1")
        gross = (snapshot.midpoint - Decimal(trade.entry_reference)) * quantity * direction
        exit_fee = exit_price * quantity * profile.taker_fee
        exit_spread = abs(reference - snapshot.midpoint) * quantity
        exit_slippage = abs(exit_price - reference) * quantity
        total_fees = Decimal(trade.entry_fee) + exit_fee
        total_spread = Decimal(trade.entry_spread_cost) + exit_spread
        total_slippage = Decimal(trade.entry_slippage_cost) + exit_slippage
        net = gross - total_fees - total_spread - total_slippage
        values = {
            "status": "CLOSED",
            "exit_reference": snapshot.midpoint,
            "exit_price": exit_price,
            "exit_reason": reason,
            "exit_fee": exit_fee,
            "exit_spread_cost": exit_spread,
            "exit_slippage_cost": exit_slippage,
            "gross_pnl": gross,
            "realized_pnl": net,
            "closed_at": snapshot.received_at,
        }
        self.repository.close_trade(trade.id, values)
        await self.notifier.closed(trade, values)
