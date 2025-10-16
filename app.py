import requests
import os
import time
from datetime import datetime, timedelta, timezone

# ---------------- Configuration ---------------- #
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_URL = "https://api.india.delta.exchange/v2"
FETCH_INTERVAL = 1  # seconds

# Minimum arbitrage difference
MIN_DIFF = {"BTC": 2, "ETH": 0.16}

# Store last alerts to prevent spamming
last_alert = {}

# ---------------- Utility Functions ---------------- #

def send_telegram_alert(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown"
        }
        requests.post(url, json=payload, timeout=5)
        print(f"✅ Telegram alert sent")
    except Exception as e:
        print(f"❌ Telegram send error: {e}")

def get_current_expiry():
    now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    if now.hour > 17 or (now.hour == 17 and now.minute >= 30):
        expiry_date = now + timedelta(days=1)
    else:
        expiry_date = now
    return expiry_date.strftime("%d%m%y")

def fetch_products():
    try:
        res = requests.get(BASE_URL + "/products", timeout=10)
        data = res.json().get("result", [])
        return data
    except Exception as e:
        print(f"❌ fetch_products error: {e}")
        return []

def fetch_tickers():
    try:
        res = requests.get(BASE_URL + "/tickers", timeout=10)
        data = res.json().get("result", [])
        return data
    except Exception as e:
        print(f"❌ fetch_tickers error: {e}")
        return []

def extract_strike(symbol):
    parts = symbol.split("-")
    for part in parts:
        if part.isdigit():
            return int(part)
    return None

# ---------------- Core Logic ---------------- #

def build_options_map(asset, expiry):
    products = fetch_products()
    tickers = fetch_tickers()

    ticker_map = {t['symbol']: t for t in tickers if 'symbol' in t}

    options_map = {}
    for p in products:
        symbol = p.get('symbol')
        if not symbol or asset not in symbol or expiry not in symbol:
            continue
        strike = extract_strike(symbol)
        if strike is None:
            continue
        if strike not in options_map:
            options_map[strike] = {'call': {}, 'put': {}}

        ticker = ticker_map.get(symbol, {})
        bid = ticker.get('best_bid_price') or ticker.get('bid') or 0
        ask = ticker.get('best_ask_price') or ticker.get('ask') or 0

        if symbol.endswith("-C"):
            options_map[strike]['call'] = {'bid': float(bid), 'ask': float(ask)}
        elif symbol.endswith("-P"):
            options_map[strike]['put'] = {'bid': float(bid), 'ask': float(ask)}
    return options_map

def check_arbitrage(asset, options_map):
    strikes = sorted(options_map.keys())
    alerts = []

    for i in range(len(strikes) - 1):
        s1 = strikes[i]
        s2 = strikes[i+1]

        # CALL arbitrage: ask - next bid < 0
        c1_ask = options_map[s1]['call'].get('ask', 0)
        c2_bid = options_map[s2]['call'].get('bid', 0)
        if c1_ask and c2_bid and (c1_ask - c2_bid) < 0 and abs(c1_ask - c2_bid) >= MIN_DIFF[asset]:
            key = f"{asset}_CALL_{s1}_{s2}"
            now = time.time()
            if now - last_alert.get(key, 0) >= 60:
                alerts.append(f"🔷 CALL {s1} Ask: {c1_ask:.2f} vs {s2} Bid: {c2_bid:.2f} → Profit: {abs(c1_ask - c2_bid):.2f}")
                last_alert[key] = now

        # PUT arbitrage: next ask - bid < 0
        p1_bid = options_map[s1]['put'].get('bid', 0)
        p2_ask = options_map[s2]['put'].get('ask', 0)
        if p1_bid and p2_ask and (p2_ask - p1_bid) < 0 and abs(p2_ask - p1_bid) >= MIN_DIFF[asset]:
            key = f"{asset}_PUT_{s1}_{s2}"
            now = time.time()
            if now - last_alert.get(key, 0) >= 60:
                alerts.append(f"🟣 PUT {s1} Bid: {p1_bid:.2f} vs {s2} Ask: {p2_ask:.2f} → Profit: {abs(p2_ask - p1_bid):.2f}")
                last_alert[key] = now

    if alerts:
        msg = f"🚨 *{asset} OPTIONS ARBITRAGE ALERT* 🚨\nTime: {datetime.now().strftime('%H:%M:%S')}\n\n" + "\n".join(alerts)
        send_telegram_alert(msg)
        print(f"✅ Sent {len(alerts)} {asset} alerts")

# ---------------- Main Loop ---------------- #

def main():
    print("🚀 Starting Delta Options Arbitrage Bot (1-second fetch)...")
    expiry = get_current_expiry()
    print(f"📅 Using expiry: {expiry}")

    while True:
        try:
            for asset in ["BTC", "ETH"]:
                options_map = build_options_map(asset, expiry)
                check_arbitrage(asset, options_map)
            time.sleep(FETCH_INTERVAL)
        except Exception as e:
            print(f"❌ Main loop error: {e}")
            time.sleep(2)

if __name__ == "__main__":
    main()
