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
    """Проверка баланса USDC через разные методы"""
    try:
        # Пробуем получить баланс через разные методы
        try:
            # Метод 1: get_balances (может не работать)
            balances = client.get_balances()
            for balance in balances:
                if balance.get('asset_type') == 'USDC' or balance.get('symbol') == 'USDC':
                    return float(balance.get('available', 0))
        except:
            pass
        
        try:
            # Метод 2: get_account
            account = client.get_account()
            if account and 'balances' in account:
                for bal in account['balances']:
                    if bal.get('asset') == 'USDC':
                        return float(bal.get('available', 0))
        except:
            pass
        
        # Если ничего не работает, возвращаем 100 (предполагаем, что баланс есть)
        print("Не удалось проверить баланс через API, предполагаем 100 USDC")
        return 100.0
        
    except Exception as e:
        print(f"Ошибка проверки баланса: {e}")
        return 100.0

def find_btc_eth_markets():
    """
    Ищет ТОЛЬКО BTC и ETH 1h рынки.
    Возвращает словарь с найденными рынками для BTC и ETH.
    """
    try:
        print("\n=== ПОИСК BTC И ETH 1h РЫНКОВ ===")
        
        # Получаем активные открытые рынки
        url = "https://gamma-api.polymarket.com/markets?limit=100&active=true&closed=false"
        print(f"Запрос к API: {url}")
        resp = requests.get(url, timeout=10)
        
        if resp.status_code != 200:
            print(f"Ошибка API: {resp.status_code}")
            return {"BTC": None, "ETH": None}
        
        markets = resp.json()
        print(f"Всего активных открытых рынков: {len(markets)}")
        
        # Результаты поиска
        found_markets = {"BTC": None, "ETH": None}
        
        # Перебираем все рынки
        for market in markets:
            question = market.get('question', '').lower()
            slug = market.get('slug', '').lower()
            
            # Проверяем, что это Up/Down рынок
            if 'up or down' not in question and 'up/down' not in question:
                continue
            
            # Проверяем BTC
            if ('btc' in question or 'bitcoin' in question) and found_markets["BTC"] is None:
                # Проверяем, что это 1h рынок
                if '1h' in question or '1 hour' in question or 'hourly' in question:
                    print(f"\n✅ НАЙДЕН BTC 1h РЫНОК:")
                    print(f"   Вопрос: {market.get('question')}")
                    print(f"   Slug: {market.get('slug')}")
                    print(f"   Цены: {market.get('outcomePrices')}")
                    found_markets["BTC"] = market
            
            # Проверяем ETH
            if ('eth' in question or 'ethereum' in question) and found_markets["ETH"] is None:
                # Проверяем, что это 1h рынок
                if '1h' in question or '1 hour' in question or 'hourly' in question:
                    print(f"\n✅ НАЙДЕН ETH 1h РЫНОК:")
                    print(f"   Вопрос: {market.get('question')}")
                    print(f"   Slug: {market.get('slug')}")
                    print(f"   Цены: {market.get('outcomePrices')}")
                    found_markets["ETH"] = market
        
        # Если не нашли через общий поиск, пробуем искать по конкретным slug
        if found_markets["BTC"] is None:
            print("\n🔍 Ищем BTC через точные slug...")
            # Получаем текущую дату для формирования slug
            today = datetime.now()
            month = today.strftime("%B").lower()
            day = today.day
            
            # Пробуем разные варианты slug для BTC (на сегодня)
            for hour in range(0, 24):
                ampm = "am" if hour < 12 else "pm"
                hour_12 = hour if hour <= 12 else hour - 12
                if hour_12 == 0:
                    hour_12 = 12
                    
                slug = f"bitcoin-up-or-down-{month}-{day}-{hour_12}{ampm}-et"
                url = f"https://gamma-api.polymarket.com/markets?slug={slug}"
                try:
                    resp = requests.get(url, timeout=5)
                    if resp.status_code == 200:
                        markets_data = resp.json()
                        if markets_data:
                            print(f"✅ Найден BTC рынок: {slug}")
                            found_markets["BTC"] = markets_data[0]
                            break
                except:
                    continue
        
        if found_markets["ETH"] is None:
            print("\n🔍 Ищем ETH через точные slug...")
            today = datetime.now()
            month = today.strftime("%B").lower()
            day = today.day
            
            for hour in range(0, 24):
                ampm = "am" if hour < 12 else "pm"
                hour_12 = hour if hour <= 12 else hour - 12
                if hour_12 == 0:
                    hour_12 = 12
                    
                slug = f"ethereum-up-or-down-{month}-{day}-{hour_12}{ampm}-et"
                url = f"https://gamma-api.polymarket.com/markets?slug={slug}"
                try:
                    resp = requests.get(url, timeout=5)
                    if resp.status_code == 200:
                        markets_data = resp.json()
                        if markets_data:
                            print(f"✅ Найден ETH рынок: {slug}")
                            found_markets["ETH"] = markets_data[0]
                            break
                except:
                    continue
        
        print("\n=== ИТОГИ ПОИСКА ===")
        print(f"BTC рынок: {'НАЙДЕН' if found_markets['BTC'] else 'НЕ НАЙДЕН'}")
        print(f"ETH рынок: {'НАЙДЕН' if found_markets['ETH'] else 'НЕ НАЙДЕН'}")
        
        return found_markets
        
    except Exception as e:
        print(f"Ошибка поиска рынков: {e}")
        import traceback
        traceback.print_exc()
        return {"BTC": None, "ETH": None}

def get_previous_hour_result(coin):
    """Получает результат предыдущего закрытого часа для монеты"""
    try:
        print(f"\n=== Получение результата предыдущего часа для {coin} ===")
        
        # Определяем время предыдущего часа
        now_utc5 = datetime.now(timezone(timedelta(hours=5)))
        et_now = now_utc5 - timedelta(hours=10)  # Конвертируем в ET
        
        # Берем предыдущий час
        prev_hour_et = et_now.hour - 1
        prev_date = et_now.day
        
        if prev_hour_et < 0:
            prev_hour_et = 23
            prev_date = et_now.day - 1  # Вчерашний день
        
        # Формируем slug для предыдущего часа
        month = et_now.strftime("%B").lower()
        ampm = "am" if prev_hour_et < 12 else "pm"
        hour_12 = prev_hour_et if prev_hour_et <= 12 else prev_hour_et - 12
        if hour_12 == 0:
            hour_12 = 12
        
        if coin == "BTC":
            slug = f"bitcoin-up-or-down-{month}-{prev_date}-{hour_12}{ampm}-et"
        else:  # ETH
            slug = f"ethereum-up-or-down-{month}-{prev_date}-{hour_12}{ampm}-et"
        
        print(f"Ищем рынок: {slug}")
        
        # Получаем рынок
        url = f"https://gamma-api.polymarket.com/markets?slug={slug}"
        resp = requests.get(url, timeout=10)
        
        if resp.status_code != 200:
            print(f"Ошибка получения рынка: {resp.status_code}")
            return None
        
        markets = resp.json()
        if not markets:
            print(f"Рынок не найден")
            return None
        
        market = markets[0]
        
        # Проверяем, закрыт ли рынок
        if not market.get("closed"):
            print(f"Рынок еще не закрыт")
            return None
        
        # Получаем победителя
        winner = get_winner(market)
        
        if winner:
            print(f"✅ Результат {coin} {prev_hour_et}:00 ET: {winner}")
            return winner
        else:
            print(f"Не удалось определить победителя")
            return None
            
    except Exception as e:
        print(f"Ошибка получения результата предыдущего часа: {e}")
        return None

def place_bet(client, coin, market, direction, bet_amount):
    """Размещает ставку на рынке"""
    try:
        print(f"\n=== Размещаем ставку {coin} {direction} ===")
        
        if not market:
            print(f"{coin} → рынок не передан")
            return False
        
        # Проверяем, что рынок открыт
        if market.get('closed') == True:
            print(f"{coin} → рынок закрыт")
            return False
        
        clob_ids = market.get("clobTokenIds", [])
        if len(clob_ids) < 2:
            print(f"{coin} → нет токенов для торговли")
            return False
        
        # Получаем данные для нужного направления
        token_id, price = get_token_id_and_price(market, direction)
        
        if token_id is None:
            print(f"{coin} → не удалось получить token ID для {direction}")
            return False
        
        print(f"{direction} цена: {price:.3f}, токен ID: {token_id}")
        
        if price > MAX_PRICE_FOR_OPPOSITE and direction == "Down":
            print(f"Цена слишком высокая, коэффициент мал, пропускаем")
            return False
        
        # Проверяем баланс
        available_balance = check_balance(client)
        print(f"Доступный баланс: ${available_balance}")
        
        if available_balance < bet_amount:
            print(f"Недостаточно USDC: нужно ${bet_amount}, доступно ${available_balance}")
            return False
        
        bet_price = min(0.99, price + PRICE_BUFFER)
        
        print(f"Пытаемся разместить ордер: {coin} {direction}, цена {bet_price:.3f}, размер ${bet_amount}")
        
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
            if "id" in resp or resp.get("status") in ("success", "placed"):
                now_str = datetime.now(timezone(timedelta(hours=5))).strftime('%Y-%m-%d %H:%M:%S')
                return True, resp.get("id")
            else:
                print(f"Ошибка при ставке: {resp}")
                return False, None
        return False, None
        
    except Exception as e:
        print(f"Ошибка при размещении ставки: {e}")
        import traceback
        traceback.print_exc()
        return False, None

# ========== ГЛАВНАЯ ФУНКЦИЯ ==========

def main():
    print("Запуск бота Polymarket...")
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

    # Диагностика баланса
    print("\n=== ДИАГНОСТИКА БАЛАНСА ===")
    try:
        balance = check_balance(client)
        print(f"Баланс: ${balance}")
    except Exception as e:
        print(f"Ошибка диагностики: {e}")

    state = load_state()
    
    # Находим все BTC и ETH рынки
    markets = find_btc_eth_markets()
    
    # Проверка результатов предыдущих ставок
    print("\n" + "="*50)
    print("ПРОВЕРКА РЕЗУЛЬТАТОВ СТАВОК")
    print("="*50)
    
    now = datetime.now(timezone(timedelta(hours=5)))
    
    for coin_key in list(state["pending_bets"].keys()):
        info = state["pending_bets"][coin_key]
        slug = info["slug"]
        direction = info["direction"]
        amount = info["amount"]
        
        print(f"Проверка ставки: {coin_key} ({slug})")
        
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
            else:
                print(f"Рынок закрыт, но победитель не определен")
        else:
            print(f"Рынок еще открыт или не найден")

    # Получаем результаты предыдущего часа (просто для информации)
    print("\n" + "="*50)
    print("РЕЗУЛЬТАТЫ ПРЕДЫДУЩЕГО ЧАСА")
    print("="*50)
    
    btc_prev = get_previous_hour_result("BTC")
    eth_prev = get_previous_hour_result("ETH")
    
    if btc_prev or eth_prev:
        msg = "📊 Результаты предыдущего часа:\n"
        if btc_prev:
            msg += f"BTC: {btc_prev}\n"
        if eth_prev:
            msg += f"ETH: {eth_prev}"
        send_telegram(msg)

    # Размещение новой ставки (если сейчас начало часа)
    print("\n" + "="*50)
    print("ПРОВЕРКА НОВОГО ЧАСА")
    print("="*50)
    
    if is_new_hour():
        print("✅ Начало часа - проверяем возможность ставки...")
        
        # Для каждого монетного рынка
        for coin in ["BTC", "ETH"]:
            if not markets[coin]:
                print(f"{coin} → рынок не найден, пропускаем")
                continue
            
            # Получаем результат предыдущего часа для определения стратегии
            prev_winner = get_previous_hour_result(coin)
            
            if not prev_winner:
                print(f"{coin} → нет информации о предыдущем результате, пропускаем")
                continue
            
            # Определяем следующее направление (противоположно предыдущему)
            next_dir = "Down" if prev_winner == "Up" else "Up"
            
            # Получаем размер следующей ставки из истории
            bet_key = f"{coin}_last"
            next_bet = state["pending_bets"].get(bet_key, {}).get("next_bet", BASE_BET)
            next_bet = min(next_bet, MAX_BET)
            
            # Размещаем ставку
            success, order_id = place_bet(client, coin, markets[coin], next_dir, next_bet)
            
            if success:
                now_str = now.strftime('%Y-%m-%d %H:%M:%S')
                msg = f"💰 Ставка: {coin} 1h → {next_dir} | ${next_bet:.1f}"
                print(msg)
                send_telegram(msg)
                
                # Сохраняем информацию о ставке
                state["pending_bets"][bet_key] = {
                    "slug": markets[coin]["slug"],
                    "direction": next_dir,
                    "amount": next_bet,
                    "price": 0,  # Цену нужно получить из ответа
                    "placed_at": now_str,
                    "next_bet": BASE_BET  # Сбрасываем для следующего раза
                }
                save_state(state)
    else:
        current_minute = datetime.now(timezone(timedelta(hours=5))).minute
        print(f"Сейчас {current_minute} минут, ждем 00 минут для новых ставок")
    
    print("\n" + "="*50)
    print("Бот завершил работу")
    print("="*50)

if __name__ == "__main__":
    main()
