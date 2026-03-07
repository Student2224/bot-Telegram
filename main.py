import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import httpx
from flask import Flask
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes, JobQueue
from telethon import TelegramClient
from telethon.sessions import StringSession
from datetime import datetime, time

# ===== Конфигурация =====
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

MEXC_TICKER_URL = "https://api.mexc.com/api/v3/ticker/price"
API_ID = 32990800
API_HASH = "f14259b31ea4bc638814833d6de13bd5"
TARGET_GROUP_USERNAME = "@alertgomno2"
TELEGRAM_BOT_TOKEN = "8213546201:AAFIFDmFqtjibgd9CkfsGGgWnb1_tTXfe8c"

# 🚨 ВАЖНО: Сессия Telethon передаётся через переменную окружения
SESSION_STRING = os.getenv("TELETHON_SESSION_STRING")

# ===== Состояние бота =====
@dataclass
class CoinInfo:
    symbol: str
    target_price: float
    direction: str = "above"

@dataclass
class BotState:
    tracking: Dict[int, List[CoinInfo]] = field(default_factory=dict)  # 👈 Теперь список монет на пользователя

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
        "Используй:\n"
        "/add SYMBOL TARGET [above|below] — добавить монету\n"
        "/list — посмотреть все отслеживаемые\n"
        "/remove N — удалить монету по номеру\n"
        "Пример: /add BTCUSDT 67000 above"
    )

async def add_coin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) not in (2, 3):
        await update.message.reply_text(
            "❌ Использование: /add <символ> <цена> [above|below]\n"
            "Пример: /add BTCUSDT 67000 above"
        )
        return

    symbol = args[0].upper().strip()
    try:
        target_price = float(args[1])
    except ValueError:
        await update.message.reply_text("❌ Цена должна быть числом!")
        return

    if len(args) == 3:
        direction = args[2].lower().strip()
        if direction not in ("above", "below"):
            await update.message.reply_text("❌ Направление должно быть 'above' или 'below'!")
            return
    else:
        current_price = await fetch_price(symbol)
        if current_price is None:
            await update.message.reply_text("❌ Не удалось получить текущую цену.")
            return
        direction = "above" if target_price > current_price else "below"

    chat_id = update.effective_chat.id

    # Инициализируем список для пользователя, если ещё не создан
    if chat_id not in state.tracking:
        state.tracking[chat_id] = []

    # Проверка на дубликаты (точно такая же монета уже есть?)
    for coin in state.tracking[chat_id]:
        if (coin.symbol == symbol and
            abs(coin.target_price - target_price) < 0.01 and
            coin.direction == direction):
            await update.message.reply_text(f"⚠️ {symbol} ({direction} {target_price}) уже отслеживается!")
            return

    # Добавляем новую монету
    new_coin = CoinInfo(symbol=symbol, target_price=target_price, direction=direction)
    state.tracking[chat_id].append(new_coin)

    await update.message.reply_text(f"✅ {symbol} ({direction} {target_price:.2f}) добавлен в отслеживание!")

async def list_coins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id not in state.tracking or not state.tracking[chat_id]:
        await update.message.reply_text("📭 Вы пока не отслеживаете ни одну монету. Используйте /add")
        return

    coins = state.tracking[chat_id]
    message = "📋 Ваши отслеживаемые монеты:\n\n"
    for i, coin in enumerate(coins, 1):
        message += f"{i}. {coin.symbol} → {coin.direction} {coin.target_price:.2f}\n"

    await update.message.reply_text(message)

async def remove_coin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await update.message.reply_text("❌ Использование: /remove <номер>\nПример: /remove 2")
        return

    try:
        index = int(args[0]) - 1  # Пользователь вводит 1, 2, 3 → индекс 0, 1, 2
    except ValueError:
        await update.message.reply_text("❌ Введите число!")
        return

    chat_id = update.effective_chat.id
    if chat_id not in state.tracking or index < 0 or index >= len(state.tracking[chat_id]):
        await update.message.reply_text("❌ Неверный номер монеты!")
        return

    removed = state.tracking[chat_id].pop(index)
    await update.message.reply_text(f"🗑️ Удалено: {removed.symbol} ({removed.direction} {removed.target_price:.2f})")

async def telethon_send_message(client: TelegramClient, text: str) -> None:
    try:
        await client.send_message(TARGET_GROUP_USERNAME, text)
        logger.info(f"📤 Отправлено сообщение в группу {TARGET_GROUP_USERNAME}: {text}")
    except Exception as exc:
        logger.error(f"❌ Не удалось отправить сообщение в группу {TARGET_GROUP_USERNAME}: {exc}")

async def price_monitor_loop(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now().time()
    start_time = time(5, 0)   # 5:00 AM
    end_time = time(21, 0)    # 9:00 PM

    # 🚫 Если сейчас не в рабочем интервале — выходим
    if not (start_time <= now <= end_time):
        logger.info("🕒 Сейчас вне рабочего времени (5:00–21:00). Пропускаем мониторинг.")
        return

    logger.info("⏳ Запуск мониторинга цен (рабочее время)...")

    telethon_client = context.bot_data.get("telethon_client")
    if telethon_client is None:
        logger.warning("Telethon-клиент не найден в контексте.")
        return

    # Проходим по всем пользователям и их спискам монет
    for chat_id, coin_list in list(state.tracking.items()):
        # Создаём копию списка, чтобы безопасно удалять элементы во время итерации
        for coin in list(coin_list):
            price = await fetch_price(coin.symbol)
            if price is None:
                continue  # Пропускаем, если не получили цену

            reached = False
            if coin.direction == "above" and price >= coin.target_price:
                reached = True
            elif coin.direction == "below" and price <= coin.target_price:
                reached = True

            if reached:
                # Отправляем команду в группу через Telethon
                command_text = f"/gm@PushoverAlerterBot"
                try:
                    await telethon_send_message(telethon_client, command_text)
                    logger.info(f"✅ Уведомление отправлено для {coin.symbol} (цена: {price:.2f})")
                except Exception as e:
                    logger.error(f"❌ Не удалось отправить уведомление для {coin.symbol}: {e}")

                # Удаляем только эту монету из списка (не весь chat_id!)
                coin_list.remove(coin)
                logger.info(f"🗑️ Удалено отслеживание: {coin.symbol} для chat_id {chat_id}")

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
    app.add_handler(CommandHandler("add", add_coin))
    app.add_handler(CommandHandler("list", list_coins))
    app.add_handler(CommandHandler("remove", remove_coin))

    # Установка команд
    await app.bot.set_my_commands([
        BotCommand("start", "Показать приветственное сообщение"),
        BotCommand("add", "Добавить монету для отслеживания"),
        BotCommand("list", "Показать все отслеживаемые монеты"),
        BotCommand("remove", "Удалить монету по номеру"),
    ])

    # 👇 ВАЖНО: Инициализируем tracking как список, а не словарь
    app.bot_data["tracking"] = {}  # {chat_id: [CoinInfo, ...]}
    app.bot_data["telethon_client"] = telethon_client

    # Запускаем мониторинг каждые 30 сек (можно уменьшить до 10, если нужно быстрее)
    app.job_queue.run_repeating(price_monitor_loop, interval=2, first=0)

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
