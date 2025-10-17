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

# -------------------------------
# Delta WebSocket Client
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
            print(f"[{datetime.now()}] 🔍 Fetching options symbols...")
            url = "https://api.india.delta.exchange/v2/products"
            response = requests.get(url, timeout=10)
            
            if response.status_code == 200:
                products = response.json().get('result', [])
                symbols = []
                
                for product in products:
                    symbol = product.get('symbol', '')
                    contract_type = str(product.get('contract_type', '')).lower()
                    
                    # Filter for BTC/ETH options for current expiry
                    is_option = any(opt in contract_type for opt in ['call', 'put', 'option'])
                    is_current_expiry = self.current_expiry in symbol
                    
                    if is_option and is_current_expiry:
                        if symbol.startswith(('C-BTC-', 'P-BTC-', 'C-ETH-', 'P-ETH-')):
                            symbols.append(symbol)
                
                # Remove duplicates and sort
                symbols = sorted(list(set(symbols)))
                
                print(f"[{datetime.now()}] ✅ Found {len(symbols)} options symbols")
                
                # Show strike ranges
                btc_strikes = sorted(list(set([self.extract_strike(sym) for sym in symbols if 'BTC' in sym])))
                eth_strikes = sorted(list(set([self.extract_strike(sym) for sym in symbols if 'ETH' in sym])))
                
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

    def decompress_brotli_data(self, compressed_data):
        """Decompress Brotli compressed data"""
        try:
            decoded_data = base64.b64decode(compressed_data)
            decompressed_data = brotli.decompress(decoded_data)
            return json.loads(decompressed_data.decode('utf-8'))
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Decompression error: {e}")
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
        print(f"[{datetime.now()}] 🔴 WebSocket closed - reconnecting in 10s...")
        sleep(10)
        self.connect()

    def on_error(self, ws, error):
        print(f"[{datetime.now()}] ❌ WebSocket error: {error}")

    def on_message(self, ws, message):
        """Handle incoming WebSocket messages"""
        try:
            message_json = json.loads(message)
            message_type = message_json.get('type')
            
            if message_type == 'l1ob_c':
                self.process_bid_ask_data(message_json)
            elif message_type == 'success':
                print(f"[{datetime.now()}] ✅ {message_json.get('message', 'Success')}")
            elif message_type == 'error':
                print(f"[{datetime.now()}] ❌ Error: {message_json}")
                
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Message processing error: {e}")

    def subscribe_to_options(self):
        """Subscribe to all available options"""
        symbols = self.get_all_options_symbols()
        
        if not symbols:
            print(f"[{datetime.now()}] ⚠️ No symbols found, using fallback...")
            symbols = self.get_fallback_symbols()
        
        self.active_symbols = symbols
        
        payload = {
            "type": "subscribe",
            "payload": {
                "channels": [
                    {
                        "name": "l1ob_c",
                        "symbols": symbols
                    }
                ]
            }
        }
        
        self.ws.send(json.dumps(payload))
        print(f"[{datetime.now()}] 📡 Subscribed to {len(symbols)} options symbols")
        
        # Send connection alert
        self.send_telegram(f"🔗 *Bot Connected* 🔗\n\n✅ Connected to Delta Exchange\n📅 Expiry: {self.current_expiry}\n📊 Monitoring: {len(symbols)} symbols\n\nBot is now live! 🚀")

    def get_fallback_symbols(self):
        """Fallback symbols if API fails"""
        symbols = []
        
        # Common strikes around current market
        btc_strikes = [58000, 59000, 60000, 61000, 62000, 63000, 64000, 65000]
        eth_strikes = [2800, 2900, 3000, 3100, 3200, 3300, 3400, 3500]
        
        for strike in btc_strikes:
            symbols.append(f"C-BTC-{self.current_expiry}-{strike}")
            symbols.append(f"P-BTC-{self.current_expiry}-{strike}")
        
        for strike in eth_strikes:
            symbols.append(f"C-ETH-{self.current_expiry}-{strike}")
            symbols.append(f"P-ETH-{self.current_expiry}-{strike}")
        
        return symbols

    def process_bid_ask_data(self, message):
        """Process bid/ask data and check for arbitrage"""
        decompressed_data = self.decompress_brotli_data(message.get('c', ''))
        if not decompressed_data:
            return

        # Update current prices
        btc_options = []
        eth_options = []
        
        for option_data in decompressed_data:
            symbol = option_data['s']
            bid_ask_data = option_data['d']
            
            # Parse: [BestAsk, AskSize, BestBid, BidSize]
            if len(bid_ask_data) >= 4:
                best_ask = float(bid_ask_data[0]) if bid_ask_data[0] else None
                best_bid = float(bid_ask_data[2]) if bid_ask_data[2] else None
                
                if best_bid and best_ask:
                    self.options_prices[symbol] = {'bid': best_bid, 'ask': best_ask}
                    
                    # Separate BTC and ETH options
                    if symbol.startswith('C-BTC-') or symbol.startswith('P-BTC-'):
                        btc_options.append({
                            'symbol': symbol,
                            'bid': best_bid,
                            'ask': best_ask
                        })
                    elif symbol.startswith('C-ETH-') or symbol.startswith('P-ETH-'):
                        eth_options.append({
                            'symbol': symbol,
                            'bid': best_bid,
                            'ask': best_ask
                        })
        
        # Check for arbitrage opportunities
        if btc_options:
            self.check_arbitrage('BTC', btc_options)
        if eth_options:
            self.check_arbitrage('ETH', eth_options)

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
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Telegram error: {e}")

    def connect(self):
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
            try:
                self.connect()
            except Exception as e:
                print(f"[{datetime.now()}] ❌ Bot error: {e}")
                sleep(10)
                self.start()
        
        bot_thread = threading.Thread(target=run_bot)
        bot_thread.daemon = True
        bot_thread.start()

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
    <p>Symbols: {len(bot.options_prices)}</p>
    <p>Active Symbols: {len(bot.active_symbols)}</p>
    <p>Last Update: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    """

@app.route('/health')
def health():
    return {"status": "healthy", "connected": bot.connected, "symbols": len(bot.options_prices)}, 200

# -------------------------------
# Main
# -------------------------------
if __name__ == "__main__":
    print("="*50)
    print("Delta Options Arbitrage Bot")
    print("="*50)
    
    # Start the bot
    bot.start()
    
    # Start Flask app
    port = int(os.environ.get("PORT", 10000))
    print(f"[{datetime.now()}] 🚀 Starting web server on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
