import logging
from datetime import UTC, datetime
from decimal import Decimal
from html import escape
import json

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


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
            "👁 <b>ПЕРВАЯ SHADOW-СДЕЛКА</b>\n\n"
            f"Биржа: {trade['exchange'].title()}\n"
            f"Пара: {trade['symbol']}\n"
            f"Направление: {trade['side']}\n"
            f"Расчётная цена входа: {trade['entry_reference']}\n"
            f"Виртуальный вход: {trade['entry_price']}\n"
            f"Стоп-лосс: {trade['stop_loss']}\n"
            f"Тейк-профит: {trade['take_profit']}\n"
            f"Расчётный риск: {trade['risk_amount']}\n"
            f"Наблюдаемый спред: {trade['observed_spread']}\n"
            f"Расчётные комиссии: {trade['expected_fees']}\n\n"
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
            "👁 <b>SHADOW-СДЕЛКА ЗАКРЫТА</b>\n\n"
            f"Биржа: {trade.exchange.title()}\nПара: {trade.symbol}\n"
            f"Направление: {trade.side}\nВыход: {values['exit_price']}\n"
            f"Чистый результат: ${values['realized_pnl']}\n"
            f"Причина: {values['exit_reason']}\n\n"
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
                    "👁 <b>SHADOW-СДЕЛКА ЗАКРЫТА</b>\n\n"
                    f"Биржа: {trade['exchange'].title()}\n"
                    f"Пара: {trade['symbol']}\n"
                    f"Направление: {trade['side']}\n"
                    f"Выход: {values['exit_price']}\n"
                    f"Чистый результат: ${values['realized_pnl']}\n"
                    f"Причина: {values['exit_reason']}\n\n"
                    "Это виртуальная сделка."
                )
            if await self._send(text):
                self.repository.mark_event_alerted(event.id, datetime.now(UTC))
                delivered += 1
        return delivered

    async def system(self, title: str, message: str) -> bool:
        translated_titles = {
            "STALE DATA": "УСТАРЕВШИЕ ДАННЫЕ",
            "EXCHANGE OFFLINE": "БИРЖА НЕДОСТУПНА",
            "EXCHANGE RESTORED": "БИРЖА СНОВА ДОСТУПНА",
            "COLLECTOR RESTARTED": "COLLECTOR ПЕРЕЗАПУЩЕН",
            "COLLECTOR STOPPED": "COLLECTOR ОСТАНОВЛЕН",
            "PROTOCOL HASH MISMATCH": "НЕСОВПАДЕНИЕ ХЭША ПРОТОКОЛА",
            "SHADOW DATABASE/COLLECTOR FAILURE": "ОШИБКА SHADOW-БАЗЫ ИЛИ COLLECTOR",
        }
        return await self._send(
            f"⚠️ <b>{escape(translated_titles.get(title, title))}</b>\n\n"
            f"{escape(message)}"
        )

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
            "📊 <b>ЕЖЕДНЕВНЫЙ SHADOW-ОТЧЁТ</b>\n\n"
            f"День: {day_number} / 30\n"
            f"Сигналы: {metrics.get('signals', 0)}\n"
            f"Сделки: {metrics.get('trades', 0)}\n"
            f"Прибыльные: {metrics.get('wins', 0)}\n"
            f"Убыточные: {metrics.get('losses', 0)}\n"
            f"Открытые позиции: {metrics.get('open_positions', 0)}\n"
            f"Результат до расходов: ${metrics.get('gross_pnl', 0)}\n"
            f"Расходы: ${costs}\n"
            f"Чистый результат: ${metrics.get('net_pnl', 0)}\n"
            f"Профит-фактор: {metrics.get('net_pf', 0)}\n"
            f"Ожидаемый результат сделки: ${metrics.get('expectancy', 0)}\n"
            f"Максимальная просадка: ${metrics.get('max_drawdown', 0)}\n\n"
            "Стратегия остаётся зафиксированной; отчёт не изменяет её параметры."
        )

    async def controlled_proposal(
        self, text: str, proposal_id: str, admin_id: int
    ) -> bool:
        """Deliver the one immutable Phase 5E preview to its owning admin."""
        if not self.bot or admin_id not in self.chat_ids:
            return False
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Подтвердить первую сделку",
                        callback_data=f"phase5e:approve:{proposal_id}",
                    ),
                    InlineKeyboardButton(
                        text="❌ Отменить",
                        callback_data=f"phase5e:cancel:{proposal_id}",
                    ),
                ]
            ]
        )
        try:
            await self.bot.send_message(
                admin_id, text, parse_mode="HTML", reply_markup=keyboard
            )
        except Exception:
            logger.exception(
                "controlled_proposal_telegram_delivery_failed",
                extra={"chat_id": admin_id, "proposal_id": proposal_id},
            )
            return False
        return True

    async def ai_order_opened(self, preview, fill) -> bool:
        return await self._send(
            "✅ <b>REAL ORDER OPENED</b>\n\n"
            f"Symbol: {escape(preview.symbol)}\n"
            f"Direction: {'LONG' if preview.side == 'BUY' else 'SHORT'}\n"
            f"AI confidence: {preview.signal_score}%\n"
            f"Entry: {fill.average_price}\n"
            f"Size: {fill.filled_quantity} (~${preview.expected_notional})\n"
            f"Leverage: {preview.leverage}x\n"
            f"SL: {preview.stop_loss}\n"
            f"TP: {preview.take_profit}\n"
            f"Order ID: {escape(fill.order_id)}\n\n"
            "Exchange-native SL/TP verified; execution recorded in PostgreSQL."
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
