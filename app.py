import threading
import time
import json
import websocket
import requests
from flask import Flask, jsonify
from datetime import datetime, timedelta, timezone

# =============== CONFIG ===============
TELEGRAM_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID = "YOUR_TELEGRAM_CHAT_ID"
BASE_URL = "https://api.india.delta.exchange/v2"
REFRESH_INTERVAL = 2  # seconds
ALERT_COOLDOWN = 60

# Thresholds for arbitrage detection
THRESHOLDS = {
    "ETH": 0.16,
    "BTC": 2
}

# =============== GLOBALS ===============
app = Flask(__name__)
alert_lock = threading.Lock()


def send_telegram_message(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print("Telegram Error:", e)


# =============== BOT CLASS ===============
class OptionsBot:
    def __init__(self, asset):
        self.asset = asset.upper()
        self.threshold = THRESHOLDS[self.asset]
        self.expiry = None
        self.strikes = {}       # {strike: {"call": {...}, "put": {...}}}
        self.product_map = {}   # {symbol: product_id}
        self.ws = None
        self.connected = False
        self.last_update = None
        self.last_alert_time = {}

    # ---------- Fetch products ----------
    def fetch_products(self):
        try:
            url = f"{BASE_URL}/products"
            response = requests.get(url, timeout=10)
            products = response.json().get("result", [])
            products = [p for p in products if p.get("underlying_asset", {}).get("symbol") == self.asset]
            expiries = sorted({p["settlement_expiry_timestamp"] for p in products})
            if expiries:
                self.expiry = expiries[-1]
                options = [p for p in products if p["settlement_expiry_timestamp"] == self.expiry]
                self._map_products(options)
                print(f"[{self.asset}] Loaded expiry: {self.expiry}, options: {len(options)}")
        except Exception as e:
            print(f"[{self.asset}] Error fetching products:", e)

    def _map_products(self, options):
        self.strikes = {}
        self.product_map = {}
        for o in options:
            sym = o["symbol"].strip().upper()
            strike = float(o["strike_price"])
            self.product_map[sym] = o["id"]

            if strike not in self.strikes:
                self.strikes[strike] = {"call": None, "put": None}

            if sym.endswith("-C"):
                self.strikes[strike]["call"] = {"symbol": sym, "bid": 0, "ask": 0}
            elif sym.endswith("-P"):
                self.strikes[strike]["put"] = {"symbol": sym, "bid": 0, "ask": 0}

    # ---------- WebSocket ----------
    def start_ws(self):
        ws_url = "wss://socket.india.delta.exchange"
        self.ws = websocket.WebSocketApp(
            ws_url,
            on_open=lambda ws: self._on_open(ws),
            on_message=lambda ws, msg: self._on_message(msg),
            on_error=lambda ws, e: print(f"[{self.asset}] WS Error:", e),
            on_close=lambda ws, *_: print(f"[{self.asset}] WS Closed")
        )
        threading.Thread(target=self.ws.run_forever, daemon=True).start()

    def _on_open(self, ws):
        print(f"[{self.asset}] WebSocket connected")
        self.connected = True
        symbols = list(self.product_map.keys())
        payload = {"type": "subscribe", "payload": {"channels": [{"name": "l2_orderbook", "symbols": symbols}]}}
        ws.send(json.dumps(payload))

    def _on_message(self, message):
        try:
            msg = json.loads(message)
            if msg.get("type") != "l2_orderbook":
                return

            symbol = msg["symbol"].strip().upper()
            if symbol not in self.product_map:
                return

            data = msg["data"]
            bid = float(data["bids"][0][0]) if data.get("bids") else 0
            ask = float(data["asks"][0][0]) if data.get("asks") else 0
            strike = float(symbol.split("-")[-2])

            if strike not in self.strikes:
                return

            if symbol.endswith("-C") and self.strikes[strike]["call"]:
                self.strikes[strike]["bid"] = bid
                self.strikes[strike]["ask"] = ask
            elif symbol.endswith("-P") and self.strikes[strike]["put"]:
                self.strikes[strike]["bid"] = bid
                self.strikes[strike]["ask"] = ask

            self.last_update = time.time()
        except Exception as e:
            print(f"[{self.asset}] Message error:", e)

    # ---------- Arbitrage ----------
    def can_alert(self, key):
        now = time.time()
        last = self.last_alert_time.get(key, 0)
        if now - last >= ALERT_COOLDOWN:
            self.last_alert_time[key] = now
            return True
        return False

    def check_arbitrage(self):
        strikes_sorted = sorted(self.strikes.keys())
        alerts = []

        for i in range(len(strikes_sorted) - 1):
            s1, s2 = strikes_sorted[i], strikes_sorted[i + 1]
            data1, data2 = self.strikes[s1], self.strikes[s2]

            # CALL arbitrage
            call1, call2 = data1["call"], data2["call"]
            if call1 and call2 and call1["ask"] > 0 and call2["bid"] > 0:
                diff = call2["bid"] - call1["ask"]
                if diff >= self.threshold:
                    key = f"{self.asset}_CALL_{s1}_{s2}"
                    if self.can_alert(key):
                        alerts.append(
                            f"🔷 {self.asset} CALL {int(s1):,} Ask: ${call1['ask']:.2f} vs {int(s2):,} Bid: ${call2['bid']:.2f} → Profit: ${diff:.2f}"
                        )

            # PUT arbitrage
            put1, put2 = data1["put"], data2["put"]
            if put1 and put2 and put1["bid"] > 0 and put2["ask"] > 0:
                diff = put1["bid"] - put2["ask"]
                if diff >= self.threshold:
                    key = f"{self.asset}_PUT_{s1}_{s2}"
                    if self.can_alert(key):
                        alerts.append(
                            f"🟣 {self.asset} PUT {int(s1):,} Bid: ${put1['bid']:.2f} vs {int(s2):,} Ask: ${put2['ask']:.2f} → Profit: ${diff:.2f}"
                        )

        if alerts:
            expiry_str = datetime.utcfromtimestamp(self.expiry).strftime("%d%b%y") if self.expiry else "N/A"
            message = f"🚨 {self.asset} {expiry_str} ARBITRAGE ALERTS 🚨\n\n" + "\n".join(alerts)
            send_telegram_message(message)

    # ---------- Main loop ----------
    def run(self):
        self.fetch_products()
        self.start_ws()
        while True:
            try:
                self.check_arbitrage()
                time.sleep(REFRESH_INTERVAL)
            except Exception as e:
                print(f"[{self.asset}] Loop error:", e)
                time.sleep(5)


# =============== FLASK DASHBOARD ===============
@app.route("/")
def home():
    return jsonify({"status": "running", "bots": ["ETH", "BTC"]})


@app.route("/health")
def health():
    return jsonify({
        "ETH": {"connected": eth_bot.connected, "last_update": eth_bot.last_update},
        "BTC": {"connected": btc_bot.connected, "last_update": btc_bot.last_update}
    })


@app.route("/debug")
def debug():
    return jsonify({
        "ETH": eth_bot.strikes,
        "BTC": btc_bot.strikes
    })


# =============== START BOTS ===============
if __name__ == "__main__":
    eth_bot = OptionsBot("ETH")
    btc_bot = OptionsBot("BTC")

    threading.Thread(target=eth_bot.run, daemon=True).start()
    threading.Thread(target=btc_bot.run, daemon=True).start()

    app.run(host="0.0.0.0", port=8000)
