import websocket
import json
import threading
import time
import requests
import os
from datetime import datetime, timedelta
from flask import Flask

app = Flask(__name__)

# Your Telegram credentials
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# Store options data
btc_options = {}
eth_options = {}
last_alert = {}
current_expiry = None
last_expiry_check = 0
active_symbols = []

class ArbitrageBot:
    def __init__(self):
        self.ws = None
        self.current_expiry = self.get_current_expiry()
        print(f"🚀 Starting Delta Arbitrage Bot...")
        print(f"📅 Initial Expiry: {self.current_expiry}")
        
    def fetch_active_options_symbols(self):
        """Fetch ALL actively traded BTC/ETH options symbols from Delta API"""
        try:
            print("🔍 Fetching active options symbols from Delta API...")
            url = "https://api.delta.exchange/v2/products"
            response = requests.get(url, timeout=10)
            
            if response.status_code == 200:
                products = response.json().get('result', [])
                btc_symbols = []
                eth_symbols = []
                
                for product in products:
                    symbol = product.get('symbol', '')
                    contract_type = product.get('contract_type', '')
                    trading_status = product.get('product_trading_status', '')
                    
                    # Filter for BTC/ETH options that are operational
                    if (contract_type in ['call_options', 'put_options', 'call', 'put'] and 
                        trading_status == 'operational'):
                        
                        if symbol.startswith('BTC-') and self.current_expiry in symbol:
                            btc_symbols.append(symbol)
                        elif symbol.startswith('ETH-') and self.current_expiry in symbol:
                            eth_symbols.append(symbol)
                
                # Remove duplicates and sort
                btc_symbols = sorted(list(set(btc_symbols)))
                eth_symbols = sorted(list(set(eth_symbols)))
                
                all_symbols = btc_symbols + eth_symbols
                print(f"✅ Found {len(btc_symbols)} BTC options, {len(eth_symbols)} ETH options")
                print(f"📊 BTC Symbols: {btc_symbols[:5]}...")  # Show first 5
                print(f"📊 ETH Symbols: {eth_symbols[:5]}...")  # Show first 5
                
                return all_symbols
            else:
                print(f"❌ API Error: {response.status_code}")
                return []
                
        except Exception as e:
            print(f"❌ Error fetching symbols: {e}")
            return []
    
    def get_current_expiry(self):
        """Automatically determine current expiry based on 5:30 PM IST cutoff"""
        now = datetime.utcnow()
        
        # Convert to IST (UTC +5:30)
        ist_now = now + timedelta(hours=5, minutes=30)
        
        # Check if past 5:30 PM IST
        if ist_now.hour >= 17 and ist_now.minute >= 30:
            # Use next day
            expiry_date = ist_now + timedelta(days=1)
        else:
            # Use today
            expiry_date = ist_now
        
        # Format as DDMMYY (Delta Exchange format)
        expiry_str = expiry_date.strftime("%d%m%y")
        print(f"🔄 Auto-detected expiry: {expiry_str} (IST: {ist_now.strftime('%Y-%m-%d %H:%M')})")
        return expiry_str
    
    def should_update_expiry(self):
        """Check if we need to update expiry (every hour or after 5:30 PM IST)"""
        global last_expiry_check
        now = time.time()
        
        # Check every hour or if more than 2 hours since last check
        if now - last_expiry_check >= 3600:  # 1 hour
            last_expiry_check = now
            new_expiry = self.get_current_expiry()
            if new_expiry != self.current_expiry:
                print(f"🔄 Expiry changed: {self.current_expiry} -> {new_expiry}")
                self.current_expiry = new_expiry
                return True
        return False
    
    def get_options_symbols(self):
        """Get ALL actively traded BTC/ETH options symbols for current expiry"""
        global active_symbols
        active_symbols = self.fetch_active_options_symbols()
        
        if not active_symbols:
            print("⚠️ No active options found, using default strikes...")
            # Fallback to default strikes if API fails
            return self.get_default_symbols()
        
        return active_symbols
    
    def get_default_symbols(self):
        """Fallback default symbols if API fails"""
        symbols = []
        
        # Default strike prices
        btc_strikes = [55000, 56000, 57000, 58000, 59000, 60000, 61000, 62000, 63000, 64000, 65000, 66000, 67000, 68000, 69000, 70000]
        eth_strikes = [2500, 2600, 2700, 2800, 2900, 3000, 3100, 3200, 3300, 3400, 3500, 3600, 3700, 3800, 3900, 4000]
        
        # Generate BTC options symbols
        for strike in btc_strikes:
            symbols.append(f"BTC-{self.current_expiry}-{strike}-C")
            symbols.append(f"BTC-{self.current_expiry}-{strike}-P")
        
        # Generate ETH options symbols
        for strike in eth_strikes:
            symbols.append(f"ETH-{self.current_expiry}-{strike}-C")
            symbols.append(f"ETH-{self.current_expiry}-{strike}-P")
        
        return symbols
    
    def send_telegram_alert(self, message):
        """Send alert to Telegram"""
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown"
        }
        try:
            requests.post(url, json=payload, timeout=5)
            print(f"✅ Alert sent: {message}")
        except Exception as e:
            print(f"❌ Telegram error: {e}")
    
    def on_message(self, ws, message):
        """Process WebSocket messages - ONLY BTC/ETH options data"""
        try:
            data = json.loads(message)
            
            # Check if this is BTC/ETH options data
            if (isinstance(data, dict) and 'symbol' in data and 
                ('BTC-' in data['symbol'] or 'ETH-' in data['symbol']) and
                ('-C' in data['symbol'] or '-P' in data['symbol'])):
                
                symbol = data['symbol']
                
                # Extract bid/ask prices
                bid_price = float(data.get('best_bid_price', 0)) or float(data.get('bid', 0)) or float(data.get('best_bid', 0))
                ask_price = float(data.get('best_ask_price', 0)) or float(data.get('ask', 0)) or float(data.get('best_ask', 0))
                
                # Only process if we have valid prices
                if bid_price > 0 and ask_price > 0:
                    # Store in appropriate dictionary
                    if 'BTC' in symbol:
                        btc_options[symbol] = {'bid': bid_price, 'ask': ask_price}
                        self.check_arbitrage('BTC', btc_options)
                    elif 'ETH' in symbol:
                        eth_options[symbol] = {'bid': bid_price, 'ask': ask_price}
                        self.check_arbitrage('ETH', eth_options)
                    
        except Exception as e:
            print(f"❌ Message error: {e}")
    
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
    
    def check_arbitrage(self, asset, options_data):
        """Check for arbitrage opportunities"""
        try:
            # Group by strike price
            strikes = {}
            for symbol, prices in options_data.items():
                strike = self.extract_strike(symbol)
                if strike > 0:
                    if strike not in strikes:
                        strikes[strike] = {'call': {}, 'put': {}}
                    
                    if 'C' in symbol:
                        strikes[strike]['call'] = prices
                    elif 'P' in symbol:
                        strikes[strike]['put'] = prices
            
            # Sort strikes
            sorted_strikes = sorted(strikes.keys())
            
            if len(sorted_strikes) < 2:
                return  # Need at least 2 strikes to compare
            
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
                    min_diff = 2 if asset == 'BTC' else 0.16
                    
                    if call_diff < 0 and abs(call_diff) >= min_diff:
                        alert_key = f"{asset}_CALL_{strike1}_{strike2}"
                        if self.can_alert(alert_key):
                            profit = abs(call_diff)
                            alerts.append(
                                f"🔷 CALL {strike1} Ask: ${call1_ask:.2f} vs "
                                f"{strike2} Bid: ${call2_bid:.2f} → "
                                f"Profit: ${profit:.2f}"
                            )
                
                # PUT arbitrage: Sell lower strike, buy higher strike
                put1_bid = strikes[strike1]['put'].get('bid', 0)
                put2_ask = strikes[strike2]['put'].get('ask', 0)
                
                if put1_bid > 0 and put2_ask > 0:
                    put_diff = put2_ask - put1_bid
                    min_diff = 2 if asset == 'BTC' else 0.16
                    
                    if put_diff < 0 and abs(put_diff) >= min_diff:
                        alert_key = f"{asset}_PUT_{strike1}_{strike2}"
                        if self.can_alert(alert_key):
                            profit = abs(put_diff)
                            alerts.append(
                                f"🟣 PUT {strike1} Bid: ${put1_bid:.2f} vs "
                                f"{strike2} Ask: ${put2_ask:.2f} → "
                                f"Profit: ${profit:.2f}"
                            )
            
            # Send alerts if any found
            if alerts:
                message = f"🚨 *{asset} ARBITRAGE ALERTS* 🚨\n\n" + "\n".join(alerts)
                message += f"\n\n_Time: {datetime.now().strftime('%H:%M:%S')}_"
                message += f"\n_Expiry: {self.current_expiry}_"
                message += f"\n_Monitoring: {len(active_symbols)} symbols_"
                self.send_telegram_alert(message)
                print(f"✅ Sent {len(alerts)} {asset} arbitrage alerts")
                
        except Exception as e:
            print(f"❌ Arbitrage check error: {e}")
    
    def can_alert(self, alert_key):
        """Check if we can send alert (1-minute cooldown)"""
        now = time.time()
        last_time = last_alert.get(alert_key, 0)
        
        if now - last_time >= 60:  # 1 minute cooldown
            last_alert[alert_key] = now
            return True
        return False
    
    def on_error(self, ws, error):
        print(f"❌ WebSocket error: {error}")
    
    def on_close(self, ws, close_status_code, close_msg):
        print("🔴 WebSocket closed - reconnecting in 5 seconds...")
        time.sleep(5)
        self.start_websocket()
    
    def on_open(self, ws):
        print("✅ WebSocket connected - fetching active options symbols...")
        self.update_subscription(ws)
    
    def update_subscription(self, ws):
        """Update WebSocket subscription with current expiry symbols"""
        symbols = self.get_options_symbols()
        subscribe_msg = {
            "type": "subscribe",
            "payload": {
                "channels": [
                    {
                        "name": "v2/ticker",
                        "symbols": symbols
                    }
                ]
            }
        }
        ws.send(json.dumps(subscribe_msg))
        print(f"✅ Subscribed to {len(symbols)} actively traded BTC/ETH options")
    
    def start_websocket(self):
        """Start WebSocket connection"""
        websocket.enableTrace(True)
        self.ws = websocket.WebSocketApp(
            "wss://socket.delta.exchange",
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        
        # Check for expiry updates every minute
        def expiry_checker():
            while True:
                if self.should_update_expiry():
                    print("🔄 Expiry updated, reconnecting WebSocket...")
                    if self.ws:
                        self.ws.close()
                time.sleep(60)  # Check every minute
        
        expiry_thread = threading.Thread(target=expiry_checker)
        expiry_thread.daemon = True
        expiry_thread.start()
        
        self.ws.run_forever()
    
    def start(self):
        """Start the bot"""
        # Start WebSocket in background thread
        ws_thread = threading.Thread(target=self.start_websocket)
        ws_thread.daemon = True
        ws_thread.start()

# Create and start bot
bot = ArbitrageBot()

@app.route('/')
def home():
    return f"""
    <h1>✅ Delta Arbitrage Bot Running</h1>
    <p>24/7 Real-time Options Arbitrage Detection</p>
    <p>🔍 Monitoring: ALL Traded BTC & ETH Options</p>
    <p>📅 Current Expiry: {bot.current_expiry}</p>
    <p>📊 Active Symbols: {len(active_symbols)}</p>
    <p>🔔 Alerts: Telegram Instant Notifications</p>
    <p>🔄 Auto-Expiry: Updates after 5:30 PM IST</p>
    <p>⏰ Status: Active</p>
    <p>Last update: {datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")}</p>
    """

@app.route('/health')
def health():
    return f"🟢 Healthy - Expiry: {bot.current_expiry} - Symbols: {len(active_symbols)} - {datetime.now().strftime('%H:%M:%S')}"

@app.route('/ping')
def ping():
    return "🏓 Pong - " + datetime.now().strftime("%H:%M:%S")

# Start the bot when app loads
bot_thread = threading.Thread(target=bot.start)
bot_thread.daemon = True
bot_thread.start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
