import websocket
import json
import requests
import os
from datetime import datetime, timedelta, timezone
from time import sleep
from flask import Flask
import threading

# -------------------------------
# Configuration
# -------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Thresholds per asset
DELTA_THRESHOLD = {"ETH": 0.16, "BTC": 2}  # BTC uses 2 as requested
ALERT_COOLDOWN = 60
PROCESS_INTERVAL = 2
EXPIRY_CHECK_INTERVAL = 60  # check every minute

# Websocket endpoint (same for both)
WEBSOCKET_URL = "wss://socket.india.delta.exchange"

# -------------------------------
# Generic Options Bot (ETH or BTC)
# -------------------------------
class OptionsBot:
    def __init__(self, asset):
        self.asset = asset  # 'ETH' or 'BTC'
        self.websocket_url = WEBSOCKET_URL
        self.ws = None
        self.last_alert_time = {}
        self.options_prices = {}
        self.connected = False
        self.current_expiry = self.get_current_expiry()
        self.active_expiry = self.get_initial_active_expiry()
        self.active_symbols = []
        self.should_reconnect = True
        self.last_arbitrage_check = 0
        self.last_expiry_check = 0
        self.message_count = 0
        self.expiry_rollover_count = 0

    def get_current_expiry(self):
        now = datetime.now(timezone.utc)
        ist_now = now + timedelta(hours=5, minutes=30)
        return ist_now.strftime("%d%m%y")

    def get_initial_active_expiry(self):
        now = datetime.now(timezone.utc)
        ist_now = now + timedelta(hours=5, minutes=30)
        # After 17:30 IST, use next day's expiry
        if ist_now.hour > 17 or (ist_now.hour == 17 and ist_now.minute >= 30):
            next_day = ist_now + timedelta(days=1)
            next_expiry = next_day.strftime("%d%m%y")
            print(f"[{datetime.now()}] 🕠 After 5:30 PM IST, starting {self.asset} with expiry: {next_expiry}")
            return next_expiry
        else:
            print(f"[{datetime.now()}] 📅 Starting {self.asset} with today's expiry: {self.current_expiry}")
            return self.current_expiry

    def should_rollover_expiry(self):
        now = datetime.now(timezone.utc)
        ist_now = now + timedelta(hours=5, minutes=30)
        if ist_now.hour > 17 or (ist_now.hour == 17 and ist_now.minute >= 30):
            next_expiry = (ist_now + timedelta(days=1)).strftime("%d%m%y")
            return next_expiry
        return None

    def get_available_expiries(self):
        try:
            url = "https://api.india.delta.exchange/v2/products"
            params = {
                'contract_types': 'call_options,put_options',
                'states': 'live'
            }
            response = requests.get(url, params=params, timeout=10)
            if response.status_code == 200:
                products = response.json().get('result', [])
                expiries = set()
                for product in products:
                    symbol = product.get('symbol', '')
                    if self.asset in symbol:
                        expiry = self.extract_expiry_from_symbol(symbol)
                        if expiry:
                            expiries.add(expiry)
                return sorted(expiries)
            return []
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Error fetching {self.asset} expiries: {e}")
            return []

    def get_next_available_expiry(self, current_expiry):
        available_expiries = self.get_available_expiries()
        if not available_expiries:
            return current_expiry
        print(f"[{datetime.now()}] 📊 Available {self.asset} expiries: {available_expiries}")
        for expiry in available_expiries:
            if expiry > current_expiry:
                return expiry
        return available_expiries[-1]

    def check_and_update_expiry(self):
        current_time = datetime.now().timestamp()
        if current_time - self.last_expiry_check >= EXPIRY_CHECK_INTERVAL:
            self.last_expiry_check = current_time
            now = datetime.now(timezone.utc)
            ist_now = now + timedelta(hours=5, minutes=30)
            current_time_ist = ist_now.strftime("%H:%M:%S")
            print(f"[{datetime.now()}] 🔄 Checking {self.asset} expiry rollover... (Current: {self.active_expiry}, Time: {current_time_ist} IST)")

            # Rollover by time
            next_expiry = self.should_rollover_expiry()
            if next_expiry and next_expiry != self.active_expiry:
                print(f"[{datetime.now()}] 🎯 {self.asset} EXPIRY ROLLOVER TRIGGERED!")
                print(f"[{datetime.now()}] 📅 Changing from {self.active_expiry} to {next_expiry}")

                # Get actual next available expiry from API
                actual_next_expiry = self.get_next_available_expiry(self.active_expiry)

                if actual_next_expiry != self.active_expiry:
                    self.active_expiry = actual_next_expiry
                    self.expiry_rollover_count += 1

                    # Reset data for new expiry
                    self.options_prices = {}
                    self.active_symbols = []

                    if self.connected and self.ws:
                        self.subscribe_to_options()

                    self.send_telegram(f"🔄 *{self.asset} Expiry Rollover Complete!*\n\n📅 Now monitoring: {self.active_expiry}\n⏰ Time: {current_time_ist} IST\n\nBot automatically switched to new expiry! ✅")
                    return True
                else:
                    print(f"[{datetime.now()}] ⚠️ No new {self.asset} expiry available yet, keeping: {self.active_expiry}")

            # If current expiry no longer available
            available_expiries = self.get_available_expiries()
            if available_expiries and self.active_expiry not in available_expiries:
                print(f"[{datetime.now()}] ⚠️ Current {self.asset} expiry {self.active_expiry} no longer available!")
                next_available = self.get_next_available_expiry(self.active_expiry)
                if next_available != self.active_expiry:
                    print(f"[{datetime.now()}] 🔄 Switching to available {self.asset} expiry: {next_available}")
                    self.active_expiry = next_available
                    self.expiry_rollover_count += 1

                    self.options_prices = {}
                    self.active_symbols = []

                    if self.connected and self.ws:
                        self.subscribe_to_options()

                    self.send_telegram(f"🔄 *{self.asset} Expiry Update*\n\n📅 Now monitoring: {self.active_expiry}\n⏰ Time: {current_time_ist} IST\n\nPrevious expiry no longer available! ✅")
                    return True

        return False

    def extract_expiry_from_symbol(self, symbol):
        try:
            parts = symbol.split('-')
            if len(parts) >= 4:
                return parts[3]
            return None
        except:
            return None

    def extract_strike(self, symbol):
        try:
            parts = symbol.split('-')
            for part in parts:
                # keep logic similar to original (digits and length > 2)
                if part.isdigit() and len(part) > 2:
                    return int(part)
            return 0
        except:
            return 0

    def get_all_options_symbols(self):
        try:
            print(f"[{datetime.now()}] 🔍 Fetching {self.asset} {self.active_expiry} expiry options symbols...")
            url = "https://api.india.delta.exchange/v2/products"
            params = {
                'contract_types': 'call_options,put_options',
                'states': 'live'
            }
            response = requests.get(url, params=params, timeout=10)
            if response.status_code == 200:
                products = response.json().get('result', [])
                symbols = []
                for product in products:
                    symbol = product.get('symbol', '')
                    contract_type = product.get('contract_type', '')
                    is_option = contract_type in ['call_options', 'put_options']
                    is_asset = self.asset in symbol
                    is_active_expiry = self.active_expiry in symbol
                    if is_option and is_asset and is_active_expiry:
                        symbols.append(symbol)
                symbols = sorted(list(set(symbols)))
                print(f"[{datetime.now()}] ✅ Found {len(symbols)} {self.asset} {self.active_expiry} expiry options symbols")
                if not symbols:
                    available_expiries = self.get_available_expiries()
                    print(f"[{datetime.now()}] ⚠️ No {self.asset} symbols found for {self.active_expiry}")
                    print(f"[{datetime.now()}] 📅 Available {self.asset} expiries: {available_expiries}")
                    if available_expiries:
                        next_expiry = self.get_next_available_expiry(self.active_expiry)
                        if next_expiry != self.active_expiry:
                            print(f"[{datetime.now()}] 🔄 Auto-switching to available {self.asset} expiry: {next_expiry}")
                            self.active_expiry = next_expiry
                            return self.get_all_options_symbols()
                return symbols
            else:
                print(f"[{datetime.now()}] ❌ API Error: {response.status_code}")
                return []
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Error fetching {self.asset} symbols: {e}")
            return []

    # ---------------------------
    # WebSocket Callbacks
    # ---------------------------
    def on_open(self, ws):
        self.connected = True
        print(f"[{datetime.now()}] ✅ Connected to Delta WebSocket for {self.asset}")
        print(f"[{datetime.now()}] 📅 Active {self.asset} expiry: {self.active_expiry}")
        self.subscribe_to_options()

    def on_close(self, ws, close_status_code, close_msg):
        self.connected = False
        print(f"[{datetime.now()}] 🔴 {self.asset} WebSocket closed")
        if self.should_reconnect:
            print(f"[{datetime.now()}] 🔄 {self.asset} Reconnecting in 10 seconds...")
            sleep(10)
            self.connect()

    def on_error(self, ws, error):
        print(f"[{datetime.now()}] ❌ {self.asset} WebSocket error: {error}")

    def on_message(self, ws, message):
        try:
            # Always check expiry rollover on messages
            self.check_and_update_expiry()

            message_json = json.loads(message)
            message_type = message_json.get('type')

            self.message_count += 1
            if self.message_count <= 3 or self.message_count % 50 == 0:
                print(f"[{datetime.now()}] 📨 {self.asset} Message {self.message_count}: type={message_type}")

            if message_type == 'l1_orderbook':
                self.process_l1_orderbook_data(message_json)
            elif message_type == 'subscriptions':
                print(f"[{datetime.now()}] ✅ {self.asset} Subscriptions confirmed for {self.active_expiry}")
            elif message_type == 'success':
                print(f"[{datetime.now()}] ✅ {message_json.get('message', 'Success')}")
            elif message_type == 'error':
                print(f"[{datetime.now()}] ❌ Error: {message_json}")

        except Exception as e:
            print(f"[{datetime.now()}] ❌ {self.asset} Message processing error: {e}")

    def process_l1_orderbook_data(self, message):
        try:
            symbol = message.get('symbol')
            best_bid = message.get('best_bid')
            best_ask = message.get('best_ask')

            if symbol and best_bid is not None and best_ask is not None:
                # Only process symbols matching this asset and active expiry
                if self.asset not in symbol:
                    return
                symbol_expiry = self.extract_expiry_from_symbol(symbol)
                if symbol_expiry != self.active_expiry:
                    return

                best_bid_price = float(best_bid) if best_bid else 0
                best_ask_price = float(best_ask) if best_ask else 0

                if best_bid_price > 0 and best_ask_price > 0:
                    self.options_prices[symbol] = {
                        'bid': best_bid_price,
                        'ask': best_ask_price
                    }

                    if len(self.options_prices) % 25 == 0:
                        print(f"[{datetime.now()}] 💰 Tracking {len(self.options_prices)} {self.asset} {self.active_expiry} symbols")

                    current_time = datetime.now().timestamp()
                    if current_time - self.last_arbitrage_check >= PROCESS_INTERVAL:
                        self.check_arbitrage_opportunities()
                        self.last_arbitrage_check = current_time

        except Exception as e:
            print(f"[{datetime.now()}] ❌ Error processing {self.asset} l1_orderbook data: {e}")

    def check_arbitrage_opportunities(self):
        if len(self.options_prices) < 10:
            return

        asset_options = []
        for symbol, prices in self.options_prices.items():
            if self.asset in symbol:
                asset_options.append({
                    'symbol': symbol,
                    'bid': prices['bid'],
                    'ask': prices['ask']
                })

        if asset_options:
            self.check_arbitrage_same_expiry(self.asset, asset_options)

    def check_arbitrage_same_expiry(self, asset, options):
        strikes = {}
        for option in options:
            strike = self.extract_strike(option['symbol'])
            if strike > 0:
                if strike not in strikes:
                    strikes[strike] = {'call': {}, 'put': {}}

                if 'C-' in option['symbol']:
                    strikes[strike]['call'] = {
                        'bid': option['bid'],
                        'ask': option['ask'],
                        'symbol': option['symbol']
                    }
                elif 'P-' in option['symbol']:
                    strikes[strike]['put'] = {
                        'bid': option['bid'],
                        'ask': option['ask'],
                        'symbol': option['symbol']
                    }

        sorted_strikes = sorted(strikes.keys())
        if len(sorted_strikes) < 2:
            return

        alerts = []
        for i in range(len(sorted_strikes) - 1):
            strike1 = sorted_strikes[i]
            strike2 = sorted_strikes[i + 1]

            # CALL arbitrage: ask of lower strike vs bid of higher strike
            call1_ask = strikes[strike1]['call'].get('ask', 0)
            call2_bid = strikes[strike2]['call'].get('bid', 0)
            if call1_ask > 0 and call2_bid > 0:
                call_diff = call1_ask - call2_bid
                if call_diff < 0 and abs(call_diff) >= DELTA_THRESHOLD.get(asset, 0):
                    alert_key = f"{asset}_CALL_{strike1}_{strike2}_{self.active_expiry}"
                    if self.can_alert(alert_key):
                        profit = abs(call_diff)
                        alerts.append(f"🔷 {asset} CALL {strike1:,} Ask: ${call1_ask:.2f} vs {strike2:,} Bid: ${call2_bid:.2f} → Profit: ${profit:.2f}")

            # PUT arbitrage: bid of lower strike vs ask of higher strike (mirrored logic)
            put1_bid = strikes[strike1]['put'].get('bid', 0)
            put2_ask = strikes[strike2]['put'].get('ask', 0)
            if put1_bid > 0 and put2_ask > 0:
                put_diff = put2_ask - put1_bid
                if put_diff < 0 and abs(put_diff) >= DELTA_THRESHOLD.get(asset, 0):
                    alert_key = f"{asset}_PUT_{strike1}_{strike2}_{self.active_expiry}"
                    if self.can_alert(alert_key):
                        profit = abs(put_diff)
                        alerts.append(f"🟣 {asset} PUT {strike1:,} Bid: ${put1_bid:.2f} vs {strike2:,} Ask: ${put2_ask:.2f} → Profit: ${profit:.2f}")

        if alerts:
            ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
            current_time_ist = ist_now.strftime("%H:%M:%S")
            message = f"🚨 *{asset} {self.active_expiry} ARBITRAGE ALERTS* 🚨\n\n" + "\n".join(alerts)
            message += f"\n\n_Expiry: {self.active_expiry}_"
            message += f"\n_Time: {current_time_ist} IST_"
            self.send_telegram(message)
            print(f"[{datetime.now()}] ✅ Sent {len(alerts)} {asset} arbitrage alerts for {self.active_expiry}")

    def subscribe_to_options(self):
        symbols = self.get_all_options_symbols()
        if not symbols:
            print(f"[{datetime.now()}] ⚠️ No {self.asset} {self.active_expiry} expiry options symbols found")
            return
        self.active_symbols = symbols
        if symbols:
            payload = {
                "type": "subscribe",
                "payload": {
                    "channels": [
                        {
                            "name": "l1_orderbook",
                            "symbols": symbols
                        }
                    ]
                }
            }
            try:
                self.ws.send(json.dumps(payload))
                print(f"[{datetime.now()}] 📡 Subscribed to {len(symbols)} {self.asset} {self.active_expiry} expiry symbols")
                now = datetime.now(timezone.utc)
                ist_now = now + timedelta(hours=5, minutes=30)
                current_time_ist = ist_now.strftime("%H:%M:%S IST")
                self.send_telegram(f"🔗 *{self.asset} Bot Connected*\n\n📅 Monitoring: {self.active_expiry}\n📊 {self.asset} Symbols: {len(symbols)}\n⏰ Time: {current_time_ist}\n\n{self.asset} Bot is now live! 🚀")
            except Exception as e:
                print(f"[{datetime.now()}] ❌ Failed to subscribe {self.asset}: {e}")

    def can_alert(self, alert_key):
        now = datetime.now().timestamp()
        last_time = self.last_alert_time.get(alert_key, 0)
        if now - last_time >= ALERT_COOLDOWN:
            self.last_alert_time[alert_key] = now
            return True
        return False

    def send_telegram(self, message):
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            print(f"[{datetime.now()}] ⚠️ Telegram credentials missing - cannot send message for {self.asset}")
            return
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            resp = requests.post(url, data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown"
            })
            if resp.status_code == 200:
                print(f"[{datetime.now()}] 📱 Telegram alert sent for {self.asset}")
            else:
                print(f"[{datetime.now()}] ❌ Telegram error {resp.status_code} for {self.asset}")
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Telegram error for {self.asset}: {e}")

    def connect(self):
        print(f"[{datetime.now()}] 🌐 Connecting to Delta WebSocket for {self.asset}...")
        self.ws = websocket.WebSocketApp(
            self.websocket_url,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        self.ws.run_forever()

    def start(self):
        def run_bot():
            while self.should_reconnect:
                try:
                    self.connect()
                except Exception as e:
                    print(f"[{datetime.now()}] ❌ {self.asset} Bot connection error: {e}")
                    sleep(10)
        bot_thread = threading.Thread(target=run_bot)
        bot_thread.daemon = True
        bot_thread.start()
        print(f"[{datetime.now()}] ✅ {self.asset} Bot thread started")


# -------------------------------
# Flask App & Instances
# -------------------------------
app = Flask(__name__)

# Create ETH and BTC bot instances
eth_bot = OptionsBot("ETH")
btc_bot = OptionsBot("BTC")

@app.route('/')
def home():
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    current_time_ist = ist_now.strftime("%Y-%m-%d %H:%M:%S IST")
    status_eth = "✅ Connected" if eth_bot.connected else "🔴 Disconnected"
    status_btc = "✅ Connected" if btc_bot.connected else "🔴 Disconnected"
    return f"""
    <h1>Delta Options Arbitrage Bot - ETH + BTC</h1>
    <h2>Overview</h2>
    <p>Time (IST): {current_time_ist}</p>
    <h3>ETH</h3>
    <p>Status: {status_eth}</p>
    <p>Messages Received: {eth_bot.message_count}</p>
    <p>Current ETH Prices: {len(eth_bot.options_prices)} symbols</p>
    <p>Active ETH Symbols: {len(eth_bot.active_symbols)}</p>
    <p>Active ETH Expiry: {eth_bot.active_expiry}</p>
    <p>ETH Expiry Rollovers: {eth_bot.expiry_rollover_count}</p>
    <h3>BTC</h3>
    <p>Status: {status_btc}</p>
    <p>Messages Received: {btc_bot.message_count}</p>
    <p>Current BTC Prices: {len(btc_bot.options_prices)} symbols</p>
    <p>Active BTC Symbols: {len(btc_bot.active_symbols)}</p>
    <p>Active BTC Expiry: {btc_bot.active_expiry}</p>
    <p>BTC Expiry Rollovers: {btc_bot.expiry_rollover_count}</p>
    <p><a href="/debug">Debug Info</a> | <a href="/health">Health</a></p>
    """

@app.route('/health')
def health():
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    current_time_ist = ist_now.strftime("%Y-%m-%d %H:%M:%S IST")
    return {
        "status": "healthy",
        "time_ist": current_time_ist,
        "eth": {
            "bot_connected": eth_bot.connected,
            "messages_received": eth_bot.message_count,
            "symbols_tracked": len(eth_bot.options_prices),
            "active_symbols": len(eth_bot.active_symbols),
            "active_expiry": eth_bot.active_expiry,
            "expiry_rollovers": eth_bot.expiry_rollover_count
        },
        "btc": {
            "bot_connected": btc_bot.connected,
            "messages_received": btc_bot.message_count,
            "symbols_tracked": len(btc_bot.options_prices),
            "active_symbols": len(btc_bot.active_symbols),
            "active_expiry": btc_bot.active_expiry,
            "expiry_rollovers": btc_bot.expiry_rollover_count
        }
    }, 200

@app.route('/debug')
def debug():
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    current_time_ist = ist_now.strftime("%Y-%m-%d %H:%M:%S IST")
    sample_eth = dict(list(eth_bot.options_prices.items())[:3])
    sample_btc = dict(list(btc_bot.options_prices.items())[:3])
    available_eth_expiries = eth_bot.get_available_expiries()
    available_btc_expiries = btc_bot.get_available_expiries()
    return {
        "time_ist": current_time_ist,
        "eth": {
            "connected": eth_bot.connected,
            "messages_received": eth_bot.message_count,
            "symbols_tracked": len(eth_bot.options_prices),
            "active_symbols_count": len(eth_bot.active_symbols),
            "active_expiry": eth_bot.active_expiry,
            "available_expiries": available_eth_expiries,
            "expiry_rollovers": eth_bot.expiry_rollover_count,
            "sample_prices": sample_eth
        },
        "btc": {
            "connected": btc_bot.connected,
            "messages_received": btc_bot.message_count,
            "symbols_tracked": len(btc_bot.options_prices),
            "active_symbols_count": len(btc_bot.active_symbols),
            "active_expiry": btc_bot.active_expiry,
            "available_expiries": available_btc_expiries,
            "expiry_rollovers": btc_bot.expiry_rollover_count,
            "sample_prices": sample_btc
        }
    }

@app.route('/ping')
def ping():
    return "pong", 200

# -------------------------------
# Start both bots
# -------------------------------
def start_bots():
    print(f"[{datetime.now()}] 🤖 Starting ETH and BTC Options Arbitrage Bots...")
    eth_thread = threading.Thread(target=eth_bot.start)
    btc_thread = threading.Thread(target=btc_bot.start)
    eth_thread.daemon = True
    btc_thread.daemon = True
    eth_thread.start()
    btc_thread.start()
    print(f"[{datetime.now()}] ✅ ETH and BTC Bot threads started")

if __name__ == "__main__":
    print("="*60)
    print("Delta Options Arbitrage Bot - ETH + BTC (same Telegram chat)")
    print("="*60)
    start_bots()
    sleep(2)
    port = int(os.environ.get("PORT", 10000))
    print(f"[{datetime.now()}] 🚀 Starting web server on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
