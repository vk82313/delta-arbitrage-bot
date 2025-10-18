import websocket
import json
import requests
import os
from datetime import datetime, timedelta, timezone
from time import sleep
from flask import Flask
import threading


# Initialize Flask app
app = Flask(__name__)

# -------------------------------
# Configuration
# -------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DELTA_THRESHOLD = {"BTC": 2, "ETH": 0.16}
ALERT_COOLDOWN = 60
PROCESS_INTERVAL = 2
EXPIRY_CHECK_INTERVAL = 60  # Check every 1 minute for expiry rollover

# -------------------------------
# Delta WebSocket Client - COMPLETE WITH ALL METHODS
# -------------------------------
class DeltaOptionsBot:
    def __init__(self):
        self.websocket_url = "wss://socket.india.delta.exchange"
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
        """Get current date in DDMMYY format"""
        now = datetime.now(timezone.utc)
        ist_now = now + timedelta(hours=5, minutes=30)
        return ist_now.strftime("%d%m%y")

    def get_initial_active_expiry(self):
        """Determine which expiry should be active right now"""
        now = datetime.now(timezone.utc)
        ist_now = now + timedelta(hours=5, minutes=30)
        
        # If it's after 5:30 PM IST, we should already be on next day's expiry
        if ist_now.hour >= 17 and ist_now.minute >= 30:
            next_day = ist_now + timedelta(days=1)
            next_expiry = next_day.strftime("%d%m%y")
            print(f"[{datetime.now()}] 🕠 After 5:30 PM IST, starting with next expiry: {next_expiry}")
            return next_expiry
        else:
            print(f"[{datetime.now()}] 📅 Starting with today's expiry: {self.current_expiry}")
            return self.current_expiry

    def should_rollover_expiry(self):
        """Check if we should move to next expiry"""
        now = datetime.now(timezone.utc)
        ist_now = now + timedelta(hours=5, minutes=30)
        
        # After 5:30 PM IST, move to next day's expiry
        if ist_now.hour >= 17 and ist_now.minute >= 30:
            next_expiry = (ist_now + timedelta(days=1)).strftime("%d%m%y")
            return next_expiry
        return None

    def get_available_expiries(self):
        """Get all available expiries from the API"""
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
                    if any(asset in symbol for asset in ['BTC', 'ETH']):
                        expiry = self.extract_expiry_from_symbol(symbol)
                        if expiry:
                            expiries.add(expiry)
                
                return sorted(expiries)
            return []
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Error fetching expiries: {e}")
            return []

    def get_next_available_expiry(self, current_expiry):
        """Get the next available expiry after current one"""
        available_expiries = self.get_available_expiries()
        if not available_expiries:
            return current_expiry
        
        print(f"[{datetime.now()}] 📊 Available expiries: {available_expiries}")
        
        # Find the first expiry that is > current expiry
        for expiry in available_expiries:
            if expiry > current_expiry:
                return expiry
        
        # If no future expiry found, return the last available one
        return available_expiries[-1]

    def check_and_update_expiry(self):
        """Check if we need to update the active expiry"""
        current_time = datetime.now().timestamp()
        if current_time - self.last_expiry_check >= EXPIRY_CHECK_INTERVAL:
            self.last_expiry_check = current_time
            
            # Get current time in IST
            now = datetime.now(timezone.utc)
            ist_now = now + timedelta(hours=5, minutes=30)
            current_time_ist = ist_now.strftime("%H:%M:%S")
            
            print(f"[{datetime.now()}] 🔄 Checking expiry rollover... (Current: {self.active_expiry}, Time: {current_time_ist} IST)")
            
            # Check if we should rollover to next expiry
            next_expiry = self.should_rollover_expiry()
            if next_expiry and next_expiry != self.active_expiry:
                print(f"[{datetime.now()}] 🎯 EXPIRY ROLLOVER TRIGGERED!")
                print(f"[{datetime.now()}] 📅 Changing from {self.active_expiry} to {next_expiry}")
                
                # Get the actual next available expiry from API
                actual_next_expiry = self.get_next_available_expiry(self.active_expiry)
                
                if actual_next_expiry != self.active_expiry:
                    self.active_expiry = actual_next_expiry
                    self.expiry_rollover_count += 1
                    
                    # Reset data for new expiry
                    self.options_prices = {}
                    self.active_symbols = []
                    
                    # Resubscribe with new expiry
                    if self.connected and self.ws:
                        self.subscribe_to_options()
                    
                    # Send Telegram notification
                    self.send_telegram(f"🔄 *Expiry Rollover Complete!*\n\n📅 Now monitoring: {self.active_expiry}\n⏰ Time: {current_time_ist} IST\n\nBot automatically switched to new expiry! ✅")
                    return True
                else:
                    print(f"[{datetime.now()}] ⚠️ No new expiry available yet, keeping: {self.active_expiry}")
            
            # Also check if current expiry is still available
            available_expiries = self.get_available_expiries()
            if available_expiries and self.active_expiry not in available_expiries:
                print(f"[{datetime.now()}] ⚠️ Current expiry {self.active_expiry} no longer available!")
                next_available = self.get_next_available_expiry(self.active_expiry)
                if next_available != self.active_expiry:
                    print(f"[{datetime.now()}] 🔄 Switching to available expiry: {next_available}")
                    self.active_expiry = next_available
                    self.expiry_rollover_count += 1
                    
                    # Reset and resubscribe
                    self.options_prices = {}
                    self.active_symbols = []
                    
                    if self.connected and self.ws:
                        self.subscribe_to_options()
                    
                    self.send_telegram(f"🔄 *Expiry Update*\n\n📅 Now monitoring: {self.active_expiry}\n⏰ Time: {current_time_ist} IST\n\nPrevious expiry no longer available! ✅")
                    return True
        
        return False

    def extract_expiry_from_symbol(self, symbol):
        """Extract expiry date from symbol string"""
        try:
            parts = symbol.split('-')
            if len(parts) >= 4:
                return parts[3]
            return None
        except:
            return None

    def extract_strike(self, symbol):
        """Extract strike price from symbol"""
        try:
            parts = symbol.split('-')
            for part in parts:
                if part.isdigit() and len(part) > 2:
                    return int(part)
            return 0
        except:
            return 0

    def get_all_options_symbols(self):
        """Fetch symbols for ACTIVE expiry only"""
        try:
            print(f"[{datetime.now()}] 🔍 Fetching {self.active_expiry} expiry options symbols...")
            
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
                    
                    # Filter for BTC/ETH options with ACTIVE expiry only
                    is_option = contract_type in ['call_options', 'put_options']
                    is_btc_eth = any(asset in symbol for asset in ['BTC', 'ETH'])
                    is_active_expiry = self.active_expiry in symbol
                    
                    if is_option and is_btc_eth and is_active_expiry:
                        symbols.append(symbol)
                
                symbols = sorted(list(set(symbols)))
                
                print(f"[{datetime.now()}] ✅ Found {len(symbols)} {self.active_expiry} expiry options symbols")
                
                if not symbols:
                    available_expiries = self.get_available_expiries()
                    print(f"[{datetime.now()}] ⚠️ No symbols found for {self.active_expiry}")
                    print(f"[{datetime.now()}] 📅 Available expiries: {available_expiries}")
                    # If no symbols for current expiry, try to find next available
                    if available_expiries:
                        next_expiry = self.get_next_available_expiry(self.active_expiry)
                        if next_expiry != self.active_expiry:
                            print(f"[{datetime.now()}] 🔄 Auto-switching to available expiry: {next_expiry}")
                            self.active_expiry = next_expiry
                            return self.get_all_options_symbols()  # Recursive call with new expiry
                
                return symbols
            else:
                print(f"[{datetime.now()}] ❌ API Error: {response.status_code}")
                return []
                
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Error fetching symbols: {e}")
            return []

    # ---------------------------
    # WebSocket Callbacks
    # ---------------------------
    def on_open(self, ws):
        self.connected = True
        print(f"[{datetime.now()}] ✅ Connected to Delta Exchange WebSocket")
        print(f"[{datetime.now()}] 📅 Active expiry: {self.active_expiry}")
        self.subscribe_to_options()

    def on_close(self, ws, close_status_code, close_msg):
        self.connected = False
        print(f"[{datetime.now()}] 🔴 WebSocket closed")
        if self.should_reconnect:
            print(f"[{datetime.now()}] 🔄 Reconnecting in 10 seconds...")
            sleep(10)
            self.connect()

    def on_error(self, ws, error):
        print(f"[{datetime.now()}] ❌ WebSocket error: {error}")

    def on_message(self, ws, message):
        """Handle incoming WebSocket messages"""
        try:
            # Check for expiry rollover first (on EVERY message)
            self.check_and_update_expiry()
            
            message_json = json.loads(message)
            message_type = message_json.get('type')
            
            self.message_count += 1
            
            if self.message_count <= 3 or self.message_count % 50 == 0:
                print(f"[{datetime.now()}] 📨 Message {self.message_count}: type={message_type}")
            
            if message_type == 'l1_orderbook':
                self.process_l1_orderbook_data(message_json)
            elif message_type == 'subscriptions':
                print(f"[{datetime.now()}] ✅ Subscriptions confirmed for {self.active_expiry}")
            elif message_type == 'success':
                print(f"[{datetime.now()}] ✅ {message_json.get('message', 'Success')}")
            elif message_type == 'error':
                print(f"[{datetime.now()}] ❌ Error: {message_json}")
                
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Message processing error: {e}")

    def process_l1_orderbook_data(self, message):
        """Process l1_orderbook data - ONLY ACTIVE EXPIRY"""
        try:
            symbol = message.get('symbol')
            best_bid = message.get('best_bid')
            best_ask = message.get('best_ask')
            
            if symbol and best_bid is not None and best_ask is not None:
                # ONLY process symbols with ACTIVE expiry
                symbol_expiry = self.extract_expiry_from_symbol(symbol)
                if symbol_expiry != self.active_expiry:
                    return  # Skip if not active expiry
                
                best_bid_price = float(best_bid) if best_bid else 0
                best_ask_price = float(best_ask) if best_ask else 0
                
                if best_bid_price > 0 and best_ask_price > 0:
                    self.options_prices[symbol] = {
                        'bid': best_bid_price,
                        'ask': best_ask_price
                    }
                    
                    if len(self.options_prices) % 25 == 0:
                        print(f"[{datetime.now()}] 💰 Tracking {len(self.options_prices)} {self.active_expiry} symbols")
                    
                    current_time = datetime.now().timestamp()
                    if current_time - self.last_arbitrage_check >= PROCESS_INTERVAL:
                        self.check_arbitrage_opportunities()
                        self.last_arbitrage_check = current_time
                    
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Error processing l1_orderbook data: {e}")

    def check_arbitrage_opportunities(self):
        """Check for arbitrage opportunities - ONLY ACTIVE EXPIRY"""
        if len(self.options_prices) < 10:
            return
            
        btc_options = []
        eth_options = []
        
        for symbol, prices in self.options_prices.items():
            option_data = {
                'symbol': symbol,
                'bid': prices['bid'],
                'ask': prices['ask']
            }
            
            if 'BTC' in symbol:
                btc_options.append(option_data)
            elif 'ETH' in symbol:
                eth_options.append(option_data)
        
        # Check arbitrage for both assets
        if btc_options:
            self.check_arbitrage_same_expiry('BTC', btc_options)
        if eth_options:
            self.check_arbitrage_same_expiry('ETH', eth_options)

    def check_arbitrage_same_expiry(self, asset, options):
        """Check for arbitrage opportunities within ACTIVE expiry"""
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
            
            # CALL arbitrage
            call1_ask = strikes[strike1]['call'].get('ask', 0)
            call2_bid = strikes[strike2]['call'].get('bid', 0)
            
            if call1_ask > 0 and call2_bid > 0:
                call_diff = call1_ask - call2_bid
                if call_diff < 0 and abs(call_diff) >= DELTA_THRESHOLD[asset]:
                    alert_key = f"{asset}_CALL_{strike1}_{strike2}_{self.active_expiry}"
                    if self.can_alert(alert_key):
                        profit = abs(call_diff)
                        alerts.append(f"🔷 {asset} CALL {strike1:,} Ask: ${call1_ask:.2f} vs {strike2:,} Bid: ${call2_bid:.2f} → Profit: ${profit:.2f}")
            
            # PUT arbitrage
            put1_bid = strikes[strike1]['put'].get('bid', 0)
            put2_ask = strikes[strike2]['put'].get('ask', 0)
            
            if put1_bid > 0 and put2_ask > 0:
                put_diff = put2_ask - put1_bid
                if put_diff < 0 and abs(put_diff) >= DELTA_THRESHOLD[asset]:
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
        """Subscribe to ACTIVE expiry options"""
        symbols = self.get_all_options_symbols()
        
        if not symbols:
            print(f"[{datetime.now()}] ⚠️ No {self.active_expiry} expiry options symbols found")
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
            
            self.ws.send(json.dumps(payload))
            print(f"[{datetime.now()}] 📡 Subscribed to {len(symbols)} {self.active_expiry} expiry symbols")
            
            # Get current IST time
            now = datetime.now(timezone.utc)
            ist_now = now + timedelta(hours=5, minutes=30)
            current_time_ist = ist_now.strftime("%H:%M:%S IST")
            
            # Send connection notification
            self.send_telegram(f"🔗 *Bot Connected*\n\n📅 Monitoring: {self.active_expiry}\n📊 Symbols: {len(symbols)}\n⏰ Time: {current_time_ist}\n\nBot is now live! 🚀")

    def can_alert(self, alert_key):
        """Check if we can send alert (cooldown)"""
        now = datetime.now().timestamp()
        last_time = self.last_alert_time.get(alert_key, 0)
        if now - last_time >= ALERT_COOLDOWN:
            self.last_alert_time[alert_key] = now
            return True
        return False

    def send_telegram(self, message):
        """Send Telegram message"""
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            return
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            resp = requests.post(url, data={
                "chat_id": TELEGRAM_CHAT_ID, 
                "text": message, 
                "parse_mode": "Markdown"
            })
            if resp.status_code == 200:
                print(f"[{datetime.now()}] 📱 Telegram alert sent")
            else:
                print(f"[{datetime.now()}] ❌ Telegram error {resp.status_code}")
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Telegram error: {e}")

    def connect(self):
        """Connect to WebSocket"""
        print(f"[{datetime.now()}] 🌐 Connecting to Delta WebSocket...")
        self.ws = websocket.WebSocketApp(
            self.websocket_url,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        self.ws.run_forever()

    def start(self):
        """Start the bot in a separate thread"""
        def run_bot():
            while self.should_reconnect:
                try:
                    self.connect()
                except Exception as e:
                    print(f"[{datetime.now()}] ❌ Bot connection error: {e}")
                    sleep(10)
        
        bot_thread = threading.Thread(target=run_bot)
        bot_thread.daemon = True
        bot_thread.start()
        print(f"[{datetime.now()}] ✅ Bot thread started")

# -------------------------------
# Flask Routes
# -------------------------------
bot = DeltaOptionsBot()

@app.route('/')
def home():
    status = "✅ Connected" if bot.connected else "🔴 Disconnected"
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    current_time_ist = ist_now.strftime("%Y-%m-%d %H:%M:%S IST")
    
    return f"""
    <h1>Delta Options Arbitrage Bot</h1>
    <p>Status: {status}</p>
    <p>Messages Received: {bot.message_count}</p>
    <p>Current Prices: {len(bot.options_prices)} symbols</p>
    <p>Active Symbols: {len(bot.active_symbols)}</p>
    <p>Active Expiry: {bot.active_expiry}</p>
    <p>Expiry Rollovers: {bot.expiry_rollover_count}</p>
    <p>Last Update: {current_time_ist}</p>
    <p><a href="/debug">Debug Info</a> | <a href="/health">Health</a></p>
    """

@app.route('/health')
def health():
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    current_time_ist = ist_now.strftime("%Y-%m-%d %H:%M:%S IST")
    
    return {
        "status": "healthy", 
        "bot_connected": bot.connected, 
        "messages_received": bot.message_count,
        "symbols_tracked": len(bot.options_prices),
        "active_symbols": len(bot.active_symbols),
        "active_expiry": bot.active_expiry,
        "expiry_rollovers": bot.expiry_rollover_count,
        "current_time_ist": current_time_ist
    }, 200

@app.route('/debug')
def debug():
    """Debug endpoint"""
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    current_time_ist = ist_now.strftime("%Y-%m-%d %H:%M:%S IST")
    
    btc_count = len([s for s in bot.options_prices.keys() if 'BTC' in s])
    eth_count = len([s for s in bot.options_prices.keys() if 'ETH' in s])
    
    sample_prices = dict(list(bot.options_prices.items())[:3])
    available_expiries = bot.get_available_expiries()
    
    return {
        "connected": bot.connected,
        "messages_received": bot.message_count,
        "symbols_tracked": len(bot.options_prices),
        "btc_symbols": btc_count,
        "eth_symbols": eth_count,
        "active_symbols_count": len(bot.active_symbols),
        "active_expiry": bot.active_expiry,
        "available_expiries": available_expiries,
        "expiry_rollovers": bot.expiry_rollover_count,
        "current_time_ist": current_time_ist,
        "sample_prices": sample_prices
    }

@app.route('/ping')
def ping():
    return "pong", 200

# -------------------------------
# Start Bot
# -------------------------------
def start_bot():
    print(f"[{datetime.now()}] 🤖 Starting Delta Options Bot...")
    bot_thread = threading.Thread(target=bot.start)
    bot_thread.daemon = True
    bot_thread.start()
    print(f"[{datetime.now()}] ✅ Bot thread started")

if __name__ == "__main__":
    print("="*50)
    print("Delta Options Arbitrage Bot - COMPLETE VERSION")
    print("="*50)
    
    start_bot()
    sleep(2)
    
    port = int(os.environ.get("PORT", 10000))
    print(f"[{datetime.now()}] 🚀 Starting web server on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
