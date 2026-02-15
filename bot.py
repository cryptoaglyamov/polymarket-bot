import os
import time
import json
import requests
from datetime import datetime, timezone, timedelta

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

# ================== НАСТРОЙКИ ==================

# Читаем переменные окружения (их добавим позже в настройках GitHub)
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
    now = datetime.now(timezone(timedelta(hours=5)))  # UTC+5 Душанбе
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
        p0 = float(prices_str[0])  # Up
        p1 = float(prices_str[1])  # Down
        
        if p0 >= 0.90:
            return "Up"
        if p1 >= 0.90:
            return "Down"
        
        if market.get("closed"):
            return "Up" if p0 > p1 else "Down"
    except:
        pass
    
    return None

def get_token_id_and_price(market, direction: str):
    clob_ids = market.get("clobTokenIds", [])
    prices = [float(p) for p in market.get("outcomePrices", ["0.5", "0.5"])]
    index = 0 if direction == "Up" else 1
    return clob_ids[index], prices[index]

def check_balance(client, required_amount):
    """Проверка баланса USDC"""
    try:
        balances = client.get_balances()
        
        for balance in balances:
            if balance.get('asset_type') == 'USDC' or balance.get('symbol') == 'USDC':
                available = float(balance.get('available', 0))
                if available >= required_amount:
                    return True, available
                else:
                    return False, available
        
        return False, 0
    except Exception as e:
        print(f"Ошибка проверки баланса: {e}")
        return False, 0

def place_initial_down_bet(client, coin, state):
    """Размещает первую ставку на Down сразу после запуска"""
    try:
        url = f"https://gamma-api.polymarket.com/markets?limit=5&active=true&question_contains={coin}%201h"
        resp = requests.get(url, timeout=10)
        
        if resp.status_code != 200:
            print(f"{coin} → ошибка поиска")
            return False
        
        markets = resp.json()
        if not markets:
            print(f"{coin} → активных рынков не найдено")
            return False
        
        market = markets[0]
        
        # Проверяем, можно ли торговать
        if market.get('active') == False:
            print(f"{coin} → рынок не активен")
            return False
        
        clob_ids = market.get("clobTokenIds", [])
        if len(clob_ids) < 2:
            print(f"{coin} → нет токенов для торговли")
            return False
        
        token_id_down, price_down = get_token_id_and_price(market, "Down")
        
        if price_down > MAX_PRICE_FOR_OPPOSITE:
            print(f"{coin} Down по {price_down:.3f} → коэффициент мал, пропускаем")
            return False
        
        has_balance, available = check_balance(client, BASE_BET)
        if not has_balance:
            print(f"Недостаточно USDC: нужно ${BASE_BET}, доступно ${available}")
            return False
        
        bet_price = min(0.99, price_down + PRICE_BUFFER)
        bet_key = f"{coin}_last"
        
        order_args = OrderArgs(
            token_id=token_id_down,
            side=BUY,
            price=bet_price,
            size=BASE_BET
        )
        
        signed = client.create_order(order_args)
        resp = client.post_order(signed, OrderType.GTC)
        
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
        return False
        
    except Exception as e:
        print(f"Ошибка при размещении первой ставки: {e}")
        return False

# ========== ГЛАВНАЯ ФУНКЦИЯ ==========

def main():
    print("Запуск бота Polymarket...")
    
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
        print("API creds получены")
    except Exception as e:
        print("Ошибка API creds:", str(e))
        send_telegram(f"Ошибка API creds: {str(e)}")
        return

    state = load_state()
    
    # Первая ставка при запуске (если еще не делали)
    if not state.get("first_run_done", False):
        print("Первый запуск - пробуем поставить на DOWN...")
        
        if place_initial_down_bet(client, "BTC", state):
            state["first_run_done"] = True
            save_state(state)
        elif place_initial_down_bet(client, "ETH", state):
            state["first_run_done"] = True
            save_state(state)
        else:
            print("Не удалось разместить первую ставку")
            state["first_run_done"] = True
            save_state(state)

    # Проверка результатов предыдущих ставок
    now = datetime.now(timezone(timedelta(hours=5)))
    
    for coin_key in list(state["pending_bets"].keys()):
        info = state["pending_bets"][coin_key]
        slug = info["slug"]
        direction = info["direction"]
        amount = info["amount"]
        
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
    if is_new_hour():
        print("Начало часа - проверяем возможность ставки...")
        
        for coin in ["BTC", "ETH"]:
            try:
                url = f"https://gamma-api.polymarket.com/markets?limit=5&active=true&question_contains={coin}%201h"
                resp = requests.get(url, timeout=10)
                
                if resp.status_code != 200:
                    continue
                
                markets = resp.json()
                if not markets:
                    continue
                
                market = markets[0]
                
                if market.get('active') == False:
                    continue
                
                clob_ids = market.get("clobTokenIds", [])
                if len(clob_ids) < 2:
                    continue
                
                token_id_up, price_up = get_token_id_and_price(market, "Up")
                token_id_down, price_down = get_token_id_and_price(market, "Down")
                
                bet_key = f"{coin}_last"
                
                # Проверяем, нет ли уже активной ставки
                if bet_key in state["pending_bets"]:
                    continue
                
                # Получаем предыдущий результат
                prev_markets_url = f"https://gamma-api.polymarket.com/markets?limit=1&closed=true&question_contains={coin}%201h&order=closed_at DESC"
                prev_resp = requests.get(prev_markets_url, timeout=10)
                prev_winner = None
                
                if prev_resp.status_code == 200:
                    prev_markets = prev_resp.json()
                    if prev_markets:
                        prev_winner = get_winner(prev_markets[0])
                
                if prev_winner == "Up":
                    next_dir = "Down"
                    next_price = price_down
                    next_token = token_id_down
                elif prev_winner == "Down":
                    next_dir = "Up"
                    next_price = price_up
                    next_token = token_id_up
                else:
                    continue
                
                if next_price > MAX_PRICE_FOR_OPPOSITE:
                    continue
                
                current_bet = state["pending_bets"].get(bet_key, {}).get("next_bet", BASE_BET)
                current_bet = min(current_bet, MAX_BET)
                
                has_balance, available = check_balance(client, current_bet)
                if not has_balance:
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
                        msg = f"💰 Ставка: {coin} 1h → {next_dir} | ${current_bet:.1f}"
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
    
    print("Бот завершил работу")

if __name__ == "__main__":
    main()
