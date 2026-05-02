import time
import logging
import asyncio
import os
import datetime
import requests
from dotenv import load_dotenv
from telegram import Bot
from telegram.ext import Application

# Загружаем переменные из .env файла
load_dotenv()

# --- Настройки ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '@farmtemki')  # Читаем из .env, если нет - дефолт
GROWTH_THRESHOLD = 2.4  # Для теста стоит 1.0, можно вернуть на 5.0
CHECK_INTERVAL = 5  # Проверка раз в 5 секунд

# Кэш для сокращения API-запросов
_symbols_cache = None
_last_cache_update = 0
CACHE_DURATION = 3600  # Обновляем кэш раз в час

# Кэш для Market Cap (CoinGecko)
_market_cap_cache = {}  # Формат: {'BTCUSDT': 1200000000000, ...}
_market_cap_last_update = 0
MARKET_CAP_CACHE_DURATION = 3600  # Обновляем раз в час

# Автоматический маппинг через список CoinGecko (загружается 1 раз)
_coingecko_list_cache = []  # Список всех монет [{'id': 'bitcoin', 'symbol': 'btc', ...}]
_coingecko_symbol_map = {}  # Словарь для быстрого поиска: {'btc': 'bitcoin', ...}
_coingecko_list_last_update = 0
COINGECKO_LIST_CACHE_DURATION = 86400  # Обновляем раз в сутки

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
        _symbols_cache = symbols
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

def load_coingecko_list():
    """Загружает полный список монет с CoinGecko для автоматического маппинга (1 раз в сутки)"""
    global _coingecko_list_cache, _coingecko_symbol_map, _coingecko_list_last_update
    current_time = time.time()
    
    if _coingecko_list_cache and (current_time - _coingecko_list_last_update) < COINGECKO_LIST_CACHE_DURATION:
        return _coingecko_list_cache
    
    logger.info("Загрузка списка монет CoinGecko для маппинга...")
    url = "https://api.coingecko.com/api/v3/coins/list"
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        _coingecko_list_cache = data
        
        _coingecko_symbol_map.clear()
        for item in data:
            symbol = item.get('symbol', '').lower()
            coin_id = item.get('id', '')
            if symbol and coin_id:
                if symbol not in _coingecko_symbol_map:
                    _coingecko_symbol_map[symbol] = coin_id
        
        _coingecko_list_last_update = current_time
        logger.info(f"Загружено {len(_coingecko_symbol_map)} монет для автоматического маппинга")
        return _coingecko_list_cache
    except Exception as e:
        logger.error(f"Ошибка загрузки списка CoinGecko: {e}")
        return _coingecko_list_cache or []

def get_coingecko_id(symbol: str) -> str:
    """Конвертирует символ Binance (BTCUSDT) в ID CoinGecko (bitcoin) через автоматический маппинг"""
    load_coingecko_list()
    
    base_symbol = symbol.replace('USDT', '').lower()
    cg_id = _coingecko_symbol_map.get(base_symbol)
    
    return cg_id if cg_id else base_symbol

def load_market_caps_from_coingecko(symbols: list) -> dict:
    """Загружает Market Cap для списка монет с CoinGecko (1 раз при старте)"""
    global _market_cap_cache, _market_cap_last_update
    current_time = time.time()
    
    if _market_cap_cache and (current_time - _market_cap_last_update) < MARKET_CAP_CACHE_DURATION:
        return _market_cap_cache
    
    logger.info("Загрузка Market Cap с CoinGecko...")
    
    ids = []
    symbol_to_id = {}
    for sym in symbols:
        cg_id = get_coingecko_id(sym)
        if cg_id:
            ids.append(cg_id)
            symbol_to_id[cg_id] = sym
    
    ids = list(set(ids))
    new_cache = {}
    chunk_size = 200
    
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i:i + chunk_size]
        ids_str = ','.join(chunk)
        url = f'https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids={ids_str}&order=market_cap_desc&per_page=250&page=1&sparkline=false'
        
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            for item in data:
                cg_id = item.get('id')
                market_cap = item.get('market_cap', 0)
                if cg_id in symbol_to_id:
                    binance_sym = symbol_to_id[cg_id]
                    new_cache[binance_sym] = market_cap
        except Exception as e:
            logger.error(f"Ошибка загрузки данных CoinGecko: {e}")
            return _market_cap_cache or {}
    
    _market_cap_cache = new_cache
    _market_cap_last_update = current_time
    logger.info(f"Загружено Market Cap для {len(new_cache)} монет")
    return _market_cap_cache

def get_market_cap(symbol: str) -> str:
    """Получение Market Cap из кэша"""
    market_cap = _market_cap_cache.get(symbol)
    if market_cap is None:
        return "N/A"
    
    if market_cap >= 1_000_000_000:
        return f"${market_cap / 1_000_000_000:.2f}B"
    elif market_cap >= 1_000_000:
        return f"${market_cap / 1_000_000:.2f}M"
    else:
        return f"${market_cap:,.0f}"

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
        f"Капитализация: {market_cap}"
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
    if not TELEGRAM_BOT_TOKEN:
        logger.error("Токен бота не найден! Проверьте файл .env")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    bot = application.bot
    
    logger.info("Бот запущен. Режим работы: 01:57 - 03:10")
    
    while True:
        now = datetime.datetime.now()
        
        # Устанавливаем время начала (01:57) и конца (03:10) на сегодня
        start_time = now.replace(hour=19, minute=50, second=0, microsecond=0)
        end_time = now.replace(hour=20, minute=35, second=0, microsecond=0)
        
        # Если время уже после окончания периода
        if now >= end_time:
            start_time += datetime.timedelta(days=1)
            end_time += datetime.timedelta(days=1)
            wait_seconds = (start_time - now).total_seconds()
            logger.info(f"Вне рабочего времени. Сон до {start_time.strftime('%H:%M:%S')} (завтра)")
            await asyncio.sleep(wait_seconds)
            continue
        
        # Если время до начала периода
        if now < start_time:
            wait_seconds = (start_time - now).total_seconds()
            logger.info(f"Ожидание начала периода (01:57). Сон до {start_time.strftime('%H:%M:%S')}")
            await asyncio.sleep(wait_seconds)
            continue
        
                                # --- ПЕРИОД МОНИТОРИНГА (с 01:57 до 03:10) ---
        logger.info(f"*** СТАРТ ПЕРИОДА МОНИТОРИНГА (до 03:10) ***")
        
        SYMBOLS = get_all_active_symbols()
        if not SYMBOLS:
            logger.error("Список монет пуст. Пропуск сеанса.")
            await asyncio.sleep((end_time - datetime.datetime.now()).total_seconds())
            continue
        
        # Загрузка Market Cap с CoinGecko
        logger.info("Загрузка капитализации с CoinGecko...")
        load_market_caps_from_coingecko(SYMBOLS)
        
        logger.info(f"Получение стартовых цен для {len(SYMBOLS)} монет...")
        initial_prices = get_bulk_prices(SYMBOLS)
        if not initial_prices:
            logger.error("Не удалось получить цены. Пропуск сеанса.")
            await asyncio.sleep((end_time - datetime.datetime.now()).total_seconds())
            continue
        
        logger.info(f"Мониторинг активен. Порог: {GROWTH_THRESHOLD}%, Интервал: {CHECK_INTERVAL}с.")
        tracking_prices = initial_prices.copy()
        
        while True:
            now = datetime.datetime.now()
            if now >= end_time:
                logger.info("*** ПЕРИОД МОНИТОРИНГА ЗАКОНЧИЛСЯ (03:10) ***")
                break
            
            current_prices = get_bulk_prices(list(tracking_prices.keys()))
            
            for symbol, base_price in tracking_prices.items():
                current_price = current_prices.get(symbol)
                if current_price is None:
                    continue
                
                growth = ((current_price - base_price) / base_price) * 100
                
                if growth >= GROWTH_THRESHOLD:
                    await send_telegram_message(bot, symbol, base_price, current_price, growth)
                    tracking_prices[symbol] = current_price
                    logger.info(f"База для {symbol} обновлена на ${current_price:.4f}")
                elif growth <= -2.0:
                    tracking_prices[symbol] = current_price
            
            await asyncio.sleep(CHECK_INTERVAL)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
