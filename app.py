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
DELTA_THRESHOLD = {"ETH": 0.16, "BTC": 2}
ALERT_COOLDOWN = 60
PROCESS_INTERVAL = 2
EXPIRY_CHECK_INTERVAL = 60
BTC_FETCH_INTERVAL = 1

# -------------------------------
# Utility Functions
# -------------------------------
def get_ist_time():
    """Get current time in IST correctly"""
    utc_now = datetime.now(timezone.utc)
    ist_offset = timedelta(hours=5, minutes=30)
    ist_time = utc_now + ist_offset
    return ist_time.strftime("%H:%M:%S IST")

def get_current_expiry():
    """Get current date in DDMMYY format"""
    utc_now = datetime.now(timezone.utc)
    ist_now = utc_now + timedelta(hours=5, minutes=30)
    return ist_now.strftime("%d%m%y")

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
            print(f"[{datetime.now()}] 🕠 ETH: After 5:30 PM IST, starting with next expiry: {next_expiry}")
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
            
            current_time_ist = get_ist_time()
            print(f"[{datetime.now()}] 🔄 ETH: Checking expiry rollover... (Current: {self.active_expiry}, Time: {current_time_ist})")
            
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
                    
                    send_telegram(f"🔄 *ETH Expiry Rollover Complete!*\n\n📅 Now monitoring: {self.active_expiry}\n⏰ Time: {current_time_ist}\n\nBot automatically switched to new expiry! ✅")
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
                    
                    send_telegram(f"🔄 *ETH Expiry Update*\n\n📅 Now monitoring: {self.active_expiry}\n⏰ Time: {current_time_ist}\n\nPrevious expiry no longer available! ✅")
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
                    alert_key = f"ETH_CALL_{strike1}_{strike2}_{self.active_expiry}"
                    if self.can_alert(alert_key):
                        profit = abs(call_diff)
                        alerts.append(f"🔷 ETH CALL {strike1:,} Ask: ${call1_ask:.2f} vs {strike2:,} Bid: ${call2_bid:.2f} → Profit: ${profit:.2f}")
            
            # PUT arbitrage
            put1_bid = strikes[strike1]['put'].get('bid', 0)
            put2_ask = strikes[strike2]['put'].get('ask', 0)
            
            if put1_bid > 0 and put2_ask > 0:
                put_diff = put2_ask - put1_bid
                if put_diff < 0 and abs(put_diff) >= DELTA_THRESHOLD[asset]:
                    alert_key = f"ETH_PUT_{strike1}_{strike2}_{self.active_expiry}"
                    if self.can_alert(alert_key):
                        profit = abs(put_diff)
                        alerts.append(f"🟣 ETH PUT {strike1:,} Bid: ${put1_bid:.2f} vs {strike2:,} Ask: ${put2_ask:.2f} → Profit: ${profit:.2f}")
        
        if alerts:
            current_time_ist = get_ist_time()
            
            message = f"🚨 *ETH {self.active_expiry} ARBITRAGE ALERTS* 🚨\n\n" + "\n".join(alerts)
            message += f"\n\n_Expiry: {self.active_expiry}_"
            message += f"\n_Time: {current_time_ist}_"
            message += f"\n_Threshold: ${DELTA_THRESHOLD['ETH']}_"
            
            send_telegram(message)
            self.alert_count += len(alerts)
            print(f"[{datetime.now()}] ✅ ETH: Sent {len(alerts)} arbitrage alerts for {self.active_expiry}")

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
            
            current_time_ist = get_ist_time()
            send_telegram(f"🔗 *ETH Bot Connected*\n\n📅 Monitoring: {self.active_expiry}\n📊 Symbols: {len(symbols)}\n⏰ Time: {current_time_ist}\n\nETH Bot is now live! 🚀")

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
        self.last_debug_log = 0

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
                if expiry == self.current_expiry:
                    current_expiry_tickers.append(ticker)

        self.debug_log(f"📅 BTC: Found {len(current_expiry_tickers)} tickers for expiry {self.current_expiry}")
        
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
                        alerts.append(f"🔷 BTC CALL {strike1:,} Ask: ${call1_ask:.2f} vs {strike2:,} Bid: ${call2_bid:.2f} → Profit: ${profit:.2f}")
            
            # PUT arbitrage
            put1_bid = grouped_data[strike1]['put']['bid']
            put2_ask = grouped_data[strike2]['put']['ask']
            
            if put1_bid > 0 and put2_ask > 0:
                put_diff = put2_ask - put1_bid
                if put_diff < 0 and abs(put_diff) >= DELTA_THRESHOLD["BTC"]:
                    alert_key = f"BTC_PUT_{strike1}_{strike2}"
                    if self.can_alert(alert_key):
                        profit = abs(put_diff)
                        alerts.append(f"🟣 BTC PUT {strike1:,} Bid: ${put1_bid:.2f} vs {strike2:,} Ask: ${put2_ask:.2f} → Profit: ${profit:.2f}")
        
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
        
        while self.running:
            try:
                self.fetch_count += 1
                
                # Process data
                grouped_data = self.process_btc_options()
                
                # Check arbitrage
                alerts = self.check_arbitrage(grouped_data)
                
                if alerts:
                    current_time_ist = get_ist_time()  # FIXED: Using correct IST time
                    
                    message = f"🚨 *BTC {self.current_expiry} ARBITRAGE ALERTS* 🚨\n\n" + "\n".join(alerts)
                    message += f"\n\n_Expiry: {self.current_expiry}_"
                    message += f"\n_Time: {current_time_ist}_"  # FIXED: Correct IST time
                    message += f"\n_Threshold: ${DELTA_THRESHOLD['BTC']}_"
                    
                    send_telegram(message)
                    self.alert_count += len(alerts)
                    self.debug_log(f"✅ BTC: Sent {len(alerts)} alerts")
                
                # Progress update
                if self.fetch_count % 30 == 0:
                    self.debug_log(f"📊 BTC: Stats: Fetches={self.fetch_count}, Alerts={self.alert_count}, Strikes={len(grouped_data)}")
                
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
    current_time_ist = get_ist_time()
    
    return f"""
    <h1>Dual Asset Options Arbitrage Bot</h1>
    
    <h2>ETH (WebSocket)</h2>
    <p>Status: {eth_status}</p>
    <p>Messages: {eth_bot.message_count}</p>
    <p>ETH Symbols: {len(eth_bot.options_prices)}</p>
    <p>Active Expiry: {eth_bot.active_expiry}</p>
    <p>ETH Alerts: {eth_bot.alert_count}</p>
    <p>ETH Threshold: ${DELTA_THRESHOLD['ETH']}</p>
    
    <h2>BTC (REST API)</h2>
    <p>Status: {btc_status}</p>
    <p>Fetches: {btc_bot.fetch_count}</p>
    <p>BTC Alerts: {btc_bot.alert_count}</p>
    <p>Current Expiry: {btc_bot.current_expiry}</p>
    <p>BTC Threshold: ${DELTA_THRESHOLD['BTC']}</p>
    
    <p>Last Update: {current_time_ist}</p>
    <p><a href="/health">Health Check</a></p>
    """

@app.route('/health')
def health():
    current_time_ist = get_ist_time()
    
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
            "alerts_sent": btc_bot.alert_count,
            "current_expiry": btc_bot.current_expiry,
            "threshold": DELTA_THRESHOLD['BTC']
        },
        "current_time_ist": current_time_ist
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
    print(f"⚡ ETH Threshold: ${DELTA_THRESHOLD['ETH']}")
    print(f"⚡ BTC Threshold: ${DELTA_THRESHOLD['BTC']}")
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
    
    port = int(os.environ.get("PORT", 10000))
    print(f"[{datetime.now()}] 🚀 Starting web server on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
