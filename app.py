import os
import time
import requests
import threading
from flask import Flask, jsonify

# === Flask app for Render ===
app = Flask(__name__)

# === Config ===
BASE_URL = "https://api.india.delta.exchange/v2"
SYMBOL = "BTC"
CHECK_INTERVAL = 60  # seconds

# === Telegram Config ===
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN"
TELEGRAM_CHAT_ID = "YOUR_CHAT_ID"


# ---------- Telegram Alert ----------
def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Telegram error: {e}")


# ---------- Get Current or Next Expiry ----------
def get_current_expiry():
    try:
        response = requests.get(f"{BASE_URL}/products")
        data = response.json()["result"]

        # Filter only options of selected symbol
        expiries = sorted(list({
            p["settlement_time"]
            for p in data
            if SYMBOL in p["symbol"] and p["contract_type"] == "option"
        }))

        if not expiries:
            return None

        # Select nearest expiry in the future
        current_time = time.time()
        for exp in expiries:
            try:
                exp_timestamp = time.mktime(time.strptime(exp.split("T")[0], "%Y-%m-%d"))
                if exp_timestamp > current_time:
                    return exp
            except Exception:
                continue

        # fallback to first expiry
        return expiries[0]
    except Exception as e:
        print(f"Error fetching expiry: {e}")
        return None


# ---------- Fetch Options Data ----------
def fetch_option_data(expiry):
    try:
        response = requests.get(
            f"{BASE_URL}/l2orderbook/summary?contract_types=option&underlying={SYMBOL}"
        )
        data = response.json()["result"]
        filtered = [o for o in data if expiry in o["symbol"]]
        return filtered
    except Exception as e:
        print(f"Error fetching data: {e}")
        return []


# ---------- Arbitrage Logic ----------
def check_arbitrage():
    expiry = get_current_expiry()
    if not expiry:
        print("⚠️ No expiry found!")
        return

    print(f"\n🔍 Checking expiry: {expiry}")
    options = fetch_option_data(expiry)

    for opt in options:
        if SYMBOL not in opt["symbol"]:
            continue

        strike = opt.get("strike_price")
        call_price = opt.get("call_price")
        put_price = opt.get("put_price")

        if call_price is not None and put_price is not None:
            diff = call_price - put_price
            if diff > 0:
                msg = f"✅ Arbitrage Found | {SYMBOL} {expiry}\nStrike: {strike}\nCall: {call_price}\nPut: {put_price}\nDiff: {diff:.2f}"
                print(msg)
                send_telegram(msg)


# ---------- Background Bot Loop ----------
def run_bot():
    while True:
        try:
            check_arbitrage()
        except Exception as e:
            print(f"❌ Error in main loop: {e}")
        time.sleep(CHECK_INTERVAL)


# ---------- Flask Routes ----------
@app.route("/")
def index():
    return jsonify({"message": "Delta Arbitrage Bot is running!"})


@app.route("/health")
def health():
    return jsonify({"status": "healthy"}), 200


# ---------- Main Entry ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=port)
