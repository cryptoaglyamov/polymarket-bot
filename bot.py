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

def parse_prices(prices_str):
    """Правильный парсинг цен из API"""
    try:
        if isinstance(prices_str, list):
            prices = []
            for p in prices_str:
                if isinstance(p, str):
                    # Убираем кавычки и преобразуем в float
                    p_clean = p.strip('"').strip("'")
                    try:
                        prices.append(float(p_clean))
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
    """
    Определяет, разрешен ли рынок (закрыт) по цене
    Возвращает True если цена одного из исходов >= 0.85
    """
    if not market:
        return False
    
    prices_str = market.get("outcomePrices", ["0.5", "0.5"])
    prices = parse_prices(prices_str)
    
    # Если одна из цен достигла 0.85 или выше - рынок разрешен
    if prices[0] >= 0.85 or prices[1] >= 0.85:
        return True
    
    return False

def get_winner(market):
    if not market:
        return None
    
    prices_str = market.get("outcomePrices", ["0.5", "0.5"])
    prices = parse_prices(prices_str)
    
    # Если рынок разрешен (цена >= 0.85)
    if prices[0] >= 0.85:
        return "Up"
    if prices[1] >= 0.85:
        return "Down"
    
    # Если рынок официально закрыт по API
    if market.get("closed"):
        return "Up" if prices[0] > prices[1] else "Down"
    
    return None

def get_token_id_and_price(market, direction: str):
    """Безопасное получение token ID и цены"""
    clob_ids = market.get("clobTokenIds", [])
    prices_str = market.get("outcomePrices", ["0.5", "0.5"])
    prices = parse_prices(prices_str)
    
    index = 0 if direction == "Up" else 1
    
    # Проверяем, что индекс существует
    if index >= len(clob_ids):
        print(f"Нет token ID для индекса {index}, direction={direction}")
        return None, prices[index] if index < len(prices) else 0.5
    
    return clob_ids[index], prices[index]

def check_balance():
    """Проверка баланса USDC на реальном кошельке"""
    try:
        address = REAL_WALLET_ADDRESS
        print(f"Проверка баланса для реального адреса: {address}")
        
        # Пробуем разные эндпоинты для получения баланса
        endpoints = [
            f"https://polygon.api.0x.org/balance?address={address}&token=USDC",
            f"https://api.polygonscan.com/api?module=account&action=tokenbalance&contractaddress=0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174&address={address}&tag=latest",
            f"https://clob.polymarket.com/balance?address={address}",
        ]
        
        for url in endpoints:
            try:
                print(f"Запрос к: {url}")
                resp = requests.get(url, timeout=10)
                print(f"Статус ответа: {resp.status_code}")
                
                if resp.status_code == 200:
                    data = resp.json()
                    print(f"Ответ: {data}")
                    
                    # Парсим разные форматы ответа
                    if isinstance(data, dict):
                        if 'balance' in data:
                            balance = float(data['balance']) / 1e6  # USDC имеет 6 decimals
                            return balance
                        elif 'result' in data:
                            balance = float(data['result']) / 1e6
                            return balance
                    elif isinstance(data, (int, float)):
                        return float(data) / 1e6
            except Exception as e:
                print(f"Ошибка при запросе к {url}: {e}")
                continue
        
        # Если не получилось через API, пробуем через простой GET запрос
        try:
            # Прямой запрос к Polygon RPC
            url = "https://polygon-rpc.com/"
            payload = {
                "jsonrpc": "2.0",
                "method": "eth_call",
                "params": [{
                    "to": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",  # USDC contract
                    "data": "0x70a08231000000000000000000000000" + address[2:]  # balanceOf
                }, "latest"],
                "id": 1
            }
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if 'result' in data:
                    balance = int(data['result'], 16) / 1e6
                    return balance
        except Exception as e:
            print(f"Ошибка RPC запроса: {e}")
        
        print("❌ Не удалось получить баланс через все методы")
        return None
        
    except Exception as e:
        print(f"Ошибка проверки баланса: {e}")
        import traceback
        traceback.print_exc()
        return None

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
                prices = market.get('outcomePrices', ['N/A', 'N/A'])
                parsed_prices = parse_prices(prices)
                resolved = is_market_resolved(market)
                print(f"✅ Найден рынок: {market.get('question')}")
                print(f"   Цены: {parsed_prices}")
                print(f"   Разрешен: {resolved}")
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
                prices = market.get('outcomePrices', ['N/A', 'N/A'])
                parsed_prices = parse_prices(prices)
                resolved = is_market_resolved(market)
                print(f"✅ Найден предыдущий рынок: {market.get('question')}")
                print(f"   Цены: {parsed_prices}")
                print(f"   Разрешен: {resolved}")
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
        
        # Проверяем, разрешен ли рынок (по цене >= 0.85)
        if not is_market_resolved(market):
            prices = parse_prices(market.get('outcomePrices', ['0.5', '0.5']))
            print(f"Предыдущий рынок еще не разрешен. Текущие цены: {prices}")
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
        
        # Проверяем, не разрешен ли рынок уже
        if is_market_resolved(market):
            print(f"{coin} → рынок уже разрешен, нельзя ставить")
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
        
        print(f"{direction} цена: {price:.4f}, токен ID: {token_id}")
        
        # Проверка коэффициента для Down
        if direction == "Down" and price > MAX_PRICE_FOR_OPPOSITE:
            print(f"Цена слишком высокая ({price:.4f} > {MAX_PRICE_FOR_OPPOSITE:.4f}), коэффициент мал")
            return False, None
        
        # Проверяем баланс
        available_balance = check_balance()
        if available_balance is None:
            print("❌ Не удалось проверить баланс, ставка отменена")
            return False, None
            
        print(f"Доступный баланс: ${available_balance:.2f}")
        
        if available_balance < bet_amount:
            print(f"Недостаточно USDC: нужно ${bet_amount}, доступно ${available_balance:.2f}")
            return False, None
        
        bet_price = min(0.99, price + PRICE_BUFFER)
        
        print(f"Размещаем ордер: {coin} {direction}, цена {bet_price:.4f}, размер ${bet_amount}")
        
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

    generated_address = client.get_address()
    print(f"Адрес из приватного ключа: {generated_address}")
    print(f"Реальный адрес кошелька: {REAL_WALLET_ADDRESS}")
    
    # Проверяем реальный баланс на правильном адресе
    print("\n=== ПРОВЕРКА БАЛАНСА ===")
    real_balance = check_balance()
    
    if real_balance is None:
        print("❌ КРИТИЧЕСКАЯ ОШИБКА: Не удалось получить баланс. Бот остановлен.")
        send_telegram("❌ Ошибка: не удалось получить баланс аккаунта")
        return
    
    print(f"💰 Реальный баланс: ${real_balance:.2f}")
    
    if real_balance < BASE_BET:
        print(f"⚠️ ВНИМАНИЕ: Баланс меньше минимальной ставки ${BASE_BET}")
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
    
    # Получаем результаты предыдущего часа
    print("\n" + "="*50)
    print("РЕЗУЛЬТАТЫ ПРЕДЫДУЩЕГО ЧАСА")
    print("="*50)
    
    btc_prev_result = get_previous_hour_result("BTC")
    eth_prev_result = get_previous_hour_result("ETH")
    
    # Отправляем результаты в Telegram
    msg_parts = []
    if btc_prev_result:
        msg_parts.append(f"BTC: {btc_prev_result}")
    if eth_prev_result:
        msg_parts.append(f"ETH: {eth_prev_result}")
    
    if msg_parts:
        msg = "📊 Результаты предыдущего часа:\n" + "\n".join(msg_parts)
        send_telegram(msg)
    else:
        send_telegram("⏳ Ожидание результатов предыдущего часа...")
    
    # Проверка результатов текущих ставок
    print("\n" + "="*50)
    print("ПРОВЕРКА ТЕКУЩИХ СТАВОК")
    print("="*50)
    
    for coin_key in list(state.get("pending_bets", {}).keys()):
        info = state["pending_bets"][coin_key]
        slug = info["slug"]
        direction = info["direction"]
        amount = info["amount"]
        
        print(f"Проверка ставки: {coin_key}")
        
        m = get_market(slug)
        if m and is_market_resolved(m):
            w = get_winner(m)
            if w:
                if w == direction:
                    profit = amount * (1 / info['price'] - 1) if info['price'] > 0 else 0
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
            
            # Проверяем, не разрешен ли уже рынок
            if is_market_resolved(current_market):
                print(f"{coin} → рынок уже разрешен, пропускаем")
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
            
            # Проверяем баланс перед ставкой
            if real_balance < next_bet:
                print(f"❌ Недостаточно средств: баланс ${real_balance}, нужно ${next_bet}")
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
                
                # Получаем реальную цену из market
                _, price = get_token_id_and_price(current_market, next_dir)
                
                state["pending_bets"][bet_key] = {
                    "slug": current_market["slug"],
                    "direction": next_dir,
                    "amount": next_bet,
                    "price": price,
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
