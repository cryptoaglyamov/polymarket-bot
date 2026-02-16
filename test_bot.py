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

# 👇 ВАШ РЕАЛЬНЫЙ АДРЕС КОШЕЛЬКА С USDC
REAL_WALLET_ADDRESS = "0xc28d92cB2D25b5282c526FA1875d0268D1C4c177"

# 👇 БАЛАНС ДЛЯ ТЕСТОВ
TEST_BALANCE = 300.0

# 👇 РЕЖИМ ТЕСТИРОВАНИЯ
TEST_MODE = True  # True = без реальных ставок, False = реальные ставки

# 👇 НАСТРОЙКИ СТРАТЕГИИ
LOOKBACK_INTERVALS = 2  # Анализируем последние 2 интервала

if not PRIVATE_KEY:
    raise ValueError("PRIVATE_KEY не найден в переменных окружения!")

print("PRIVATE_KEY загружен:", PRIVATE_KEY[:10] + "..." + PRIVATE_KEY[-6:])
print(f"🔧 РЕЖИМ ТЕСТИРОВАНИЯ: {'ВКЛЮЧЕН (без реальных ставок)' if TEST_MODE else 'ВЫКЛЮЧЕН (реальные ставки)'}")
print(f"💰 ТЕСТОВЫЙ БАЛАНС: ${TEST_BALANCE}")
print(f"📊 СТРАТЕГИЯ: Анализ последних {LOOKBACK_INTERVALS} интервалов")

CHAIN_ID = 137
HOST = "https://clob.polymarket.com"

BASE_BET = 2.0
MAX_BET = 64.0
MIN_MULTIPLIER = 1.7
MAX_PRICE_FOR_OPPOSITE = 1.0 / MIN_MULTIPLIER  # ≈ 0.588
PRICE_BUFFER = 0.01

STATE_FILE = "test_bot_state.json"

# ========== ФУНКЦИЯ ОТПРАВКИ В ТЕЛЕГРАМ ==========

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram] Токен или chat_id не указаны → сообщение не отправлено")
        return
    
    if TEST_MODE:
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
                    "last_reset_date": datetime.now().strftime('%Y-%m-%d')
                }
            if "last_results" not in data:
                data["last_results"] = {}
            return data
    return {
        "pending_bets": {},
        "statistics": {
            "total_profit": 0.0,
            "total_bets": 0,
            "wins": 0,
            "losses": 0,
            "history": [],
            "last_reset_date": datetime.now().strftime('%Y-%m-%d')
        },
        "last_results": {}
    }

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def update_statistics(state, coin, result, profit, bet_amount):
    """Обновляет статистику после завершения ставки"""
    stats = state["statistics"]
    
    stats["history"].append({
        "timestamp": datetime.now().isoformat(),
        "coin": coin,
        "result": result,
        "profit": profit,
        "bet_amount": bet_amount
    })
    
    stats["total_bets"] += 1
    stats["total_profit"] += profit
    
    if profit > 0:
        stats["wins"] += 1
    else:
        stats["losses"] += 1
    
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
    
    if len(state["last_results"][coin]) > LOOKBACK_INTERVALS:
        state["last_results"][coin] = state["last_results"][coin][-LOOKBACK_INTERVALS:]
    
    save_state(state)

def get_last_results(state, coin):
    """Получает последние результаты для монеты"""
    if coin not in state["last_results"]:
        return []
    return [r["result"] for r in state["last_results"][coin]]

def get_statistics_period(state, hours):
    """Получает статистику за указанный период"""
    stats = state["statistics"]
    now = datetime.now()
    period_ago = now - timedelta(hours=hours)
    
    period_profit = 0
    period_bets = 0
    period_wins = 0
    
    for entry in stats["history"]:
        entry_time = datetime.fromisoformat(entry["timestamp"])
        if entry_time > period_ago:
            period_profit += entry["profit"]
            period_bets += 1
            if entry["profit"] > 0:
                period_wins += 1
    
    period_losses = period_bets - period_wins
    win_rate = (period_wins / period_bets * 100) if period_bets > 0 else 0
    
    return {
        "profit": period_profit,
        "bets": period_bets,
        "wins": period_wins,
        "losses": period_losses,
        "win_rate": win_rate
    }

def check_midnight():
    """Проверяет, наступила ли полночь по UTC+5"""
    now = datetime.now(timezone(timedelta(hours=5)))
    return now.hour == 0 and now.minute == 0

# ========== ФУНКЦИИ ДЛЯ РАБОТЫ С POLYMARKET ==========

def is_new_interval(minutes=15):
    """Проверяет, наступило ли начало интервала (в течение всей первой минуты)"""
    now = datetime.now(timezone(timedelta(hours=5)))
    return now.minute % minutes == 0  # Игнорируем секунды

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

def check_balance():
    """Проверка баланса"""
    try:
        address = REAL_WALLET_ADDRESS
        print(f"Проверка баланса для реального адреса: {address}")
        print(f"💰 Используем тестовый баланс: ${TEST_BALANCE}")
        return TEST_BALANCE
    except Exception as e:
        print(f"Ошибка проверки баланса: {e}")
        return None

def get_current_et_time():
    """Получает текущее время в ET"""
    now_utc5 = datetime.now(timezone(timedelta(hours=5)))
    et_now = now_utc5 - timedelta(hours=10)
    return et_now

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
                return markets[0]
        return None
    except Exception as e:
        print(f"Ошибка получения рынка по timestamp: {e}")
        return None

def get_interval_result(coin, interval_offset):
    """
    Получает результат для указанного интервала
    interval_offset: -1 = предыдущий, -2 = позапрошлый и т.д.
    """
    try:
        et_now = get_current_et_time()
        
        # Вычисляем время для нужного интервала
        minutes_back = abs(interval_offset) * 15
        target_time = et_now - timedelta(minutes=minutes_back)
        
        # Округляем до начала 15-минутного интервала
        target_minute = target_time.minute
        interval_start = (target_minute // 15) * 15
        target_time = target_time.replace(minute=interval_start, second=0, microsecond=0)
        
        print(f"\n=== Получение результата для {coin}, интервал {interval_offset} ===")
        print(f"Время ET: {target_time.hour}:{target_time.minute:02d}")
        
        # ✅ Проверяем, закончился ли интервал
        interval_end_time = target_time + timedelta(minutes=15)
        if et_now < interval_end_time:
            print(f"⏳ Интервал {interval_offset} еще НЕ ЗАКОНЧИЛСЯ (закончится в {interval_end_time.hour}:{interval_end_time.minute:02d})")
            print(f"   Текущее время ET: {et_now.hour}:{et_now.minute:02d}")
            return None
        
        # Конвертируем в Unix timestamp
        target_time_utc = target_time + timedelta(hours=5)
        timestamp = int(target_time_utc.timestamp())
        print(f"Timestamp: {timestamp}")
        
        # Получаем рынок
        market = get_market_by_timestamp(coin, timestamp)
        
        if not market:
            print(f"❌ Рынок для интервала {interval_offset} не найден")
            return None
        
        print(f"Найден рынок: {market.get('question')}")
        print(f"Цены: {market.get('outcomePrices')}")
        
        if not is_market_resolved(market):
            print(f"⏳ Рынок для интервала {interval_offset} еще не разрешен")
            return None
        
        winner = get_winner(market)
        if winner:
            print(f"✅ Результат интервала {interval_offset} для {coin}: {winner}")
            return winner
        else:
            print(f"❌ Не удалось определить победителя")
            return None
        
    except Exception as e:
        print(f"Ошибка получения результата интервала: {e}")
        return None

def determine_bet_direction(coin, state):
    """
    Определяет направление ставки на основе последних результатов
    Возвращает "Up", "Down" или None (если нет ставки)
    """
    # Получаем результаты последних двух интервалов
    print(f"\n{'='*50}")
    print(f"АНАЛИЗ ДЛЯ {coin}")
    print(f"{'='*50}")
    
    result_minus_1 = get_interval_result(coin, -1)  # Предыдущий
    result_minus_2 = get_interval_result(coin, -2)  # Позапрошлый
    
    print(f"\n📊 Результаты анализа:")
    print(f"   Интервал -1 (предыдущий): {result_minus_1 if result_minus_1 else 'Нет данных'}")
    print(f"   Интервал -2 (позапрошлый): {result_minus_2 if result_minus_2 else 'Нет данных'}")
    
    # Если оба результата одинаковые
    if result_minus_1 and result_minus_2 and result_minus_1 == result_minus_2:
        direction = "Up" if result_minus_1 == "Down" else "Down"
        print(f"\n🎯 Обнаружено два одинаковых исхода подряд: {result_minus_1}")
        print(f"👉 СТАВИМ НА: {direction}")
        return direction
    
    print(f"\n⏸️ Нет двух одинаковых исходов подряд, пропускаем ставку")
    return None

def place_bet(client, coin, direction, bet_amount):
    """Размещает ставку на текущий интервал"""
    try:
        print(f"\n{'='*50}")
        print(f"РАЗМЕЩЕНИЕ СТАВКИ {coin} {direction}")
        print(f"{'='*50}")
        
        # Получаем текущий интервал
        et_now = get_current_et_time()
        current_minute = et_now.minute
        interval_start = (current_minute // 15) * 15
        current_time = et_now.replace(minute=interval_start, second=0, microsecond=0)
        current_time_utc = current_time + timedelta(hours=5)
        timestamp = int(current_time_utc.timestamp())
        
        print(f"Текущий интервал ET: {current_time.hour}:{current_time.minute:02d}")
        print(f"Timestamp: {timestamp}")
        
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
        
        available_balance = check_balance()
        if available_balance is None:
            print("❌ Не удалось проверить баланс")
            return False, None
            
        print(f"💵 Доступный баланс: ${available_balance:.2f}")
        
        if available_balance < bet_amount:
            print(f"❌ Недостаточно USDC: нужно ${bet_amount}, доступно ${available_balance:.2f}")
            return False, None
        
        if TEST_MODE:
            print("🧪 ТЕСТОВЫЙ РЕЖИМ: ставка не отправляется на биржу")
            mock_order_id = f"test_order_{int(time.time())}"
            print(f"✅ Тестовая ставка размещена (ID: {mock_order_id})")
            return True, mock_order_id
        else:
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
    print(f"Адрес из приватного ключа: {generated_address}")
    print(f"Реальный адрес кошелька: {REAL_WALLET_ADDRESS}")
    
    print("\n=== ПРОВЕРКА БАЛАНСА ===")
    real_balance = check_balance()
    
    if real_balance is None:
        print("❌ КРИТИЧЕСКАЯ ОШИБКА: Не удалось получить баланс")
        send_telegram("❌ Ошибка: не удалось получить баланс аккаунта")
        return
    
    print(f"💰 Баланс: ${real_balance:.2f}")
    
    if real_balance < BASE_BET:
        print(f"⚠️ Баланс меньше минимальной ставки ${BASE_BET}")
        send_telegram(f"⚠️ Баланс ${real_balance:.2f} меньше минимальной ставки ${BASE_BET}")
    else:
        send_telegram(f"💰 Баланс: ${real_balance:.2f}")

    try:
        api_creds = client.create_or_derive_api_creds()
        client.set_api_creds(api_creds)
        print("✅ API creds получены")
    except Exception as e:
        print("❌ Ошибка API creds:", str(e))
        send_telegram(f"❌ Ошибка API creds: {str(e)}")
        return

    state = load_state()
    
    # Проверка полночи для статистики
    if check_midnight():
        print("\n" + "="*50)
        print("📊 ЕЖЕДНЕВНАЯ СТАТИСТИКА (00:00 UTC+5)")
        print("="*50)
        
        daily = get_statistics_period(state, 24)
        six_hours = get_statistics_period(state, 6)
        total = state["statistics"]
        
        msg = f"""📊 <b>Статистика за 6 часов:</b>
💰 Профит: ${six_hours['profit']:.2f}
🎲 Ставок: {six_hours['bets']} | ✅ {six_hours['wins']} | ❌ {six_hours['losses']}
📈 Винрейт: {six_hours['win_rate']:.1f}%

📊 <b>Статистика за 24 часа:</b>
💰 Профит: ${daily['profit']:.2f}
🎲 Ставок: {daily['bets']} | ✅ {daily['wins']} | ❌ {daily['losses']}
📈 Винрейт: {daily['win_rate']:.1f}%

<b>Общая статистика:</b>
💰 Общий профит: ${total['total_profit']:.2f}
🎲 Всего ставок: {total['total_bets']}
✅ Выигрышей: {total['wins']}
❌ Проигрышей: {total['losses']}"""
        
        print(msg)
        send_telegram(msg)
    
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
        
        print(f"Проверка ставки: {coin_key}")
        
        m = get_market(slug)
        if m and is_market_resolved(m):
            w = get_winner(m)
            if w:
                if w == direction:
                    profit = amount * (1 / price - 1) if price > 0 else 0
                    msg = f"✅ Выиграна ставка {coin_key} → {direction} | +${profit:.2f}"
                    print(msg)
                    send_telegram(msg)
                    update_statistics(state, coin_key, "win", profit, amount)
                    
                    # Сохраняем результат
                    update_last_result(state, coin_key.split('_')[0], w)
                    
                else:
                    new_bet = min(amount * 2, MAX_BET)
                    profit = -amount
                    msg = f"❌ Проиграна ставка {coin_key} → {direction} | следующая ${new_bet:.1f}"
                    print(msg)
                    send_telegram(msg)
                    update_statistics(state, coin_key, "loss", -amount, amount)
                    
                    # Сохраняем результат
                    update_last_result(state, coin_key.split('_')[0], w)
                    
                    state["pending_bets"][coin_key]["next_bet"] = new_bet
                
                del state["pending_bets"][coin_key]
                save_state(state)

    # Проверка нового интервала
    print("\n" + "="*50)
    print("ПРОВЕРКА НОВОГО 15-МИНУТНОГО ИНТЕРВАЛА")
    print("="*50)
    
    if is_new_interval(15):
        print("✅ НАЧАЛО ИНТЕРВАЛА - выполняем анализ...")
        
        for coin in ["BTC", "ETH"]:
            # Определяем направление ставки по стратегии
            direction = determine_bet_direction(coin, state)
            
            if not direction:
                continue
            
            bet_key = f"{coin}_last"
            next_bet = state.get("pending_bets", {}).get(bet_key, {}).get("next_bet", BASE_BET)
            next_bet = min(next_bet, MAX_BET)
            
            if bet_key in state.get("pending_bets", {}):
                print(f"{coin} → уже есть активная ставка")
                continue
            
            if real_balance < next_bet:
                print(f"❌ Недостаточно средств: баланс ${real_balance}, нужно ${next_bet}")
                continue
            
            success, order_id = place_bet(client, coin, direction, next_bet)
            
            if success:
                now_str = utc5_now.strftime('%Y-%m-%d %H:%M:%S')
                direction_word = "ВВЕРХ" if direction == "Up" else "ВНИЗ"
                msg = f"💰 Ставка: {coin} 15m → {direction} | ${next_bet:.1f} (после двух {direction_word})"
                if TEST_MODE:
                    msg = "🧪 [ТЕСТ] " + msg
                print(msg)
                send_telegram(msg)
                
                if "pending_bets" not in state:
                    state["pending_bets"] = {}
                
                # Получаем timestamp для текущего интервала
                current_time = et_now.replace(minute=(et_now.minute // 15) * 15, second=0, microsecond=0)
                current_time_utc = current_time + timedelta(hours=5)
                timestamp = int(current_time_utc.timestamp())
                
                state["pending_bets"][bet_key] = {
                    "slug": f"{coin.lower()}-updown-15m-{timestamp}",
                    "direction": direction,
                    "amount": next_bet,
                    "price": 0.5,
                    "placed_at": now_str,
                    "next_bet": BASE_BET
                }
                save_state(state)
    else:
        current_minute = utc5_now.minute
        et_hour = get_current_et_time().hour
        et_minute = get_current_et_time().minute
        next_interval = ((et_minute // 15) + 1) * 15
        if next_interval >= 60:
            next_interval = 0
        print(f"⏳ Сейчас {current_minute} минут, ET {et_hour}:{et_minute:02d}, следующий интервал в {et_hour}:{next_interval:02d}")
    
    print("\n" + "="*50)
    print("Бот завершил работу")
    print("="*50)

if __name__ == "__main__":
    main()
