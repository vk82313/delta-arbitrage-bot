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
# Rate limiting to avoid excessive processing
LAST_PROCESS_TIME = 0
PROCESS_INTERVAL = 2  # Process every 2 seconds

# -------------------------------
# Delta WebSocket Client - FULLY CORRECTED
# -------------------------------
class DeltaOptionsBot:
    def __init__(self):
        # ✅ CORRECT WebSocket URL
        self.websocket_url = "wss://socket.india.delta.exchange"
        self.ws = None
        self.last_alert_time = {}
        self.options_prices = {}
        self.connected = False
        self.current_expiry = self.get_current_expiry()
        self.active_symbols = []
        self.should_reconnect = True
        self.last_arbitrage_check = 0

    def get_current_expiry(self):
        """Get current expiry in DDMMYY format"""
        now = datetime.now(timezone.utc)
        ist_now = now + timedelta(hours=5, minutes=30)
        
        if ist_now.hour >= 17 and ist_now.minute >= 30:
            expiry_date = ist_now + timedelta(days=1)
        else:
            expiry_date = ist_now
        
        expiry_str = expiry_date.strftime("%d%m%y")
        print(f"[{datetime.now()}] 📅 Using expiry: {expiry_str}")
        return expiry_str

    def get_all_options_symbols(self):
        """Fetch ALL available BTC/ETH options symbols"""
        try:
            print(f"[{datetime.now()}] 🔍 Fetching options symbols from Delta API...")
            # ✅ CORRECT API endpoint
            url = "https://api.india.delta.exchange/v2/products"
            response = requests.get(url, timeout=10)
            
            if response.status_code == 200:
                products = response.json().get('result', [])
                symbols = []
                
                for product in products:
                    symbol = product.get('symbol', '')
                    contract_type = str(product.get('contract_type', '')).lower()
                    underlying_asset = product.get('underlying_asset', '')
                    
                    # Filter for BTC/ETH options for current expiry
                    is_option = any(opt in contract_type for opt in ['call', 'put', 'option'])
                    is_current_expiry = self.current_expiry in symbol
                    is_btc_eth = underlying_asset in ['BTC', 'ETH']
                    
                    if is_option and is_current_expiry and is_btc_eth:
                        symbols.append(symbol)
                
                # Remove duplicates and sort
                symbols = sorted(list(set(symbols)))
                
                print(f"[{datetime.now()}] ✅ Found {len(symbols)} options symbols")
                
                if symbols:
                    # Show strike ranges
                    btc_symbols = [s for s in symbols if 'BTC' in s]
                    eth_symbols = [s for s in symbols if 'ETH' in s]
                    
                    btc_strikes = sorted(list(set([self.extract_strike(sym) for sym in btc_symbols])))
                    eth_strikes = sorted(list(set([self.extract_strike(sym) for sym in eth_symbols])))
                    
                    if btc_strikes:
                        print(f"[{datetime.now()}] 📊 BTC Strikes: {btc_strikes[0]:,} to {btc_strikes[-1]:,} ({len(btc_strikes)} strikes)")
                    if eth_strikes:
                        print(f"[{datetime.now()}] 📊 ETH Strikes: {eth_strikes[0]:,} to {eth_strikes[-1]:,} ({len(eth_strikes)} strikes)")
                
                return symbols
            else:
                print(f"[{datetime.now()}] ❌ API Error: {response.status_code}")
                return []
                
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Error fetching symbols: {e}")
            return []

    def extract_strike(self, symbol):
        """Extract strike price from symbol"""
        try:
            parts = symbol.split('-')
            for part in parts:
                if part.isdigit():
                    return int(part)
            return 0
        except:
            return 0

    # ---------------------------
    # WebSocket Callbacks - FULLY CORRECTED
    # ---------------------------
    def on_open(self, ws):
        self.connected = True
        print(f"[{datetime.now()}] ✅ Connected to Delta Exchange WebSocket")
        self.subscribe_to_options()

    def on_close(self, ws, close_status_code, close_msg):
        self.connected = False
        print(f"[{datetime.now()}] 🔴 WebSocket closed - Code: {close_status_code}, Msg: {close_msg}")
        if self.should_reconnect:
            print(f"[{datetime.now()}] 🔄 Reconnecting in 10 seconds...")
            sleep(10)
            self.connect()

    def on_error(self, ws, error):
        print(f"[{datetime.now()}] ❌ WebSocket error: {error}")

    def on_message(self, ws, message):
        """Handle incoming WebSocket messages - CORRECTED"""
        try:
            message_json = json.loads(message)
            message_type = message_json.get('type')
            
            # Debug: Log all message types initially
            if len(self.options_prices) < 10:  # Only log first few messages for debugging
                print(f"[{datetime.now()}] 📨 Received message type: {message_type}")
            
            # ✅ CORRECT message type for l1_orderbook
            if message_type == 'l1_orderbook':
                self.process_l1_orderbook_data(message_json)
            elif message_type == 'success':
                print(f"[{datetime.now()}] ✅ {message_json.get('message', 'Success')}")
            elif message_type == 'error':
                print(f"[{datetime.now()}] ❌ Subscription error: {message_json}")
            elif message_type == 'subscribe':
                print(f"[{datetime.now()}] ✅ Subscription confirmed for channel")
                
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Message processing error: {e}")

    def process_l1_orderbook_data(self, message):
        """Process l1_orderbook data - CORRECTED"""
        try:
            symbol = message.get('symbol')
            # ✅ CORRECT field names for l1_orderbook
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
                    
                    # Only log occasionally to avoid spam
                    if len(self.options_prices) % 50 == 0:  # Log every 50th update
                        print(f"[{datetime.now()}] 💰 Prices tracking: {len(self.options_prices)} symbols")
                    
                    # Check for arbitrage with rate limiting
                    current_time = datetime.now().timestamp()
                    if current_time - self.last_arbitrage_check >= PROCESS_INTERVAL:
                        self.check_arbitrage_opportunities()
                        self.last_arbitrage_check = current_time
                    
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Error processing l1_orderbook data: {e}")

    def check_arbitrage_opportunities(self):
        """Check for arbitrage opportunities with rate limiting"""
        # Only check if we have sufficient data
        if len(self.options_prices) < 20:  # Wait for more data
            return
            
        btc_options = []
        eth_options = []
        
        # Separate BTC and ETH options
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
            self.check_arbitrage('BTC', btc_options)
        if eth_options:
            self.check_arbitrage('ETH', eth_options)

    def subscribe_to_options(self):
        """Subscribe to all available options"""
        symbols = self.get_all_options_symbols()
        
        if not symbols:
            print(f"[{datetime.now()}] ⚠️ No symbols found from API, using fallback...")
            symbols = self.get_fallback_symbols()
        
        self.active_symbols = symbols
        
        if symbols:
            # ✅ CORRECT channel name
            payload = {
                "type": "subscribe",
                "payload": {
                    "channels": [
                        {
                            "name": "l1_orderbook",  # ✅ CORRECT channel name
                            "symbols": symbols
                        }
                    ]
                }
            }
            
            self.ws.send(json.dumps(payload))
            print(f"[{datetime.now()}] 📡 Subscribed to {len(symbols)} options symbols on l1_orderbook channel")
            
            # Send connection alert
            if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
                self.send_telegram(f"🔗 *Bot Connected* 🔗\n\n✅ Connected to Delta Exchange\n📅 Expiry: {self.current_expiry}\n📊 Monitoring: {len(symbols)} symbols\n📈 Channel: l1_orderbook\n\nBot is now live! 🚀")
        else:
            print(f"[{datetime.now()}] ❌ No symbols available to subscribe")

    def get_fallback_symbols(self):
        """Fallback symbols if API fails - CORRECTED FORMAT"""
        symbols = []
        
        # Common strikes around current market - ✅ CORRECT FORMAT
        btc_strikes = [58000, 59000, 60000, 61000, 62000, 63000, 64000, 65000]
        eth_strikes = [2800, 2900, 3000, 3100, 3200, 3300, 3400, 3500]
        
        # ✅ CORRECT SYMBOL FORMAT: C-BTC-{strike}-{expiry}
        for strike in btc_strikes:
            symbols.append(f"C-BTC-{strike}-{self.current_expiry}")
            symbols.append(f"P-BTC-{strike}-{self.current_expiry}")
        
        for strike in eth_strikes:
            symbols.append(f"C-ETH-{strike}-{self.current_expiry}")
            symbols.append(f"P-ETH-{strike}-{self.current_expiry}")
        
        print(f"[{datetime.now()}] 🔄 Using {len(symbols)} fallback symbols")
        return symbols

    def check_arbitrage(self, asset, options):
        """Check for arbitrage opportunities"""
        # Group by strike price
        strikes = {}
        for option in options:
            strike = self.extract_strike(option['symbol'])
            if strike > 0:
                if strike not in strikes:
                    strikes[strike] = {'call': {}, 'put': {}}
                
                if 'C-' in option['symbol']:
                    strikes[strike]['call'] = {'bid': option['bid'], 'ask': option['ask']}
                elif 'P-' in option['symbol']:
                    strikes[strike]['put'] = {'bid': option['bid'], 'ask': option['ask']}
        
        # Sort strikes
        sorted_strikes = sorted(strikes.keys())
        
        if len(sorted_strikes) < 2:
            return
        
        # Check adjacent strikes for arbitrage
        alerts = []
        for i in range(len(sorted_strikes) - 1):
            strike1 = sorted_strikes[i]
            strike2 = sorted_strikes[i + 1]
            
            # CALL arbitrage: Buy lower strike, sell higher strike
            call1_ask = strikes[strike1]['call'].get('ask', 0)
            call2_bid = strikes[strike2]['call'].get('bid', 0)
            
            if call1_ask > 0 and call2_bid > 0:
                call_diff = call1_ask - call2_bid
                if call_diff < 0 and abs(call_diff) >= DELTA_THRESHOLD[asset]:
                    alert_key = f"{asset}_CALL_{strike1}_{strike2}"
                    if self.can_alert(alert_key):
                        profit = abs(call_diff)
                        alerts.append(f"🔷 CALL {strike1:,} Ask: ${call1_ask:.2f} vs {strike2:,} Bid: ${call2_bid:.2f} → Profit: ${profit:.2f}")
            
            # PUT arbitrage: Sell lower strike, buy higher strike
            put1_bid = strikes[strike1]['put'].get('bid', 0)
            put2_ask = strikes[strike2]['put'].get('ask', 0)
            
            if put1_bid > 0 and put2_ask > 0:
                put_diff = put2_ask - put1_bid
                if put_diff < 0 and abs(put_diff) >= DELTA_THRESHOLD[asset]:
                    alert_key = f"{asset}_PUT_{strike1}_{strike2}"
                    if self.can_alert(alert_key):
                        profit = abs(put_diff)
                        alerts.append(f"🟣 PUT {strike1:,} Bid: ${put1_bid:.2f} vs {strike2:,} Ask: ${put2_ask:.2f} → Profit: ${profit:.2f}")
        
        # Send alerts if any found
        if alerts:
            message = f"🚨 *{asset} ARBITRAGE ALERTS* 🚨\n\n" + "\n".join(alerts)
            message += f"\n\n_Time: {datetime.now().strftime('%H:%M:%S')}_"
            message += f"\n_Expiry: {self.current_expiry}_"
            self.send_telegram(message)
            print(f"[{datetime.now()}] ✅ Sent {len(alerts)} {asset} arbitrage alerts")

    def can_alert(self, alert_key):
        now = datetime.now().timestamp()
        last_time = self.last_alert_time.get(alert_key, 0)
        if now - last_time >= ALERT_COOLDOWN:
            self.last_alert_time[alert_key] = now
            return True
        return False

    def send_telegram(self, message):
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            print(f"[{datetime.now()}] ⚠️ Telegram credentials not set")
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
                print(f"[{datetime.now()}] ❌ Telegram API error: {resp.status_code}")
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Telegram send error: {e}")

    def connect(self):
        """Connect to WebSocket - runs in its own thread"""
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
                    if self.should_reconnect:
                        print(f"[{datetime.now()}] 🔄 Restarting bot in 10 seconds...")
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
    <p>Monitoring: BTC & ETH Options</p>
    <p>Current Prices: {len(bot.options_prices)} symbols</p>
    <p>Active Symbols: {len(bot.active_symbols)}</p>
    <p>Expiry: {bot.current_expiry}</p>
    <p>WebSocket Channel: l1_orderbook</p>
    <p>Last Update: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    <p><a href="/debug">Debug Info</a> | <a href="/health">Health</a></p>
    """

@app.route('/health')
def health():
    return {
        "status": "healthy", 
        "bot_connected": bot.connected, 
        "symbols_tracked": len(bot.options_prices),
        "active_symbols": len(bot.active_symbols),
        "expiry": bot.current_expiry,
        "websocket_channel": "l1_orderbook"
    }, 200

@app.route('/debug')
def debug():
    """Debug endpoint to check WebSocket data"""
    sample_prices = dict(list(bot.options_prices.items())[:5])  # First 5 symbols
    return {
        "connected": bot.connected,
        "active_symbols_count": len(bot.active_symbols),
        "price_data_count": len(bot.options_prices),
        "sample_prices": sample_prices,
        "active_symbols_sample": bot.active_symbols[:5] if bot.active_symbols else [],
        "websocket_channel": "l1_orderbook"
    }

@app.route('/ping')
def ping():
    return "pong", 200

# -------------------------------
# Start Bot in Background Thread
# -------------------------------
def start_bot():
    """Start the bot in a separate thread"""
    print(f"[{datetime.now()}] 🤖 Starting Delta Options Bot...")
    bot_thread = threading.Thread(target=bot.start)
    bot_thread.daemon = True
    bot_thread.start()
    print(f"[{datetime.now()}] ✅ Bot thread started")

# -------------------------------
# Main
# -------------------------------
if __name__ == "__main__":
    print("="*50)
    print("Delta Options Arbitrage Bot - FULLY CORRECTED")
    print("WebSocket Channel: l1_orderbook")
    print("="*50)
    
    # Start the bot FIRST
    start_bot()
    
    # Give bot a moment to start connecting
    sleep(2)
    
    # Then start Flask app
    port = int(os.environ.get("PORT", 10000))
    print(f"[{datetime.now()}] 🚀 Starting web server on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
