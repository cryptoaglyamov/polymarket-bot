import os
import time
import json
import requests
from datetime import datetime, timezone, timedelta

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

# ================== НАСТРОЙКИ ==================

PRIVATE_KEY = os.environ.get('PRIVATE_KEY')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

# 👇 РЕАЛЬНЫЙ РЕЖИМ
REAL_MODE = True  # True = реальные ставки, False = тестовый режим

if not PRIVATE_KEY:
    raise ValueError("PRIVATE_KEY не найден в переменных окружения!")

print("PRIVATE_KEY загружен:", PRIVATE_KEY[:10] + "..." + PRIVATE_KEY[-6:])
print(f"🔧 РЕЖИМ: {'РЕАЛЬНЫЙ (ставки на реальные деньги)' if REAL_MODE else 'ТЕСТОВЫЙ (без реальных ставок)'}")

CHAIN_ID = 137
HOST = "https://clob.polymarket.com"

BASE_BET = 2.0
MAX_BET = 64.0
MIN_MULTIPLIER = 1.7
MAX_PRICE_FOR_OPPOSITE = 1.0 / MIN_MULTIPLIER  # ≈ 0.588
PRICE_BUFFER = 0.01

STATE_FILE = "bot_state.json"

# ========== ФУНКЦИЯ ОТПРАВКИ В ТЕЛЕГРАМ ==========

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram] Токен или chat_id не указаны → сообщение не отправлено")
        return
    
    # В реальном режиме убираем метку [ТЕСТ]
    if not REAL_MODE:
        msg = "🧪 [ТЕСТ]\n" + msg
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "HTML"
    }
    try:
        r = requests.post(url, json=payload, timeout=8)
        if r.status_code != 200:
            print(f"[Telegram] Ошибка отправки: {r.text}")
    except Exception as e:
        print(f"[Telegram] Ошибка: {e}")

# ========== ФУНКЦИИ ДЛЯ РАБОТЫ С СОСТОЯНИЕМ ==========

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            if "pending_bets" not in data:
                data["pending_bets"] = {}
            if "statistics" not in data:
                data["statistics"] = {
                    "total_profit": 0.0,
                    "total_bets": 0,
                    "wins": 0,
                    "losses": 0,
                    "history": [],
                    "max_loss_streak": 0,
                    "current_loss_streak": 0,
                    "last_reset_date": datetime.now().strftime('%Y-%m-%d'),
                    "last_6h_report": None,
                    "last_24h_report": None
                }
            if "last_results" not in data:
                data["last_results"] = {}
            if "martingale" not in data:
                data["martingale"] = {}
            if "last_balance_check" not in data:
                data["last_balance_check"] = None
            return data
    return {
        "pending_bets": {},
        "statistics": {
            "total_profit": 0.0,
            "total_bets": 0,
            "wins": 0,
            "losses": 0,
            "history": [],
            "max_loss_streak": 0,
            "current_loss_streak": 0,
            "last_reset_date": datetime.now().strftime('%Y-%m-%d'),
            "last_6h_report": None,
            "last_24h_report": None
        },
        "last_results": {},
        "martingale": {},
        "last_balance_check": None
    }

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def update_statistics(state, coin, result, profit, bet_amount, direction):
    """Обновляет статистику после завершения ставки"""
    stats = state["statistics"]
    
    # Добавляем в историю
    stats["history"].append({
        "timestamp": datetime.now().isoformat(),
        "coin": coin,
        "result": result,
        "profit": profit,
        "bet_amount": bet_amount,
        "direction": direction
    })
    
    # Обновляем общую статистику
    stats["total_bets"] += 1
    stats["total_profit"] += profit
    
    if profit > 0:
        stats["wins"] += 1
        stats["current_loss_streak"] = 0
        # Если выиграли - очищаем мартингейл для этой монеты
        if coin in state["martingale"]:
            del state["martingale"][coin]
    else:
        stats["losses"] += 1
        stats["current_loss_streak"] += 1
        # Обновляем максимальную серию поражений
        if stats["current_loss_streak"] > stats["max_loss_streak"]:
            stats["max_loss_streak"] = stats["current_loss_streak"]
        
        # Если проиграли - обновляем мартингейл
        next_bet = min(bet_amount * 2, MAX_BET)
        if coin not in state["martingale"]:
            state["martingale"][coin] = {
                "direction": direction,
                "next_bet": next_bet,
                "losses_count": 1
            }
        else:
            state["martingale"][coin]["next_bet"] = next_bet
            state["martingale"][coin]["losses_count"] += 1
    
    # Ограничиваем историю последними 1000 записями
    if len(stats["history"]) > 1000:
        stats["history"] = stats["history"][-1000:]
    
    save_state(state)

def update_last_result(state, coin, result):
    """Сохраняет последний результат для монеты"""
    if coin not in state["last_results"]:
        state["last_results"][coin] = []
    
    state["last_results"][coin].append({
        "timestamp": datetime.now().isoformat(),
        "result": result
    })
    
    if len(state["last_results"][coin]) > 2:
        state["last_results"][coin] = state["last_results"][coin][-2:]
    
    save_state(state)

def get_statistics_period(state, hours):
    """Получает статистику за указанный период"""
    stats = state["statistics"]
    now = datetime.now()
    period_ago = now - timedelta(hours=hours)
    
    period_profit = 0
    period_bets = 0
    period_wins = 0
    period_loss_streak = 0
    max_period_loss_streak = 0
    
    for entry in stats["history"]:
        entry_time = datetime.fromisoformat(entry["timestamp"])
        if entry_time > period_ago:
            period_profit += entry["profit"]
            period_bets += 1
            if entry["profit"] > 0:
                period_wins += 1
                period_loss_streak = 0
            else:
                period_loss_streak += 1
                if period_loss_streak > max_period_loss_streak:
                    max_period_loss_streak = period_loss_streak
    
    period_losses = period_bets - period_wins
    win_rate = (period_wins / period_bets * 100) if period_bets > 0 else 0
    
    return {
        "profit": period_profit,
        "bets": period_bets,
        "wins": period_wins,
        "losses": period_losses,
        "win_rate": win_rate,
        "max_loss_streak": max_period_loss_streak
    }

def check_reports(state):
    """Проверяет, нужно ли отправить отчеты за 6 и 24 часа"""
    now = datetime.now()
    stats = state["statistics"]
    need_6h = False
    need_24h = False
    
    # Проверка для 6-часового отчета
    if stats["last_6h_report"] is None:
        need_6h = True
    else:
        last_6h = datetime.fromisoformat(stats["last_6h_report"])
        if (now - last_6h).total_seconds() >= 6 * 3600:
            need_6h = True
    
    # Проверка для 24-часового отчета
    if stats["last_24h_report"] is None:
        need_24h = True
    else:
        last_24h = datetime.fromisoformat(stats["last_24h_report"])
        if (now - last_24h).total_seconds() >= 24 * 3600:
            need_24h = True
    
    return need_6h, need_24h

def get_current_balance(client):
    """Получает реальный баланс USDC с биржи"""
    try:
        # Получаем адрес кошелька
        address = client.get_address()
        print(f"Проверка баланса для адреса: {address}")
        
        # Прямой запрос к API Polymarket для получения баланса
        url = f"https://clob.polymarket.com/balance?address={address}"
        
        headers = {}
        if hasattr(client, '_api_creds') and client._api_creds:
            headers = {
                "Authorization": f"Bearer {client._api_creds.get('api_key', '')}",
                "Content-Type": "application/json"
            }
        
        print(f"Запрос к: {url}")
        resp = requests.get(url, headers=headers, timeout=10)
        print(f"Статус ответа: {resp.status_code}")
        
        if resp.status_code == 200:
            data = resp.json()
            print(f"Ответ: {data}")
            
            # Парсим разные форматы ответа
            if isinstance(data, dict):
                if 'balance' in data:
                    return float(data['balance'])
                elif 'usdc' in data:
                    return float(data['usdc'])
                elif 'amount' in data:
                    return float(data['amount'])
            elif isinstance(data, (int, float)):
                return float(data)
            elif isinstance(data, str):
                try:
                    return float(data)
                except:
                    pass
        
        # Если не получилось, пробуем другой эндпоинт
        url2 = f"https://clob.polymarket.com/balances"
        print(f"Пробуем альтернативный URL: {url2}")
        resp2 = requests.get(url2, headers=headers, timeout=10)
        
        if resp2.status_code == 200:
            data = resp2.json()
            print(f"Ответ: {data}")
            if isinstance(data, list):
                for item in data:
                    if item.get('currency') == 'USDC' or item.get('asset') == 'USDC':
                        return float(item.get('balance', 0))
        
        print("❌ Не удалось получить баланс через API")
        return None
        
    except Exception as e:
        print(f"Ошибка проверки баланса: {e}")
        import traceback
        traceback.print_exc()
        return None

def check_midnight():
    """Проверяет, наступила ли полночь по UTC+5"""
    now = datetime.now(timezone(timedelta(hours=5)))
    return now.hour == 0 and now.minute == 0

# ========== ФУНКЦИИ ДЛЯ РАБОТЫ С POLYMARKET ==========

def is_new_interval(minutes=15):
    """Проверяет, наступило ли начало интервала (в течение всей первой минуты)"""
    now = datetime.now(timezone(timedelta(hours=5)))
    return now.minute % minutes == 0

def get_market(slug: str):
    url = f"https://gamma-api.polymarket.com/markets?slug={slug}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data[0] if data else None
    except Exception as e:
        print(f"Ошибка gamma API {slug}: {e}")
        return None

def parse_prices(prices_field):
    """Парсинг цен из API"""
    try:
        if isinstance(prices_field, str):
            try:
                prices_str = prices_field.replace('\\"', '"')
                prices_list = json.loads(prices_str)
                return [float(p) for p in prices_list]
            except:
                import re
                numbers = re.findall(r"[\d.]+", prices_field)
                return [float(n) for n in numbers[:2]]
        elif isinstance(prices_field, list):
            prices = []
            for p in prices_field[:2]:
                if isinstance(p, str):
                    try:
                        prices.append(float(p))
                    except:
                        prices.append(0.5)
                elif isinstance(p, (int, float)):
                    prices.append(float(p))
                else:
                    prices.append(0.5)
            return prices
        return [0.5, 0.5]
    except Exception as e:
        print(f"Ошибка парсинга цен: {e}")
        return [0.5, 0.5]

def is_market_resolved(market):
    """Определяет, разрешен ли рынок"""
    if not market:
        return False
    
    prices = parse_prices(market.get("outcomePrices", ["0.5", "0.5"]))
    
    if prices[0] >= 0.85 or prices[1] >= 0.85:
        return True
    
    uma_status = market.get("umaResolutionStatus")
    if uma_status in ["resolved", "confirmed"]:
        return True
    
    return False

def get_winner(market):
    """Получает победителя на рынке"""
    if not market:
        return None
    
    prices = parse_prices(market.get("outcomePrices", ["0.5", "0.5"]))
    
    if prices[0] >= 0.85:
        return "Up"
    if prices[1] >= 0.85:
        return "Down"
    
    uma_status = market.get("umaResolutionStatus")
    if uma_status in ["resolved", "confirmed"]:
        return "Up" if prices[0] > prices[1] else "Down"
    
    return None

def get_token_id_and_price(market, direction: str):
    """Получение token ID и цены"""
    clob_ids = market.get("clobTokenIds", [])
    
    if isinstance(clob_ids, str):
        try:
            clob_ids = json.loads(clob_ids)
        except:
            clob_ids = []
    
    prices = parse_prices(market.get("outcomePrices", ["0.5", "0.5"]))
    
    index = 0 if direction == "Up" else 1
    
    if index >= len(clob_ids):
        return None, prices[index] if index < len(prices) else 0.5
    
    return clob_ids[index], prices[index]

def get_current_et_time():
    """Получает текущее время в ET для отображения"""
    now_utc5 = datetime.now(timezone(timedelta(hours=5)))
    et_now = now_utc5 - timedelta(hours=10)
    return et_now

def get_current_interval_timestamp(coin):
    """Получает правильный timestamp для текущего интервала (на основе UTC)"""
    # Текущее время в UTC
    now_utc = datetime.now(timezone.utc)
    
    # Округляем до начала 15-минутного интервала в UTC
    current_minute = now_utc.minute
    interval_start = (current_minute // 15) * 15
    interval_time_utc = now_utc.replace(minute=interval_start, second=0, microsecond=0)
    
    # Timestamp - это просто Unix время начала интервала в UTC
    timestamp = int(interval_time_utc.timestamp())
    
    # Для отладки покажем соответствие времени
    interval_time_et = interval_time_utc - timedelta(hours=5)
    
    print(f"Текущий интервал UTC: {interval_time_utc.hour}:{interval_time_utc.minute:02d}")
    print(f"Соответствует ET: {interval_time_et.hour}:{interval_time_et.minute:02d}")
    print(f"Timestamp: {timestamp}")
    
    return timestamp, interval_time_et

def get_interval_timestamp(coin, minutes_ago):
    """Получает timestamp для интервала, который был minutes_ago минут назад"""
    now_utc = datetime.now(timezone.utc)
    
    # Отнимаем нужное количество минут
    target_time_utc = now_utc - timedelta(minutes=minutes_ago)
    
    # Округляем до начала 15-минутного интервала
    target_minute = target_time_utc.minute
    interval_start = (target_minute // 15) * 15
    interval_time_utc = target_time_utc.replace(minute=interval_start, second=0, microsecond=0)
    
    timestamp = int(interval_time_utc.timestamp())
    
    interval_time_et = interval_time_utc - timedelta(hours=5)
    print(f"Интервал UTC: {interval_time_utc.hour}:{interval_time_utc.minute:02d}")
    print(f"Соответствует ET: {interval_time_et.hour}:{interval_time_et.minute:02d}")
    print(f"Timestamp: {timestamp}")
    
    return timestamp, interval_time_et

def get_market_by_timestamp(coin, timestamp):
    """Получает рынок по timestamp"""
    try:
        if coin == "BTC":
            slug = f"btc-updown-15m-{timestamp}"
        else:
            slug = f"eth-updown-15m-{timestamp}"
        
        print(f"Ищем рынок по slug: {slug}")
        url = f"https://gamma-api.polymarket.com/markets?slug={slug}"
        resp = requests.get(url, timeout=10)
        
        if resp.status_code == 200:
            markets = resp.json()
            if markets:
                market = markets[0]
                print(f"✅ Найден рынок: {market.get('question')}")
                return market
        
        # Если не нашли по точному slug, пробуем найти по времени в названии
        print(f"❌ Рынок по slug не найден, пробуем альтернативный поиск...")
        
        # Конвертируем timestamp обратно в время ET для поиска в названиях
        dt_utc = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        dt_et = dt_utc - timedelta(hours=5)
        hour = dt_et.hour
        minute = dt_et.minute
        day = dt_et.day
        
        ampm = "AM" if hour < 12 else "PM"
        hour_12 = hour if hour <= 12 else hour - 12
        if hour_12 == 0:
            hour_12 = 12
        
        month = dt_et.strftime("%B")
        time_str = f"{month} {day}, {hour_12}:{minute:02d} {ampm}"
        
        print(f"Ищем по времени: {time_str}")
        
        url = f"https://gamma-api.polymarket.com/markets?limit=100"
        resp = requests.get(url, timeout=10)
        
        if resp.status_code == 200:
            markets = resp.json()
            
            for market in markets:
                question = market.get('question', '')
                if coin in question and "15 min" in question.lower() and time_str in question:
                    print(f"✅ Найден по времени: {question}")
                    return market
        
        return None
    except Exception as e:
        print(f"Ошибка получения рынка по timestamp: {e}")
        return None

def get_interval_result(coin, minutes_ago):
    """
    Получает результат для интервала, который был minutes_ago минут назад
    """
    try:
        print(f"\n=== Получение результата для {coin}, {minutes_ago} минут назад ===")
        
        # Получаем timestamp на основе UTC
        timestamp, interval_time_et = get_interval_timestamp(coin, minutes_ago)
        
        # Получаем рынок
        market = get_market_by_timestamp(coin, timestamp)
        
        if not market:
            print(f"❌ Рынок для интервала не найден")
            return None
        
        # Только проверка разрешен ли рынок, без проверки времени
        if not is_market_resolved(market):
            print(f"⏳ Рынок для интервала еще не разрешен")
            return None
        
        winner = get_winner(market)
        if winner:
            print(f"✅ Результат: {winner}")
            return winner
        else:
            print(f"❌ Не удалось определить победителя")
            return None
        
    except Exception as e:
        print(f"Ошибка получения результата интервала: {e}")
        return None

def determine_bet_direction(coin, state):
    """
    Определяет направление ставки на основе последних результатов и мартингейла
    Возвращает (direction, bet_amount) или (None, None)
    """
    print(f"\n{'='*50}")
    print(f"АНАЛИЗ ДЛЯ {coin}")
    print(f"{'='*50}")
    
    # Проверяем, есть ли активная ставка
    bet_key = f"{coin}_last"
    if bet_key in state.get("pending_bets", {}):
        print(f"⏸️ Есть активная ставка, ждем ее завершения")
        return None, None
    
    # Проверяем мартингейл (были ли проигрыши подряд)
    if coin in state["martingale"]:
        martingale = state["martingale"][coin]
        print(f"📉 Продолжаем серию мартингейла:")
        print(f"   Направление: {martingale['direction']}")
        print(f"   Ставка: ${martingale['next_bet']}")
        print(f"   Проигрышей подряд: {martingale['losses_count']}")
        return martingale['direction'], martingale['next_bet']
    
    # Получаем результаты последних двух интервалов для начала новой серии
    result_minus_1 = get_interval_result(coin, 15)  # Предыдущий (15 мин назад)
    result_minus_2 = get_interval_result(coin, 30)  # Позапрошлый (30 мин назад)
    
    print(f"\n📊 Результаты анализа:")
    print(f"   Интервал -1 (15 мин назад): {result_minus_1 if result_minus_1 else 'Нет данных'}")
    print(f"   Интервал -2 (30 мин назад): {result_minus_2 if result_minus_2 else 'Нет данных'}")
    
    # Если два последних исхода одинаковые - начинаем новую серию
    if result_minus_1 and result_minus_2 and result_minus_1 == result_minus_2:
        direction = "Up" if result_minus_1 == "Down" else "Down"
        print(f"\n🎯 Обнаружено два одинаковых исхода подряд: {result_minus_1}")
        print(f"👉 НАЧИНАЕМ НОВУЮ СЕРИЮ НА: {direction} со ставкой ${BASE_BET}")
        return direction, BASE_BET
    
    print(f"\n⏸️ Нет двух одинаковых исходов подряд, пропускаем ставку")
    return None, None

def place_bet(client, coin, direction, bet_amount, state):
    """Размещает реальную ставку на текущий интервал"""
    try:
        print(f"\n{'='*50}")
        print(f"РАЗМЕЩЕНИЕ СТАВКИ {coin} {direction}")
        print(f"{'='*50}")
        
        # Получаем правильный timestamp для текущего интервала
        timestamp, interval_time_et = get_current_interval_timestamp(coin)
        
        print(f"Интервал ET для ставки: {interval_time_et.hour}:{interval_time_et.minute:02d}")
        
        # Получаем рынок
        market = get_market_by_timestamp(coin, timestamp)
        
        if not market:
            print(f"❌ {coin} → рынок для текущего интервала не найден")
            return False, None
        
        print(f"Найден рынок: {market.get('question')}")
        
        if is_market_resolved(market):
            print(f"❌ {coin} → рынок уже разрешен, нельзя ставить")
            return False, None
        
        clob_ids = market.get("clobTokenIds", [])
        if isinstance(clob_ids, str):
            try:
                clob_ids = json.loads(clob_ids)
            except:
                clob_ids = []
        
        if len(clob_ids) < 2:
            print(f"❌ {coin} → нет токенов для торговли")
            return False, None
        
        token_id, price = get_token_id_and_price(market, direction)
        
        if token_id is None:
            print(f"❌ {coin} → не удалось получить token ID для {direction}")
            return False, None
        
        print(f"💰 Цена {direction}: {price:.4f}")
        
        if direction == "Down" and price > MAX_PRICE_FOR_OPPOSITE:
            print(f"❌ Цена слишком высокая ({price:.4f} > {MAX_PRICE_FOR_OPPOSITE:.4f})")
            return False, None
        
        # Проверяем баланс
        current_balance = get_current_balance(client)
        if current_balance is None:
            print("❌ Не удалось проверить баланс")
            return False, None
            
        print(f"💵 Текущий баланс: ${current_balance:.2f}")
        
        if current_balance < bet_amount:
            print(f"❌ Недостаточно средств: баланс ${current_balance:.2f}, нужно ${bet_amount}")
            return False, None
        
        # Размещаем реальный ордер
        bet_price = min(0.99, price + PRICE_BUFFER)
        print(f"📤 Размещаем реальный ордер: {coin} {direction}, цена {bet_price:.4f}, размер ${bet_amount}")
        
        order_args = OrderArgs(
            token_id=token_id,
            side=BUY,
            price=bet_price,
            size=bet_amount
        )
        
        signed = client.create_order(order_args)
        resp = client.post_order(signed, OrderType.GTC)
        
        print(f"Ответ от биржи: {resp}")
        
        if isinstance(resp, dict):
            if "id" in resp:
                return True, resp["id"]
            elif resp.get("status") in ("success", "placed"):
                return True, resp.get("order", {}).get("id")
        
        return False, None
        
    except Exception as e:
        print(f"❌ Ошибка при размещении ставки: {e}")
        import traceback
        traceback.print_exc()
        return False, None

# ========== ГЛАВНАЯ ФУНКЦИЯ ==========

def main():
    print("Запуск бота Polymarket...")
    et_now = get_current_et_time()
    utc5_now = datetime.now(timezone(timedelta(hours=5)))
    print(f"Время ET: {et_now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Время сервера (UTC+5): {utc5_now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Интервал: 15 минут")
    
    client = ClobClient(
        host=HOST,
        key=PRIVATE_KEY,
        chain_id=CHAIN_ID,
        signature_type=1,
        funder=None
    )

    generated_address = client.get_address()
    print(f"Адрес кошелька: {generated_address}")
    
    state = load_state()
    
    # Получаем API credentials
    try:
        api_creds = client.create_or_derive_api_creds()
        client.set_api_creds(api_creds)
        print("✅ API creds получены")
    except Exception as e:
        print("❌ Ошибка API creds:", str(e))
        send_telegram(f"❌ Ошибка API creds: {str(e)}")
        return

    # Проверка баланса
    print("\n=== ПРОВЕРКА БАЛАНСА ===")
    current_balance = get_current_balance(client)
    
    if current_balance is None:
        print("❌ Не удалось получить баланс")
        send_telegram("❌ Ошибка: не удалось получить баланс аккаунта")
        return
    
    print(f"💰 Текущий баланс: ${current_balance:.2f}")
    
    if current_balance < BASE_BET:
        print(f"⚠️ Баланс меньше минимальной ставки ${BASE_BET}")
        send_telegram(f"⚠️ Баланс ${current_balance:.2f} меньше минимальной ставки ${BASE_BET}")
        return

    # Проверка отчетов
    need_6h, need_24h = check_reports(state)
    
    if need_6h:
        print("\n" + "="*50)
        print("📊 ОТЧЕТ ЗА 6 ЧАСОВ")
        print("="*50)
        
        period = get_statistics_period(state, 6)
        total = state["statistics"]
        current_balance = get_current_balance(client)
        
        msg = f"""📊 <b>Отчет за 6 часов:</b>
💰 Текущий баланс: ${current_balance:.2f}
📈 Прибыль: ${period['profit']:.2f}
🎲 Ставок: {period['bets']} (✅ {period['wins']} | ❌ {period['losses']})
📊 Винрейт: {period['win_rate']:.1f}%
🔥 Макс. серия поражений: {period['max_loss_streak']}

<b>Общая статистика:</b>
💰 Общая прибыль: ${total['total_profit']:.2f}
🎲 Всего ставок: {total['total_bets']}
✅ Выигрышей: {total['wins']}
❌ Проигрышей: {total['losses']}
📈 Макс. серия поражений: {total['max_loss_streak']}"""
        
        print(msg)
        send_telegram(msg)
        state["statistics"]["last_6h_report"] = datetime.now().isoformat()
        save_state(state)
    
    if need_24h:
        print("\n" + "="*50)
        print("📊 ОТЧЕТ ЗА 24 ЧАСА")
        print("="*50)
        
        period = get_statistics_period(state, 24)
        total = state["statistics"]
        current_balance = get_current_balance(client)
        
        msg = f"""📊 <b>Отчет за 24 часа:</b>
💰 Текущий баланс: ${current_balance:.2f}
📈 Прибыль: ${period['profit']:.2f}
🎲 Ставок: {period['bets']} (✅ {period['wins']} | ❌ {period['losses']})
📊 Винрейт: {period['win_rate']:.1f}%
🔥 Макс. серия поражений: {period['max_loss_streak']}

<b>Общая статистика:</b>
💰 Общая прибыль: ${total['total_profit']:.2f}
🎲 Всего ставок: {total['total_bets']}
✅ Выигрышей: {total['wins']}
❌ Проигрышей: {total['losses']}
📈 Макс. серия поражений: {total['max_loss_streak']}"""
        
        print(msg)
        send_telegram(msg)
        state["statistics"]["last_24h_report"] = datetime.now().isoformat()
        save_state(state)
    
    # Проверка результатов текущих ставок
    print("\n" + "="*50)
    print("ПРОВЕРКА ТЕКУЩИХ СТАВОК")
    print("="*50)
    
    for coin_key in list(state.get("pending_bets", {}).keys()):
        info = state["pending_bets"][coin_key]
        slug = info["slug"]
        direction = info["direction"]
        amount = info["amount"]
        price = info.get("price", 0.5)
        coin = coin_key.split('_')[0]
        
        print(f"Проверка ставки: {coin_key}")
        
        m = get_market(slug)
        if m and is_market_resolved(m):
            w = get_winner(m)
            if w:
                if w == direction:
                    # Выигрыш
                    profit = amount * (1 / price - 1) if price > 0 else 0
                    msg = f"✅ Выиграна ставка {coin_key} → {direction} | +${profit:.2f}"
                    print(msg)
                    send_telegram(msg)
                    update_statistics(state, coin, "win", profit, amount, direction)
                    update_last_result(state, coin, w)
                    
                else:
                    # Проигрыш
                    profit = -amount
                    msg = f"❌ Проиграна ставка {coin_key} → {direction} | -${amount:.2f}"
                    print(msg)
                    send_telegram(msg)
                    update_statistics(state, coin, "loss", -amount, amount, direction)
                    update_last_result(state, coin, w)
                
                del state["pending_bets"][coin_key]
                save_state(state)

    # Проверка нового интервала
    print("\n" + "="*50)
    print("ПРОВЕРКА НОВОГО 15-МИНУТНОГО ИНТЕРВАЛА")
    print("="*50)
    
    if is_new_interval(15):
        print("✅ НАЧАЛО ИНТЕРВАЛА - выполняем анализ...")
        
        for coin in ["BTC", "ETH"]:
            direction, bet_amount = determine_bet_direction(coin, state)
            
            if not direction or not bet_amount:
                continue
            
            bet_key = f"{coin}_last"
            
            # Проверяем баланс
            current_balance = get_current_balance(client)
            if current_balance < bet_amount:
                print(f"❌ Недостаточно средств для {coin}: баланс ${current_balance:.2f}, нужно ${bet_amount}")
                continue
            
            success, order_id = place_bet(client, coin, direction, bet_amount, state)
            
            if success:
                now_str = utc5_now.strftime('%Y-%m-%d %H:%M:%S')
                
                # Определяем, новая это серия или продолжение
                if coin in state["martingale"]:
                    series_info = f"(серия {state['martingale'][coin]['losses_count'] + 1})"
                else:
                    series_info = "(новая серия)"
                
                msg = f"💰 Ставка: {coin} 15m → {direction} | ${bet_amount:.1f} {series_info}"
                print(msg)
                send_telegram(msg)
                
                if "pending_bets" not in state:
                    state["pending_bets"] = {}
                
                timestamp, _ = get_current_interval_timestamp(coin)
                
                state["pending_bets"][bet_key] = {
                    "slug": f"{coin.lower()}-updown-15m-{timestamp}",
                    "direction": direction,
                    "amount": bet_amount,
                    "price": 0.5,
                    "placed_at": now_str
                }
                save_state(state)
    else:
        current_minute = utc5_now.minute
        et_hour = get_current_et_time().hour
        et_minute = get_current_et_time().minute
        next_interval = ((et_minute // 15) + 1) * 15
        if next_interval >= 60:
            next_interval = 0
        print(f"⏳ Следующий интервал в {et_hour}:{next_interval:02d}")
    
    print("\n" + "="*50)
    print("Бот завершил работу")
    print("="*50)

if __name__ == "__main__":
    main()
