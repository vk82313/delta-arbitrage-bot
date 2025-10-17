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
PROCESS_INTERVAL = 2  # Process every 2 seconds

# -------------------------------
# Delta WebSocket Client - CORRECTED
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
        
        # For testing, let's try multiple expiry formats
        expiry_str = ist_now.strftime("%d%m%y")
        print(f"[{datetime.now()}] 📅 Using expiry: {expiry_str}")
        return expiry_str

    def get_all_options_symbols(self):
        """Fetch ALL available BTC/ETH options symbols - CORRECTED"""
        try:
            print(f"[{datetime.now()}] 🔍 Fetching options symbols from Delta API...")
            
            # ✅ CORRECT: Use contract_types parameter to filter options
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
                    underlying_asset = product.get('underlying_asset', {})
                    
                    # Debug: Print some product info
                    if len(symbols) < 5:  # Print first 5 for debugging
                        print(f"[{datetime.now()}] 🔍 Product: {symbol} (type: {contract_type})")
                    
                    # Filter for BTC/ETH options
                    is_option = contract_type in ['call_options', 'put_options']
                    is_btc_eth = any(asset in symbol for asset in ['BTC', 'ETH'])
                    
                    if is_option and is_btc_eth:
                        symbols.append(symbol)
                
                # Remove duplicates and sort
                symbols = sorted(list(set(symbols)))
                
                print(f"[{datetime.now()}] ✅ Found {len(symbols)} options symbols")
                
                # Show what symbols we found
                if symbols:
                    print(f"[{datetime.now()}] 📋 Sample symbols: {symbols[:5]}")
                else:
                    print(f"[{datetime.now()}] ⚠️ No options symbols found. Available products:")
                    for product in products[:10]:  # Show first 10 products
                        print(f"  - {product.get('symbol')} ({product.get('contract_type')})")
                
                return symbols
            else:
                print(f"[{datetime.now()}] ❌ API Error: {response.status_code}")
                print(f"[{datetime.now()}] 📝 Response: {response.text}")
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
    # WebSocket Callbacks
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
        """Handle incoming WebSocket messages"""
        try:
            message_json = json.loads(message)
            message_type = message_json.get('type')
            
            self.message_count += 1
            
            # Log first few messages and then periodically
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
                    
                    # Log progress
                    if len(self.options_prices) % 20 == 0:
                        print(f"[{datetime.now()}] 💰 Tracking {len(self.options_prices)} symbols with price data")
                    
                    # Check for arbitrage with rate limiting
                    current_time = datetime.now().timestamp()
                    if current_time - self.last_arbitrage_check >= PROCESS_INTERVAL:
                        self.check_arbitrage_opportunities()
                        self.last_arbitrage_check = current_time
                    
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Error processing l1_orderbook data: {e}")

    def check_arbitrage_opportunities(self):
        """Check for arbitrage opportunities"""
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
        
        if btc_options:
            self.check_arbitrage('BTC', btc_options)
        if eth_options:
            self.check_arbitrage('ETH', eth_options)

    def subscribe_to_options(self):
        """Subscribe to available options"""
        symbols = self.get_all_options_symbols()
        
        if not symbols:
            print(f"[{datetime.now()}] ⚠️ No live options symbols found from API")
            print(f"[{datetime.now()}] 🔄 Using spot symbols for testing...")
            # Subscribe to some spot symbols for testing
            symbols = [
                "BTCUSDT", "ETHUSDT", "BTC-17OCT25-60000-C", "BTC-17OCT25-60000-P",
                "ETH-17OCT25-3000-C", "ETH-17OCT25-3000-P"
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
            print(f"[{datetime.now()}] 📋 Symbols: {symbols}")
            
            # Test Telegram with simple message
            self.test_telegram()
        else:
            print(f"[{datetime.now()}] ❌ No symbols available to subscribe")

    def test_telegram(self):
        """Test Telegram connection with simple message"""
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            print(f"[{datetime.now()}] ⚠️ Telegram credentials not configured")
            return
            
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            resp = requests.post(url, data={
                "chat_id": TELEGRAM_CHAT_ID, 
                "text": "🤖 Bot connected to Delta Exchange", 
                "parse_mode": "Markdown"
            })
            if resp.status_code == 200:
                print(f"[{datetime.now()}] 📱 Telegram test message sent")
            else:
                print(f"[{datetime.now()}] ❌ Telegram error {resp.status_code}: {resp.text}")
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Telegram test failed: {e}")

    def check_arbitrage(self, asset, options):
        """Check for arbitrage opportunities"""
        strikes = {}
        for option in options:
            strike = self.extract_strike(option['symbol'])
            if strike > 0:
                if strike not in strikes:
                    strikes[strike] = {'call': {}, 'put': {}}
                
                if 'C' in option['symbol']:
                    strikes[strike]['call'] = {'bid': option['bid'], 'ask': option['ask']}
                elif 'P' in option['symbol']:
                    strikes[strike]['put'] = {'bid': option['bid'], 'ask': option['ask']}
        
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
                    alert_key = f"{asset}_CALL_{strike1}_{strike2}"
                    if self.can_alert(alert_key):
                        profit = abs(call_diff)
                        alerts.append(f"🔷 CALL {strike1:,} Ask: ${call1_ask:.2f} vs {strike2:,} Bid: ${call2_bid:.2f} → Profit: ${profit:.2f}")
            
            # PUT arbitrage
            put1_bid = strikes[strike1]['put'].get('bid', 0)
            put2_ask = strikes[strike2]['put'].get('ask', 0)
            
            if put1_bid > 0 and put2_ask > 0:
                put_diff = put2_ask - put1_bid
                if put_diff < 0 and abs(put_diff) >= DELTA_THRESHOLD[asset]:
                    alert_key = f"{asset}_PUT_{strike1}_{strike2}"
                    if self.can_alert(alert_key):
                        profit = abs(put_diff)
                        alerts.append(f"🟣 PUT {strike1:,} Bid: ${put1_bid:.2f} vs {strike2:,} Ask: ${put2_ask:.2f} → Profit: ${profit:.2f}")
        
        if alerts:
            message = f"🚨 *{asset} ARBITRAGE ALERTS* 🚨\n\n" + "\n".join(alerts)
            message += f"\n\n_Time: {datetime.now().strftime('%H:%M:%S')}_"
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
    <p>Expiry: {bot.current_expiry}</p>
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
        "expiry": bot.current_expiry
    }, 200

@app.route('/debug')
def debug():
    """Debug endpoint"""
    sample_prices = dict(list(bot.options_prices.items())[:3])
    return {
        "connected": bot.connected,
        "messages_received": bot.message_count,
        "symbols_tracked": len(bot.options_prices),
        "active_symbols_count": len(bot.active_symbols),
        "sample_prices": sample_prices,
        "active_symbols_sample": bot.active_symbols[:5]
    }

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
    print("Delta Options Arbitrage Bot - DEBUG VERSION")
    print("="*50)
    
    start_bot()
    sleep(2)
    
    port = int(os.environ.get("PORT", 10000))
    print(f"[{datetime.now()}] 🚀 Starting web server on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
