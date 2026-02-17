import os
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
print("🔧 РЕЖИМ: РЕАЛЬНЫЙ (ставки на реальные деньги)")

CHAIN_ID = 137
HOST = "https://clob.polymarket.com"

BASE_BET = 2.0
MAX_BET = 64.0
MIN_MULTIPLIER = 1.7
MAX_PRICE_FOR_OPPOSITE = 1.0 / MIN_MULTIPLIER
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
            if "statistics" not in data:
                data["statistics"] = {
                    "total_profit": 0.0,
                    "total_bets": 0,
                    "wins": 0,
                    "losses": 0,
                    "history": [],
                    "max_loss_streak": 0,
                    "current_loss_streak": 0,
                    "last_6h_report": None,
                    "last_24h_report": None
                }
            if "last_results" not in data:
                data["last_results"] = {}
            if "martingale" not in data:
                data["martingale"] = {}
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
            "last_6h_report": None,
            "last_24h_report": None
        },
        "last_results": {},
        "martingale": {}
    }

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def update_statistics(state, coin, result, profit, bet_amount, direction):
    stats = state["statistics"]
    
    stats["history"].append({
        "timestamp": datetime.now().isoformat(),
        "coin": coin,
        "result": result,
        "profit": profit,
        "bet_amount": bet_amount,
        "direction": direction
    })
    
    stats["total_bets"] += 1
    stats["total_profit"] += profit
    
    if profit > 0:
        stats["wins"] += 1
        stats["current_loss_streak"] = 0
        if coin in state["martingale"]:
            del state["martingale"][coin]
    else:
        stats["losses"] += 1
        stats["current_loss_streak"] += 1
        if stats["current_loss_streak"] > stats["max_loss_streak"]:
            stats["max_loss_streak"] = stats["current_loss_streak"]
        
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
    
    if len(stats["history"]) > 1000:
        stats["history"] = stats["history"][-1000:]
    
    save_state(state)

def update_last_result(state, coin, result):
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
    now = datetime.now()
    stats = state["statistics"]
    need_6h = False
    need_24h = False
    
    if stats["last_6h_report"] is None:
        need_6h = True
    else:
        last_6h = datetime.fromisoformat(stats["last_6h_report"])
        if (now - last_6h).total_seconds() >= 6 * 3600:
            need_6h = True
    
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
        address = client.get_address()
        print(f"Проверка баланса для адреса: {address}")
        
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
            if isinstance(data, dict) and 'balance' in data:
                balance = float(data['balance'])
                print(f"💰 Баланс: ${balance:.2f}")
                return balance
        
        print("❌ Не удалось получить баланс")
        return None
        
    except Exception as e:
        print(f"Ошибка проверки баланса: {e}")
        return None

def is_new_interval(minutes=15):
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
    now_utc5 = datetime.now(timezone(timedelta(hours=5)))
    et_now = now_utc5 - timedelta(hours=10)
    return et_now

def get_current_interval_timestamp(coin):
    now_utc = datetime.now(timezone.utc)
    
    current_minute = now_utc.minute
    interval_start = (current_minute // 15) * 15
    interval_time_utc = now_utc.replace(minute=interval_start, second=0, microsecond=0)
    
    timestamp = int(interval_time_utc.timestamp())
    interval_time_et = interval_time_utc - timedelta(hours=5)
    
    return timestamp, interval_time_et

def get_interval_timestamp(coin, minutes_ago):
    now_utc = datetime.now(timezone.utc)
    
    target_time_utc = now_utc - timedelta(minutes=minutes_ago)
    
    target_minute = target_time_utc.minute
    interval_start = (target_minute // 15) * 15
    interval_time_utc = target_time_utc.replace(minute=interval_start, second=0, microsecond=0)
    
    timestamp = int(interval_time_utc.timestamp())
    interval_time_et = interval_time_utc - timedelta(hours=5)
    
    return timestamp, interval_time_et

def get_market_by_timestamp(coin, timestamp):
    try:
        if coin == "BTC":
            slug = f"btc-updown-15m-{timestamp}"
        else:
            slug = f"eth-updown-15m-{timestamp}"
        
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

def get_interval_result(coin, minutes_ago):
    try:
        timestamp, interval_time_et = get_interval_timestamp(coin, minutes_ago)
        
        market = get_market_by_timestamp(coin, timestamp)
        
        if not market:
            return None
        
        if not is_market_resolved(market):
            return None
        
        return get_winner(market)
        
    except Exception as e:
        print(f"Ошибка получения результата интервала: {e}")
        return None

def determine_bet_direction(coin, state):
    bet_key = f"{coin}_last"
    if bet_key in state.get("pending_bets", {}):
        return None, None
    
    if coin in state["martingale"]:
        martingale = state["martingale"][coin]
        return martingale['direction'], martingale['next_bet']
    
    result_minus_1 = get_interval_result(coin, 15)
    result_minus_2 = get_interval_result(coin, 30)
    
    if result_minus_1 and result_minus_2 and result_minus_1 == result_minus_2:
        direction = "Up" if result_minus_1 == "Down" else "Down"
        return direction, BASE_BET
    
    return None, None

def place_bet(client, coin, direction, bet_amount, state):
    try:
        timestamp, interval_time_et = get_current_interval_timestamp(coin)
        
        market = get_market_by_timestamp(coin, timestamp)
        
        if not market:
            print(f"❌ {coin} → рынок не найден")
            return False, None
        
        if is_market_resolved(market):
            print(f"❌ {coin} → рынок уже разрешен")
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
            print(f"❌ {coin} → нет token ID")
            return False, None
        
        if direction == "Down" and price > MAX_PRICE_FOR_OPPOSITE:
            print(f"❌ Цена слишком высокая")
            return False, None
        
        current_balance = get_current_balance(client)
        if current_balance is None:
            print("❌ Не удалось проверить баланс")
            return False, None
            
        if current_balance < bet_amount:
            print(f"❌ Недостаточно средств")
            return False, None
        
        bet_price = min(0.99, price + PRICE_BUFFER)
        
        order_args = OrderArgs(
            token_id=token_id,
            side=BUY,
            price=bet_price,
            size=bet_amount
        )
        
        signed = client.create_order(order_args)
        resp = client.post_order(signed, OrderType.GTC)
        
        if isinstance(resp, dict):
            if "id" in resp:
                return True, resp["id"]
            elif resp.get("status") in ("success", "placed"):
                return True, resp.get("order", {}).get("id")
        
        return False, None
        
    except Exception as e:
        print(f"❌ Ошибка при размещении ставки: {e}")
        return False, None

# ========== ГЛАВНАЯ ФУНКЦИЯ ==========

def main():
    print("Запуск бота Polymarket...")
    et_now = get_current_et_time()
    utc5_now = datetime.now(timezone(timedelta(hours=5)))
    print(f"Время ET: {et_now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Время сервера (UTC+5): {utc5_now.strftime('%Y-%m-%d %H:%M:%S')}")
    
    client = ClobClient(
        host=HOST,
        key=PRIVATE_KEY,
        chain_id=CHAIN_ID,
        signature_type=1,
        funder=None
    )

    print(f"Адрес кошелька: {client.get_address()}")
    
    state = load_state()
    
    try:
        api_creds = client.create_or_derive_api_creds()
        client.set_api_creds(api_creds)
        print("✅ API creds получены")
    except Exception as e:
        print("❌ Ошибка API creds:", str(e))
        send_telegram(f"❌ Ошибка API creds: {str(e)}")
        return

    print("\n=== ПРОВЕРКА БАЛАНСА ===")
    current_balance = get_current_balance(client)
    
    if current_balance is None:
        print("❌ Не удалось получить баланс")
        send_telegram("❌ Ошибка: не удалось получить баланс аккаунта")
        return
    
    print(f"💰 Баланс: ${current_balance:.2f}")
    
    if current_balance < BASE_BET:
        print(f"⚠️ Баланс меньше минимальной ставки ${BASE_BET}")
        send_telegram(f"⚠️ Баланс ${current_balance:.2f} меньше минимальной ставки ${BASE_BET}")
        return

    need_6h, need_24h = check_reports(state)
    
    if need_6h:
        period = get_statistics_period(state, 6)
        total = state["statistics"]
        
        msg = f"""📊 <b>Отчет за 6 часов:</b>
💰 Баланс: ${current_balance:.2f}
📈 Прибыль: ${period['profit']:.2f}
🎲 Ставок: {period['bets']} (✅ {period['wins']} | ❌ {period['losses']})
📊 Винрейт: {period['win_rate']:.1f}%
🔥 Макс. серия: {period['max_loss_streak']}

<b>Общее:</b>
💰 Прибыль: ${total['total_profit']:.2f}
🎲 Ставок: {total['total_bets']}
📈 Макс. серия: {total['max_loss_streak']}"""
        
        send_telegram(msg)
        state["statistics"]["last_6h_report"] = datetime.now().isoformat()
        save_state(state)
    
    if need_24h:
        period = get_statistics_period(state, 24)
        total = state["statistics"]
        
        msg = f"""📊 <b>Отчет за 24 часа:</b>
💰 Баланс: ${current_balance:.2f}
📈 Прибыль: ${period['profit']:.2f}
🎲 Ставок: {period['bets']} (✅ {period['wins']} | ❌ {period['losses']})
📊 Винрейт: {period['win_rate']:.1f}%
🔥 Макс. серия: {period['max_loss_streak']}

<b>Общее:</b>
💰 Прибыль: ${total['total_profit']:.2f}
🎲 Ставок: {total['total_bets']}
📈 Макс. серия: {total['max_loss_streak']}"""
        
        send_telegram(msg)
        state["statistics"]["last_24h_report"] = datetime.now().isoformat()
        save_state(state)
    
    for coin_key in list(state.get("pending_bets", {}).keys()):
        info = state["pending_bets"][coin_key]
        slug = info["slug"]
        direction = info["direction"]
        amount = info["amount"]
        price = info.get("price", 0.5)
        coin = coin_key.split('_')[0]
        
        m = get_market(slug)
        if m and is_market_resolved(m):
            w = get_winner(m)
            if w:
                if w == direction:
                    profit = amount * (1 / price - 1) if price > 0 else 0
                    msg = f"✅ {coin_key} → {direction} | +${profit:.2f}"
                    send_telegram(msg)
                    update_statistics(state, coin, "win", profit, amount, direction)
                    update_last_result(state, coin, w)
                else:
                    profit = -amount
                    msg = f"❌ {coin_key} → {direction} | -${amount:.2f}"
                    send_telegram(msg)
                    update_statistics(state, coin, "loss", -amount, amount, direction)
                    update_last_result(state, coin, w)
                
                del state["pending_bets"][coin_key]
                save_state(state)
    
    if is_new_interval(15):
        for coin in ["BTC", "ETH"]:
            direction, bet_amount = determine_bet_direction(coin, state)
            
            if not direction or not bet_amount:
                continue
            
            bet_key = f"{coin}_last"
            
            current_balance = get_current_balance(client)
            if current_balance < bet_amount:
                continue
            
            success, order_id = place_bet(client, coin, direction, bet_amount, state)
            
            if success:
                now_str = utc5_now.strftime('%Y-%m-%d %H:%M:%S')
                
                if coin in state["martingale"]:
                    series_info = f"(серия {state['martingale'][coin]['losses_count'] + 1})"
                else:
                    series_info = "(новая серия)"
                
                msg = f"💰 {coin} 15m → {direction} | ${bet_amount:.1f} {series_info}"
                send_telegram(msg)
                
                timestamp, _ = get_current_interval_timestamp(coin)
                
                state["pending_bets"][bet_key] = {
                    "slug": f"{coin.lower()}-updown-15m-{timestamp}",
                    "direction": direction,
                    "amount": bet_amount,
                    "price": 0.5,
                    "placed_at": now_str
                }
                save_state(state)

if __name__ == "__main__":
    main()
