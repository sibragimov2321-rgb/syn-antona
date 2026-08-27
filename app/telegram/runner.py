import asyncio
from decimal import Decimal

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.core.config import get_settings
from app.db import ExecutionOrderRecord, SessionLocal
from app.trading.controlled_live import ControlledLiveRepository
from app.trading.first_live_proposal import (
    FROZEN_SIGNAL_SOURCE,
    FirstLiveProposalRepository,
)
from app.trading.multi_symbol_scanner import (
    format_scanner_status_ru,
    format_scanner_wait_reasons_ru,
    scanner_status,
)

router = Router()


def is_admin_telegram_user(user_id: int, admin_ids: set[int]) -> bool:
    return user_id in admin_ids


def activate_persistent_execution_kill_switch() -> None:
    ControlledLiveRepository(SessionLocal).activate_kill_switch()


def dashboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🟢 CONTROLLED LIVE", callback_data="controlled:status"
                ),
                InlineKeyboardButton(
                    text="📊 Почему WAIT?", callback_data="controlled:why"
                ),
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
        ]
    )


def dashboard_text() -> str:
    status = scanner_status(SessionLocal)
    return (
        "🤖 <b>СЫН АНТОНА</b>\n\n"
        "🟢 <b>CONTROLLED LIVE</b>\n"
        f"💰 REAL TRADING: <b>{status.real_order_execution_runtime}</b>\n"
        f"Equity: {_money(status.equity)}\n"
        f"Positions: {_number(status.open_positions)} / 3\n"
        f"Open orders: {_number(status.open_orders)}\n"
        f"Trades today: {_number(status.trades_today)}\n"
        f"Realized PnL today: {_money(status.daily_realized_pnl)}\n"
        "Daily loss budget remaining: "
        f"{_money(status.remaining_daily_loss)} / $5\n"
        "Total experiment loss remaining: "
        f"{_money(status.remaining_experiment_loss)} / $10\n\n"
        "Threshold: 70\nRisk max: 5%\nLeverage: 2x\nMin R/R: 1:1.5\n"
        "SHADOW: OFF | DEMO: OFF"
    )


def _money(value: Decimal | None) -> str:
    return f"${value.quantize(Decimal('0.0001'))}" if value is not None else "НЕДОСТУПНО"


def _number(value: int | None) -> str:
    return str(value) if value is not None else "НЕДОСТУПНО"


def signal_wait_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Проверить сигнал сейчас",
                    callback_data="controlled:status",
                ),
                InlineKeyboardButton(
                    text="📊 Почему WAIT?",
                    callback_data="controlled:why",
                ),
            ]
        ]
    )


@router.message(CommandStart())
async def start(message: Message) -> None:
    if not is_admin_telegram_user(
        message.from_user.id, get_settings().admin_telegram_ids
    ):
        await message.answer("Доступ разрешён только администратору.")
        return
    await message.answer(
        dashboard_text(),
        reply_markup=dashboard(),
        parse_mode="HTML",
    )


@router.callback_query()
async def actions(callback: CallbackQuery) -> None:
    admin_actions = {
        "bot:emergency",
        "controlled:status",
        "controlled:why",
        "positions",
        "analysis",
        "risk",
        "history",
        "statistics",
    }
    admin_only = callback.data in admin_actions or bool(
        callback.data and callback.data.startswith("phase5e:")
    )
    if admin_only and not is_admin_telegram_user(
        callback.from_user.id, get_settings().admin_telegram_ids
    ):
        await callback.answer("Доступ разрешён только администратору.", show_alert=True)
        return
    if callback.data and callback.data.startswith("phase5e:"):
        try:
            _, action, proposal_id = callback.data.split(":", 2)
            controlled = ControlledLiveRepository(SessionLocal)
            phase5e = FirstLiveProposalRepository(SessionLocal)
            record = controlled.proposal(proposal_id)
            if record is None or record.source != FROZEN_SIGNAL_SOURCE:
                raise PermissionError("Предложение не найдено или не относится к Phase 5E.")
            if record.admin_telegram_id != callback.from_user.id:
                raise PermissionError("Предложение принадлежит другому администратору.")
            if action == "approve":
                # The Telegram process only records consent. The separately isolated
                # The dedicated controlled-live worker owns the gateway and
                # re-checks every execution gate.
                controlled.approve(record.proposal_hash, callback.from_user.id)
                settings = get_settings()
                armed = (
                    not settings.dry_run
                    and settings.live_trading_enabled
                    and settings.controlled_live_enabled
                    and settings.manual_first_order_approved
                )
                if armed:
                    phase5e.mark_approved_for_execution(proposal_id)
                    await callback.message.answer(
                        "✅ Подтверждение сохранено. Предложение передано изолированному "
                        "execution worker; перед HTTP он повторно проверит все safety gates."
                    )
                else:
                    phase5e.mark_approved_dry_run(proposal_id)
                    await callback.message.answer(
                        "✅ Подтверждение сохранено. DRY RUN: реальный ордер не отправлен. "
                        "Execution gates остаются закрыты."
                    )
            elif action == "cancel":
                phase5e.cancel(proposal_id, callback.from_user.id)
                await callback.message.answer(
                    "❌ Первое controlled-live предложение отменено. Ордер не отправлен."
                )
            else:
                raise PermissionError("Неизвестное действие Phase 5E.")
        except Exception as error:
            await callback.answer(str(error), show_alert=True)
            return
    elif callback.data == "bot:emergency":
        activate_persistent_execution_kill_switch()
        await callback.message.answer(
            "🚨 HARD STOP активирован: новые Mainnet-входы запрещены. "
            "Существующие позиции сохраняют exchange-native SL/TP."
        )
    elif callback.data == "positions":
        status = scanner_status(SessionLocal)
        text = (
            "📈 <b>РЕАЛЬНЫЕ BYBIT ПОЗИЦИИ</b>\n\n"
            f"Positions: {_number(status.open_positions)} / 3\n"
            f"Open orders: {_number(status.open_orders)}\n"
            f"Equity: {_money(status.equity)}\n"
            f"Open planned risk: {_money(status.open_planned_risk)}\n"
            "Источник: последний приватный Bybit Mainnet reconciliation snapshot."
        )
        await callback.message.answer(text, parse_mode="HTML")
    elif callback.data == "history":
        with SessionLocal() as session:
            records = session.scalars(
                select(ExecutionOrderRecord)
                .where(ExecutionOrderRecord.exchange == "bybit")
                .order_by(ExecutionOrderRecord.created_at.desc())
                .limit(10)
            ).all()
        lines = [
            f"{item.symbol} {item.side} {item.quantity} — {item.status}"
            for item in records
        ]
        await callback.message.answer(
            "💼 <b>MAINNET EXECUTION LEDGER</b>\n\n"
            + ("Реальных сделок пока нет." if not lines else "\n".join(lines)),
            parse_mode="HTML",
        )
    elif callback.data == "statistics":
        status = scanner_status(SessionLocal)
        await callback.message.answer(
            "📉 <b>CONTROLLED LIVE СТАТИСТИКА</b>\n\n"
            f"Сделок сегодня: {_number(status.trades_today)}\n"
            f"Realized PnL: {_money(status.daily_realized_pnl)}\n"
            f"Позиции: {_number(status.open_positions)} / 3\n"
            f"Остаток дневного лимита: {_money(status.remaining_daily_loss)}",
            parse_mode="HTML",
        )
    elif callback.data == "risk":
        await callback.message.answer(
            "🛡 <b>CONTROLLED LIVE — РИСК</b>\n\n"
            "Порог сигнала: 70\nРиск на сделку: максимум 5% equity\n"
            "Абсолютный дневной risk budget: $5\n"
            "Лимит эксперимента: $10\nМаксимум позиций: 3\n"
            "Количество сделок/день: без лимита\nПлечо: 2x\n"
            "Минимум риск/прибыль: 1:1,5\nTrailing: OFF\n"
            "После двух последовательных убытков: STOP до следующего UTC дня.",
            parse_mode="HTML",
        )
    elif callback.data == "analysis":
        await callback.message.answer("📊 <b>АНАЛИЗ</b>\n\nИспользуются только технические правила. AI API отключён.", parse_mode="HTML")
    elif callback.data == "controlled:status":
        # Read-only refresh: no market fetch into the strategy and no decision run.
        await callback.message.answer(
            format_scanner_status_ru(scanner_status(SessionLocal)),
            parse_mode="HTML",
            reply_markup=signal_wait_keyboard(),
        )
    elif callback.data == "controlled:why":
        await callback.message.answer(
            format_scanner_wait_reasons_ru(scanner_status(SessionLocal)),
            parse_mode="HTML",
            reply_markup=signal_wait_keyboard(),
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
