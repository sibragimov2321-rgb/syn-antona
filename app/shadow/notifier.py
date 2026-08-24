import logging
from datetime import UTC, datetime
from decimal import Decimal
from html import escape
import json

from aiogram import Bot


logger = logging.getLogger(__name__)


class ShadowNotifier:
    def __init__(
        self,
        token: str | None,
        chat_ids: set[int],
        *,
        first_trade_already_seen: bool = False,
        repository=None,
        protocol_id: str | None = None,
    ) -> None:
        self.bot = Bot(token) if token and chat_ids else None
        self.chat_ids = chat_ids
        self.first_trade_alerted = first_trade_already_seen
        self.repository = repository
        self.protocol_id = protocol_id

    @staticmethod
    def _opened_text(trade) -> str:
        return (
            "👁 <b>FIRST SHADOW TRADE</b>\n\n"
            f"Exchange: {trade['exchange'].title()}\n"
            f"Pair: {trade['symbol']}\n"
            f"Direction: {trade['side']}\n"
            f"Entry reference: {trade['entry_reference']}\n"
            f"Simulated entry: {trade['entry_price']}\n"
            f"Stop Loss: {trade['stop_loss']}\n"
            f"Take Profit: {trade['take_profit']}\n"
            f"Estimated risk: {trade['risk_amount']}\n"
            f"Observed spread: {trade['observed_spread']}\n"
            f"Estimated fees: {trade['expected_fees']}\n\n"
            "Это виртуальная сделка. Реальный ордер не отправлен."
        )

    async def opened(self, trade) -> None:
        if self.repository:
            await self.deliver_pending()
            return
        if self.first_trade_alerted:
            return
        self.first_trade_alerted = await self._send(self._opened_text(trade))

    async def closed(self, trade, values: dict) -> None:
        if self.repository:
            await self.deliver_pending()
            return
        await self._send(
            "👁 <b>SHADOW TRADE CLOSED</b>\n\n"
            f"Exchange: {trade.exchange.title()}\nPair: {trade.symbol}\n"
            f"Direction: {trade.side}\nExit: {values['exit_price']}\n"
            f"Net PnL: ${values['realized_pnl']}\nReason: {values['exit_reason']}\n\n"
            "Это виртуальная сделка."
        )

    async def deliver_pending(self) -> int:
        if not self.repository or not self.protocol_id:
            return 0
        delivered = 0
        for event in self.repository.pending_trade_alerts(self.protocol_id):
            details = json.loads(event.details_json)
            if event.event_type == "FIRST_SHADOW_TRADE":
                text = self._opened_text(details["trade"])
            else:
                trade = details["trade"]
                values = details["values"]
                text = (
                    "👁 <b>SHADOW TRADE CLOSED</b>\n\n"
                    f"Exchange: {trade['exchange'].title()}\n"
                    f"Pair: {trade['symbol']}\n"
                    f"Direction: {trade['side']}\n"
                    f"Exit: {values['exit_price']}\n"
                    f"Net PnL: ${values['realized_pnl']}\n"
                    f"Reason: {values['exit_reason']}\n\n"
                    "Это виртуальная сделка."
                )
            if await self._send(text):
                self.repository.mark_event_alerted(event.id, datetime.now(UTC))
                delivered += 1
        return delivered

    async def system(self, title: str, message: str) -> None:
        await self._send(f"⚠️ <b>{escape(title)}</b>\n\n{escape(message)}")

    async def daily(self, day_number: int, metrics: dict) -> None:
        costs = sum(
            (
                Decimal(str(metrics.get("fees", 0))),
                Decimal(str(metrics.get("spread_cost", 0))),
                Decimal(str(metrics.get("slippage", 0))),
            ),
            Decimal(),
        )
        await self._send(
            "📊 <b>SHADOW DAILY REPORT</b>\n\n"
            f"Day: {day_number} / 30\n"
            f"Signals: {metrics.get('signals', 0)}\n"
            f"Trades: {metrics.get('trades', 0)}\n"
            f"Wins: {metrics.get('wins', 0)}\n"
            f"Losses: {metrics.get('losses', 0)}\n"
            f"Open positions: {metrics.get('open_positions', 0)}\n"
            f"Gross PnL: ${metrics.get('gross_pnl', 0)}\n"
            f"Costs: ${costs}\n"
            f"Net PnL: ${metrics.get('net_pnl', 0)}\n"
            f"PF: {metrics.get('net_pf', 0)}\n"
            f"Expectancy: ${metrics.get('expectancy', 0)}\n"
            f"Max DD: ${metrics.get('max_drawdown', 0)}\n\n"
            "Стратегия остаётся frozen; отчёт не изменяет её параметры."
        )

    async def _send(self, text: str) -> bool:
        if not self.bot:
            return False
        for chat_id in self.chat_ids:
            try:
                await self.bot.send_message(chat_id, text, parse_mode="HTML")
            except Exception:
                logger.exception("shadow_telegram_delivery_failed", extra={"chat_id": chat_id})
                return False
        return True

    async def close(self) -> None:
        if self.bot:
            await self.bot.session.close()
