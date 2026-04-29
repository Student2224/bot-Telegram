import time
import logging
import asyncio
import os
import requests
from dotenv import load_dotenv
from telegram import Bot
from telegram.ext import Application

# Загружаем переменные из .env файла
load_dotenv()

# --- Настройки ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID='@farmtemki'  # Дефолтное значение
GROWTH_THRESHOLD = 2.4  # Для теста стоит 1.0, можно вернуть на 5.0
CHECK_INTERVAL = 5  # Проверка раз в 30 секунд

# Кэш для сокращения API-запросов
_symbols_cache = None
_last_cache_update = 0
CACHE_DURATION = 3600  # Обновляем кэш раз в час

# --- Логирование ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Функции API ---

def get_all_active_symbols() -> list:
    """Получение списка активных торговых пар с кэшированием"""
    global _symbols_cache, _last_cache_update
    
    current_time = time.time()
    if _symbols_cache and (current_time - _last_cache_update) < CACHE_DURATION:
        return _symbols_cache
    
    url = 'https://fapi.binance.com/fapi/v1/exchangeInfo'
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        data = response.json()
        symbols = [
            item['symbol'] 
            for item in data['symbols'] 
            if item['status'] == 'TRADING' 
            and item['symbol'].endswith('USDT')
        ]
        _symbols_cache = symbols  # Убрано ограничение — мониторим ВСЕ монеты
        _last_cache_update = current_time
        logger.info(f"Найдено и добавлено {len(symbols)} активных пар USDT для мониторинга")
        return _symbols_cache
    except Exception as e:
        logger.error(f"Ошибка получения списка монет: {e}")
        return _symbols_cache or []

def get_bulk_prices(symbols: list) -> dict:
    url = 'https://fapi.binance.com/fapi/v1/ticker/price'
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        all_prices = response.json()
        prices_dict = {item['symbol']: float(item['price']) for item in all_prices if item['symbol'] in symbols}
        return prices_dict
    except Exception as e:
        logger.error(f"Ошибка получения цен: {e}")
        return {}

def get_market_cap(symbol: str) -> str:
    """Получение 24-часового объема торгов"""
    url = f'https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={symbol}'
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        volume = float(data.get('quoteVolume', 0))  # Объем в USDT
        return f"{volume:,.2f}"
    except Exception:
        return "N/A"

async def send_telegram_message(bot, symbol: str, start_price: float, 
                              current_price: float, growth: float):
    """Отправка уведомления с подробной информацией"""
    market_cap = get_market_cap(symbol)
    
    text = (
        f"<b>🚀 Рост цены!</b>\n"
        f"Монета: <code>{symbol}</code>\n"
        f"Начальная цена: ${start_price:.4f}\n"
        f"Текущая цена: ${current_price:.4f}\n"
        f"Рост: <b>{growth:.2f}%</b>\n"
        f"Объем (24ч): ${market_cap}"
    )
    
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID, 
            text=text, 
            parse_mode='HTML'
        )
        logger.info(f"Уведомление отправлено для {symbol}: +{growth:.2f}%")
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения для {symbol}: {e}")

# --- Основной цикл ---

async def main():
    """Основной цикл работы бота"""
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    bot = application.bot
    
    logger.info("Бот запущен. Инициализация...")
    
    SYMBOLS = get_all_active_symbols()
    
    if not SYMBOLS:
        logger.error("Список монет пуст. Завершение работы.")
        return
    
    logger.info(f"Получение стартовых цен для {len(SYMBOLS)} монет...")
    initial_prices = get_bulk_prices(SYMBOLS)
    
    if not initial_prices:
        logger.error("Не удалось получить стартовые цены. Завершение работы.")
        return
    
        logger.info(f"Старт мониторинга. Интервал: {CHECK_INTERVAL}с. Порог: {GROWTH_THRESHOLD}%")
    
    # База для трекинга. Храним цену, от которой считаем рост.
    # Изначально это стартовая цена, но после срабатывания мы её обновим.
    tracking_prices = initial_prices.copy()
    
    while True:
        current_prices = get_bulk_prices(list(tracking_prices.keys()))
        
        for symbol, base_price in tracking_prices.items():
            current_price = current_prices.get(symbol)
            
            if current_price is None:
                continue
            
            growth = ((current_price - base_price) / base_price) * 100
            
            # Если рост превысил порог — уведомляем и обновляем базу
            if growth >= GROWTH_THRESHOLD:
                await send_telegram_message(
                    bot, symbol, base_price, 
                    current_price, growth
                )
                # СДВИГ ЦЕЛИ: новая база = текущая цена
                tracking_prices[symbol] = current_price
                logger.info(f"База для {symbol} обновлена на ${current_price:.4f}")
            
            # Опционально: можно добавить сброс, если цена УПАЛА (чтобы поймать новый импульс)
            # Например, если цена упала на 10% от нашей базы, сбрасываем базу на текущую,
            # чтобы мониторинг не был привязан к старым максимумам.
            elif growth <= -2.0:
                tracking_prices[symbol] = current_price
        
        # Асинхронная пауза
        await asyncio.sleep(CHECK_INTERVAL)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
