import websocket
import json
import brotli
import base64
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
EXPIRY_CHECK_INTERVAL = 60

# -------------------------------
# Delta WebSocket Client - FIXED STRIKE EXTRACTION
# -------------------------------
class DeltaOptionsBot:
    def __init__(self):
        self.websocket_url = "wss://socket.india.delta.exchange"
        self.ws = None
        self.last_alert_time = {}
        self.options_prices = {}
        self.connected = False
        self.current_expiry = self.get_current_expiry()
        self.active_expiry = self.current_expiry
        self.active_symbols = []
        self.should_reconnect = True
        self.last_arbitrage_check = 0
        self.last_expiry_check = 0
        self.message_count = 0
        self.expiry_rollover_count = 0

    def get_current_expiry(self):
        """Get current expiry in DDMMYY format"""
        now = datetime.now(timezone.utc)
        ist_now = now + timedelta(hours=5, minutes=30)
        expiry_str = ist_now.strftime("%d%m%y")
        print(f"[{datetime.now()}] 📅 Current date expiry: {expiry_str}")
        return expiry_str

    def should_rollover_expiry(self):
        """Check if we should move to next expiry"""
        now = datetime.now(timezone.utc)
        ist_now = now + timedelta(hours=5, minutes=30)
        
        if ist_now.hour >= 17 and ist_now.minute >= 30:
            next_expiry = (ist_now + timedelta(days=1)).strftime("%d%m%y")
            print(f"[{datetime.now()}] ⏰ Time-based rollover: {self.active_expiry} → {next_expiry}")
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

    def check_and_update_expiry(self):
        """Check if we need to update the active expiry"""
        current_time = datetime.now().timestamp()
        if current_time - self.last_expiry_check >= EXPIRY_CHECK_INTERVAL:
            self.last_expiry_check = current_time
            
            next_expiry = self.should_rollover_expiry()
            if next_expiry and next_expiry != self.active_expiry:
                print(f"[{datetime.now()}] 🔄 Expiry rollover detected!")
                print(f"[{datetime.now()}] 📅 Changing from {self.active_expiry} to {next_expiry}")
                self.active_expiry = next_expiry
                self.expiry_rollover_count += 1
                self.options_prices = {}
                self.active_symbols = []
                
                if self.connected and self.ws:
                    self.subscribe_to_options()
                
                self.send_telegram(f"🔄 *Expiry Rollover*\n\n📅 Now monitoring: {self.active_expiry}\n\nBot automatically switched to new expiry! ✅")
                return True
            
            available_expiries = self.get_available_expiries()
            if available_expiries and self.active_expiry not in available_expiries:
                next_available = self.get_next_active_expiry()
                if next_available != self.active_expiry:
                    print(f"[{datetime.now()}] 🔄 Expiry {self.active_expiry} no longer available, switching to {next_available}")
                    self.active_expiry = next_available
                    self.expiry_rollover_count += 1
                    self.options_prices = {}
                    self.active_symbols = []
                    
                    if self.connected and self.ws:
                        self.subscribe_to_options()
                    
                    self.send_telegram(f"🔄 *Expiry Update*\n\n📅 Now monitoring: {self.active_expiry}\n\nPrevious expiry no longer available! ✅")
                    return True
        
        return False

    def get_next_active_expiry(self):
        """Get the next active expiry after current time"""
        available_expiries = self.get_available_expiries()
        if not available_expiries:
            return self.current_expiry
        
        for expiry in available_expiries:
            if expiry >= self.current_expiry:
                return expiry
        
        return available_expiries[-1]

    def extract_expiry_from_symbol(self, symbol):
        """Extract expiry date from symbol string"""
        try:
            parts = symbol.split('-')
            if len(parts) >= 4:
                return parts[3]  # Expiry is the 4th part
            return None
        except:
            return None

    def extract_strike(self, symbol):
        """Extract strike price from symbol - FIXED VERSION"""
        try:
            # Delta Exchange option format: C-BTC-90000-310125 or P-ETH-3500-141025
            parts = symbol.split('-')
            
            # Should have exactly 4 parts for options: C-BTC-104200-191025
            if len(parts) != 4:
                return 0
                
            # Strike price is always the 3rd part (index 2)
            strike_part = parts[2]
            
            # Validate it's numeric - NO LENGTH RESTRICTION
            if strike_part.isdigit():
                strike = int(strike_part)
                # Debug specific strikes to verify
                if '104200' in symbol or '104400' in symbol:
                    print(f"[{datetime.now()}] 🔍 Strike extraction: {symbol} → {strike}")
                return strike
            
            return 0
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Error extracting strike from {symbol}: {e}")
            return 0

    def is_valid_option_symbol(self, symbol):
        """Validate if symbol is a proper option symbol"""
        try:
            parts = symbol.split('-')
            if len(parts) != 4:
                return False
                
            option_type, asset, strike, expiry = parts
            
            # Validate option type
            if option_type not in ['C', 'P']:
                return False
                
            # Validate asset
            if asset not in ['BTC', 'ETH']:
                return False
                
            # Validate strike is numeric
            if not strike.isdigit():
                return False
                
            # Validate expiry format (6 digits)
            if not expiry.isdigit() or len(expiry) != 6:
                return False
                
            return True
        except:
            return False

    def debug_strike_extraction(self):
        """Debug method to test strike extraction"""
        test_symbols = [
            "C-BTC-104200-191025",  # Your problematic symbol
            "C-BTC-104400-191025",  # Your problematic symbol  
            "P-BTC-104200-191025",
            "P-BTC-104400-191025",
            "C-BTC-90000-310125",
            "P-BTC-85000-310125", 
            "C-ETH-3500-141025",
            "P-ETH-3200-141025"
        ]
        
        print(f"[{datetime.now()}] 🔍 Testing strike extraction:")
        for symbol in test_symbols:
            strike = self.extract_strike(symbol)
            valid = self.is_valid_option_symbol(symbol)
            print(f"  {symbol} → Strike: {strike}, Valid: {valid}")

    def decompress_brotli_data(self, compressed_data):
        """Decompress Brotli compressed data"""
        try:
            if not compressed_data:
                return []
                
            # Decode base64 and decompress Brotli
            decoded_data = base64.b64decode(compressed_data)
            decompressed_data = brotli.decompress(decoded_data)
            
            # Parse the JSON array
            return json.loads(decompressed_data.decode('utf-8'))
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Brotli decompression error: {e}")
            return []

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
                    
                    is_option = contract_type in ['call_options', 'put_options']
                    is_btc_eth = any(asset in symbol for asset in ['BTC', 'ETH'])
                    is_active_expiry = self.active_expiry in symbol
                    
                    if is_option and is_btc_eth and is_active_expiry:
                        symbols.append(symbol)
                
                symbols = sorted(list(set(symbols)))
                
                print(f"[{datetime.now()}] ✅ Found {len(symbols)} {self.active_expiry} expiry options symbols")
                
                # Run strike extraction debug
                self.debug_strike_extraction()
                
                if not symbols:
                    available_expiries = self.get_available_expiries()
                    print(f"[{datetime.now()}] ⚠️ No symbols found for {self.active_expiry}")
                    print(f"[{datetime.now()}] 📅 Available expiries: {available_expiries}")
                
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
            # Check for expiry rollover first
            self.check_and_update_expiry()
            
            message_json = json.loads(message)
            message_type = message_json.get('type')
            
            self.message_count += 1
            
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
        """Process l1_orderbook data - PROPER BROTLI DECOMPRESSION"""
        try:
            # Delta Exchange sends compressed data in the 'c' field
            compressed_data = message.get('c')
            if not compressed_data:
                # If no compressed data, try direct processing (for debugging)
                symbol = message.get('symbol')
                best_bid = message.get('best_bid')
                best_ask = message.get('best_ask')
                
                if symbol and best_bid is not None and best_ask is not None:
                    self.process_single_symbol(symbol, best_bid, best_ask)
                return
            
            # Decompress the Brotli compressed data
            decompressed_data = self.decompress_brotli_data(compressed_data)
            if not decompressed_data:
                return
            
            # Process each symbol in the decompressed data
            processed_count = 0
            for symbol_data in decompressed_data:
                if isinstance(symbol_data, dict):
                    symbol = symbol_data.get('s')  # Symbol name
                    data_array = symbol_data.get('d', [])  # Data array
                    
                    if symbol and len(data_array) >= 4:
                        # Data format: [best_ask, ask_size, best_bid, bid_size, ...]
                        best_ask = data_array[0]
                        best_bid = data_array[2]
                        
                        if self.process_single_symbol(symbol, best_bid, best_ask):
                            processed_count += 1
            
            # Log processing stats occasionally
            if self.message_count % 100 == 0:
                print(f"[{datetime.now()}] 🔄 Processed {processed_count} symbols from compressed data")
                    
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Error processing l1_orderbook data: {e}")

    def process_single_symbol(self, symbol, best_bid, best_ask):
        """Process a single symbol's data"""
        try:
            # ONLY process symbols with ACTIVE expiry
            symbol_expiry = self.extract_expiry_from_symbol(symbol)
            if symbol_expiry != self.active_expiry:
                return False
            
            best_bid_price = float(best_bid) if best_bid else 0
            best_ask_price = float(best_ask) if best_ask else 0
            
            # Validate prices
            if best_bid_price <= 0 or best_ask_price <= 0 or best_ask_price < best_bid_price:
                return False
            
            # Store the price data
            self.options_prices[symbol] = {
                'bid': best_bid_price,
                'ask': best_ask_price
            }
            
            # Log specific strikes to debug
            if '104200' in symbol or '104400' in symbol:
                print(f"[{datetime.now()}] 🔍 SPECIFIC STRIKE: {symbol}: Bid=${best_bid_price:.2f}, Ask=${best_ask_price:.2f}")
            
            # Progress logging
            if len(self.options_prices) % 25 == 0:
                print(f"[{datetime.now()}] 📊 Tracking {len(self.options_prices)} {self.active_expiry} symbols")
            
            # Check for arbitrage with rate limiting
            current_time = datetime.now().timestamp()
            if current_time - self.last_arbitrage_check >= PROCESS_INTERVAL:
                self.check_arbitrage_opportunities()
                self.last_arbitrage_check = current_time
            
            return True
            
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Error processing symbol {symbol}: {e}")
            return False

    def check_arbitrage_opportunities(self):
        """Check for arbitrage opportunities - WITH PROPER STRIKE EXTRACTION"""
        if len(self.options_prices) < 10:
            return
            
        btc_options = []
        eth_options = []
        
        # Filter out obviously wrong prices (sanity checks)
        for symbol, prices in self.options_prices.items():
            # Validate symbol format first
            if not self.is_valid_option_symbol(symbol):
                continue
                
            bid = prices['bid']
            ask = prices['ask']
            
            # Sanity checks for price data
            if bid <= 0 or ask <= 0:
                continue
                
            if ask < bid:
                continue  # Skip if ask < bid (impossible)
                
            # Skip obviously wrong prices (too high or too low)
            if 'BTC' in symbol and (bid > 10000 or ask > 10000):
                if self.message_count % 10 == 0:  # Don't spam logs
                    print(f"[{datetime.now()}] ⚠️ Suspicious BTC price: {symbol} Bid=${bid:.2f}, Ask=${ask:.2f}")
                continue
                
            if 'ETH' in symbol and (bid > 1000 or ask > 1000):
                if self.message_count % 10 == 0:
                    print(f"[{datetime.now()}] ⚠️ Suspicious ETH price: {symbol} Bid=${bid:.2f}, Ask=${ask:.2f}")
                continue
            
            option_data = {
                'symbol': symbol,
                'bid': bid,
                'ask': ask
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
        """Check for arbitrage opportunities with PROPER STRIKE EXTRACTION"""
        strikes = {}
        
        # First, validate all options data
        valid_options = []
        for option in options:
            symbol = option['symbol']
            bid = option['bid']
            ask = option['ask']
            
            # Additional validation
            if bid <= 0 or ask <= 0 or ask < bid:
                continue
                
            # Price range validation based on asset
            if asset == 'BTC' and (bid > 5000 or ask > 5000):
                continue
            elif asset == 'ETH' and (bid > 500 or ask > 500):
                continue
                
            valid_options.append(option)
        
        if len(valid_options) < 4:  # Need at least 2 calls and 2 puts
            return
        
        # Group by strike price using FIXED extraction
        for option in valid_options:
            strike = self.extract_strike(option['symbol'])
            if strike <= 0:  # Changed to <= 0 for better validation
                continue
                
            if strike not in strikes:
                strikes[strike] = {'call': {}, 'put': {}}
            
            if option['symbol'].startswith('C-'):
                strikes[strike]['call'] = {
                    'bid': option['bid'], 
                    'ask': option['ask'],
                    'symbol': option['symbol']
                }
            elif option['symbol'].startswith('P-'):
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
            
            # Verify we have valid data for both strikes
            call1_data = strikes[strike1]['call']
            call2_data = strikes[strike2]['call']
            put1_data = strikes[strike1]['put']
            put2_data = strikes[strike2]['put']
            
            # CALL arbitrage - with validation
            if call1_data.get('ask', 0) > 0 and call2_data.get('bid', 0) > 0:
                call1_ask = call1_data['ask']
                call2_bid = call2_data['bid']
                
                # Additional validation - skip impossible profits
                if call1_ask < 0.01 or call2_bid < 0.01:  # Skip very small prices
                    continue
                    
                # Skip if profit seems too large (likely data error)
                max_reasonable_profit = 1000  # $1000 max reasonable profit
                potential_profit = call2_bid - call1_ask
                if potential_profit > max_reasonable_profit:
                    print(f"[{datetime.now()}] ⚠️ Skipping unrealistic CALL profit: ${potential_profit:.2f}")
                    continue
                    
                call_diff = call1_ask - call2_bid
                if call_diff < 0 and abs(call_diff) >= DELTA_THRESHOLD[asset]:
                    alert_key = f"{asset}_CALL_{strike1}_{strike2}_{self.active_expiry}"
                    if self.can_alert(alert_key):
                        profit = abs(call_diff)
                        # Log the actual prices for debugging
                        print(f"[{datetime.now()}] ✅ VALID CALL Arbitrage: {strike1} Ask=${call1_ask:.2f} vs {strike2} Bid=${call2_bid:.2f} → Profit=${profit:.2f}")
                        alerts.append(f"🔷 {asset} CALL {strike1:,} Ask: ${call1_ask:.2f} vs {strike2:,} Bid: ${call2_bid:.2f} → Profit: ${profit:.2f}")
            
            # PUT arbitrage - with validation
            if put1_data.get('bid', 0) > 0 and put2_data.get('ask', 0) > 0:
                put1_bid = put1_data['bid']
                put2_ask = put2_data['ask']
                
                # Additional validation
                if put1_bid < 0.01 or put2_ask < 0.01:  # Skip very small prices
                    continue
                    
                # Skip if profit seems too large (likely data error)
                max_reasonable_profit = 1000  # $1000 max reasonable profit
                potential_profit = put1_bid - put2_ask
                if potential_profit > max_reasonable_profit:
                    print(f"[{datetime.now()}] ⚠️ Skipping unrealistic PUT profit: ${potential_profit:.2f}")
                    continue
                    
                put_diff = put2_ask - put1_bid
                if put_diff < 0 and abs(put_diff) >= DELTA_THRESHOLD[asset]:
                    alert_key = f"{asset}_PUT_{strike1}_{strike2}_{self.active_expiry}"
                    if self.can_alert(alert_key):
                        profit = abs(put_diff)
                        # Log the actual prices for debugging
                        print(f"[{datetime.now()}] ✅ VALID PUT Arbitrage: {strike1} Bid=${put1_bid:.2f} vs {strike2} Ask=${put2_ask:.2f} → Profit=${profit:.2f}")
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
            
            self.send_telegram(f"🔗 *Bot Connected*\n\n📅 Monitoring: {self.active_expiry}\n📊 Symbols: {len(symbols)}\n\nBot is now live! 🚀")

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
    
    # Show actual prices for debugging
    sample_prices = {}
    for symbol, prices in list(bot.options_prices.items())[:8]:
        sample_prices[symbol] = {
            'bid': round(prices['bid'], 2),
            'ask': round(prices['ask'], 2)
        }
    
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
    print("Delta Options Arbitrage Bot - FIXED STRIKE EXTRACTION")
    print("="*50)
    
    start_bot()
    sleep(2)
    
    port = int(os.environ.get("PORT", 10000))
    print(f"[{datetime.now()}] 🚀 Starting web server on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
