import requests
import json
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
DELTA_THRESHOLD = 2  # BTC threshold set to 2
ALERT_COOLDOWN = 60
FETCH_INTERVAL = 1  # Fetch data every 1 second

# -------------------------------
# BTC Options Bot (Based on Your Working Logic)
# -------------------------------
class BTCOptionsBot:
    def __init__(self):
        self.base_url = "https://api.india.delta.exchange/v2"
        self.last_alert_time = {}
        self.running = True
        self.fetch_count = 0
        self.alert_count = 0
        self.current_expiry = self.get_current_expiry()

    def get_current_expiry(self):
        """Get current date in DDMMYY format (same as your GAS code)"""
        now = datetime.now(timezone.utc)
        ist_now = now + timedelta(hours=5, minutes=30)
        return ist_now.strftime("%d%m%y")

    def fetch_tickers(self):
        """Fetch all tickers in one call (like your working GAS code)"""
        try:
            url = f"{self.base_url}/tickers"
            response = requests.get(url, timeout=5)
            
            if response.status_code == 200:
                data = response.json()
                if data.get('success'):
                    return data.get('result', [])
            return []
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Error fetching tickers: {e}")
            return []

    def parse_expiry_code(self, code):
        """Parse expiry code (same as your GAS function)"""
        if not code:
            return "N/A"
        code = str(code)
        if len(code) == 6 and code.isdigit():
            dd = code[0:2]
            mm = code[2:4]
            yy = code[4:6]
            return f"20{yy}-{mm}-{dd}"
        return code

    def extract_strike_from_symbol(self, symbol):
        """Extract strike price from symbol (like your GAS code)"""
        try:
            parts = symbol.split('-')
            for part in parts:
                if part.isdigit() and len(part) > 2:  # Strike prices are usually > 100
                    return int(part)
            return 0
        except:
            return 0

    def detect_option_type(self, symbol):
        """Detect if option is CALL or PUT (like your GAS logic)"""
        symbol_str = str(symbol).upper()
        
        # Check first part for C or P (like your GAS code)
        parts = symbol_str.split('-')
        if len(parts) > 0:
            first_part = parts[0]
            if first_part.startswith('C'):
                return 'call'
            elif first_part.startswith('P'):
                return 'put'
        
        # Fallback to string search
        if 'C-' in symbol_str or '/C' in symbol_str:
            return 'call'
        elif 'P-' in symbol_str or '/P' in symbol_str:
            return 'put'
        
        return 'unknown'

    def get_bid_ask_prices(self, ticker):
        """Extract bid/ask prices from ticker (like your GAS code)"""
        try:
            # Try multiple possible fields (like your GAS code)
            quotes = ticker.get('quotes', {})
            
            bid = (ticker.get('best_bid_price') or 
                  ticker.get('best_bid') or 
                  quotes.get('best_bid') or 
                  quotes.get('bid_price') or 
                  ticker.get('bid'))
            
            ask = (ticker.get('best_ask_price') or 
                  ticker.get('best_ask') or 
                  quotes.get('best_ask') or 
                  quotes.get('ask_price') or 
                  ticker.get('ask'))
            
            mark = ticker.get('mark_price') or ticker.get('last_price') or ticker.get('close')
            
            # Convert to float, return 0 if invalid
            bid_price = float(bid) if bid and str(bid).replace('.', '').isdigit() else 0
            ask_price = float(ask) if ask and str(ask).replace('.', '').isdigit() else 0
            mark_price = float(mark) if mark and str(mark).replace('.', '').isdigit() else 0
            
            return bid_price, ask_price, mark_price
            
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Error parsing prices: {e}")
            return 0, 0, 0

    def process_btc_options(self):
        """Process BTC options data (main logic similar to your GAS code)"""
        tickers = self.fetch_tickers()
        if not tickers:
            return {}
        
        # Group by strike (like your GAS code)
        grouped = {}
        
        for ticker in tickers:
            symbol = ticker.get('symbol', '')
            
            # Filter for BTC options with current expiry
            if 'BTC' in symbol.upper():
                parts = symbol.split('-')
                if len(parts) >= 4:
                    expiry_raw = parts[-1]
                    expiry = self.parse_expiry_code(expiry_raw)
                    
                    # Only process current expiry options
                    if expiry_raw == self.current_expiry:
                        strike = self.extract_strike_from_symbol(symbol)
                        option_type = self.detect_option_type(symbol)
                        
                        if strike > 0 and option_type in ['call', 'put']:
                            if strike not in grouped:
                                grouped[strike] = {
                                    'strike': strike,
                                    'expiry': expiry,
                                    'call': {'bid': 0, 'ask': 0, 'mark': 0},
                                    'put': {'bid': 0, 'ask': 0, 'mark': 0}
                                }
                            
                            bid, ask, mark = self.get_bid_ask_prices(ticker)
                            
                            if option_type == 'call':
                                grouped[strike]['call']['bid'] = bid
                                grouped[strike]['call']['ask'] = ask
                                grouped[strike]['call']['mark'] = mark
                            else:  # put
                                grouped[strike]['put']['bid'] = bid
                                grouped[strike]['put']['ask'] = ask
                                grouped[strike]['put']['mark'] = mark
        
        return grouped

    def check_arbitrage_opportunities(self, grouped_data):
        """Check for arbitrage (same logic as your working GAS code)"""
        if not grouped_data:
            return []
        
        strikes = sorted(grouped_data.keys())
        alerts = []
        
        for i in range(len(strikes) - 1):
            strike1 = strikes[i]
            strike2 = strikes[i + 1]
            
            g1 = grouped_data[strike1]
            g2 = grouped_data[strike2]
            
            # CALL arbitrage: call1_ask - call2_bid
            call1_ask = g1['call']['ask']
            call2_bid = g2['call']['bid']
            
            if call1_ask > 0 and call2_bid > 0:
                call_diff = call1_ask - call2_bid
                if call_diff < 0 and abs(call_diff) >= DELTA_THRESHOLD:
                    alert_key = f"BTC_CALL_{strike1}_{strike2}"
                    if self.can_alert(alert_key):
                        profit = abs(call_diff)
                        alerts.append(f"🔷 BTC CALL {strike1:,} Ask: ${call1_ask:.2f} vs {strike2:,} Bid: ${call2_bid:.2f} → Profit: ${profit:.2f}")
            
            # PUT arbitrage: put2_ask - put1_bid
            put1_bid = g1['put']['bid']
            put2_ask = g2['put']['ask']
            
            if put1_bid > 0 and put2_ask > 0:
                put_diff = put2_ask - put1_bid
                if put_diff < 0 and abs(put_diff) >= DELTA_THRESHOLD:
                    alert_key = f"BTC_PUT_{strike1}_{strike2}"
                    if self.can_alert(alert_key):
                        profit = abs(put_diff)
                        alerts.append(f"🟣 BTC PUT {strike1:,} Bid: ${put1_bid:.2f} vs {strike2:,} Ask: ${put2_ask:.2f} → Profit: ${profit:.2f}")
        
        return alerts

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
            print(f"[{datetime.now()}] 📱 Telegram not configured, would send: {message}")
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

    def start_monitoring(self):
        """Start monitoring BTC options"""
        print(f"[{datetime.now()}] 🤖 Starting BTC Options Bot")
        print(f"[{datetime.now()}] 📅 Current expiry: {self.current_expiry}")
        print(f"[{datetime.now()}] ⚡ BTC Threshold: ${DELTA_THRESHOLD}")
        
        # Send startup notification
        self.send_telegram(f"🔗 *BTC Bot Started*\n\n📅 Monitoring expiry: {self.current_expiry}\n⚡ Threshold: ${DELTA_THRESHOLD}\n⏰ Started at: {datetime.now().strftime('%H:%M:%S IST')}")
        
        while self.running:
            try:
                # Fetch and process data
                grouped_data = self.process_btc_options()
                self.fetch_count += 1
                
                # Check for arbitrage
                alerts = self.check_arbitrage_opportunities(grouped_data)
                
                if alerts:
                    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
                    current_time_ist = ist_now.strftime("%H:%M:%S")
                    
                    message = f"🚨 *BTC {self.current_expiry} ARBITRAGE ALERTS* 🚨\n\n" + "\n".join(alerts)
                    message += f"\n\n_Expiry: {self.current_expiry}_"
                    message += f"\n_Time: {current_time_ist} IST_"
                    message += f"\n_Threshold: ${DELTA_THRESHOLD}_"
                    
                    self.send_telegram(message)
                    self.alert_count += len(alerts)
                    print(f"[{datetime.now()}] ✅ Sent {len(alerts)} BTC arbitrage alerts")
                
                # Log progress
                if self.fetch_count % 30 == 0:
                    strike_count = len(grouped_data)
                    print(f"[{datetime.now()}] 🔄 Fetched {self.fetch_count} times | Strikes: {strike_count} | Alerts: {self.alert_count}")
                
                sleep(FETCH_INTERVAL)
                
            except Exception as e:
                print(f"[{datetime.now()}] ❌ Error in main loop: {e}")
                sleep(1)

    def stop(self):
        """Stop the bot"""
        self.running = False
        print(f"[{datetime.now()}] 🛑 BTC Bot stopped")

# -------------------------------
# Flask Routes
# -------------------------------
bot = BTCOptionsBot()

@app.route('/')
def home():
    status = "✅ Running" if bot.running else "🔴 Stopped"
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")
    
    return f"""
    <h1>BTC Options Arbitrage Bot</h1>
    <p>Status: {status}</p>
    <p>API Fetches: {bot.fetch_count}</p>
    <p>BTC Alerts Sent: {bot.alert_count}</p>
    <p>Current Expiry: {bot.current_expiry}</p>
    <p>BTC Threshold: ${DELTA_THRESHOLD}</p>
    <p>Last Update: {current_time}</p>
    <p><a href="/health">Health Check</a></p>
    """

@app.route('/health')
def health():
    return {
        "status": "healthy", 
        "bot_running": bot.running, 
        "api_fetches": bot.fetch_count,
        "btc_alerts_sent": bot.alert_count,
        "current_expiry": bot.current_expiry,
        "btc_threshold": DELTA_THRESHOLD
    }, 200

@app.route('/start')
def start_bot():
    if not bot.running:
        bot.running = True
        bot_thread = threading.Thread(target=bot.start_monitoring)
        bot_thread.daemon = True
        bot_thread.start()
        return "Bot started", 200
    return "Bot already running", 200

@app.route('/stop')
def stop_bot():
    bot.stop()
    return "Bot stopped", 200

@app.route('/ping')
def ping():
    return "pong", 200

# -------------------------------
# Start Bot
# -------------------------------
if __name__ == "__main__":
    print("="*50)
    print("BTC Options Arbitrage Bot")
    print("="*50)
    print(f"⚡ BTC Threshold: ${DELTA_THRESHOLD}")
    print(f"📅 Current expiry: {bot.current_expiry}")
    print("="*50)
    
    # Start the bot
    bot_thread = threading.Thread(target=bot.start_monitoring)
    bot_thread.daemon = True
    bot_thread.start()
    
    # Start Flask app
    port = int(os.environ.get("PORT", 10000))
    print(f"[{datetime.now()}] 🚀 Starting web server on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
