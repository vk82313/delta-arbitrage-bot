import websocket
import json
import requests
import os
import random
from datetime import datetime, timedelta, timezone
from time import sleep
from flask import Flask, request
import threading

# Initialize Flask app
app = Flask(__name__)

# -------------------------------
# Configuration & Global State
# -------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Global thresholds - can be updated live
DELTA_THRESHOLD = {"ETH": 0.16, "BTC": 2}
ALERT_COOLDOWN = 60
PROCESS_INTERVAL = 2
EXPIRY_CHECK_INTERVAL = 60
BTC_FETCH_INTERVAL = 1

# -------------------------------
# Stealth Keep-Alive System
# -------------------------------
def system_maintenance_daemon():
    """System maintenance and health monitoring"""
    maintenance_counter = 0
    
    while True:
        try:
            # Internal service health check
            service_status = requests.get('http://localhost:10000/ping', timeout=5)
            if service_status.status_code == 200:
                maintenance_counter += 1
                
                # Rotate between different system checks
                if maintenance_counter % 4 == 0:
                    check_routes = ['/', '/health']
                    selected_route = random.choice(check_routes)
                    requests.get(f'http://localhost:10000{selected_route}', timeout=5)
            
        except Exception:
            pass
        
        # Variable maintenance intervals
        maintenance_intervals = [830, 850, 820, 840, 860]
        current_interval = random.choice(maintenance_intervals)
        sleep(current_interval)

def initialize_system_services():
    """Initialize background system services"""
    maintenance_thread = threading.Thread(target=system_maintenance_daemon, daemon=True)
    maintenance_thread.start()

# -------------------------------
# Utility Functions
# -------------------------------
def get_ist_time():
    """Get current time in IST correctly"""
    utc_now = datetime.now(timezone.utc)
    ist_offset = timedelta(hours=5, minutes=30)
    ist_time = utc_now + ist_offset
    return ist_time.strftime("%H:%M:%S")

def get_current_expiry():
    """Get current date in DDMMYY format"""
    utc_now = datetime.now(timezone.utc)
    ist_now = utc_now + timedelta(hours=5, minutes=30)
    return ist_now.strftime("%d%m%y")

def format_expiry_display(expiry_code):
    """Convert DDMMYY to DD MMM YY format"""
    try:
        day = expiry_code[:2]
        month = expiry_code[2:4]
        year = "20" + expiry_code[4:6]
        
        month_names = {
            '01': 'Jan', '02': 'Feb', '03': 'Mar', '04': 'Apr',
            '05': 'May', '06': 'Jun', '07': 'Jul', '08': 'Aug',
            '09': 'Sep', '10': 'Oct', '11': 'Nov', '12': 'Dec'
        }
        
        return f"{day} {month_names[month]} {year}"
    except:
        return expiry_code

def send_telegram(message):
    """Send Telegram message"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[{datetime.now()}] 📱 Telegram not configured: {message}")
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

# -------------------------------
# ETH WebSocket Bot
# -------------------------------
class ETHWebSocketBot:
    def __init__(self):
        self.websocket_url = "wss://socket.india.delta.exchange"
        self.ws = None
        self.last_alert_time = {}
        self.options_prices = {}
        self.connected = False
        self.current_expiry = get_current_expiry()
        self.active_expiry = self.get_initial_active_expiry()
        self.active_symbols = []
        self.should_reconnect = True
        self.last_arbitrage_check = 0
        self.last_expiry_check = 0
        self.message_count = 0
        self.expiry_rollover_count = 0
        self.alert_count = 0

    def get_initial_active_expiry(self):
        """Determine which expiry should be active right now"""
        now = datetime.now(timezone.utc)
        ist_now = now + timedelta(hours=5, minutes=30)
        
        if ist_now.hour >= 17 and ist_now.minute >= 30:
            next_day = ist_now + timedelta(days=1)
            next_expiry = next_day.strftime("%d%m%y")
            print(f"[{datetime.now()}] 🕠 ETH: After 5:30 PM, starting with next expiry: {next_expiry}")
            return next_expiry
        else:
            print(f"[{datetime.now()}] 📅 ETH: Starting with today's expiry: {self.current_expiry}")
            return self.current_expiry

    def should_rollover_expiry(self):
        """Check if we should move to next expiry"""
        now = datetime.now(timezone.utc)
        ist_now = now + timedelta(hours=5, minutes=30)
        
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
                    if 'ETH' in symbol:
                        expiry = self.extract_expiry_from_symbol(symbol)
                        if expiry:
                            expiries.add(expiry)
                
                return sorted(expiries)
            return []
        except Exception as e:
            print(f"[{datetime.now()}] ❌ ETH: Error fetching expiries: {e}")
            return []

    def get_next_available_expiry(self, current_expiry):
        """Get the next available expiry after current one"""
        available_expiries = self.get_available_expiries()
        if not available_expiries:
            return current_expiry
        
        print(f"[{datetime.now()}] 📊 ETH: Available expiries: {available_expiries}")
        
        for expiry in available_expiries:
            if expiry > current_expiry:
                return expiry
        
        return available_expiries[-1]

    def check_and_update_expiry(self):
        """Check if we need to update the active expiry"""
        current_time = datetime.now().timestamp()
        if current_time - self.last_expiry_check >= EXPIRY_CHECK_INTERVAL:
            self.last_expiry_check = current_time
            
            current_time_str = get_ist_time()
            print(f"[{datetime.now()}] 🔄 ETH: Checking expiry rollover... (Current: {self.active_expiry}, Time: {current_time_str})")
            
            next_expiry = self.should_rollover_expiry()
            if next_expiry and next_expiry != self.active_expiry:
                print(f"[{datetime.now()}] 🎯 ETH: EXPIRY ROLLOVER TRIGGERED!")
                print(f"[{datetime.now()}] 📅 ETH: Changing from {self.active_expiry} to {next_expiry}")
                
                actual_next_expiry = self.get_next_available_expiry(self.active_expiry)
                
                if actual_next_expiry != self.active_expiry:
                    self.active_expiry = actual_next_expiry
                    self.expiry_rollover_count += 1
                    
                    self.options_prices = {}
                    self.active_symbols = []
                    
                    if self.connected and self.ws:
                        self.subscribe_to_options()
                    
                    send_telegram(f"🔄 ETH Expiry Rollover Complete!\n\n📅 Now monitoring: {self.active_expiry}\n⏰ Time: {current_time_str}")
                    return True
                else:
                    print(f"[{datetime.now()}] ⚠️ ETH: No new expiry available yet, keeping: {self.active_expiry}")
            
            available_expiries = self.get_available_expiries()
            if available_expiries and self.active_expiry not in available_expiries:
                print(f"[{datetime.now()}] ⚠️ ETH: Current expiry {self.active_expiry} no longer available!")
                next_available = self.get_next_available_expiry(self.active_expiry)
                if next_available != self.active_expiry:
                    print(f"[{datetime.now()}] 🔄 ETH: Switching to available expiry: {next_available}")
                    self.active_expiry = next_available
                    self.expiry_rollover_count += 1
                    
                    self.options_prices = {}
                    self.active_symbols = []
                    
                    if self.connected and self.ws:
                        self.subscribe_to_options()
                    
                    send_telegram(f"🔄 ETH Expiry Update!\n\n📅 Now monitoring: {self.active_expiry}\n⏰ Time: {current_time_str}")
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
        """Fetch symbols for ACTIVE expiry only - ETH ONLY"""
        try:
            print(f"[{datetime.now()}] 🔍 ETH: Fetching {self.active_expiry} expiry options symbols...")
            
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
                    is_eth = 'ETH' in symbol
                    is_active_expiry = self.active_expiry in symbol
                    
                    if is_option and is_eth and is_active_expiry:
                        symbols.append(symbol)
                
                symbols = sorted(list(set(symbols)))
                
                print(f"[{datetime.now()}] ✅ ETH: Found {len(symbols)} {self.active_expiry} expiry options symbols")
                
                if not symbols:
                    available_expiries = self.get_available_expiries()
                    print(f"[{datetime.now()}] ⚠️ ETH: No symbols found for {self.active_expiry}")
                    print(f"[{datetime.now()}] 📅 ETH: Available expiries: {available_expiries}")
                    if available_expiries:
                        next_expiry = self.get_next_available_expiry(self.active_expiry)
                        if next_expiry != self.active_expiry:
                            print(f"[{datetime.now()}] 🔄 ETH: Auto-switching to available expiry: {next_expiry}")
                            self.active_expiry = next_expiry
                            return self.get_all_options_symbols()
                
                return symbols
            else:
                print(f"[{datetime.now()}] ❌ ETH: API Error: {response.status_code}")
                return []
                
        except Exception as e:
            print(f"[{datetime.now()}] ❌ ETH: Error fetching symbols: {e}")
            return []

    # WebSocket Callbacks
    def on_open(self, ws):
        self.connected = True
        print(f"[{datetime.now()}] ✅ ETH: Connected to WebSocket")
        print(f"[{datetime.now()}] 📅 ETH: Active expiry: {self.active_expiry}")
        self.subscribe_to_options()

    def on_close(self, ws, close_status_code, close_msg):
        self.connected = False
        print(f"[{datetime.now()}] 🔴 ETH: WebSocket closed")
        if self.should_reconnect:
            print(f"[{datetime.now()}] 🔄 ETH: Reconnecting in 10 seconds...")
            sleep(10)
            self.connect()

    def on_error(self, ws, error):
        print(f"[{datetime.now()}] ❌ ETH: WebSocket error: {error}")

    def on_message(self, ws, message):
        """Handle incoming WebSocket messages"""
        try:
            self.check_and_update_expiry()
            
            message_json = json.loads(message)
            message_type = message_json.get('type')
            
            self.message_count += 1
            
            if self.message_count % 100 == 0:
                print(f"[{datetime.now()}] 📨 ETH: Message {self.message_count}")
            
            if message_type == 'l1_orderbook':
                self.process_l1_orderbook_data(message_json)
            elif message_type == 'subscriptions':
                print(f"[{datetime.now()}] ✅ ETH: Subscriptions confirmed for {self.active_expiry}")
                
        except Exception as e:
            print(f"[{datetime.now()}] ❌ ETH: Message processing error: {e}")

    def process_l1_orderbook_data(self, message):
        """Process l1_orderbook data - ONLY ETH ACTIVE EXPIRY"""
        try:
            symbol = message.get('symbol')
            best_bid = message.get('best_bid')
            best_ask = message.get('best_ask')
            
            if symbol and best_bid is not None and best_ask is not None:
                if 'ETH' not in symbol:
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
                    
                    if len(self.options_prices) % 50 == 0:
                        print(f"[{datetime.now()}] 💰 ETH: Tracking {len(self.options_prices)} {self.active_expiry} symbols")
                    
                    current_time = datetime.now().timestamp()
                    if current_time - self.last_arbitrage_check >= PROCESS_INTERVAL:
                        self.check_arbitrage_opportunities()
                        self.last_arbitrage_check = current_time
                    
        except Exception as e:
            print(f"[{datetime.now()}] ❌ ETH: Error processing l1_orderbook data: {e}")

    def check_arbitrage_opportunities(self):
        """Check for arbitrage opportunities - ONLY ETH"""
        if len(self.options_prices) < 10:
            return
            
        eth_options = []
        
        for symbol, prices in self.options_prices.items():
            if 'ETH' in symbol:
                option_data = {
                    'symbol': symbol,
                    'bid': prices['bid'],
                    'ask': prices['ask']
                }
                eth_options.append(option_data)
        
        if eth_options:
            self.check_arbitrage_same_expiry(eth_options)

    def check_arbitrage_same_expiry(self, options):
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
                if call_diff < 0 and abs(call_diff) >= DELTA_THRESHOLD["ETH"]:
                    alert_key = f"ETH_CALL_{strike1}_{strike2}_{self.active_expiry}"
                    if self.can_alert(alert_key):
                        profit = abs(call_diff)
                        expiry_display = format_expiry_display(self.active_expiry)
                        current_time = get_ist_time()
                        
                        alert_msg = f"🔵 ETH Alert Call\n{strike1} (B) → {strike2} (S)\n${call1_ask:.2f}    ${call2_bid:.2f}\nProfit: ${profit:.2f}\n{expiry_display} | {current_time}"
                        alerts.append(alert_msg)
            
            # PUT arbitrage
            put1_bid = strikes[strike1]['put'].get('bid', 0)
            put2_ask = strikes[strike2]['put'].get('ask', 0)
            
            if put1_bid > 0 and put2_ask > 0:
                put_diff = put2_ask - put1_bid
                if put_diff < 0 and abs(put_diff) >= DELTA_THRESHOLD["ETH"]:
                    alert_key = f"ETH_PUT_{strike1}_{strike2}_{self.active_expiry}"
                    if self.can_alert(alert_key):
                        profit = abs(put_diff)
                        expiry_display = format_expiry_display(self.active_expiry)
                        current_time = get_ist_time()
                        
                        alert_msg = f"🔵 ETH Alert Put\n{strike2} (B) → {strike1} (S)\n${put2_ask:.2f}    ${put1_bid:.2f}\nProfit: ${profit:.2f}\n{expiry_display} | {current_time}"
                        alerts.append(alert_msg)
        
        if alerts:
            for alert in alerts:
                send_telegram(alert)
                self.alert_count += 1
                print(f"[{datetime.now()}] ✅ ETH: Sent arbitrage alert")

    def subscribe_to_options(self):
        """Subscribe to ACTIVE ETH expiry options"""
        symbols = self.get_all_options_symbols()
        
        if not symbols:
            print(f"[{datetime.now()}] ⚠️ ETH: No {self.active_expiry} expiry options symbols found")
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
            print(f"[{datetime.now()}] 📡 ETH: Subscribed to {len(symbols)} {self.active_expiry} expiry symbols")
            
            current_time_str = get_ist_time()
            send_telegram(f"🔗 ETH Bot Connected\n\n📅 Monitoring: {self.active_expiry}\n📊 Symbols: {len(symbols)}\n⏰ Time: {current_time_str}\n\nETH Bot is now live! 🚀")

    def can_alert(self, alert_key):
        """Check if we can send alert (cooldown)"""
        now = datetime.now().timestamp()
        last_time = self.last_alert_time.get(alert_key, 0)
        if now - last_time >= ALERT_COOLDOWN:
            self.last_alert_time[alert_key] = now
            return True
        return False

    def connect(self):
        """Connect to WebSocket"""
        print(f"[{datetime.now()}] 🌐 ETH: Connecting to WebSocket...")
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
                    print(f"[{datetime.now()}] ❌ ETH: Connection error: {e}")
                    sleep(10)
        
        bot_thread = threading.Thread(target=run_bot)
        bot_thread.daemon = True
        bot_thread.start()
        print(f"[{datetime.now()}] ✅ ETH: Bot thread started")

# -------------------------------
# BTC REST API Bot
# -------------------------------
class BTCRESTBot:
    def __init__(self):
        self.base_url = "https://api.india.delta.exchange/v2"
        self.last_alert_time = {}
        self.running = True
        self.fetch_count = 0
        self.alert_count = 0
        self.current_expiry = get_current_expiry()
        self.active_expiry = self.get_initial_active_expiry()
        self.active_symbols = []
        self.last_expiry_check = 0
        self.expiry_rollover_count = 0
        self.last_debug_log = 0
        self.options_prices = {}  # Track BTC symbols

    def get_initial_active_expiry(self):
        """Determine which expiry should be active right now"""
        now = datetime.now(timezone.utc)
        ist_now = now + timedelta(hours=5, minutes=30)
        
        if ist_now.hour >= 17 and ist_now.minute >= 30:
            next_day = ist_now + timedelta(days=1)
            next_expiry = next_day.strftime("%d%m%y")
            print(f"[{datetime.now()}] 🕠 BTC: After 5:30 PM, starting with next expiry: {next_expiry}")
            return next_expiry
        else:
            print(f"[{datetime.now()}] 📅 BTC: Starting with today's expiry: {self.current_expiry}")
            return self.current_expiry

    def should_rollover_expiry(self):
        """Check if we should move to next expiry"""
        now = datetime.now(timezone.utc)
        ist_now = now + timedelta(hours=5, minutes=30)
        
        if ist_now.hour >= 17 and ist_now.minute >= 30:
            next_expiry = (ist_now + timedelta(days=1)).strftime("%d%m%y")
            return next_expiry
        return None

    def get_available_expiries(self):
        """Get all available BTC expiries from the API"""
        try:
            url = f"{self.base_url}/tickers"
            params = {
                'contract_types': 'call_options,put_options',
                'underlying_asset_symbols': 'BTC'
            }
            
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if data.get('success'):
                    tickers = data.get('result', [])
                    expiries = set()
                    
                    for ticker in tickers:
                        symbol = ticker.get('symbol', '')
                        if 'BTC' in symbol:
                            expiry = self.extract_expiry_from_symbol(symbol)
                            if expiry:
                                expiries.add(expiry)
                    
                    return sorted(expiries)
            return []
        except Exception as e:
            print(f"[{datetime.now()}] ❌ BTC: Error fetching expiries: {e}")
            return []

    def get_next_available_expiry(self, current_expiry):
        """Get the next available expiry after current one"""
        available_expiries = self.get_available_expiries()
        if not available_expiries:
            return current_expiry
        
        print(f"[{datetime.now()}] 📊 BTC: Available expiries: {available_expiries}")
        
        for expiry in available_expiries:
            if expiry > current_expiry:
                return expiry
        
        return available_expiries[-1]

    def check_and_update_expiry(self):
        """Check if we need to update the active expiry"""
        current_time = datetime.now().timestamp()
        if current_time - self.last_expiry_check >= EXPIRY_CHECK_INTERVAL:
            self.last_expiry_check = current_time
            
            current_time_str = get_ist_time()
            print(f"[{datetime.now()}] 🔄 BTC: Checking expiry rollover... (Current: {self.active_expiry}, Time: {current_time_str})")
            
            next_expiry = self.should_rollover_expiry()
            if next_expiry and next_expiry != self.active_expiry:
                print(f"[{datetime.now()}] 🎯 BTC: EXPIRY ROLLOVER TRIGGERED!")
                print(f"[{datetime.now()}] 📅 BTC: Changing from {self.active_expiry} to {next_expiry}")
                
                actual_next_expiry = self.get_next_available_expiry(self.active_expiry)
                
                if actual_next_expiry != self.active_expiry:
                    self.active_expiry = actual_next_expiry
                    self.expiry_rollover_count += 1
                    
                    self.options_prices = {}
                    self.active_symbols = []
                    
                    send_telegram(f"🔄 BTC Expiry Rollover Complete!\n\n📅 Now monitoring: {self.active_expiry}\n⏰ Time: {current_time_str}")
                    return True
                else:
                    print(f"[{datetime.now()}] ⚠️ BTC: No new expiry available yet, keeping: {self.active_expiry}")
            
            available_expiries = self.get_available_expiries()
            if available_expiries and self.active_expiry not in available_expiries:
                print(f"[{datetime.now()}] ⚠️ BTC: Current expiry {self.active_expiry} no longer available!")
                next_available = self.get_next_available_expiry(self.active_expiry)
                if next_available != self.active_expiry:
                    print(f"[{datetime.now()}] 🔄 BTC: Switching to available expiry: {next_available}")
                    self.active_expiry = next_available
                    self.expiry_rollover_count += 1
                    
                    self.options_prices = {}
                    self.active_symbols = []
                    
                    send_telegram(f"🔄 BTC Expiry Update!\n\n📅 Now monitoring: {self.active_expiry}\n⏰ Time: {current_time_str}")
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

    def debug_log(self, message, force=False):
        """Debug logging with rate limiting"""
        current_time = datetime.now().timestamp()
        if force or current_time - self.last_debug_log >= 10:
            print(f"[{datetime.now()}] {message}")
            self.last_debug_log = current_time

    def fetch_tickers(self):
        """Fetch all tickers with detailed error handling"""
        try:
            self.debug_log("🔄 BTC: Fetching tickers from API...")
            url = f"{self.base_url}/tickers"
            response = requests.get(url, timeout=10)
            
            self.debug_log(f"📡 BTC: API Response Status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                if data.get('success'):
                    tickers = data.get('result', [])
                    self.debug_log(f"✅ BTC: Got {len(tickers)} tickers")
                    return tickers
                else:
                    self.debug_log(f"❌ BTC: API success=False: {data}")
            else:
                self.debug_log(f"❌ BTC: HTTP Error: {response.status_code} - {response.text}")
                
        except Exception as e:
            self.debug_log(f"❌ BTC: Exception fetching tickers: {e}")
        
        return []

    def process_btc_options(self):
        """Process BTC options with detailed logging"""
        tickers = self.fetch_tickers()
        if not tickers:
            self.debug_log("❌ BTC: No tickers received")
            return {}

        btc_tickers = [t for t in tickers if 'BTC' in str(t.get('symbol', '')).upper()]
        self.debug_log(f"🔍 BTC: Found {len(btc_tickers)} BTC tickers")
        
        current_expiry_tickers = []
        for ticker in btc_tickers:
            symbol = ticker.get('symbol', '')
            parts = symbol.split('-')
            if len(parts) >= 4:
                expiry = parts[-1]
                if expiry == self.active_expiry:
                    current_expiry_tickers.append(ticker)

        self.active_symbols = [t.get('symbol', '') for t in current_expiry_tickers]
        self.debug_log(f"📅 BTC: Found {len(current_expiry_tickers)} tickers for expiry {self.active_expiry}")
        
        if current_expiry_tickers and self.fetch_count % 10 == 0:
            sample_symbols = [t.get('symbol', '') for t in current_expiry_tickers[:3]]
            self.debug_log(f"📋 BTC: Sample symbols: {sample_symbols}")
        
        return self.group_by_strike(current_expiry_tickers)

    def group_by_strike(self, tickers):
        """Group tickers by strike price"""
        grouped = {}
        
        for ticker in tickers:
            symbol = ticker.get('symbol', '')
            parts = symbol.split('-')
            
            # Extract strike
            strike = 0
            for part in parts:
                if part.isdigit() and len(part) > 2:
                    strike = int(part)
                    break
            
            if strike == 0:
                continue
                
            # Detect option type
            option_type = 'call' if parts[0].startswith('C') else 'put' if parts[0].startswith('P') else 'unknown'
            
            if option_type == 'unknown':
                continue
                
            # Get prices
            quotes = ticker.get('quotes', {})
            bid = float(quotes.get('best_bid', 0)) or 0
            ask = float(quotes.get('best_ask', 0)) or 0
            
            if strike not in grouped:
                grouped[strike] = {'call': {'bid': 0, 'ask': 0}, 'put': {'bid': 0, 'ask': 0}}
            
            if option_type == 'call':
                grouped[strike]['call']['bid'] = bid
                grouped[strike]['call']['ask'] = ask
            else:  # put
                grouped[strike]['put']['bid'] = bid
                grouped[strike]['put']['ask'] = ask
        
        self.debug_log(f"💰 BTC: Grouped {len(grouped)} strikes with valid prices")
        return grouped

    def check_arbitrage(self, grouped_data):
        """Check for arbitrage opportunities"""
        if not grouped_data:
            return []
            
        strikes = sorted(grouped_data.keys())
        alerts = []
        
        for i in range(len(strikes) - 1):
            strike1 = strikes[i]
            strike2 = strikes[i + 1]
            
            # CALL arbitrage
            call1_ask = grouped_data[strike1]['call']['ask']
            call2_bid = grouped_data[strike2]['call']['bid']
            
            if call1_ask > 0 and call2_bid > 0:
                call_diff = call1_ask - call2_bid
                if call_diff < 0 and abs(call_diff) >= DELTA_THRESHOLD["BTC"]:
                    alert_key = f"BTC_CALL_{strike1}_{strike2}"
                    if self.can_alert(alert_key):
                        profit = abs(call_diff)
                        expiry_display = format_expiry_display(self.active_expiry)
                        current_time = get_ist_time()
                        
                        alert_msg = f"🔔 BTC Alert Call\n{strike1} (B) → {strike2} (S)\n${call1_ask:.2f}    ${call2_bid:.2f}\nProfit: ${profit:.2f}\n{expiry_display} | {current_time}"
                        alerts.append(alert_msg)
            
            # PUT arbitrage
            put1_bid = grouped_data[strike1]['put']['bid']
            put2_ask = grouped_data[strike2]['put']['ask']
            
            if put1_bid > 0 and put2_ask > 0:
                put_diff = put2_ask - put1_bid
                if put_diff < 0 and abs(put_diff) >= DELTA_THRESHOLD["BTC"]:
                    alert_key = f"BTC_PUT_{strike1}_{strike2}"
                    if self.can_alert(alert_key):
                        profit = abs(put_diff)
                        expiry_display = format_expiry_display(self.active_expiry)
                        current_time = get_ist_time()
                        
                        alert_msg = f"🔔 BTC Alert Put\n{strike2} (B) → {strike1} (S)\n${put2_ask:.2f}    ${put1_bid:.2f}\nProfit: ${profit:.2f}\n{expiry_display} | {current_time}"
                        alerts.append(alert_msg)
        
        return alerts

    def can_alert(self, alert_key):
        now = datetime.now().timestamp()
        last_time = self.last_alert_time.get(alert_key, 0)
        if now - last_time >= ALERT_COOLDOWN:
            self.last_alert_time[alert_key] = now
            return True
        return False

    def start_monitoring(self):
        self.debug_log("🤖 BTC: Starting Options Monitoring", force=True)
        
        # Send connection notification
        current_time_str = get_ist_time()
        send_telegram(f"🔗 BTC Bot Connected\n\n📅 Monitoring: {self.active_expiry}\n📊 Symbols: {len(self.active_symbols)}\n⏰ Time: {current_time_str}\n\nBTC Bot is now live! 🚀")
        
        while self.running:
            try:
                self.fetch_count += 1
                
                # Check expiry rollover
                self.check_and_update_expiry()
                
                # Process data
                grouped_data = self.process_btc_options()
                
                # Check arbitrage
                alerts = self.check_arbitrage(grouped_data)
                
                if alerts:
                    for alert in alerts:
                        send_telegram(alert)
                        self.alert_count += 1
                        self.debug_log(f"✅ BTC: Sent arbitrage alert")
                
                # Progress update
                if self.fetch_count % 30 == 0:
                    self.debug_log(f"📊 BTC: Stats: Fetches={self.fetch_count}, Alerts={self.alert_count}, Strikes={len(grouped_data)}, Symbols={len(self.active_symbols)}")
                
                sleep(BTC_FETCH_INTERVAL)
                
            except Exception as e:
                self.debug_log(f"❌ BTC: Main loop error: {e}")
                sleep(1)

    def stop(self):
        self.running = False

# -------------------------------
# Initialize Bots
# -------------------------------
eth_bot = ETHWebSocketBot()
btc_bot = BTCRESTBot()

# -------------------------------
# Flask Routes
# -------------------------------
@app.route('/')
def home():
    eth_status = "✅ Connected" if eth_bot.connected else "🔴 Disconnected"
    btc_status = "✅ Running" if btc_bot.running else "🔴 Stopped"
    current_time_str = get_ist_time()
    
    return f'''
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Dual Asset Options Arbitrage Bot</title>
        <style>
            * {{
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }}
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 20px;
            }}
            .container {{
                max-width: 1200px;
                margin: 0 auto;
            }}
            .header {{
                text-align: center;
                color: white;
                margin-bottom: 30px;
            }}
            .header h1 {{
                font-size: 2.5rem;
                margin-bottom: 10px;
                text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
            }}
            .stats-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
                gap: 20px;
                margin-bottom: 30px;
            }}
            .stat-card {{
                background: white;
                padding: 25px;
                border-radius: 15px;
                box-shadow: 0 10px 30px rgba(0,0,0,0.2);
            }}
            .stat-card h2 {{
                color: #333;
                margin-bottom: 15px;
                font-size: 1.4rem;
                border-bottom: 2px solid #f0f0f0;
                padding-bottom: 10px;
            }}
            .stat-item {{
                margin-bottom: 8px;
                font-size: 1.1rem;
                color: #555;
            }}
            .threshold-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(350px, 1fr));
                gap: 25px;
            }}
            .threshold-card {{
                background: white;
                padding: 30px;
                border-radius: 20px;
                box-shadow: 0 15px 35px rgba(0,0,0,0.1);
                text-align: center;
            }}
            .threshold-card h3 {{
                color: #333;
                margin-bottom: 20px;
                font-size: 1.3rem;
            }}
            .current-value {{
                font-size: 1.4rem;
                color: #666;
                margin-bottom: 25px;
                font-weight: 500;
            }}
            .input-group {{
                margin-bottom: 25px;
            }}
            .threshold-input {{
                width: 100%;
                padding: 15px;
                font-size: 1.2rem;
                border: 2px solid #e0e0e0;
                border-radius: 10px;
                text-align: center;
                transition: all 0.3s ease;
            }}
            .threshold-input:focus {{
                outline: none;
                border-color: #667eea;
                box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
            }}
            .update-btn {{
                width: 100%;
                padding: 18px;
                font-size: 1.2rem;
                font-weight: 600;
                border: none;
                border-radius: 12px;
                cursor: pointer;
                transition: all 0.3s ease;
                text-transform: uppercase;
                letter-spacing: 1px;
            }}
            .eth-btn {{
                background: linear-gradient(135deg, #667eea, #764ba2);
                color: white;
            }}
            .eth-btn:hover {{
                transform: translateY(-2px);
                box-shadow: 0 8px 25px rgba(102, 126, 234, 0.4);
            }}
            .btc-btn {{
                background: linear-gradient(135deg, #f093fb, #f5576c);
                color: white;
            }}
            .btc-btn:hover {{
                transform: translateY(-2px);
                box-shadow: 0 8px 25px rgba(245, 87, 108, 0.4);
            }}
            .footer {{
                text-align: center;
                margin-top: 30px;
                color: white;
                font-size: 1.1rem;
            }}
            .alert-success {{
                background: #d4edda;
                color: #155724;
                padding: 15px;
                border-radius: 10px;
                margin-bottom: 20px;
                border: 1px solid #c3e6cb;
            }}
            @media (max-width: 768px) {{
                .header h1 {{
                    font-size: 2rem;
                }}
                .stat-card, .threshold-card {{
                    padding: 20px;
                }}
                .update-btn {{
                    padding: 15px;
                    font-size: 1.1rem;
                }}
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>Dual Asset Options Arbitrage Bot</h1>
            </div>

            <!-- Success Message -->
            {'<div class="alert-success">Threshold updated successfully! Telegram notification sent.</div>' if request.args.get('success') else ''}

            <div class="stats-grid">
                <!-- ETH Stats Card -->
                <div class="stat-card">
                    <h2>ETH (WebSocket)</h2>
                    <div class="stat-item"><strong>Status:</strong> {eth_status}</div>
                    <div class="stat-item"><strong>Messages:</strong> {eth_bot.message_count}</div>
                    <div class="stat-item"><strong>ETH Symbols:</strong> {len(eth_bot.options_prices)}</div>
                    <div class="stat-item"><strong>Active Expiry:</strong> {eth_bot.active_expiry}</div>
                    <div class="stat-item"><strong>ETH Alerts:</strong> {eth_bot.alert_count}</div>
                    <div class="stat-item"><strong>ETH Threshold:</strong> ${DELTA_THRESHOLD['ETH']:.2f}</div>
                </div>

                <!-- BTC Stats Card -->
                <div class="stat-card">
                    <h2>BTC (REST API)</h2>
                    <div class="stat-item"><strong>Status:</strong> {btc_status}</div>
                    <div class="stat-item"><strong>Fetches:</strong> {btc_bot.fetch_count}</div>
                    <div class="stat-item"><strong>BTC Symbols:</strong> {len(btc_bot.active_symbols)}</div>
                    <div class="stat-item"><strong>Active Expiry:</strong> {btc_bot.active_expiry}</div>
                    <div class="stat-item"><strong>BTC Alerts:</strong> {btc_bot.alert_count}</div>
                    <div class="stat-item"><strong>BTC Threshold:</strong> ${DELTA_THRESHOLD['BTC']:.2f}</div>
                </div>
            </div>

            <div class="threshold-grid">
                <!-- ETH Threshold Card -->
                <div class="threshold-card">
                    <h3>Update ETH Threshold</h3>
                    <div class="current-value">Current: ${DELTA_THRESHOLD['ETH']:.2f}</div>
                    <form action="/update_eth_threshold" method="POST">
                        <div class="input-group">
                            <input type="number" name="threshold" value="{DELTA_THRESHOLD['ETH']:.2f}" step="0.01" min="0.01" max="10" 
                                   class="threshold-input" required placeholder="Enter ETH threshold">
                        </div>
                        <button type="submit" class="update-btn eth-btn">UPDATE ETH</button>
                    </form>
                </div>

                <!-- BTC Threshold Card -->
                <div class="threshold-card">
                    <h3>Update BTC Threshold</h3>
                    <div class="current-value">Current: ${DELTA_THRESHOLD['BTC']:.2f}</div>
                    <form action="/update_btc_threshold" method="POST">
                        <div class="input-group">
                            <input type="number" name="threshold" value="{DELTA_THRESHOLD['BTC']:.2f}" step="0.01" min="0.01" max="50" 
                                   class="threshold-input" required placeholder="Enter BTC threshold">
                        </div>
                        <button type="submit" class="update-btn btc-btn">UPDATE BTC</button>
                    </form>
                </div>
            </div>

            <div class="footer">
                <p>Last Update: {current_time_str}</p>
                <p><a href="/health" style="color: white; text-decoration: underline;">Health Check</a></p>
            </div>
        </div>
    </body>
    </html>
    '''

@app.route('/update_eth_threshold', methods=['POST'])
def update_eth_threshold():
    """Update ETH threshold"""
    try:
        new_threshold = float(request.form['threshold'])
        if new_threshold <= 0:
            return "Threshold must be positive", 400
        
        old_threshold = DELTA_THRESHOLD['ETH']
        DELTA_THRESHOLD['ETH'] = new_threshold
        
        # Send Telegram notification
        current_time_str = get_ist_time()
        send_telegram(f"⚙️ ETH Threshold Updated\n\n📊 New Value: ${new_threshold:.2f}\n⏰ Time: {current_time_str}\n\nThreshold changed successfully!")
        
        print(f"[{datetime.now()}] ✅ ETH threshold updated: ${old_threshold:.2f} → ${new_threshold:.2f}")
        
        return redirect_with_success()
    except ValueError:
        return "Invalid threshold value", 400
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Error updating ETH threshold: {e}")
        return "Error updating threshold", 500

@app.route('/update_btc_threshold', methods=['POST'])
def update_btc_threshold():
    """Update BTC threshold"""
    try:
        new_threshold = float(request.form['threshold'])
        if new_threshold <= 0:
            return "Threshold must be positive", 400
        
        old_threshold = DELTA_THRESHOLD['BTC']
        DELTA_THRESHOLD['BTC'] = new_threshold
        
        # Send Telegram notification
        current_time_str = get_ist_time()
        send_telegram(f"⚙️ BTC Threshold Updated\n\n📊 New Value: ${new_threshold:.2f}\n⏰ Time: {current_time_str}\n\nThreshold changed successfully!")
        
        print(f"[{datetime.now()}] ✅ BTC threshold updated: ${old_threshold:.2f} → ${new_threshold:.2f}")
        
        return redirect_with_success()
    except ValueError:
        return "Invalid threshold value", 400
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Error updating BTC threshold: {e}")
        return "Error updating threshold", 500

def redirect_with_success():
    """Redirect to home with success parameter"""
    from flask import redirect
    return redirect('/?success=true')

@app.route('/health')
def health():
    current_time_str = get_ist_time()
    
    return {
        "eth": {
            "connected": eth_bot.connected,
            "messages_received": eth_bot.message_count,
            "symbols_tracked": len(eth_bot.options_prices),
            "active_expiry": eth_bot.active_expiry,
            "alerts_sent": eth_bot.alert_count,
            "threshold": DELTA_THRESHOLD['ETH']
        },
        "btc": {
            "running": btc_bot.running,
            "fetch_count": btc_bot.fetch_count,
            "symbols_tracked": len(btc_bot.active_symbols),
            "active_expiry": btc_bot.active_expiry,
            "alerts_sent": btc_bot.alert_count,
            "threshold": DELTA_THRESHOLD['BTC']
        },
        "current_time": current_time_str
    }, 200

@app.route('/start_btc')
def start_btc():
    if not btc_bot.running:
        btc_bot.running = True
        threading.Thread(target=btc_bot.start_monitoring, daemon=True).start()
        return "BTC Bot started"
    return "BTC Bot already running"

@app.route('/stop_btc')
def stop_btc():
    btc_bot.stop()
    return "BTC Bot stopped"

@app.route('/ping')
def ping():
    return "pong", 200

# -------------------------------
# Start Both Bots
# -------------------------------
def start_bots():
    print("="*60)
    print("Dual Asset Options Arbitrage Bot")
    print("="*60)
    print(f"⚡ ETH Threshold: ${DELTA_THRESHOLD['ETH']:.2f}")
    print(f"⚡ BTC Threshold: ${DELTA_THRESHOLD['BTC']:.2f}")
    print(f"📅 Current expiry: {get_current_expiry()}")
    print("="*60)
    
    # Start ETH WebSocket bot
    eth_bot.start()
    
    # Start BTC REST API bot
    btc_thread = threading.Thread(target=btc_bot.start_monitoring, daemon=True)
    btc_thread.start()
    
    print(f"[{datetime.now()}] ✅ Both bots started")

if __name__ == "__main__":
    start_bots()
    sleep(2)
    
    # Start stealth keep-alive system
    initialize_system_services()
    
    port = int(os.environ.get("PORT", 10000))
    print(f"[{datetime.now()}] 🚀 Starting web server on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
