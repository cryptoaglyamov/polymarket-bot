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

if not PRIVATE_KEY:
    raise ValueError("PRIVATE_KEY не найден в переменных окружения!")

print("PRIVATE_KEY загружен:", PRIVATE_KEY[:10] + "..." + PRIVATE_KEY[-6:])

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
            return data
    return {"pending_bets": {}}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ========== ФУНКЦИИ ДЛЯ РАБОТЫ С POLYMARKET ==========

def is_new_hour():
    now = datetime.now(timezone(timedelta(hours=5)))
    return now.minute == 0 and now.second < 10

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

def get_winner(market):
    if not market:
        return None
    
    prices_str = market.get("outcomePrices", ["0.5", "0.5"])
    
    try:
        # Безопасное преобразование цен
        p0 = float(prices_str[0]) if prices_str[0] != "0" else 0.5
        p1 = float(prices_str[1]) if prices_str[1] != "0" else 0.5
        
        if p0 >= 0.90:
            return "Up"
        if p1 >= 0.90:
            return "Down"
        
        if market.get("closed"):
            return "Up" if p0 > p1 else "Down"
    except Exception as e:
        print(f"Ошибка при определении победителя: {e}")
    
    return None

def get_token_id_and_price(market, direction: str):
    """Безопасное получение token ID и цены"""
    clob_ids = market.get("clobTokenIds", [])
    prices_str = market.get("outcomePrices", ["0.5", "0.5"])
    
    try:
        # Безопасное преобразование цен
        prices = []
        for p in prices_str:
            if isinstance(p, str):
                # Если строка "0", заменяем на 0.5
                if p == "0":
                    prices.append(0.5)
                else:
                    try:
                        prices.append(float(p))
                    except:
                        prices.append(0.5)
            elif isinstance(p, (int, float)):
                prices.append(float(p))
            else:
                prices.append(0.5)
    except Exception as e:
        print(f"Ошибка при преобразовании цен: {e}")
        prices = [0.5, 0.5]
    
    # Убеждаемся, что у нас 2 цены
    while len(prices) < 2:
        prices.append(0.5)
    
    index = 0 if direction == "Up" else 1
    
    # Проверяем, что индекс существует
    if index >= len(clob_ids):
        print(f"Нет token ID для индекса {index}, direction={direction}")
        return None, prices[index]
    
    return clob_ids[index], prices[index]

def check_balance(client):
    """Проверка баланса USDC через прямые запросы к API"""
    try:
        # Прямой запрос к API Polymarket для получения баланса
        address = client.get_address()
        
        # Пробуем разные эндпоинты для получения баланса
        endpoints = [
            f"https://clob.polymarket.com/balance?address={address}",
            f"https://clob.polymarket.com/api/balance?address={address}",
            f"https://clob.polymarket.com/v1/balance?address={address}",
        ]
        
        for url in endpoints:
            try:
                headers = {"Authorization": f"Bearer {PRIVATE_KEY}"}
                resp = requests.get(url, headers=headers, timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    if "balance" in data:
                        return float(data["balance"])
                    elif "usdc" in data:
                        return float(data["usdc"])
            except:
                continue
        
        # Если не получилось, пробуем через симуляцию ордера
        print("⚠️ Не удалось проверить баланс через API, предполагаем 100 USDC для теста")
        return 100.0
        
    except Exception as e:
        print(f"Ошибка проверки баланса: {e}")
        return 100.0  # Возвращаем 100 для теста

def get_current_et_time():
    """Получает текущее время в ET (Eastern Time)"""
    now_utc5 = datetime.now(timezone(timedelta(hours=5)))
    et_now = now_utc5 - timedelta(hours=10)  # UTC+5 -> ET (UTC-5)
    return et_now

def find_current_hour_market(coin):
    """Находит рынок для текущего часа ET"""
    try:
        et_now = get_current_et_time()
        current_hour = et_now.hour
        current_date = et_now.day
        
        print(f"\n=== Поиск рынка для {coin} на час {current_hour}:00 ET ===")
        
        # Формируем правильный slug для текущего часа
        month = et_now.strftime("%B").lower()
        
        # Определяем AM/PM
        ampm = "am" if current_hour < 12 else "pm"
        hour_12 = current_hour if current_hour <= 12 else current_hour - 12
        if hour_12 == 0:
            hour_12 = 12
        
        # Формируем slug
        if coin == "BTC":
            slug = f"bitcoin-up-or-down-{month}-{current_date}-{hour_12}{ampm}-et"
        else:  # ETH
            slug = f"ethereum-up-or-down-{month}-{current_date}-{hour_12}{ampm}-et"
        
        print(f"Ищем slug: {slug}")
        
        # Получаем рынок
        url = f"https://gamma-api.polymarket.com/markets?slug={slug}"
        resp = requests.get(url, timeout=10)
        
        if resp.status_code == 200:
            markets = resp.json()
            if markets:
                market = markets[0]
                print(f"✅ Найден рынок: {market.get('question')}")
                print(f"   Цены: {market.get('outcomePrices')}")
                print(f"   Открыт: {not market.get('closed')}")
                return market
        
        print(f"❌ Рынок для часа {current_hour} не найден")
        return None
        
    except Exception as e:
        print(f"Ошибка поиска рынка: {e}")
        return None

def get_previous_hour_market(coin):
    """Находит рынок для предыдущего часа ET"""
    try:
        et_now = get_current_et_time()
        prev_hour = et_now.hour - 1
        prev_date = et_now.day
        
        # Корректировка даты если перешли через полночь
        if prev_hour < 0:
            prev_hour = 23
            prev_date = et_now.day - 1
        
        print(f"\n=== Поиск предыдущего рынка для {coin} на час {prev_hour}:00 ET ===")
        
        month = et_now.strftime("%B").lower()
        
        # Определяем AM/PM
        ampm = "am" if prev_hour < 12 else "pm"
        hour_12 = prev_hour if prev_hour <= 12 else prev_hour - 12
        if hour_12 == 0:
            hour_12 = 12
        
        # Формируем slug
        if coin == "BTC":
            slug = f"bitcoin-up-or-down-{month}-{prev_date}-{hour_12}{ampm}-et"
        else:  # ETH
            slug = f"ethereum-up-or-down-{month}-{prev_date}-{hour_12}{ampm}-et"
        
        print(f"Ищем slug: {slug}")
        
        # Получаем рынок
        url = f"https://gamma-api.polymarket.com/markets?slug={slug}"
        resp = requests.get(url, timeout=10)
        
        if resp.status_code == 200:
            markets = resp.json()
            if markets:
                market = markets[0]
                print(f"✅ Найден предыдущий рынок: {market.get('question')}")
                print(f"   Закрыт: {market.get('closed')}")
                print(f"   Цены: {market.get('outcomePrices')}")
                return market
        
        print(f"❌ Предыдущий рынок не найден")
        return None
        
    except Exception as e:
        print(f"Ошибка поиска предыдущего рынка: {e}")
        return None

def get_previous_hour_result(coin):
    """Получает результат предыдущего часа"""
    try:
        market = get_previous_hour_market(coin)
        
        if not market:
            return None
        
        # Проверяем, закрыт ли рынок
        if not market.get("closed"):
            print(f"Предыдущий рынок еще не закрыт")
            return None
        
        # Получаем победителя
        winner = get_winner(market)
        
        if winner:
            print(f"✅ Результат {coin}: {winner}")
            return winner
        else:
            print(f"Не удалось определить победителя")
            return None
            
    except Exception as e:
        print(f"Ошибка получения результата: {e}")
        return None

def place_bet(client, coin, market, direction, bet_amount):
    """Размещает ставку на рынке"""
    try:
        print(f"\n=== Размещаем ставку {coin} {direction} ===")
        
        if not market:
            print(f"{coin} → рынок не передан")
            return False, None
        
        # Проверяем, что рынок открыт
        if market.get('closed') == True:
            print(f"{coin} → рынок закрыт")
            return False, None
        
        clob_ids = market.get("clobTokenIds", [])
        if len(clob_ids) < 2:
            print(f"{coin} → нет токенов для торговли")
            return False, None
        
        # Получаем данные для нужного направления
        token_id, price = get_token_id_and_price(market, direction)
        
        if token_id is None:
            print(f"{coin} → не удалось получить token ID для {direction}")
            return False, None
        
        print(f"{direction} цена: {price:.3f}, токен ID: {token_id}")
        
        # Проверка коэффициента для Down
        if direction == "Down" and price > MAX_PRICE_FOR_OPPOSITE:
            print(f"Цена слишком высокая ({price:.3f} > {MAX_PRICE_FOR_OPPOSITE:.3f}), коэффициент мал")
            return False, None
        
        # Проверяем баланс
        available_balance = check_balance(client)
        print(f"Доступный баланс: ${available_balance}")
        
        if available_balance < bet_amount:
            print(f"Недостаточно USDC: нужно ${bet_amount}, доступно ${available_balance}")
            return False, None
        
        bet_price = min(0.99, price + PRICE_BUFFER)
        
        print(f"Размещаем ордер: {coin} {direction}, цена {bet_price:.3f}, размер ${bet_amount}")
        
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
        print(f"Ошибка при размещении ставки: {e}")
        import traceback
        traceback.print_exc()
        return False, None

# ========== ГЛАВНАЯ ФУНКЦИЯ ==========

def main():
    print("Запуск бота Polymarket...")
    et_now = get_current_et_time()
    print(f"Время ET: {et_now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Время сервера (UTC+5): {datetime.now(timezone(timedelta(hours=5))).strftime('%Y-%m-%d %H:%M:%S')}")
    
    client = ClobClient(
        host=HOST,
        key=PRIVATE_KEY,
        chain_id=CHAIN_ID,
        signature_type=1,
        funder=None
    )

    print(f"Адрес кошелька: {client.get_address()}")
    send_telegram("🟢 Бот запущен на GitHub Actions")

    try:
        api_creds = client.create_or_derive_api_creds()
        client.set_api_creds(api_creds)
        print("✅ API creds получены")
    except Exception as e:
        print("❌ Ошибка API creds:", str(e))
        send_telegram(f"❌ Ошибка API creds: {str(e)}")
        return

    state = load_state()
    
    # Получаем результаты предыдущего часа
    print("\n" + "="*50)
    print("РЕЗУЛЬТАТЫ ПРЕДЫДУЩЕГО ЧАСА")
    print("="*50)
    
    btc_prev_result = get_previous_hour_result("BTC")
    eth_prev_result = get_previous_hour_result("ETH")
    
    # Отправляем результаты в Telegram
    if btc_prev_result or eth_prev_result:
        msg = "📊 Результаты предыдущего часа:\n"
        if btc_prev_result:
            msg += f"BTC: {btc_prev_result}\n"
        if eth_prev_result:
            msg += f"ETH: {eth_prev_result}"
        send_telegram(msg)
    
    # Проверка результатов текущих ставок
    print("\n" + "="*50)
    print("ПРОВЕРКА ТЕКУЩИХ СТАВОК")
    print("="*50)
    
    for coin_key in list(state["pending_bets"].keys()):
        info = state["pending_bets"][coin_key]
        slug = info["slug"]
        direction = info["direction"]
        amount = info["amount"]
        
        print(f"Проверка ставки: {coin_key}")
        
        m = get_market(slug)
        if m and m.get("closed"):
            w = get_winner(m)
            if w:
                if w == direction:
                    profit = amount * (1 / info['price'] - 1)
                    msg = f"✅ Выиграна ставка {coin_key} → {direction} | +${profit:.2f}"
                    print(msg)
                    send_telegram(msg)
                else:
                    new_bet = min(amount * 2, MAX_BET)
                    msg = f"❌ Проиграна ставка {coin_key} → {direction} | следующая ${new_bet:.1f}"
                    print(msg)
                    send_telegram(msg)
                    state["pending_bets"][coin_key]["next_bet"] = new_bet
                
                del state["pending_bets"][coin_key]
                save_state(state)

    # Размещение новой ставки (если сейчас начало часа)
    print("\n" + "="*50)
    print("ПРОВЕРКА НОВОГО ЧАСА")
    print("="*50)
    
    if is_new_hour():
        print("✅ Начало часа - проверяем возможность ставки...")
        
        for coin in ["BTC", "ETH"]:
            # Находим рынок для текущего часа
            current_market = find_current_hour_market(coin)
            
            if not current_market:
                print(f"{coin} → рынок для текущего часа не найден")
                continue
            
            # Получаем результат предыдущего часа
            prev_result = get_previous_hour_result(coin)
            
            if not prev_result:
                print(f"{coin} → нет результата предыдущего часа")
                continue
            
            # Определяем следующее направление (противоположно предыдущему)
            next_dir = "Down" if prev_result == "Up" else "Up"
            
            # Получаем размер следующей ставки
            bet_key = f"{coin}_last"
            next_bet = state.get("pending_bets", {}).get(bet_key, {}).get("next_bet", BASE_BET)
            next_bet = min(next_bet, MAX_BET)
            
            # Проверяем, нет ли уже активной ставки
            if bet_key in state.get("pending_bets", {}):
                print(f"{coin} → уже есть активная ставка")
                continue
            
            # Размещаем ставку
            success, order_id = place_bet(client, coin, current_market, next_dir, next_bet)
            
            if success:
                now_str = datetime.now(timezone(timedelta(hours=5))).strftime('%Y-%m-%d %H:%M:%S')
                msg = f"💰 Ставка: {coin} 1h → {next_dir} | ${next_bet:.1f}"
                print(msg)
                send_telegram(msg)
                
                # Сохраняем информацию о ставке
                if "pending_bets" not in state:
                    state["pending_bets"] = {}
                
                state["pending_bets"][bet_key] = {
                    "slug": current_market["slug"],
                    "direction": next_dir,
                    "amount": next_bet,
                    "price": 0,  # Цену нужно будет обновить из ответа
                    "placed_at": now_str,
                    "next_bet": BASE_BET
                }
                save_state(state)
    else:
        current_minute = datetime.now(timezone(timedelta(hours=5))).minute
        et_hour = get_current_et_time().hour
        print(f"Сейчас {current_minute} минут, ET час {et_hour}:00, ждем 00 минут для новых ставок")
    
    print("\n" + "="*50)
    print("Бот завершил работу")
    print("="*50)

if __name__ == "__main__":
    main()
