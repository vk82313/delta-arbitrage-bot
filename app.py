import websocket
import json
import threading
import time
import requests
import os
from datetime import datetime, timedelta, timezone
from flask import Flask

app = Flask(__name__)

# === Telegram Config ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

print("🚀 Starting Delta Arbitrage Bot...")


class ArbitrageBot:
    def __init__(self):
        self.ws = None
        self.current_expiry = self.get_current_expiry()
        self.active_symbols = []
        self.btc_options = {}
        self.eth_options = {}
        self.last_alert = {}
        self.msg_count = 0

    def get_current_expiry(self):
        """Get current expiry in DDMMYY format"""
        now = datetime.now(timezone.utc)
        ist_now = now + timedelta(hours=5, minutes=30)
        expiry_date = ist_now if ist_now.hour < 17 else ist_now + timedelta(days=1)
        expiry_str = expiry_date.strftime("%d%m%y")
        print(f"📅 Using expiry: {expiry_str}")
        return expiry_str

    def fetch_all_traded_strikes(self):
        """Fetch ALL traded BTC/ETH option symbols"""
        try:
            print("🔍 Fetching ALL traded strikes from Delta API...")
            url = "https://api.delta.exchange/v2/products"
            response = requests.get(url, timeout=15)
            if response.status_code != 200:
                raise Exception(f"API Error {response.status_code}")

            products = response.json().get("result", [])
            btc_symbols, eth_symbols = [], []

            for product in products:
                symbol = product.get("symbol", "")
                contract_type = str(product.get("contract_type", "")).lower()
                status = product.get("product_trading_status", "")
                if any(x in contract_type for x in ["call", "put", "option"]) and \
                   self.current_expiry in symbol and status == "operational":
                    if symbol.startswith("BTC-"):
                        btc_symbols.append(symbol)
                    elif symbol.startswith("ETH-"):
                        eth_symbols.append(symbol)

            all_symbols = sorted(set(btc_symbols + eth_symbols))
            print(f"🎯 Found {len(all_symbols)} symbols for expiry {self.current_expiry}")
            return all_symbols if all_symbols else self.get_fallback_symbols()

        except Exception as e:
            print(f"❌ Error fetching strikes: {e}")
            return self.get_fallback_symbols()

    def get_fallback_symbols(self):
        """Fallback list of strikes if API fails"""
        print("🔄 Using fallback symbols...")
        btc_strikes = [65000, 66000, 67000, 68000, 69000, 70000]
        eth_strikes = [3000, 3100, 3200, 3300, 3400, 3500]
        symbols = []
        for strike in btc_strikes:
            symbols += [f"BTC-{self.current_expiry}-{strike}-C", f"BTC-{self.current_expiry}-{strike}-P"]
        for strike in eth_strikes:
            symbols += [f"ETH-{self.current_expiry}-{strike}-C", f"ETH-{self.current_expiry}-{strike}-P"]
        return symbols

    def extract_strike(self, symbol):
        """Extract strike price from option symbol"""
        try:
            parts = symbol.split("-")
            for part in parts:
                if part.isdigit():
                    return int(part)
        except:
            pass
        return 0

    def send_telegram_alert(self, message):
        """Send alert to Telegram"""
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
            requests.post(url, json=payload, timeout=5)
            print("✅ Telegram alert sent")
        except Exception as e:
            print(f"❌ Telegram error: {e}")

    def check_arbitrage(self, asset, options_data):
        """Detect arbitrage opportunities"""
        try:
            strikes = {}
            for symbol, prices in options_data.items():
                strike = self.extract_strike(symbol)
                if strike == 0:
                    continue
                if strike not in strikes:
                    strikes[strike] = {"call": {}, "put": {}}
                if "C" in symbol:
                    strikes[strike]["call"] = prices
                elif "P" in symbol:
                    strikes[strike]["put"] = prices

            sorted_strikes = sorted(strikes.keys())
            alerts = []

            for i in range(len(sorted_strikes) - 1):
                s1, s2 = sorted_strikes[i], sorted_strikes[i + 1]
                c1_ask = strikes[s1]["call"].get("ask", 0)
                c2_bid = strikes[s2]["call"].get("bid", 0)
                p1_bid = strikes[s1]["put"].get("bid", 0)
                p2_ask = strikes[s2]["put"].get("ask", 0)

                min_diff = 2 if asset == "BTC" else 0.16
                if c1_ask and c2_bid and c1_ask - c2_bid < -min_diff:
                    key = f"{asset}_CALL_{s1}_{s2}"
                    if self.can_alert(key):
                        alerts.append(f"🔷 CALL {s1} → {s2} profit: ${abs(c1_ask - c2_bid):.2f}")
                if p1_bid and p2_ask and p2_ask - p1_bid < -min_diff:
                    key = f"{asset}_PUT_{s1}_{s2}"
                    if self.can_alert(key):
                        alerts.append(f"🟣 PUT {s1} → {s2} profit: ${abs(p2_ask - p1_bid):.2f}")

            if alerts:
                msg = f"🚨 *{asset} Arbitrage Alerts* 🚨\n" + "\n".join(alerts)
                msg += f"\n\n_Time: {datetime.now().strftime('%H:%M:%S')}_"
                self.send_telegram_alert(msg)

        except Exception as e:
            print(f"❌ Arbitrage error: {e}")

    def can_alert(self, key):
        """Throttle alerts (1 per min per type)"""
        now = time.time()
        last = self.last_alert.get(key, 0)
        if now - last >= 60:
            self.last_alert[key] = now
            return True
        return False

    # -------------------------
    #  WebSocket Event Methods
    # -------------------------
    def on_message(self, ws, message):
        """Handle incoming WebSocket messages"""
        try:
            data = json.loads(message)
            self.msg_count += 1
            if self.msg_count <= 5:
                print("🟢 Raw message:", data)

            symbol = data.get("symbol")
            if not symbol:
                return

            bid = float(data.get("best_bid_price", 0) or data.get("bid", 0))
            ask = float(data.get("best_ask_price", 0) or data.get("ask", 0))

            if bid and ask:
                if symbol.startswith("BTC-"):
                    self.btc_options[symbol] = {"bid": bid, "ask": ask}
                    self.check_arbitrage("BTC", self.btc_options)
                elif symbol.startswith("ETH-"):
                    self.eth_options[symbol] = {"bid": bid, "ask": ask}
                    self.check_arbitrage("ETH", self.eth_options)

        except Exception as e:
            print(f"❌ Message error: {e}")

    def on_error(self, ws, error):
        print(f"❌ WebSocket error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        print("🔴 WebSocket closed - reconnecting in 5s...")
        time.sleep(5)
        self.start_websocket()

    def on_open(self, ws):
        print("✅ WebSocket connected successfully!")
        self.active_symbols = self.fetch_all_traded_strikes()
        print(f"📡 Total symbols: {len(self.active_symbols)}")

        # Split into batches of 30
        def chunks(lst, n):
            for i in range(0, len(lst), n):
                yield lst[i:i + n]

        for batch in chunks(self.active_symbols, 30):
            sub_msg = {
                "type": "subscribe",
                "payload": {"channels": [{"name": "v2/ticker", "symbols": batch}]}
            }
            ws.send(json.dumps(sub_msg))
            print(f"📤 Subscribed batch of {len(batch)}")

        self.send_telegram_alert(
            f"🔗 *Bot Connected*\n✅ Monitoring {len(self.active_symbols)} symbols\n📅 Expiry: {self.current_expiry}"
        )

    def start_websocket(self):
        """Connect and run WebSocket"""
        print("🌐 Connecting to Delta WebSocket...")
        self.ws = websocket.WebSocketApp(
            "wss://socket.delta.exchange",
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        self.ws.run_forever()

    def start(self):
        """Launch bot thread"""
        ws_thread = threading.Thread(target=self.start_websocket)
        ws_thread.daemon = True
        ws_thread.start()


# -------------------------
#  Flask + Bot Runner
# -------------------------
bot = ArbitrageBot()
bot.start()


@app.route("/")
def home():
    return f"<h1>✅ Delta Arbitrage Bot</h1><p>Expiry: {bot.current_expiry}</p><p>Monitoring: {len(bot.active_symbols)} symbols</p>"


@app.route("/health")
def health():
    return f"🟢 Healthy - {len(bot.active_symbols)} symbols"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
