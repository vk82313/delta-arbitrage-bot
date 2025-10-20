import websocket
import json
import requests
import os
from datetime import datetime, timedelta, timezone
from time import sleep
from flask import Flask
import threading

# Flask app
app = Flask(__name__)

# -------------------------------
# Configuration
# -------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

DELTA_THRESHOLD = {
    "ETH": 0.16,
    "BTC": 2
}

ALERT_COOLDOWN = 60
PROCESS_INTERVAL = 2
EXPIRY_CHECK_INTERVAL = 60

# -------------------------------
# Common Bot Class
# -------------------------------
class DeltaOptionsBot:
    def __init__(self, asset):
        self.asset = asset
        self.websocket_url = "wss://socket.india.delta.exchange"
        self.ws = None
        self.connected = False
        self.should_reconnect = True
        self.last_alert_time = {}
        self.options_prices = {}
        self.active_symbols = []
        self.current_expiry = self.get_current_expiry()
        self.active_expiry = self.get_initial_active_expiry()
        self.last_arbitrage_check = 0
        self.last_expiry_check = 0
        self.message_count = 0
        self.expiry_rollover_count = 0

    # ---------------------------
    # Expiry Helpers
    # ---------------------------
    def get_current_expiry(self):
        now = datetime.now(timezone.utc)
        ist_now = now + timedelta(hours=5, minutes=30)
        return ist_now.strftime("%d%m%y")

    def get_initial_active_expiry(self):
        now = datetime.now(timezone.utc)
        ist_now = now + timedelta(hours=5, minutes=30)
        if ist_now.hour >= 17 and ist_now.minute >= 30:
            next_day = ist_now + timedelta(days=1)
            return next_day.strftime("%d%m%y")
        else:
            return self.get_current_expiry()

    def should_rollover_expiry(self):
        now = datetime.now(timezone.utc)
        ist_now = now + timedelta(hours=5, minutes=30)
        if ist_now.hour >= 17 and ist_now.minute >= 30:
            return (ist_now + timedelta(days=1)).strftime("%d%m%y")
        return None

    # ---------------------------
    # API Helpers
    # ---------------------------
    def get_available_expiries(self):
        try:
            url = "https://api.india.delta.exchange/v2/products"
            params = {'contract_types': 'call_options,put_options', 'states': 'live'}
            r = requests.get(url, params=params, timeout=10)
            expiries = set()
            if r.status_code == 200:
                for product in r.json().get('result', []):
                    symbol = product.get('symbol', '')
                    if self.asset in symbol:
                        exp = self.extract_expiry_from_symbol(symbol)
                        if exp:
                            expiries.add(exp)
            return sorted(expiries)
        except Exception as e:
            print(f"[{self.asset}] ❌ Error fetching expiries: {e}")
            return []

    def get_next_available_expiry(self, current_expiry):
        all_exp = self.get_available_expiries()
        for e in all_exp:
            if e > current_expiry:
                return e
        return all_exp[-1] if all_exp else current_expiry

    def extract_expiry_from_symbol(self, symbol):
        try:
            parts = symbol.split('-')
            if len(parts) >= 4:
                return parts[3]
        except:
            pass
        return None

    def extract_strike(self, symbol):
        try:
            parts = symbol.split('-')
            for p in parts:
                if p.isdigit() and len(p) > 2:
                    return int(p)
        except:
            pass
        return 0

    # ---------------------------
    # WebSocket Core
    # ---------------------------
    def connect(self):
        print(f"[{datetime.now()}] 🌐 Connecting {self.asset} bot...")
        self.ws = websocket.WebSocketApp(
            self.websocket_url,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        self.ws.run_forever()

    def on_open(self, ws):
        self.connected = True
        print(f"[{datetime.now()}] ✅ {self.asset} WebSocket connected")
        self.subscribe_to_options()

    def on_close(self, ws, code, msg):
        self.connected = False
        print(f"[{datetime.now()}] 🔴 {self.asset} WebSocket closed")
        if self.should_reconnect:
            sleep(10)
            self.connect()

    def on_error(self, ws, error):
        print(f"[{datetime.now()}] ❌ {self.asset} WebSocket error: {error}")

    def on_message(self, ws, msg):
        try:
            self.check_and_update_expiry()
            data = json.loads(msg)
            t = data.get('type')
            self.message_count += 1
            if t == 'l1_orderbook':
                self.process_l1(data)
        except Exception as e:
            print(f"[{self.asset}] ❌ Message error: {e}")

    # ---------------------------
    # Symbol & Subscription
    # ---------------------------
    def get_all_symbols(self):
        try:
            url = "https://api.india.delta.exchange/v2/products"
            params = {'contract_types': 'call_options,put_options', 'states': 'live'}
            r = requests.get(url, params=params, timeout=10)
            syms = []
            if r.status_code == 200:
                for p in r.json().get('result', []):
                    s = p.get('symbol', '')
                    if self.asset in s and self.active_expiry in s:
                        syms.append(s)
            print(f"[{self.asset}] ✅ Found {len(syms)} symbols for {self.active_expiry}")
            return sorted(list(set(syms)))
        except Exception as e:
            print(f"[{self.asset}] ❌ Error fetching symbols: {e}")
            return []

    def subscribe_to_options(self):
        symbols = self.get_all_symbols()
        if not symbols:
            print(f"[{self.asset}] ⚠️ No symbols found for {self.active_expiry}")
            return
        self.active_symbols = symbols
        payload = {
            "type": "subscribe",
            "payload": {"channels": [{"name": "l1_orderbook", "symbols": symbols}]}
        }
        self.ws.send(json.dumps(payload))
        self.send_telegram(f"🔗 *{self.asset} Bot Connected*\n📅 {self.active_expiry}\n📊 {len(symbols)} symbols")

    # ---------------------------
    # Expiry Update Check
    # ---------------------------
    def check_and_update_expiry(self):
        now = datetime.now().timestamp()
        if now - self.last_expiry_check >= EXPIRY_CHECK_INTERVAL:
            self.last_expiry_check = now
            nxt = self.should_rollover_expiry()
            if nxt and nxt != self.active_expiry:
                self.active_expiry = self.get_next_available_expiry(self.active_expiry)
                self.options_prices = {}
                self.active_symbols = []
                if self.connected:
                    self.subscribe_to_options()
                self.send_telegram(f"🔄 *{self.asset} Expiry Rollover*\n📅 Now: {self.active_expiry}")
                self.expiry_rollover_count += 1

    # ---------------------------
    # Orderbook & Arbitrage
    # ---------------------------
    def process_l1(self, msg):
        sym = msg.get('symbol')
        bid = msg.get('best_bid')
        ask = msg.get('best_ask')
        if not sym or bid is None or ask is None:
            return
        if self.asset not in sym or self.active_expiry not in sym:
            return
        try:
            b, a = float(bid), float(ask)
            # Filter invalid spikes
            if b <= 0 or a <= 0 or a > 200000 or b > 200000:
                return
            self.options_prices[sym] = {'bid': b, 'ask': a}
            cur = datetime.now().timestamp()
            if cur - self.last_arbitrage_check >= PROCESS_INTERVAL:
                self.check_arbitrage()
                self.last_arbitrage_check = cur
        except:
            pass

    def check_arbitrage(self):
        if len(self.options_prices) < 10:
            return
        opts = []
        for s, p in self.options_prices.items():
            opts.append({'symbol': s, 'bid': p['bid'], 'ask': p['ask']})
        self.check_same_expiry(opts)

    def check_same_expiry(self, options):
        strikes = {}
        for o in options:
            strike = self.extract_strike(o['symbol'])
            if strike <= 0:
                continue
            if strike not in strikes:
                strikes[strike] = {'call': {}, 'put': {}}
            if o['symbol'].endswith('-C'):
                strikes[strike]['call'] = {'bid': o['bid'], 'ask': o['ask'], 'symbol': o['symbol']}
            elif o['symbol'].endswith('-P'):
                strikes[strike]['put'] = {'bid': o['bid'], 'ask': o['ask'], 'symbol': o['symbol']}

        keys = sorted(strikes.keys())
        if len(keys) < 2:
            return
        alerts = []
        thr = DELTA_THRESHOLD[self.asset]

        for i in range(len(keys)-1):
            s1, s2 = keys[i], keys[i+1]
            # CALL arb
            c1_ask = strikes[s1]['call'].get('ask', 0)
            c2_bid = strikes[s2]['call'].get('bid', 0)
            if c1_ask > 0 and c2_bid > 0:
                diff = c1_ask - c2_bid
                if diff < 0 and abs(diff) >= thr:
                    k = f"{self.asset}_CALL_{s1}_{s2}_{self.active_expiry}"
                    if self.can_alert(k):
                        alerts.append(f"🔷 {self.asset} CALL {s1:,} Ask: ${c1_ask:.2f} vs {s2:,} Bid: ${c2_bid:.2f} → Profit: ${abs(diff):.2f}")
            # PUT arb
            p1_bid = strikes[s1]['put'].get('bid', 0)
            p2_ask = strikes[s2]['put'].get('ask', 0)
            if p1_bid > 0 and p2_ask > 0:
                diff = p2_ask - p1_bid
                if diff < 0 and abs(diff) >= thr:
                    k = f"{self.asset}_PUT_{s1}_{s2}_{self.active_expiry}"
                    if self.can_alert(k):
                        alerts.append(f"🟣 {self.asset} PUT {s1:,} Bid: ${p1_bid:.2f} vs {s2:,} Ask: ${p2_ask:.2f} → Profit: ${abs(diff):.2f}")

        if alerts:
            t = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
            tm = t.strftime("%H:%M:%S IST")
            msg = f"🚨 *{self.asset} {self.active_expiry} ARBITRAGE ALERTS* 🚨\n\n" + "\n".join(alerts)
            msg += f"\n\n_Time: {tm}_"
            self.send_telegram(msg)

    # ---------------------------
    # Utilities
    # ---------------------------
    def can_alert(self, key):
        now = datetime.now().timestamp()
        if now - self.last_alert_time.get(key, 0) >= ALERT_COOLDOWN:
            self.last_alert_time[key] = now
            return True
        return False

    def send_telegram(self, msg):
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            return
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"})
            if r.status_code != 200:
                print(f"[{self.asset}] ❌ Telegram {r.status_code}")
        except Exception as e:
            print(f"[{self.asset}] ❌ Telegram err: {e}")

    def start(self):
        def run():
            while self.should_reconnect:
                try:
                    self.connect()
                except Exception as e:
                    print(f"[{self.asset}] ❌ Reconnect error: {e}")
                    sleep(10)
        th = threading.Thread(target=run, daemon=True)
        th.start()
        print(f"[{datetime.now()}] ✅ {self.asset} bot thread started")

# -------------------------------
# Instantiate Both Bots
# -------------------------------
eth_bot = DeltaOptionsBot("ETH")
btc_bot = DeltaOptionsBot("BTC")

# -------------------------------
# Flask Routes
# -------------------------------
@app.route('/')
def home():
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    t = ist_now.strftime("%Y-%m-%d %H:%M:%S IST")
    return f"""
    <h1>Delta ETH + BTC Options Arbitrage Bot</h1>
    <p>Time: {t}</p>
    <h3>ETH</h3>
    <p>Status: {'✅' if eth_bot.connected else '🔴'}</p>
    <p>Symbols: {len(eth_bot.options_prices)}</p>
    <p>Expiry: {eth_bot.active_expiry}</p>
    <h3>BTC</h3>
    <p>Status: {'✅' if btc_bot.connected else '🔴'}</p>
    <p>Symbols: {len(btc_bot.options_prices)}</p>
    <p>Expiry: {btc_bot.active_expiry}</p>
    """

@app.route('/debug')
def debug():
    return {
        "ETH": {
            "connected": eth_bot.connected,
            "symbols": len(eth_bot.options_prices),
            "expiry": eth_bot.active_expiry
        },
        "BTC": {
            "connected": btc_bot.connected,
            "symbols": len(btc_bot.options_prices),
            "expiry": btc_bot.active_expiry
        }
    }

@app.route('/ping')
def ping():
    return "pong", 200

# -------------------------------
# Start Both Bots
# -------------------------------
def start_bots():
    print("="*60)
    print("Delta Arbitrage Bot - ETH + BTC")
    print("="*60)
    eth_bot.start()
    btc_bot.start()

if __name__ == "__main__":
    start_bots()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
