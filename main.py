import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Dict, Optional

import httpx
from flask import Flask
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes
from telethon import TelegramClient
from telethon.sessions import StringSession
from telegram.ext import Application, JobQueue

# ===== Конфигурация =====
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

MEXC_TICKER_URL = "https://api.mexc.com/api/v3/ticker/price"
API_ID = 32990800
API_HASH = "f14259b31ea4bc638814833d6de13bd5"
TARGET_GROUP_USERNAME = "@alert_gamno"
TELEGRAM_BOT_TOKEN = "8213546201:AAFIFDmFqtjibgd9CkfsGGgWnb1_tTXfe8c"

# 🚨 ВАЖНО: Сессия Telethon передаётся через переменную окружения
SESSION_STRING = os.getenv("TELETHON_SESSION_STRING")

# ===== Состояние бота =====
@dataclass
class TrackingInfo:
    symbol: str
    target_price: float
    direction: str = "above"

@dataclass
class BotState:
    tracking: Dict[int, TrackingInfo] = field(default_factory=dict)

state = BotState()

# ===== Функции =====
async def fetch_price(symbol: str) -> Optional[float]:
    params = {"symbol": symbol.upper()}
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(MEXC_TICKER_URL, params=params)
            resp.raise_for_status()
            return float(resp.json()["price"])
        except Exception as exc:
            logger.error(f"Ошибка при получении цены для {symbol}: {exc}")
            return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я отслеживаю цены на MEXC.\n"
        "Используй команду /set SYMBOL TARGET, чтобы задать наблюдение.\n"
        "Например: /set BTCUSDT 30000"
    )

async def set_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) not in (2, 3):
        await update.message.reply_text(
            "Неверный формат. Используй: /set SYMBOL TARGET [above|below]"
        )
        return

    symbol = args[0].upper()
    try:
        target_price = float(args[1])
    except ValueError:
        await update.message.reply_text("TARGET должен быть числом.")
        return

    if len(args) == 3:
        direction = args[2].lower()
        if direction not in ("above", "below"):
            await update.message.reply_text("Направление может быть 'above' или 'below'.")
            return
    else:
        current_price = await fetch_price(symbol)
        if current_price is None:
            await update.message.reply_text("Не удалось получить текущую цену.")
            return
        direction = "above" if target_price > current_price else "below"

    chat_id = update.effective_chat.id
    state.tracking[chat_id] = TrackingInfo(symbol=symbol, target_price=target_price, direction=direction)

    await update.message.reply_text(
        f"Отслеживание запущено!\n"
        f"Символ: {symbol}\n"
        f"Цель: {target_price}\n"
        f"Направление: {direction}"
    )

async def telethon_send_message(client: TelegramClient, text: str) -> None:
    try:
        await client.send_message(TARGET_GROUP_USERNAME, text)
        logger.info(f"Отправлено сообщение в группу {TARGET_GROUP_USERNAME}: {text}")
    except Exception as exc:
        logger.error(f"Не удалось отправить сообщение в группу {TARGET_GROUP_USERNAME}: {exc}")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id in state.tracking:
        del state.tracking[chat_id]
        await update.message.reply_text("Отслеживание остановлено.")
    else:
        await update.message.reply_text("Ничего не отслеживается.")

async def price_monitor_loop(context: ContextTypes.DEFAULT_TYPE) -> None:
    telethon_client = context.bot_data.get("telethon_client")
    if telethon_client is None:
        logger.warning("Telethon-клиент не найден в контексте.")
        return

    for chat_id, info in list(state.tracking.items()):
        price = await fetch_price(info.symbol)
        if price is None:
            continue

        reached = False
        if info.direction == "above" and price >= info.target_price:
            reached = True
        elif info.direction == "below" and price <= info.target_price:
            reached = True

        if reached:
            # Отправляем команду в группу через Telethon
            command_text = f"/gm@PushoverAlerterBot"
            await telethon_send_message(telethon_client, command_text)

            # Удаляем отслеживание (можно убрать, если хотите повторные уведомления)
            del state.tracking[chat_id]

# ===== Инициализация Telethon =====
async def init_telethon_client() -> TelegramClient:
    if SESSION_STRING:
        client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    else:
        client = TelegramClient('telethon_session', API_ID, API_HASH)

    await client.start()
    logger.info("✅ Telethon-клиент авторизован.")
    return client

# ===== Flask-сервер для Render.com =====
app_flask = Flask(__name__)

@app_flask.route('/')
def health():
    return "✅ MEXC Bot is running on Render.com!", 200

# ===== Главный запуск =====
async def main():
    # 1. Запускаем Telethon
    telethon_client = await init_telethon_client()

    # 2. Создаём Telegram-бота
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).job_queue(JobQueue()).build()

    # Регистрация команд
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("set", set_price))
    app.add_handler(CommandHandler("stop", stop))

    # Установка команд
    await app.bot.set_my_commands([
        BotCommand("start", "Показать приветственное сообщение"),
        BotCommand("set", "Установить отслеживание цены: /set SYMBOL TARGET"),
        BotCommand("stop", "Остановить отслеживание"),
    ])

    # Сохраняем Telethon-клиент в bot_data
    app.bot_data["telethon_client"] = telethon_client

    # Запускаем мониторинг каждые 30 сек
    app.job_queue.run_repeating(price_monitor_loop, interval=1, first=0)

    # Запускаем бота
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    # Запускаем Flask-сервер в отдельном потоке (для Render)
    from threading import Thread
    def run_flask():
        app_flask.run(host='0.0.0.0', port=int(os.getenv('PORT', 10000)))

    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Ждём завершения (бесконечно)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
