import websocket
import json
import threading
import time
import requests
import os
from datetime import datetime, timedelta, timezone
from flask import Flask

app = Flask(__name__)

# Your Telegram credentials
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

print("🚀 Starting Delta Arbitrage Bot...")

class ArbitrageBot:
    def __init__(self):
        self.ws = None
        self.current_expiry = self.get_current_expiry()
        self.active_symbols = []
        self.btc_options = {}
        self.eth_options = {}
        self.last_alert = {}
        
    def get_current_expiry(self):
        """Get current expiry in DDMMYY format"""
        now = datetime.now(timezone.utc)
        ist_now = now + timedelta(hours=5, minutes=30)
        
        # If past 5:30 PM IST, use next day
        if ist_now.hour >= 17 and ist_now.minute >= 30:
            expiry_date = ist_now + timedelta(days=1)
        else:
            expiry_date = ist_now
        
        expiry_str = expiry_date.strftime("%d%m%y")
        print(f"📅 Using expiry: {expiry_str}")
        return expiry_str
    
    def fetch_all_traded_strikes(self):
        """Fetch ALL traded BTC/ETH options strikes from Delta API"""
        try:
            print("🔍 Fetching ALL traded strikes from Delta API...")
            url = "https://api.delta.exchange/v2/products"
            response = requests.get(url, timeout=15)
            
            if response.status_code == 200:
                products = response.json().get('result', [])
                print(f"📦 Found {len(products)} total products")
                
                btc_symbols = []
                eth_symbols = []
                options_count = 0
                
                for product in products:
                    symbol = product.get('symbol', '')
                    contract_type = str(product.get('contract_type', '')).lower()
                    trading_status = product.get('product_trading_status', '')
                    
                    # Check if it's an options contract for current expiry
                    is_option = any(opt in contract_type for opt in ['call', 'put', 'option'])
                    is_current_expiry = self.current_expiry in symbol
                    is_operational = trading_status == 'operational'
                    
                    if is_option and is_current_expiry:
                        options_count += 1
                        if symbol.startswith('BTC-'):
                            btc_symbols.append(symbol)
                        elif symbol.startswith('ETH-'):
                            eth_symbols.append(symbol)
                
                print(f"🎯 Found {options_count} options contracts for expiry {self.current_expiry}")
                print(f"   - BTC: {len(btc_symbols)} symbols")
                print(f"   - ETH: {len(eth_symbols)} symbols")
                
                # Remove duplicates and sort
                btc_symbols = sorted(list(set(btc_symbols)))
                eth_symbols = sorted(list(set(eth_symbols)))
                
                all_symbols = btc_symbols + eth_symbols
                
                # Show strike ranges
                btc_strikes = sorted(list(set([self.extract_strike(sym) for sym in btc_symbols])))
                eth_strikes = sorted(list(set([self.extract_strike(sym) for sym in eth_symbols])))
                
                if btc_strikes:
                    print(f"📊 BTC Strike Range: {btc_strikes[0]:,} to {btc_strikes[-1]:,} ({len(btc_strikes)} strikes)")
                if eth_strikes:
                    print(f"📊 ETH Strike Range: {eth_strikes[0]:,} to {eth_strikes[-1]:,} ({len(eth_strikes)} strikes)")
                
                if not all_symbols:
                    print("⚠️ No options found! Using fallback strikes...")
                    return self.get_fallback_symbols()
                
                return all_symbols
                
            else:
                print(f"❌ API Error: {response.status_code}")
                return self.get_fallback_symbols()
                
        except Exception as e:
            print(f"❌ Error fetching strikes: {e}")
            return self.get_fallback_symbols()
    
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
    
    def get_fallback_symbols(self):
        """Fallback if API fails"""
        print("🔄 Using fallback strikes...")
        symbols = []
        
        # Wider strike range as fallback
        btc_strikes = [55000, 56000, 57000, 58000, 59000, 60000, 61000, 62000, 
                      63000, 64000, 65000, 66000, 67000, 68000, 69000, 70000]
        eth_strikes = [2500, 2600, 2700, 2800, 2900, 3000, 3100, 3200,
                      3300, 3400, 3500, 3600, 3700, 3800, 3900, 4000]
        
        for strike in btc_strikes:
            symbols.append(f"BTC-{self.current_expiry}-{strike}-C")
            symbols.append(f"BTC-{self.current_expiry}-{strike}-P")
        
        for strike in eth_strikes:
            symbols.append(f"ETH-{self.current_expiry}-{strike}-C")
            symbols.append(f"ETH-{self.current_expiry}-{strike}-P")
        
        return symbols
    
    def send_telegram_alert(self, message):
        """Send alert to Telegram"""
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown"
            }
            requests.post(url, json=payload, timeout=5)
            print(f"✅ Alert sent to Telegram")
        except Exception as e:
            print(f"❌ Telegram error: {e}")
    
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
                return
            
            # Check adjacent strikes for arbitrage
            alerts = []
            for i in range(len(sorted_strikes) - 1):
                strike1 = sorted_strikes[i]
                strike2 = sorted_strikes[i + 1]
                
                # CALL arbitrage
                call1_ask = strikes[strike1]['call'].get('ask', 0)
                call2_bid = strikes[strike2]['call'].get('bid', 0)
                
                if call1_ask > 0 and call2_bid > 0:
                    call_diff = call1_ask - call2_bid
                    min_diff = 2 if asset == 'BTC' else 0.16
                    
                    if call_diff < 0 and abs(call_diff) >= min_diff:
                        alert_key = f"{asset}_CALL_{strike1}_{strike2}"
                        if self.can_alert(alert_key):
                            profit = abs(call_diff)
                            alerts.append(f"🔷 CALL {strike1:,} Ask: ${call1_ask:.2f} vs {strike2:,} Bid: ${call2_bid:.2f} → Profit: ${profit:.2f}")
                
                # PUT arbitrage
                put1_bid = strikes[strike1]['put'].get('bid', 0)
                put2_ask = strikes[strike2]['put'].get('ask', 0)
                
                if put1_bid > 0 and put2_ask > 0:
                    put_diff = put2_ask - put1_bid
                    min_diff = 2 if asset == 'BTC' else 0.16
                    
                    if put_diff < 0 and abs(put_diff) >= min_diff:
                        alert_key = f"{asset}_PUT_{strike1}_{strike2}"
                        if self.can_alert(alert_key):
                            profit = abs(put_diff)
                            alerts.append(f"🟣 PUT {strike1:,} Bid: ${put1_bid:.2f} vs {strike2:,} Ask: ${put2_ask:.2f} → Profit: ${profit:.2f}")
            
            if alerts:
                message = f"🚨 *{asset} ARBITRAGE ALERTS* 🚨\n\n" + "\n".join(alerts)
                message += f"\n\n_Time: {datetime.now().strftime('%H:%M:%S')}_"
                message += f"\n_Expiry: {self.current_expiry}_"
                message += f"\n_Monitoring: {len(self.active_symbols)} symbols_"
                self.send_telegram_alert(message)
                print(f"✅ Sent {len(alerts)} {asset} arbitrage alerts")
                
        except Exception as e:
            print(f"❌ Arbitrage error: {e}")
    
    def can_alert(self, alert_key):
        now = time.time()
        last_time = self.last_alert.get(alert_key, 0)
        if now - last_time >= 60:
            self.last_alert[alert_key] = now
            return True
        return False
    
    def on_message(self, ws, message):
        """Process WebSocket messages"""
        try:
            data = json.loads(message)
            
            if isinstance(data, dict) and 'symbol' in data:
                symbol = data['symbol']
                
                # Check if it's BTC/ETH options data
                if ('BTC-' in symbol or 'ETH-' in symbol) and ('-C' in symbol or '-P' in symbol):
                    print(f"📈 Options data: {symbol}")
                    
                    # Get prices
                    bid = float(data.get('best_bid_price', 0)) or float(data.get('bid', 0)) or 0
                    ask = float(data.get('best_ask_price', 0)) or float(data.get('ask', 0)) or 0
                    
                    if bid > 0 and ask > 0:
                        # Store data
                        if 'BTC' in symbol:
                            self.btc_options[symbol] = {'bid': bid, 'ask': ask}
                            self.check_arbitrage('BTC', self.btc_options)
                        elif 'ETH' in symbol:
                            self.eth_options[symbol] = {'bid': bid, 'ask': ask}
                            self.check_arbitrage('ETH', self.eth_options)
                    
        except Exception as e:
            print(f"❌ Message error: {e}")
    
    def on_error(self, ws, error):
        print(f"❌ WebSocket error: {error}")
    
    def on_close(self, ws, close_status_code, close_msg):
        print("🔴 WebSocket closed - reconnecting in 10 seconds...")
        time.sleep(10)
        self.start_websocket()
    
    def on_open(self, ws):
        print("✅ WebSocket connected successfully!")
        
        # Fetch ALL traded strikes
        self.active_symbols = self.fetch_all_traded_strikes()
        
        print(f"📡 Subscribing to {len(self.active_symbols)} symbols...")
        
        subscribe_msg = {
            "type": "subscribe",
            "payload": {
                "channels": [
                    {
                        "name": "v2/ticker", 
                        "symbols": self.active_symbols
                    }
                ]
            }
        }
        
        ws.send(json.dumps(subscribe_msg))
        print("✅ Subscription sent to Delta Exchange!")
        
        # Send connection alert
        conn_msg = f"🔗 *Bot Connected* 🔗\n\n✅ WebSocket connected to Delta Exchange\n📅 Expiry: {self.current_expiry}\n📊 Monitoring: {len(self.active_symbols)} symbols\n\nBot is now live! 🚀"
        self.send_telegram_alert(conn_msg)
    
    def start_websocket(self):
        """Start WebSocket connection"""
        print("🌐 Connecting to Delta WebSocket...")
        
        self.ws = websocket.WebSocketApp(
            "wss://socket.delta.exchange",
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        
        self.ws.run_forever()
    
    def start(self):
        """Start the bot"""
        print("🤖 Starting arbitrage bot...")
        ws_thread = threading.Thread(target=self.start_websocket)
        ws_thread.daemon = True
        ws_thread.start()

# Create and start bot
bot = ArbitrageBot()

@app.route('/')
def home():
    return f"""
    <h1>✅ Delta Arbitrage Bot</h1>
    <p>Status: Running</p>
    <p>Monitoring: {len(bot.active_symbols)} symbols</p>
    <p>Expiry: {bot.current_expiry}</p>
    """

@app.route('/health')
def health():
    return f"🟢 Healthy - Monitoring {len(bot.active_symbols)} symbols"

# Start the bot
print("🎯 Initializing bot...")
bot.start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
