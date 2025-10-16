import requests
import threading
import time
import os
from datetime import datetime, timedelta, timezone
from flask import Flask

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BASE_URL = "https://api.india.delta.exchange/v2"

print("🚀 Starting Optimized Delta Options Arbitrage Bot...")

class ArbitrageBot:
    def __init__(self):
        self.current_expiry = self.get_current_expiry()
        self.symbols = self.fetch_all_symbols()
        self.options_data = {}  # Stores live bid/ask for all symbols
        self.last_alert = {}

    def get_current_expiry(self):
        now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
        expiry_date = now + timedelta(days=1) if (now.hour >= 17 and now.minute >= 30) else now
        return expiry_date.strftime("%d%m%y")

    def fetch_all_symbols(self):
        """Fetch all BTC/ETH options for current expiry"""
        try:
            res = requests.get(f"{BASE_URL}/products", timeout=10)
            products = res.json().get("result", [])
        except:
            products = []

        symbols = []
        for p in products:
            sym = p.get("symbol", "")
            ctype = str(p.get("contract_type", "")).lower()
            trading = p.get("product_trading_status", "")
            if self.current_expiry in sym and trading == "operational" and any(x in ctype for x in ["call","put","option"]):
                symbols.append(sym)
        print(f"📡 Monitoring {len(symbols)} options for expiry {self.current_expiry}")
        return symbols

    def fetch_option_price(self, symbol):
        """Fetch bid/ask via REST and update local storage"""
        try:
            res = requests.get(f"{BASE_URL}/tickers/{symbol}", timeout=5)
            data = res.json().get("result", {})
            bid = float(data.get("best_bid_price") or data.get("bid") or 0)
            ask = float(data.get("best_ask_price") or data.get("ask") or 0)
            updated = False
            if symbol not in self.options_data or self.options_data[symbol] != {"bid": bid, "ask": ask}:
                self.options_data[symbol] = {"bid": bid, "ask": ask}
                updated = True
            return updated
        except:
            return False

    def extract_strike(self, symbol):
        parts = symbol.split("-")
        for p in parts:
            if p.isdigit():
                return int(p)
        return 0

    def can_alert(self, key):
        now = time.time()
        last = self.last_alert.get(key, 0)
        if now - last >= 60:
            self.last_alert[key] = now
            return True
        return False

    def send_telegram(self, message):
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"})
        except:
            pass

    def check_arbitrage(self, asset):
        """Check arbitrage for asset using only updated bid/ask"""
        strikes = {}
        for sym, data in self.options_data.items():
            if asset not in sym:
                continue
            strike = self.extract_strike(sym)
            if strike == 0:
                continue
            if strike not in strikes:
                strikes[strike] = {"call": {}, "put": {}}
            if "-C" in sym:
                strikes[strike]["call"] = data
            elif "-P" in sym:
                strikes[strike]["put"] = data

        sorted_strikes = sorted(strikes.keys())
        alerts = []
        min_diff = 2 if asset=="BTC" else 0.16

        for i in range(len(sorted_strikes)-1):
            s1, s2 = sorted_strikes[i], sorted_strikes[i+1]
            # CALL
            c1_ask = strikes[s1]["call"].get("ask",0)
            c2_bid = strikes[s2]["call"].get("bid",0)
            if c1_ask>0 and c2_bid>0 and c1_ask-c2_bid<0 and abs(c1_ask-c2_bid)>=min_diff:
                key = f"{asset}_CALL_{s1}_{s2}"
                if self.can_alert(key):
                    alerts.append(f"🔷 CALL {s1:,} Ask: {c1_ask:.2f} vs {s2:,} Bid: {c2_bid:.2f} → Profit: {abs(c1_ask-c2_bid):.2f}")
            # PUT
            p1_bid = strikes[s1]["put"].get("bid",0)
            p2_ask = strikes[s2]["put"].get("ask",0)
            if p1_bid>0 and p2_ask>0 and p2_ask-p1_bid<0 and abs(p2_ask-p1_bid)>=min_diff:
                key = f"{asset}_PUT_{s1}_{s2}"
                if self.can_alert(key):
                    alerts.append(f"🟣 PUT {s1:,} Bid: {p1_bid:.2f} vs {s2:,} Ask: {p2_ask:.2f} → Profit: {abs(p2_ask-p1_bid):.2f}")

        if alerts:
            msg = f"🚨 *{asset} ARBITRAGE ALERTS* 🚨\n\n" + "\n".join(alerts)
            msg += f"\n\n_Time: {datetime.now().strftime('%H:%M:%S')}_"
            msg += f"\n_Expiry: {self.current_expiry}_"
            msg += f"\n_Monitoring: {len(self.symbols)} symbols_"
            self.send_telegram(msg)
            print(f"✅ Sent {len(alerts)} {asset} arbitrage alerts")

    def poll_loop(self):
        """Poll prices every second, check arbitrage only on updated symbols"""
        while True:
            try:
                updated_btc = any(self.fetch_option_price(sym) for sym in self.symbols if "BTC" in sym)
                updated_eth = any(self.fetch_option_price(sym) for sym in self.symbols if "ETH" in sym)
                if updated_btc:
                    self.check_arbitrage("BTC")
                if updated_eth:
                    self.check_arbitrage("ETH")
                time.sleep(1)
            except Exception as e:
                print(f"❌ Polling error: {e}")
                time.sleep(1)

    def start(self):
        thread = threading.Thread(target=self.poll_loop, daemon=True)
        thread.start()

# Initialize bot
bot = ArbitrageBot()
bot.start()

@app.route("/")
def home():
    return f"""
    <h1>✅ Delta Arbitrage Bot</h1>
    <p>Status: Running</p>
    <p>Monitoring: {len(bot.symbols)} symbols</p>
    <p>Expiry: {bot.current_expiry}</p>
    """

@app.route("/health")
def health():
    return f"🟢 Healthy - Monitoring {len(bot.symbols)} symbols"

if __name__=="__main__":
    app.run(host="0.0.0.0", port=10000)
