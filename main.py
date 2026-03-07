import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional, Dict
import httpx
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    JobQueue,
)
from telethon import TelegramClient
from telethon.sessions import StringSession

# --- Конфигурация ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MEXC_TICKER_URL = "https://api.mexc.com/api/v3/ticker/price"
TELETHON_API_ID = int(os.getenv("API_ID"))
TELETHON_API_HASH = os.getenv("API_HASH")
TELETHON_SESSION_STRING = os.getenv("TELETHON_SESSION_STRING")
CHECK_INTERVAL = 2  # секунд
TARGET_CHAT_ID = os.getenv("TARGET_GROUP_USERNAME")  # куда отправлять уведомления

# --- Логирование ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# --- Данные монеты ---
@dataclass
class CoinInfo:
    symbol: str
    target_price: float
    direction: str  # "above" или "below"

    def __eq__(self, other):
        return isinstance(other, CoinInfo) and self.symbol == other.symbol

    def __hash__(self):
        return hash(self.symbol)


# --- Инициализация Telethon ---
async def init_telethon_client() -> TelegramClient:
    client = TelegramClient(StringSession(TELETHON_SESSION_STRING), TELETHON_API_ID, TELETHON_API_HASH)
    await client.connect()
    if not await client.is_connected():
        raise RuntimeError("Не удалось подключиться к Telethon")
    logger.info("✅ Telethon клиент инициализирован")
    return client


# --- HTTP-запрос цены ---
async def fetch_price(symbol: str, http_client: httpx.AsyncClient) -> Optional[float]:
    params = {"symbol": symbol.upper()}
    try:
        resp = await http_client.get(MEXC_TICKER_URL, params=params, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        return float(data["price"])
    except Exception as exc:
        logger.error(f"Ошибка при получении цены для {symbol}: {exc}")
        return None


# --- Отправка сообщения через Telethon ---
async def telethon_send_message(client: TelegramClient, message: str) -> None:
    try:
        await client.send_message(TARGET_CHAT_ID, message)
        logger.info(f"📤 Отправлено сообщение: {message[:50]}...")
    except Exception as exc:
        logger.error(f"❌ Ошибка отправки через Telethon: {exc}")


# --- Мониторинг цен ---
async def price_monitor_loop(context: ContextTypes.DEFAULT_TYPE) -> None:
    http_client = context.bot_data["http_client"]
    telethon_client = context.bot_data["telethon_client"]
    tracking = context.bot_data["tracking"]  # {chat_id: List[CoinInfo]}

    # Проверка подключения Telethon
    if not telethon_client.is_connected():
        logger.warning("⚠️ Telethon клиент не подключен. Переподключение...")
        try:
            await telethon_client.connect()
            logger.info("✅ Telethon переподключен.")
        except Exception as e:
            logger.error(f"❌ Не удалось переподключить Telethon: {e}")
            return

    # Копируем список для безопасного итерирования
    for chat_id in list(tracking.keys()):
        coin_list = tracking[chat_id]
        if not coin_list:
            del tracking[chat_id]  # Удаляем пустые списки
            logger.info(f"🗑️ Удалён пустой список пользователей: chat_id={chat_id}")
            continue

        for coin in list(coin_list):
            price = await fetch_price(coin.symbol, http_client)
            if price is None:
                continue

            reached = False
            if coin.direction == "above" and price >= coin.target_price:
                reached = True
            elif coin.direction == "below" and price <= coin.target_price:
                reached = True

            if reached:
                message = (
                    f"🔔 Уведомление о цене!\n"
                    f"Монета: {coin.symbol}\n"
                    f"Цель: {coin.target_price} ({coin.direction})\n"
                    f"Текущая цена: {price:.6f}".rstrip('0').rstrip('.')
                )
                await telethon_send_message(telethon_client, message)
                coin_list.remove(coin)
                logger.info(f"✅ Удалена монета {coin.symbol} из списка {chat_id} (цена достигнута)")


# --- Команды бота ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я бот для отслеживания цен криптовалют.\n"
        "Используй /add <символ> <цена> <above/below> для добавления монеты.\n"
        "Например: /add BTC 60000 above"
    )


async def add_coin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or len(context.args) != 3:
        await update.message.reply_text(
            "Использование: /add <символ> <цена> <above/below>\n"
            "Пример: /add BTC 60000 above"
        )
        return

    symbol = context.args[0].upper()
    try:
        target_price = float(context.args[1])
    except ValueError:
        await update.message.reply_text("Цена должна быть числом.")
        return

    direction = context.args[2].lower()
    if direction not in ("above", "below"):
        await update.message.reply_text("Направление должно быть 'above' или 'below'.")
        return

    coin = CoinInfo(symbol=symbol, target_price=target_price, direction=direction)

    tracking = context.bot_data["tracking"]
    chat_id = update.effective_chat.id
    if chat_id not in tracking:
        tracking[chat_id] = []

    if coin in tracking[chat_id]:
        await update.message.reply_text(f"Монета {symbol} уже отслеживается.")
        return

    tracking[chat_id].append(coin)
    await update.message.reply_text(
        f"✅ Добавлена монета {symbol} с целью {target_price:.6f} ({direction})".rstrip('0').rstrip('.')
    )


async def remove_coin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Использование: /remove <символ>")
        return

    symbol = context.args[0].upper()
    tracking = context.bot_data["tracking"]
    chat_id = update.effective_chat.id

    if chat_id not in tracking:
        await update.message.reply_text("У вас нет отслеживаемых монет.")
        return

    coin_list = tracking[chat_id]
    removed = None
    for coin in coin_list:
        if coin.symbol == symbol:
            removed = coin
            break

    if removed:
        coin_list.remove(removed)
        await update.message.reply_text(
            f"❌ Удалена монета {removed.symbol} с целью {removed.target_price:.6f} ({removed.direction})".rstrip('0').rstrip('.')
        )
    else:
        await update.message.reply_text(f"Монета {symbol} не найдена в вашем списке.")


async def list_coins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tracking = context.bot_data["tracking"]
    chat_id = update.effective_chat.id

    if chat_id not in tracking or not tracking[chat_id]:
        await update.message.reply_text("У вас нет отслеживаемых монет.")
        return

    coins = tracking[chat_id]
    message = "📋 Ваши отслеживаемые монеты:\n"
    for coin in coins:
        message += f"• {coin.symbol}: {coin.target_price:.6f} ({coin.direction})\n".rstrip('0').rstrip('.')
    await update.message.reply_text(message)


async def clear_coins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tracking = context.bot_data["tracking"]
    chat_id = update.effective_chat.id
    if chat_id in tracking:
        tracking[chat_id].clear()
        await update.message.reply_text("🗑️ Все монеты удалены из вашего списка.")
    else:
        await update.message.reply_text("Ваш список пуст.")


# --- Главная функция ---
async def main() -> None:
    # Инициализация Telethon
    telethon_client = await init_telethon_client()

    # Создание HTTP-клиента (один на весь бот)
    http_client = httpx.AsyncClient(timeout=10)

    # Создание бота
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).job_queue(JobQueue()).build()

    # Сохраняем клиенты в bot_data
    app.bot_data["telethon_client"] = telethon_client
    app.bot_data["http_client"] = http_client
    app.bot_data["tracking"] = {}  # {chat_id: List[CoinInfo]}

    # Регистрация обработчиков
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_coin))
    app.add_handler(CommandHandler("remove", remove_coin))
    app.add_handler(CommandHandler("list", list_coins))
    app.add_handler(CommandHandler("clear", clear_coins))

    # Запуск мониторинга цен каждые 2 секунды
    app.job_queue.run_repeating(price_monitor_loop, interval=CHECK_INTERVAL, first=1)

    logger.info("🚀 Бот запущен. Ожидание команд...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    # Запуск в бесконечном цикле
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Бот остановлен пользователем.")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}")


