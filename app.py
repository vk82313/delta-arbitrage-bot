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

# -------------------------------
# Delta WebSocket Client - FIXED ARBITRAGE LOGIC
# -------------------------------
class DeltaOptionsBot:
    def __init__(self):
        self.websocket_url = "wss://socket.india.delta.exchange"
        self.ws = None
        self.last_alert_time = {}
        self.options_prices = {}
        self.connected = False
        self.current_expiry = self.get_current_expiry()
        self.active_symbols = []
        self.should_reconnect = True
        self.last_arbitrage_check = 0
        self.message_count = 0

    def get_current_expiry(self):
        """Get current expiry in DDMMYY format"""
        now = datetime.now(timezone.utc)
        ist_now = now + timedelta(hours=5, minutes=30)
        
        expiry_str = ist_now.strftime("%d%m%y")
        print(f"[{datetime.now()}] 📅 Using expiry: {expiry_str}")
        return expiry_str

    def extract_expiry_from_symbol(self, symbol):
        """Extract expiry date from symbol string"""
        try:
            # Symbol format: C-BTC-{strike}-{expiry} or P-BTC-{strike}-{expiry}
            parts = symbol.split('-')
            if len(parts) >= 4:
                return parts[3]  # Expiry is the 4th part
            return None
        except:
            return None

    def extract_strike(self, symbol):
        """Extract strike price from symbol"""
        try:
            parts = symbol.split('-')
            for part in parts:
                if part.isdigit() and len(part) > 2:  # Strike prices are usually > 100
                    return int(part)
            return 0
        except:
            return 0

    def get_all_options_symbols(self):
        """Fetch ALL available BTC/ETH options symbols"""
        try:
            print(f"[{datetime.now()}] 🔍 Fetching options symbols from Delta API...")
            
            url = "https://api.india.delta.exchange/v2/products"
            params = {
                'contract_types': 'call_options,put_options',
                'states': 'live'
            }
            
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                products = response.json().get('result', [])
                symbols = []
                
                print(f"[{datetime.now()}] 📊 Total products from API: {len(products)}")
                
                for product in products:
                    symbol = product.get('symbol', '')
                    contract_type = product.get('contract_type', '')
                    
                    # Filter for BTC/ETH options
                    is_option = contract_type in ['call_options', 'put_options']
                    is_btc_eth = any(asset in symbol for asset in ['BTC', 'ETH'])
                    
                    if is_option and is_btc_eth:
                        symbols.append(symbol)
                
                # Remove duplicates and sort
                symbols = sorted(list(set(symbols)))
                
                print(f"[{datetime.now()}] ✅ Found {len(symbols)} options symbols")
                
                if symbols:
                    print(f"[{datetime.now()}] 📋 Sample symbols: {symbols[:5]}")
                
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
            message_json = json.loads(message)
            message_type = message_json.get('type')
            
            self.message_count += 1
            
            if self.message_count <= 10 or self.message_count % 50 == 0:
                print(f"[{datetime.now()}] 📨 Message {self.message_count}: type={message_type}")
            
            if message_type == 'l1_orderbook':
                self.process_l1_orderbook_data(message_json)
            elif message_type == 'subscriptions':
                print(f"[{datetime.now()}] ✅ Subscriptions confirmed")
            elif message_type == 'success':
                print(f"[{datetime.now()}] ✅ {message_json.get('message', 'Success')}")
            elif message_type == 'error':
                print(f"[{datetime.now()}] ❌ Error: {message_json}")
                
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Message processing error: {e}")

    def process_l1_orderbook_data(self, message):
        """Process l1_orderbook data"""
        try:
            symbol = message.get('symbol')
            best_bid = message.get('best_bid')
            best_ask = message.get('best_ask')
            
            if symbol and best_bid is not None and best_ask is not None:
                best_bid_price = float(best_bid) if best_bid else 0
                best_ask_price = float(best_ask) if best_ask else 0
                
                if best_bid_price > 0 and best_ask_price > 0:
                    # Store the price data
                    self.options_prices[symbol] = {
                        'bid': best_bid_price,
                        'ask': best_ask_price
                    }
                    
                    # Log progress occasionally
                    if len(self.options_prices) % 25 == 0:
                        print(f"[{datetime.now()}] 💰 Tracking {len(self.options_prices)} symbols with price data")
                    
                    # Check for arbitrage with rate limiting
                    current_time = datetime.now().timestamp()
                    if current_time - self.last_arbitrage_check >= PROCESS_INTERVAL:
                        self.check_arbitrage_opportunities()
                        self.last_arbitrage_check = current_time
                    
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Error processing l1_orderbook data: {e}")

    def check_arbitrage_opportunities(self):
        """Check for arbitrage opportunities - FIXED LOGIC"""
        if len(self.options_prices) < 10:
            return
            
        # Group options by asset and expiry first
        options_by_expiry = {}
        
        for symbol, prices in self.options_prices.items():
            if 'BTC' in symbol or 'ETH' in symbol:
                asset = 'BTC' if 'BTC' in symbol else 'ETH'
                expiry = self.extract_expiry_from_symbol(symbol)
                
                if expiry not in options_by_expiry:
                    options_by_expiry[expiry] = {}
                if asset not in options_by_expiry[expiry]:
                    options_by_expiry[expiry][asset] = []
                
                options_by_expiry[expiry][asset].append({
                    'symbol': symbol,
                    'bid': prices['bid'],
                    'ask': prices['ask']
                })
        
        # Check arbitrage for each expiry separately
        for expiry, assets in options_by_expiry.items():
            for asset, options in assets.items():
                if len(options) >= 4:  # Need at least 2 calls and 2 puts
                    self.check_arbitrage_same_expiry(asset, expiry, options)

    def check_arbitrage_same_expiry(self, asset, expiry, options):
        """Check for arbitrage opportunities within the same expiry"""
        # Group by strike price
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
        
        # Sort strikes and only compare adjacent strikes
        sorted_strikes = sorted(strikes.keys())
        
        if len(sorted_strikes) < 2:
            return
        
        alerts = []
        
        # Check adjacent strikes only (within same expiry)
        for i in range(len(sorted_strikes) - 1):
            strike1 = sorted_strikes[i]
            strike2 = sorted_strikes[i + 1]
            
            # Verify both strikes have the same expiry
            strike1_expiry = self.extract_expiry_from_symbol(strikes[strike1]['call'].get('symbol', '')) if strikes[strike1]['call'] else None
            strike2_expiry = self.extract_expiry_from_symbol(strikes[strike2]['call'].get('symbol', '')) if strikes[strike2]['call'] else None
            
            if strike1_expiry != strike2_expiry:
                continue  # Skip if different expiries
            
            # CALL arbitrage: Buy lower strike CALL, sell higher strike CALL
            call1_ask = strikes[strike1]['call'].get('ask', 0)
            call2_bid = strikes[strike2]['call'].get('bid', 0)
            
            if call1_ask > 0 and call2_bid > 0:
                call_diff = call1_ask - call2_bid
                if call_diff < 0 and abs(call_diff) >= DELTA_THRESHOLD[asset]:
                    alert_key = f"{asset}_CALL_{strike1}_{strike2}_{expiry}"
                    if self.can_alert(alert_key):
                        profit = abs(call_diff)
                        alerts.append(f"🔷 {asset} CALL {strike1:,} Ask: ${call1_ask:.2f} vs {strike2:,} Bid: ${call2_bid:.2f} → Profit: ${profit:.2f}")
            
            # PUT arbitrage: Sell lower strike PUT, buy higher strike PUT
            put1_bid = strikes[strike1]['put'].get('bid', 0)
            put2_ask = strikes[strike2]['put'].get('ask', 0)
            
            if put1_bid > 0 and put2_ask > 0:
                put_diff = put2_ask - put1_bid
                if put_diff < 0 and abs(put_diff) >= DELTA_THRESHOLD[asset]:
                    alert_key = f"{asset}_PUT_{strike1}_{strike2}_{expiry}"
                    if self.can_alert(alert_key):
                        profit = abs(put_diff)
                        alerts.append(f"🟣 {asset} PUT {strike1:,} Bid: ${put1_bid:.2f} vs {strike2:,} Ask: ${put2_ask:.2f} → Profit: ${profit:.2f}")
        
        # Send alerts if any found
        if alerts:
            message = f"🚨 *{asset} {expiry} ARBITRAGE ALERTS* 🚨\n\n" + "\n".join(alerts)
            message += f"\n\n_Expiry: {expiry}_"
            message += f"\n_Time: {datetime.now().strftime('%H:%M:%S')}_"
            self.send_telegram(message)
            print(f"[{datetime.now()}] ✅ Sent {len(alerts)} {asset} arbitrage alerts for expiry {expiry}")

    def subscribe_to_options(self):
        """Subscribe to available options"""
        symbols = self.get_all_options_symbols()
        
        if not symbols:
            print(f"[{datetime.now()}] ⚠️ No live options symbols found from API")
            print(f"[{datetime.now()}] 🔄 Using common symbols for testing...")
            # Use common symbols that likely exist
            symbols = [
                "BTCUSDT", "ETHUSDT", 
                "C-BTC-60000-171025", "P-BTC-60000-171025",
                "C-BTC-61000-171025", "P-BTC-61000-171025",
                "C-BTC-62000-171025", "P-BTC-62000-171025",
                "C-ETH-3000-171025", "P-ETH-3000-171025",
                "C-ETH-3100-171025", "P-ETH-3100-171025"
            ]
        
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
            print(f"[{datetime.now()}] 📡 Subscribed to {len(symbols)} symbols")
            
            # Test Telegram
            self.test_telegram()
        else:
            print(f"[{datetime.now()}] ❌ No symbols available to subscribe")

    def test_telegram(self):
        """Test Telegram connection"""
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            print(f"[{datetime.now()}] ⚠️ Telegram credentials not configured")
            return
            
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            resp = requests.post(url, data={
                "chat_id": TELEGRAM_CHAT_ID, 
                "text": "🤖 Arbitrage Bot Connected - Fixed same-expiry logic", 
            })
            if resp.status_code == 200:
                print(f"[{datetime.now()}] 📱 Telegram test message sent")
            else:
                print(f"[{datetime.now()}] ❌ Telegram error {resp.status_code}")
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Telegram test failed: {e}")

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
    return f"""
    <h1>Delta Options Arbitrage Bot</h1>
    <p>Status: {status}</p>
    <p>Messages Received: {bot.message_count}</p>
    <p>Current Prices: {len(bot.options_prices)} symbols</p>
    <p>Active Symbols: {len(bot.active_symbols)}</p>
    <p>Current Expiry: {bot.current_expiry}</p>
    <p>Last Update: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    <p><a href="/debug">Debug Info</a> | <a href="/health">Health</a></p>
    """

@app.route('/health')
def health():
    return {
        "status": "healthy", 
        "bot_connected": bot.connected, 
        "messages_received": bot.message_count,
        "symbols_tracked": len(bot.options_prices),
        "active_symbols": len(bot.active_symbols),
        "current_expiry": bot.current_expiry
    }, 200

@app.route('/debug')
def debug():
    """Debug endpoint"""
    # Group symbols by expiry for debugging
    expiries = {}
    for symbol in bot.options_prices.keys():
        expiry = bot.extract_expiry_from_symbol(symbol)
        if expiry:
            if expiry not in expiries:
                expiries[expiry] = []
            expiries[expiry].append(symbol)
    
    sample_prices = dict(list(bot.options_prices.items())[:3])
    return {
        "connected": bot.connected,
        "messages_received": bot.message_count,
        "symbols_tracked": len(bot.options_prices),
        "active_symbols_count": len(bot.active_symbols),
        "symbols_by_expiry": expiries,
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
    print("Delta Options Arbitrage Bot - SAME EXPIRY FIX")
    print("Only compares adjacent strikes within same expiry")
    print("="*50)
    
    start_bot()
    sleep(2)
    
    port = int(os.environ.get("PORT", 10000))
    print(f"[{datetime.now()}] 🚀 Starting web server on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
