import asyncio
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.core.config import get_settings
from app.demo import DemoAutotrader
from app.domain.models import BotState
from app.market.synthetic import SyntheticDemoData
from app.shadow.engine import PROTOCOL_ID
from app.shadow.repository import ShadowRepository, shadow_metrics
from app.shadow.status import telegram_system_status
from app.statistics import calculate_statistics

router = Router()
demo = DemoAutotrader()
demo_task: asyncio.Task | None = None


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
                        "🟢 <b>DEMO POSITION OPENED</b>\n\n"
                        f"Symbol: {position.symbol}\nDirection: {position.side}\n"
                        f"Entry: {position.entry_price}\nPosition Size: {position.quantity}\n"
                        f"Stop Loss: {position.stop_loss}\nTake Profit: {position.take_profit}\n"
                        f"Risk: {demo.profile.risk_per_trade_pct:.2%}\n"
                        f"R/R: {signal.risk_reward_ratio}\nSignal Score: {signal.signal_score}",
                        parse_mode="HTML",
                    )
                for closed in demo.on_price(symbol, frames["5M"].price):
                    await bot.send_message(
                        chat_id,
                        "🔴 <b>DEMO POSITION CLOSED</b>\n\n"
                        f"{closed.position.symbol} {closed.position.side}\n"
                        f"PnL: ${closed.realized_pnl}\nReason: {closed.reason}",
                        parse_mode="HTML",
                    )
        await asyncio.sleep(5)


def dashboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="▶️ Start DEMO", callback_data="demo:start"),
                InlineKeyboardButton(text="⏸ Pause", callback_data="bot:pause"),
            ],
            [InlineKeyboardButton(text="🚨 Emergency Stop", callback_data="bot:emergency")],
            [
                InlineKeyboardButton(text="📊 Positions", callback_data="positions"),
                InlineKeyboardButton(text="📈 AI Analysis", callback_data="analysis"),
            ],
            [
                InlineKeyboardButton(text="🛡 Risk Management", callback_data="risk"),
                InlineKeyboardButton(text="💼 Trade History", callback_data="history"),
            ],
            [InlineKeyboardButton(text="📉 Statistics", callback_data="statistics")],
            [
                InlineKeyboardButton(text="👁 Shadow Trading", callback_data="shadow"),
                InlineKeyboardButton(
                    text="🟢 System Status", callback_data="shadow:status"
                ),
            ],
        ]
    )


def dashboard_text() -> str:
    return (
        "🤖 <b>AI TRADING</b>\n\n"
        "💰 Equity: $10,000.00\n📈 Today PnL: $0.00\n📊 Total PnL: $0.00\n"
        "🟡 Bot: PAUSED\n⚙️ Mode: DEMO\n🎯 Open Positions: 0\n\n"
        "Paper mode only. Every trade must pass the Risk Manager."
    )


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "Добро пожаловать в AI Trading Bot.\n\n"
        "1. Выберите DEMO\n2. Виртуальный баланс: $10,000\n"
        "3. Пары: BTC/USDT, ETH/USDT\n4. Риск: Low\n\n"
        + dashboard_text(),
        reply_markup=dashboard(),
        parse_mode="HTML",
    )


@router.callback_query()
async def actions(callback: CallbackQuery) -> None:
    if callback.data == "bot:emergency":
        demo.emergency_stop()
        await callback.message.answer(
            "🚨 EMERGENCY STOP: DEMO entries stopped. Choose what to do with positions.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Leave positions", callback_data="emergency:keep"),
                InlineKeyboardButton(text="Close all DEMO positions", callback_data="emergency:close"),
            ]]),
        )
    elif callback.data == "demo:start":
        global demo_task
        demo.start()
        if demo_task is None or demo_task.done():
            demo_task = asyncio.create_task(run_demo_notifications(callback.bot, callback.message.chat.id))
        await callback.message.answer("DEMO active. Risk: 0.5% per trade; daily loss limit: 2%.")
    elif callback.data == "bot:pause":
        demo.pause()
        await callback.message.answer("DEMO search paused. Existing DEMO positions remain protected.")
    elif callback.data == "emergency:keep":
        demo.emergency_stop()
        await callback.message.answer("🚨 DEMO search stopped. Existing positions remain monitored.")
    elif callback.data == "emergency:close":
        demo.emergency_stop(close_positions=True)
        await callback.message.answer("🚨 DEMO search stopped and all DEMO positions were closed.")
    elif callback.data == "positions":
        positions = demo.broker.positions
        text = "📈 <b>OPEN POSITIONS</b>\n\n" + ("No open DEMO positions." if not positions else "\n".join(
            f"{item.symbol} {item.side}: {item.quantity} @ {item.entry_price}" for item in positions
        ))
        await callback.message.answer(text, parse_mode="HTML")
    elif callback.data == "history":
        records = [item for item in demo.journal.records if item.event_type != "SIGNAL"][-10:]
        await callback.message.answer("💼 <b>TRADE HISTORY</b>\n\n" + ("No trades yet." if not records else "\n".join(
            f"{item.event_type}: {item.symbol}" for item in records
        )), parse_mode="HTML")
    elif callback.data == "statistics":
        stats = calculate_statistics(demo.closed_positions)
        await callback.message.answer(f"📉 <b>STATISTICS</b>\n\nTrades: {stats.trades}\nNet PnL: ${stats.net_pnl}", parse_mode="HTML")
    elif callback.data == "risk":
        await callback.message.answer("🛡 <b>RISK SETTINGS</b>\n\nRisk/trade: 0.5%\nDaily loss: 2%\nMax positions: 2\nMax leverage: 2x\nMin R/R: 1:2", parse_mode="HTML")
    elif callback.data == "analysis":
        await callback.message.answer("📊 <b>AI ANALYSIS</b>\n\nPhase 2 uses technical rules only; AI API is disabled.", parse_mode="HTML")
    elif callback.data == "shadow":
        repository = ShadowRepository()
        protocol = repository.protocol(PROTOCOL_ID)
        if not protocol:
            text = "👁 <b>SHADOW TRADING</b>\n\nProspective service has not created its protocol lock yet."
        else:
            locked_at = protocol.locked_at.replace(tzinfo=UTC) if protocol.locked_at.tzinfo is None else protocol.locked_at
            days = (datetime.now(UTC) - locked_at).total_seconds() / 86400
            closed = repository.closed_trades(PROTOCOL_ID)
            metrics = shadow_metrics(closed)
            text = (
                "👁 <b>SHADOW TRADING</b>\n\n"
                "Mode: SHADOW\nLive trading: OFF\n"
                "Strategy: Volatility Expansion 1h\n"
                f"Days observed: {days:.2f}\n"
                f"Signals: {repository.decisions_count(PROTOCOL_ID, signals_only=True)}\n"
                f"Open shadow positions: {len(repository.open_trades(PROTOCOL_ID))}\n"
                f"Closed shadow positions: {metrics['trades']}\n"
                f"Net Shadow PnL: ${metrics['net_pnl']}\n"
                f"Net PF: {metrics['net_pf']}\n"
                f"Expectancy: ${metrics['expectancy']}\n"
                f"Max DD: ${metrics['max_drawdown']}\n"
                f"Current protocol hash: <code>{protocol.protocol_hash}</code>"
            )
        await callback.message.answer(text, parse_mode="HTML")
    elif callback.data == "shadow:status":
        await callback.message.answer(
            telegram_system_status(ShadowRepository()), parse_mode="HTML"
        )
    else:
        await callback.message.answer("This dashboard section is planned for the next increment.")
    await callback.answer()


async def main() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required to start Telegram polling")
    bot = Bot(settings.telegram_bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
