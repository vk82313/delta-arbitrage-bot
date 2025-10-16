import websocket
import json
import threading
import time
import requests
import os
from datetime import datetime
from flask import Flask

app = Flask(__name__)

# Your Telegram credentials
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# Store options data
btc_options = {}
eth_options = {}
last_alert = {}

class ArbitrageBot:
    def __init__(self):
        self.ws = None
        print("🚀 Starting Delta Arbitrage Bot...")
        
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
        """Process WebSocket messages"""
        try:
            data = json.loads(message)
            
            # Check if this is options data (contains C- or P- in symbol)
            if isinstance(data, dict) and 'symbol' in data:
                symbol = data['symbol']
                
                if 'C-' in symbol or 'P-' in symbol:
                    # Extract bid/ask prices from different possible fields
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
                        else:
                            print(f"📊 Received data: {symbol} - Bid: {bid_price}, Ask: {ask_price}")
                    
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
        print("✅ WebSocket connected - subscribing to ALL options data...")
        # Subscribe to ALL symbols using "all" keyword
        subscribe_msg = {
            "type": "subscribe",
            "payload": {
                "channels": [
                    {
                        "name": "v2/ticker",
                        "symbols": ["all"]
                    }
                ]
            }
        }
        ws.send(json.dumps(subscribe_msg))
        print("✅ Subscribed to ALL market data")
    
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
    return """
    <h1>✅ Delta Arbitrage Bot Running</h1>
    <p>24/7 Real-time Options Arbitrage Detection</p>
    <p>🔍 Monitoring: BTC & ETH Options</p>
    <p>🔔 Alerts: Telegram Instant Notifications</p>
    <p>⏰ Status: Active</p>
    <p>Last update: """ + datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC") + """</p>
    """

@app.route('/health')
def health():
    return "🟢 Healthy - " + datetime.now().strftime("%H:%M:%S")

@app.route('/ping')
def ping():
    return "🏓 Pong - " + datetime.now().strftime("%H:%M:%S")

# Start the bot when app loads
bot_thread = threading.Thread(target=bot.start)
bot_thread.daemon = True
bot_thread.start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
