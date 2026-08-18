import time
import requests
import urllib.parse
import hashlib
import hmac
import base64
from datetime import datetime

# =============================
# API KEYS
# =============================
api_key = ""
api_sec = ""
api_url = "https://api.kraken.com"

# =============================
# SETTINGS
# =============================

pair = "SOLUSD"

risk_fraction = 0.05
fee_percent = 0.52
base_take_profit = 2.5

max_trades_per_hour = 4
cooldown_seconds = 300
loop_time = 60

paper_trade = False  # change to False to use real orders

# =============================
# SIGNATURE
# =============================

def get_kraken_signature(url_path, data, secret):

    postdata = urllib.parse.urlencode(data)
    encoded = (str(data["nonce"]) + postdata).encode()

    message = url_path.encode() + hashlib.sha256(encoded).digest()

    mac = hmac.new(base64.b64decode(secret), message, hashlib.sha512)

    return base64.b64encode(mac.digest()).decode()


def kraken_request(url_path, data):

    headers = {
        "API-Key": api_key,
        "API-Sign": get_kraken_signature(url_path, data, api_sec),
    }

    return requests.post(api_url + url_path, headers=headers, data=data)


# =============================
# ACCOUNT
# =============================

def get_usd_balance():

    if paper_trade:
        return paper_balance

    resp = kraken_request(
        "/0/private/Balance",
        {"nonce": str(int(1000 * time.time()))},
    ).json()

    if resp["error"]:
        return 0

    return float(resp["result"].get("ZUSD", 0))


def get_current_price():

    return float(
        requests.get(
            f"https://api.kraken.com/0/public/Ticker?pair={pair}"
        ).json()["result"][pair]["c"][0]
    )


# =============================
# INDICATORS
# =============================

def get_ohlc(interval):

    resp = requests.get(
        f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval}"
    ).json()

    return resp["result"][pair][:-1]


def EMA(data, length):

    ema = data[-length]
    k = 2 / (length + 1)

    for price in data[-length + 1:]:
        ema = price * k + ema * (1 - k)

    return ema


def RSI(closes, period=14):

    gains = []
    losses = []

    for i in range(-period, -1):

        change = closes[i] - closes[i - 1]

        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def ATR(data, period=14):

    trs = []

    for i in range(-period, -1):

        high = float(data[i][2])
        low = float(data[i][3])
        prev_close = float(data[i - 1][4])

        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))

        trs.append(tr)

    return sum(trs) / period


# =============================
# POSITION SIZE
# =============================

def calculate_position_size(price):

    usd_balance = get_usd_balance()

    position_value = usd_balance * risk_fraction

    size = position_value / price

    return round(size, 3)


# =============================
# LOGGING
# =============================

def log_trade(message):

    with open("trade_log.txt", "a") as f:
        f.write(f"{datetime.now()} - {message}\n")


# =============================
# START BOT
# =============================

print("Advanced Kraken Bot Running...")

in_position = False
entry_price = 0
position_size = 0

atr_value = 0
highest_price = 0

last_sell_time = 0
trade_times = []

wins = 0
losses = 0

paper_balance = 50

while True:

    try:

        price = get_current_price()

        # =============================
        # 5 MINUTE DATA
        # =============================

        data_5m = get_ohlc(5)

        closes_5m = [float(x[4]) for x in data_5m]
        volumes = [float(x[6]) for x in data_5m]

        rsi = RSI(closes_5m)

        ema_5m = EMA(closes_5m, 50)

        avg_volume = sum(volumes[-20:]) / 20
        current_volume = volumes[-1]

        atr_value = ATR(data_5m)

        # =============================
        # 1 HOUR TREND FILTER
        # =============================

        data_1h = get_ohlc(60)

        closes_1h = [float(x[4]) for x in data_1h]

        ema_1h = EMA(closes_1h, 50)

        trend_up = price > ema_1h

        print("\nPrice:", price)
        print("RSI:", rsi)
        print("ATR:", atr_value)

        # =============================
        # ENTRY
        # =============================

        if not in_position:

            trade_times = [t for t in trade_times if time.time() - t < 3600]

            if len(trade_times) >= max_trades_per_hour:
                time.sleep(loop_time)
                continue

            dip_buy = rsi < 35
            volume_spike = current_volume > avg_volume
            trend_confirm = price > ema_5m and trend_up

            if dip_buy and volume_spike and trend_confirm:

                position_size = calculate_position_size(price)

                entry_price = price
                highest_price = price

                in_position = True
                trade_times.append(time.time())

                print("BUY", position_size, "SOL")

                log_trade(f"BUY {position_size} at {price}")

                if not paper_trade:

                    kraken_request(
                        "/0/private/AddOrder",
                        {
                            "nonce": str(int(1000 * time.time())),
                            "ordertype": "market",
                            "type": "buy",
                            "volume": position_size,
                            "pair": pair,
                        },
                    )

        # =============================
        # EXIT
        # =============================

        else:

            if price > highest_price:
                highest_price = price

            stop_loss = entry_price - (1.5 * atr_value)
            trailing_stop = highest_price - (1.2 * atr_value)

            take_profit = entry_price * (1 + (base_take_profit + fee_percent) / 100)

            reason = None

            if price <= stop_loss:
                reason = "ATR Stop"

            elif price <= trailing_stop:
                reason = "ATR Trail"

            elif price >= take_profit:
                reason = "Take Profit"

            if reason:

                profit = (price - entry_price) * position_size

                if paper_trade:
                    paper_balance += profit

                if profit > 0:
                    wins += 1
                else:
                    losses += 1

                print("SELL:", reason)
                print("Profit:", round(profit, 2))
                print("Balance:", round(paper_balance, 2))

                log_trade(f"SELL {price} | {reason} | P/L {profit}")

                in_position = False
                last_sell_time = time.time()

                if not paper_trade:

                    kraken_request(
                        "/0/private/AddOrder",
                        {
                            "nonce": str(int(1000 * time.time())),
                            "ordertype": "market",
                            "type": "sell",
                            "volume": position_size,
                            "pair": pair,
                        },
                    )

        time.sleep(loop_time)

    except Exception as e:

        print("Error:", e)
        time.sleep(loop_time)