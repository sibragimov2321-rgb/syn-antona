import asyncio
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.core.config import get_settings
from app.db import SessionLocal
from app.demo import DemoAutotrader
from app.domain.models import BotState
from app.market.synthetic import SyntheticDemoData
from app.shadow.engine import PROTOCOL_ID
from app.shadow.repository import ShadowRepository, shadow_metrics
from app.shadow.status import telegram_system_status
from app.statistics import calculate_statistics
from app.trading.controlled_live import ControlledLiveRepository

router = Router()
demo = DemoAutotrader()
demo_task: asyncio.Task | None = None


def is_admin_telegram_user(user_id: int, admin_ids: set[int]) -> bool:
    return user_id in admin_ids


def activate_persistent_execution_kill_switch() -> None:
    ControlledLiveRepository(SessionLocal).activate_kill_switch()


async def run_demo_notifications(bot: Bot, chat_id: int) -> None:
    """Telegram-facing DEMO loop; its data source is explicitly synthetic in Phase 2."""
    provider = SyntheticDemoData()
    while demo.state is not BotState.EMERGENCY_STOP:
        if demo.state is BotState.ACTIVE:
            for symbol in ("BTCUSDT", "ETHUSDT"):
                frames = await provider.frames(symbol)
                signal, position = demo.process(symbol, frames)
                if position:
                    await bot.send_message(
                        chat_id,
                        "🟢 <b>DEMO-ПОЗИЦИЯ ОТКРЫТА</b>\n\n"
                        f"Символ: {position.symbol}\nНаправление: {position.side}\n"
                        f"Вход: {position.entry_price}\nРазмер позиции: {position.quantity}\n"
                        f"Стоп-лосс: {position.stop_loss}\nТейк-профит: {position.take_profit}\n"
                        f"Риск: {demo.profile.risk_per_trade_pct:.2%}\n"
                        f"Риск/прибыль: {signal.risk_reward_ratio}\n"
                        f"Оценка сигнала: {signal.signal_score}",
                        parse_mode="HTML",
                    )
                for closed in demo.on_price(symbol, frames["5M"].price):
                    await bot.send_message(
                        chat_id,
                        "🔴 <b>DEMO-ПОЗИЦИЯ ЗАКРЫТА</b>\n\n"
                        f"{closed.position.symbol} {closed.position.side}\n"
                        f"Результат: ${closed.realized_pnl}\nПричина: {closed.reason}",
                        parse_mode="HTML",
                    )
        await asyncio.sleep(5)


def dashboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="▶️ Запустить DEMO", callback_data="demo:start"),
                InlineKeyboardButton(text="⏸ Пауза", callback_data="bot:pause"),
            ],
            [InlineKeyboardButton(text="🚨 Аварийная остановка", callback_data="bot:emergency")],
            [
                InlineKeyboardButton(text="📊 Открытые позиции", callback_data="positions"),
                InlineKeyboardButton(text="📈 Анализ", callback_data="analysis"),
            ],
            [
                InlineKeyboardButton(text="🛡 Управление риском", callback_data="risk"),
                InlineKeyboardButton(text="💼 История сделок", callback_data="history"),
            ],
            [InlineKeyboardButton(text="📉 Статистика", callback_data="statistics")],
            [
                InlineKeyboardButton(text="👁 Shadow-торговля", callback_data="shadow"),
                InlineKeyboardButton(
                    text="🟢 Состояние системы", callback_data="shadow:status"
                ),
            ],
        ]
    )


def dashboard_text() -> str:
    return (
        "🤖 <b>СЫН АНТОНА</b>\n\n"
        "💰 Баланс: $10,000.00\n📈 Результат за сегодня: $0.00\n"
        "📊 Общий результат: $0.00\n"
        "🟡 Бот: НА ПАУЗЕ\n⚙️ Режим: DEMO\n🎯 Открытые позиции: 0\n\n"
        "Только виртуальная торговля. Каждая сделка проходит проверку риск-менеджера."
    )


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "Добро пожаловать в «Сын Антона».\n\n"
        "1. Выберите DEMO\n2. Виртуальный баланс: $10,000\n"
        "3. Пары: BTC/USDT, ETH/USDT\n4. Риск: низкий\n\n"
        + dashboard_text(),
        reply_markup=dashboard(),
        parse_mode="HTML",
    )


@router.callback_query()
async def actions(callback: CallbackQuery) -> None:
    emergency_actions = {"bot:emergency", "emergency:keep", "emergency:close"}
    if callback.data in emergency_actions and not is_admin_telegram_user(
        callback.from_user.id, get_settings().admin_telegram_ids
    ):
        await callback.answer("Доступ разрешён только администратору.", show_alert=True)
        return
    if callback.data == "bot:emergency":
        activate_persistent_execution_kill_switch()
        demo.emergency_stop()
        await callback.message.answer(
            "🚨 АВАРИЙНАЯ ОСТАНОВКА: новые DEMO и Mainnet-входы запрещены. "
            "Выберите действие с DEMO-позициями.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Оставить позиции", callback_data="emergency:keep"),
                InlineKeyboardButton(text="Закрыть все DEMO-позиции", callback_data="emergency:close"),
            ]]),
        )
    elif callback.data == "demo:start":
        global demo_task
        demo.start()
        if demo_task is None or demo_task.done():
            demo_task = asyncio.create_task(run_demo_notifications(callback.bot, callback.message.chat.id))
        await callback.message.answer("DEMO запущен. Риск на сделку: 0,5%; дневной лимит убытка: 2%.")
    elif callback.data == "bot:pause":
        demo.pause()
        await callback.message.answer("Поиск DEMO-сделок приостановлен. Открытые позиции остаются защищены.")
    elif callback.data == "emergency:keep":
        demo.emergency_stop()
        await callback.message.answer("🚨 Поиск DEMO-сделок остановлен. Открытые позиции продолжают отслеживаться.")
    elif callback.data == "emergency:close":
        demo.emergency_stop(close_positions=True)
        await callback.message.answer("🚨 Поиск DEMO-сделок остановлен, все DEMO-позиции закрыты.")
    elif callback.data == "positions":
        positions = demo.broker.positions
        text = "📈 <b>ОТКРЫТЫЕ ПОЗИЦИИ</b>\n\n" + ("Открытых DEMO-позиций нет." if not positions else "\n".join(
            f"{item.symbol} {item.side}: {item.quantity} @ {item.entry_price}" for item in positions
        ))
        await callback.message.answer(text, parse_mode="HTML")
    elif callback.data == "history":
        records = [item for item in demo.journal.records if item.event_type != "SIGNAL"][-10:]
        await callback.message.answer("💼 <b>ИСТОРИЯ СДЕЛОК</b>\n\n" + ("Сделок пока нет." if not records else "\n".join(
            f"{item.event_type}: {item.symbol}" for item in records
        )), parse_mode="HTML")
    elif callback.data == "statistics":
        stats = calculate_statistics(demo.closed_positions)
        await callback.message.answer(f"📉 <b>СТАТИСТИКА</b>\n\nСделок: {stats.trades}\nЧистый результат: ${stats.net_pnl}", parse_mode="HTML")
    elif callback.data == "risk":
        await callback.message.answer("🛡 <b>НАСТРОЙКИ РИСКА</b>\n\nРиск на сделку: 0,5%\nДневной лимит убытка: 2%\nМаксимум позиций: 2\nМаксимальное плечо: 2x\nМинимум риск/прибыль: 1:2", parse_mode="HTML")
    elif callback.data == "analysis":
        await callback.message.answer("📊 <b>АНАЛИЗ</b>\n\nИспользуются только технические правила. AI API отключён.", parse_mode="HTML")
    elif callback.data == "shadow":
        repository = ShadowRepository()
        protocol = repository.protocol(PROTOCOL_ID)
        if not protocol:
            text = "👁 <b>SHADOW-ТОРГОВЛЯ</b>\n\nЗафиксированный протокол пока не найден."
        else:
            locked_at = protocol.locked_at.replace(tzinfo=UTC) if protocol.locked_at.tzinfo is None else protocol.locked_at
            days = (datetime.now(UTC) - locked_at).total_seconds() / 86400
            closed = repository.closed_trades(PROTOCOL_ID)
            metrics = shadow_metrics(closed)
            text = (
                "👁 <b>SHADOW-ТОРГОВЛЯ</b>\n\n"
                "Режим: SHADOW\nРеальная торговля: ВЫКЛЮЧЕНА\n"
                "Стратегия: расширение волатильности, 1 час\n"
                f"Дней наблюдения: {days:.2f}\n"
                f"Сигналов: {repository.decisions_count(PROTOCOL_ID, signals_only=True)}\n"
                f"Открытых shadow-позиций: {len(repository.open_trades(PROTOCOL_ID))}\n"
                f"Закрытых shadow-позиций: {metrics['trades']}\n"
                f"Чистый результат: ${metrics['net_pnl']}\n"
                f"Профит-фактор: {metrics['net_pf']}\n"
                f"Ожидаемый результат сделки: ${metrics['expectancy']}\n"
                f"Максимальная просадка: ${metrics['max_drawdown']}\n"
                f"Хэш протокола: <code>{protocol.protocol_hash}</code>"
            )
        await callback.message.answer(text, parse_mode="HTML")
    elif callback.data == "shadow:status":
        await callback.message.answer(
            telegram_system_status(ShadowRepository()), parse_mode="HTML"
        )
    else:
        await callback.message.answer("Этот раздел пока находится в разработке.")
    await callback.answer()


async def main() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise RuntimeError("Для запуска Telegram-бота требуется TELEGRAM_BOT_TOKEN")
    bot = Bot(settings.telegram_bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
