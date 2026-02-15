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
            if "first_run_done" not in data:
                data["first_run_done"] = False
            return data
    return {"pending_bets": {}, "first_run_done": False}

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
        return 100.0  # Возвращаем 100 для теста

def find_correct_market(coin):
    """Поиск правильного 1h рынка"""
    try:
        # Сначала ищем через поиск
        url = f"https://gamma-api.polymarket.com/markets?limit=20&active=true&question_contains={coin}"
        resp = requests.get(url, timeout=10)
        
        if resp.status_code != 200:
            return None
        
        markets = resp.json()
        
        # Ищем рынок с "1h" в названии
        for market in markets:
            question = market.get('question', '').lower()
            if f"{coin.lower()} 1h" in question or f"{coin.lower()} hourly" in question:
                print(f"Найден правильный рынок: {market.get('question')}")
                return market
        
        # Если не нашли, берем первый попавшийся с 1h
        for market in markets:
            question = market.get('question', '').lower()
            if "1h" in question or "hourly" in question:
                print(f"Найден рынок с 1h: {market.get('question')}")
                return market
        
        return None
    except Exception as e:
        print(f"Ошибка поиска рынка: {e}")
        return None

def place_initial_down_bet(client, coin, state):
    """Размещает первую ставку на Down сразу после запуска"""
    try:
        print(f"\n=== Поиск рынка для {coin} ===")
        
        # Ищем правильный рынок
        market = find_correct_market(coin)
        
        if not market:
            print(f"{coin} → не найден подходящий рынок")
            return False
        
        print(f"Рынок: {market.get('question')}")
        print(f"Цены (сырые): {market.get('outcomePrices')}")
        print(f"Токены: {market.get('clobTokenIds')}")
        
        # Проверяем, можно ли торговать
        if market.get('active') == False:
            print(f"{coin} → рынок не активен")
            return False
        
        clob_ids = market.get("clobTokenIds", [])
        if len(clob_ids) < 2:
            print(f"{coin} → нет токенов для торговли")
            return False
        
        # Получаем данные для Down
        token_id_down, price_down = get_token_id_and_price(market, "Down")
        
        if token_id_down is None:
            print(f"{coin} → не удалось получить token ID для Down")
            return False
        
        print(f"Down цена: {price_down:.3f}, токен ID: {token_id_down}")
        
        if price_down > MAX_PRICE_FOR_OPPOSITE:
            print(f"{coin} Down по {price_down:.3f} → коэффициент мал (< {MIN_MULTIPLIER}), пропускаем")
            return False
        
        # Проверяем баланс
        available_balance = check_balance(client)
        print(f"Доступный баланс: ${available_balance}")
        
        if available_balance < BASE_BET:
            print(f"Недостаточно USDC: нужно ${BASE_BET}, доступно ${available_balance}")
            return False
        
        bet_price = min(0.99, price_down + PRICE_BUFFER)
        bet_key = f"{coin}_last"
        
        print(f"Пытаемся разместить ордер: {coin} Down, цена {bet_price:.3f}, размер ${BASE_BET}")
        
        order_args = OrderArgs(
            token_id=token_id_down,
            side=BUY,
            price=bet_price,
            size=BASE_BET
        )
        
        signed = client.create_order(order_args)
        resp = client.post_order(signed, OrderType.GTC)
        
        print(f"Ответ от биржи: {resp}")
        
        if isinstance(resp, dict):
            if "id" in resp or resp.get("status") in ("success", "placed"):
                now_str = datetime.now(timezone(timedelta(hours=5))).strftime('%Y-%m-%d %H:%M:%S')
                msg = f"🎯 ПЕРВАЯ СТАВКА: {coin} 1h → Down | ${BASE_BET:.1f} по {bet_price:.3f}"
                print(msg)
                send_telegram(msg)
                
                state["pending_bets"][bet_key] = {
                    "slug": market["slug"],
                    "direction": "Down",
                    "amount": BASE_BET,
                    "price": bet_price,
                    "placed_at": now_str,
                    "next_bet": BASE_BET
                }
                save_state(state)
                return True
            else:
                print(f"{coin} ошибка при первой ставке: {resp}")
        return False
        
    except Exception as e:
        print(f"Ошибка при размещении первой ставки: {e}")
        import traceback
        traceback.print_exc()
        return False

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

    state = load_state()
    
    # Первая ставка при запуске (если еще не делали)
    if not state.get("first_run_done", False):
        print("\n" + "="*50)
        print("ПЕРВЫЙ ЗАПУСК - пробуем поставить на DOWN...")
        print("="*50)
        
        # Пробуем BTC, потом ETH
        if place_initial_down_bet(client, "BTC", state):
            state["first_run_done"] = True
            save_state(state)
            print("✅ Первая ставка на BTC Down размещена!")
        elif place_initial_down_bet(client, "ETH", state):
            state["first_run_done"] = True
            save_state(state)
            print("✅ Первая ставка на ETH Down размещена!")
        else:
            print("❌ Не удалось разместить первую ставку")
            # Не помечаем как выполненный, чтобы попробовать в следующий раз
            # state["first_run_done"] = True

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

    # Размещение новой ставки (если сейчас начало часа)
    print("\n" + "="*50)
    print("ПРОВЕРКА НОВОГО ЧАСА")
    print("="*50)
    
    if is_new_hour():
        print("✅ Начало часа - проверяем возможность ставки...")
        
        for coin in ["BTC", "ETH"]:
            try:
                print(f"\n=== Проверка {coin} ===")
                
                # Ищем правильный рынок
                market = find_correct_market(coin)
                
                if not market:
                    print(f"{coin} → не найден подходящий рынок")
                    continue
                
                print(f"Рынок: {market.get('question')}")
                
                if market.get('active') == False:
                    print(f"{coin} → рынок не активен")
                    continue
                
                clob_ids = market.get("clobTokenIds", [])
                if len(clob_ids) < 2:
                    print(f"{coin} → нет токенов для торговли")
                    continue
                
                token_id_up, price_up = get_token_id_and_price(market, "Up")
                token_id_down, price_down = get_token_id_and_price(market, "Down")
                
                if token_id_up is None or token_id_down is None:
                    print(f"{coin} → не удалось получить token ID")
                    continue
                
                bet_key = f"{coin}_last"
                
                # Проверяем, нет ли уже активной ставки
                if bet_key in state["pending_bets"]:
                    print(f"{coin} → уже есть активная ставка")
                    continue
                
                # Получаем предыдущий результат
                prev_markets_url = f"https://gamma-api.polymarket.com/markets?limit=1&closed=true&question_contains={coin}%201h&order=closed_at DESC"
                prev_resp = requests.get(prev_markets_url, timeout=10)
                prev_winner = None
                
                if prev_resp.status_code == 200:
                    prev_markets = prev_resp.json()
                    if prev_markets:
                        prev_winner = get_winner(prev_markets[0])
                        print(f"Предыдущий результат: {prev_winner}")
                
                if prev_winner == "Up":
                    next_dir = "Down"
                    next_price = price_down
                    next_token = token_id_down
                elif prev_winner == "Down":
                    next_dir = "Up"
                    next_price = price_up
                    next_token = token_id_up
                else:
                    print(f"{coin} → нет предыдущего результата")
                    continue
                
                print(f"Направление: {next_dir}, цена: {next_price:.3f}")
                
                if next_price > MAX_PRICE_FOR_OPPOSITE:
                    print(f"{coin} → цена слишком высокая (> {MAX_PRICE_FOR_OPPOSITE:.3f})")
                    continue
                
                current_bet = state["pending_bets"].get(bet_key, {}).get("next_bet", BASE_BET)
                current_bet = min(current_bet, MAX_BET)
                print(f"Размер ставки: ${current_bet}")
                
                available_balance = check_balance(client)
                if available_balance < current_bet:
                    print(f"Недостаточно USDC: нужно ${current_bet}, доступно ${available_balance}")
                    continue
                
                bet_price = min(0.99, next_price + PRICE_BUFFER)
                
                order_args = OrderArgs(
                    token_id=next_token,
                    side=BUY,
                    price=bet_price,
                    size=current_bet
                )
                
                signed = client.create_order(order_args)
                resp = client.post_order(signed, OrderType.GTC)
                
                if isinstance(resp, dict):
                    if "id" in resp or resp.get("status") in ("success", "placed"):
                        now_str = now.strftime('%Y-%m-%d %H:%M:%S')
                        msg = f"💰 Ставка: {coin} 1h → {next_dir} | ${current_bet:.1f} по {bet_price:.3f}"
                        print(msg)
                        send_telegram(msg)
                        
                        state["pending_bets"][bet_key] = {
                            "slug": market["slug"],
                            "direction": next_dir,
                            "amount": current_bet,
                            "price": bet_price,
                            "placed_at": now_str,
                            "next_bet": BASE_BET
                        }
                        save_state(state)
                        
            except Exception as e:
                print(f"Ошибка при обработке {coin}: {e}")
                import traceback
                traceback.print_exc()
    else:
        current_minute = datetime.now(timezone(timedelta(hours=5))).minute
        print(f"Сейчас {current_minute} минут, ждем 00 минут для новых ставок")
    
    print("\n" + "="*50)
    print("Бот завершил работу")
    print("="*50)

if __name__ == "__main__":
    main()
