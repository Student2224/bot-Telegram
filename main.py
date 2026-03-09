import asyncio
from datetime import datetime, time
import logging
from dataclasses import dataclass
from typing import List, Dict, Optional

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
from dotenv import load_dotenv
import os

load_dotenv()
# --------------------------- Конфигурация ---------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MEXC_TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/price"
TELETHON_API_ID = os.getenv("TELETHON_API_ID")
TELETHON_API_HASH = os.getenv("TELETHON_API_HASH")
TELETHON_SESSION_STRING = os.getenv("TELETHON_SESSION_STRING")
CHECK_INTERVAL = 2          # секунд
TARGET_CHAT_ID = "@alertgomno2"

# --------------------------- Логирование ---------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --------------------------- Модели ---------------------------
@dataclass(eq=True, frozen=True)
class CoinInfo:
    """Информация о монете, которую пользователь хочет отслеживать."""
    symbol: str
    target_price: float
    direction: str          # "above" или "below"

# --------------------------- Инициализация Telethon ---------------------------
async def init_telethon_client() -> TelegramClient:
    client = TelegramClient(
        StringSession(TELETHON_SESSION_STRING),
        TELETHON_API_ID,
        TELETHON_API_HASH,
    )
    await client.connect()
    if not client.is_connected():
        raise RuntimeError("Не удалось подключиться к Telethon")
    logger.info("✅ Telethon клиент инициализирован")
    return client

# --------------------------- Запрос цены ---------------------------
async def fetch_price(symbol: str, http_client: httpx.AsyncClient) -> Optional[float]:
    params = {"symbol": symbol.upper()}
    try:
        resp = await http_client.get(MEXC_TICKER_URL, params=params, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        return float(data["price"])
    except Exception as exc:   # pragma: no cover
        logger.error(f"Ошибка при получении цены для {symbol}: {exc}")
        return None

# --------------------------- Отправка сообщения через Telethon ---------------------------
async def telethon_send_message(client: TelegramClient, message: str) -> None:
    try:
        await client.send_message(TARGET_CHAT_ID, message)
        logger.info(f"📤 Отправлено сообщение: {message[:50]}...")
    except Exception as exc:   # pragma: no cover
        logger.error(f"❌ Ошибка отправки через Telethon: {exc}")

# --------------------------- Мониторинг цен ---------------------------
async def price_monitor_loop(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Цикл, который каждый CHECK_INTERVAL секунд проверяет цены всех
    монет, находящихся в словаре `tracking`.
    """
    now = datetime.now().time()
    start_time = time(5, 0)   # 5:00 AM
    end_time = time(21, 0)    # 9:00 PM

    # 🚫 Если сейчас не в рабочем интервале — выходим
    if not (start_time <= now <= end_time):
        logger.info("🕒 Сейчас вне рабочего времени (5:00–21:00). Пропускаем мониторинг.")
        return

    logger.info("⏳ Запуск мониторинга цен (рабочее время)...")
    http_client = context.bot_data["http_client"]  # ✅ Используем один клиент!
    telethon_client = context.bot_data.get("telethon_client")
    if telethon_client is None:
        logger.warning("Telethon-клиент не найден в контексте.")
        return
    http_client: httpx.AsyncClient = context.bot_data["http_client"]
    telethon_client: TelegramClient = context.bot_data["telethon_client"]
    tracking: Dict[int, List[CoinInfo]] = context.bot_data["tracking"]
    # Храним предыдущее значение цены, чтобы обнаружить пересечение.
    # Структура: {chat_id: {symbol: previous_price}}
    prev_prices: Dict[int, Dict[str, float]] = context.bot_data.setdefault("prev_prices", {})

    for chat_id, coin_list in list(tracking.items()):
        if not coin_list:
            del tracking[chat_id]
            logger.info(f"🗑️ Удалён пустой список пользователей: chat_id={chat_id}")
            continue

        # Убеждаемся, что для данного чата есть контейнер под предыдущие цены
        prev_prices.setdefault(chat_id, {})

        for coin in list(coin_list):
            price = await fetch_price(coin.symbol, http_client)
            if price is None:
                continue

            # Предыдущее значение цены (может быть None при первой проверке)
            prev_price = prev_prices[chat_id].get(coin.symbol)

            # ------------------- ЛОГИКА ПЕРЕСЕЧЕНИЯ -------------------
            reached = False
            if coin.direction == "above":
                # Нужно, чтобы цена **пересекла** уровень снизу вверх
                if prev_price is not None and prev_price < coin.target_price <= price:
                    reached = True
            else:  # "below"
                # Пересечение сверху вниз
                if prev_price is not None and prev_price > coin.target_price >= price:
                    reached = True
            # ---------------------------------------------------------

            # Сохраняем текущую цену для следующей итерации
            prev_prices[chat_id][coin.symbol] = price

            if reached:
                message = (
                    f"🔔 Уведомление о цене!\n"
                    f"Монета: {coin.symbol}\n"
                    f"Цель: {coin.target_price} ({coin.direction})\n"
                    f"Текущая цена: {price:.6f}".rstrip('0').rstrip('.')
                )
                await telethon_send_message(telethon_client, message)

                # Если вам всё‑равно нужен отдельный /gm‑команда, её можно отправить так:
                gm_command = "/gm@PushoverAlerterBot"
                await telethon_send_message(telethon_client, gm_command)

                # Удаляем монету из списка, т.к. цель уже достигнута
                coin_list.remove(coin)
                logger.info(
                    f"✅ Удалена монета {coin.symbol} из списка {chat_id} (цена достигнута)"
                )

# --------------------------- Обработчики команд ---------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я бот для отслеживания цен криптовалют.\n"
        "Используй /add <символ> <цена> [above|below] для добавления монеты.\n"
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

    # ------------------- Определяем направление -------------------
    if len(args) == 3:
        direction = args[2].lower().strip()
        if direction not in ("above", "below"):
            await update.message.reply_text("❌ Направление должно быть 'above' или 'below'!")
            return
    else:
        # Если направление не указано – определяем его автоматически,
        # сравнивая с текущей ценой.
        http_client: httpx.AsyncClient = context.bot_data["http_client"]
        current_price = await fetch_price(symbol, http_client)
        if current_price is None:
            await update.message.reply_text("❌ Не удалось получить текущую цену.")
            return
        direction = "above" if target_price > current_price else "below"
    # ---------------------------------------------------------

    chat_id = update.effective_chat.id
    tracking: Dict[int, List[CoinInfo]] = context.bot_data["tracking"]

    # Инициализируем список для пользователя, если ещё нет
    tracking.setdefault(chat_id, [])

    # Проверка на дубликаты (точно такая же монета уже есть?)
    for coin in tracking[chat_id]:
        if (
            coin.symbol == symbol
            and abs(coin.target_price - target_price) < 0.000001
            and coin.direction == direction
        ):
            await update.message.reply_text(
                f"⚠️ {symbol} ({direction} {target_price}) уже отслеживается!"
            )
            return

    # Добавляем новую монету
    new_coin = CoinInfo(symbol=symbol, target_price=target_price, direction=direction)
    tracking[chat_id].append(new_coin)

    await update.message.reply_text(
        f"✅ {symbol} ({direction} {target_price:.4f}) добавлен в отслеживание!"
    )

async def remove_coin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Использование: /remove <символ>")
        return

    symbol = context.args[0].upper()
    tracking: Dict[int, List[CoinInfo]] = context.bot_data["tracking"]
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
            f"❌ Удалена монета {removed.symbol} с целью {removed.target_price:.6f} ({removed.direction})"
            .rstrip('0')
            .rstrip('.')
        )
    else:
        await update.message.reply_text(f"Монета {symbol} не найдена в вашем списке.")

async def list_coins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tracking: Dict[int, List[CoinInfo]] = context.bot_data["tracking"]
    chat_id = update.effective_chat.id

    if chat_id not in tracking or not tracking[chat_id]:
        await update.message.reply_text("У вас нет отслеживаемых монет.")
        return

    coins = tracking[chat_id]
    message = "📋 Ваши отслеживаемые монеты:\n"
    for coin in coins:
        message += (
            f"• {coin.symbol}: {coin.target_price:.6f} ({coin.direction})\n"
            .rstrip('0')
            .rstrip('.')
        )
    await update.message.reply_text(message)

async def clear_coins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tracking: Dict[int, List[CoinInfo]] = context.bot_data["tracking"]
    chat_id = update.effective_chat.id
    if chat_id in tracking:
        tracking[chat_id].clear()
        await update.message.reply_text("🗑️ Все монеты удалены из вашего списка.")
    else:
        await update.message.reply_text("Ваш список пуст.")

# --------------------------- Точка входа ---------------------------
async def main() -> None:
    # Инициализируем Telethon один раз
    telethon_client = await init_telethon_client()

    # Один HTTP‑клиент на весь бот (экономит соединения)
    http_client = httpx.AsyncClient(timeout=10)

    # Создаём приложение Telegram‑Bot
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .job_queue(JobQueue())
        .build()
    )

    # Сохраняем общие ресурсы в bot_data
    app.bot_data["telethon_client"] = telethon_client
    app.bot_data["http_client"] = http_client
    app.bot_data["tracking"] = {}          # {chat_id: List[CoinInfo]}
    # `prev_prices` будет создано в price_monitor_loop при первой итерации

    # Регистрация команд
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_coin))
    app.add_handler(CommandHandler("remove", remove_coin))
    app.add_handler(CommandHandler("list", list_coins))
    app.add_handler(CommandHandler("clear", clear_coins))

    # Запуск периодической проверки цен
    app.job_queue.run_repeating(
        price_monitor_loop,
        interval=CHECK_INTERVAL,
        first=1,
    )

    logger.info("🚀 Бот запущен. Ожидание команд...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    # Ожидаем завершения (Ctrl+C)
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Бот остановлен пользователем.")
    except Exception as exc:   # pragma: no cover
        logger.error(f"❌ Критическая ошибка: {exc}")

