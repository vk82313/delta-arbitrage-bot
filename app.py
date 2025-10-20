import requests
import json
import os
from datetime import datetime, timedelta, timezone
from time import sleep
from flask import Flask
import threading

app = Flask(__name__)

# Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DELTA_THRESHOLD = 2
FETCH_INTERVAL = 1

class BTCOptionsBot:
    def __init__(self):
        self.base_url = "https://api.india.delta.exchange/v2"
        self.last_alert_time = {}
        self.running = True
        self.fetch_count = 0
        self.alert_count = 0
        self.current_expiry = self.get_current_expiry()
        self.last_debug_log = 0

    def get_current_expiry(self):
        now = datetime.now(timezone.utc)
        ist_now = now + timedelta(hours=5, minutes=30)
        return ist_now.strftime("%d%m%y")

    def debug_log(self, message, force=False):
        """Debug logging with rate limiting"""
        current_time = datetime.now().timestamp()
        if force or current_time - self.last_debug_log >= 10:  # Log every 10 seconds max
            print(f"[{datetime.now()}] {message}")
            self.last_debug_log = current_time

    def fetch_tickers(self):
        """Fetch all tickers with detailed error handling"""
        try:
            self.debug_log("🔄 Fetching tickers from API...")
            url = f"{self.base_url}/tickers"
            response = requests.get(url, timeout=10)
            
            self.debug_log(f"📡 API Response Status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                if data.get('success'):
                    tickers = data.get('result', [])
                    self.debug_log(f"✅ Got {len(tickers)} tickers")
                    return tickers
                else:
                    self.debug_log(f"❌ API success=False: {data}")
            else:
                self.debug_log(f"❌ HTTP Error: {response.status_code} - {response.text}")
                
        except Exception as e:
            self.debug_log(f"❌ Exception fetching tickers: {e}")
        
        return []

    def process_btc_options(self):
        """Process BTC options with detailed logging"""
        tickers = self.fetch_tickers()
        if not tickers:
            self.debug_log("❌ No tickers received")
            return {}

        btc_tickers = [t for t in tickers if 'BTC' in str(t.get('symbol', '')).upper()]
        self.debug_log(f"🔍 Found {len(btc_tickers)} BTC tickers")
        
        current_expiry_tickers = []
        for ticker in btc_tickers:
            symbol = ticker.get('symbol', '')
            parts = symbol.split('-')
            if len(parts) >= 4:
                expiry = parts[-1]
                if expiry == self.current_expiry:
                    current_expiry_tickers.append(ticker)

        self.debug_log(f"📅 Found {len(current_expiry_tickers)} tickers for expiry {self.current_expiry}")
        
        # Show sample symbols
        if current_expiry_tickers and self.fetch_count % 10 == 0:
            sample_symbols = [t.get('symbol', '') for t in current_expiry_tickers[:3]]
            self.debug_log(f"📋 Sample symbols: {sample_symbols}")
        
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
        
        self.debug_log(f"💰 Grouped {len(grouped)} strikes with valid prices")
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
                if call_diff < 0 and abs(call_diff) >= DELTA_THRESHOLD:
                    alert_key = f"BTC_CALL_{strike1}_{strike2}"
                    if self.can_alert(alert_key):
                        profit = abs(call_diff)
                        alerts.append(f"🔷 BTC CALL {strike1:,} Ask: ${call1_ask:.2f} vs {strike2:,} Bid: ${call2_bid:.2f} → Profit: ${profit:.2f}")
            
            # PUT arbitrage
            put1_bid = grouped_data[strike1]['put']['bid']
            put2_ask = grouped_data[strike2]['put']['ask']
            
            if put1_bid > 0 and put2_ask > 0:
                put_diff = put2_ask - put1_bid
                if put_diff < 0 and abs(put_diff) >= DELTA_THRESHOLD:
                    alert_key = f"BTC_PUT_{strike1}_{strike2}"
                    if self.can_alert(alert_key):
                        profit = abs(put_diff)
                        alerts.append(f"🟣 BTC PUT {strike1:,} Bid: ${put1_bid:.2f} vs {strike2:,} Ask: ${put2_ask:.2f} → Profit: ${profit:.2f}")
        
        return alerts

    def can_alert(self, alert_key):
        now = datetime.now().timestamp()
        last_time = self.last_alert_time.get(alert_key, 0)
        if now - last_time >= 60:  # 60 second cooldown
            self.last_alert_time[alert_key] = now
            return True
        return False

    def send_telegram(self, message):
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            self.debug_log(f"📱 Telegram not configured: {message}")
            return
            
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(url, data={
                "chat_id": TELEGRAM_CHAT_ID, 
                "text": message, 
                "parse_mode": "Markdown"
            }, timeout=10)
            self.debug_log("📱 Telegram alert sent")
        except Exception as e:
            self.debug_log(f"❌ Telegram error: {e}")

    def start_monitoring(self):
        self.debug_log("🤖 Starting BTC Options Monitoring", force=True)
        
        while self.running:
            try:
                self.fetch_count += 1
                
                # Process data
                grouped_data = self.process_btc_options()
                
                # Check arbitrage
                alerts = self.check_arbitrage(grouped_data)
                
                if alerts:
                    message = f"🚨 *BTC {self.current_expiry} ARBITRAGE ALERTS* 🚨\n\n" + "\n".join(alerts)
                    message += f"\n\n_Time: {datetime.now().strftime('%H:%M:%S')}_"
                    self.send_telegram(message)
                    self.alert_count += len(alerts)
                    self.debug_log(f"✅ Sent {len(alerts)} alerts")
                
                # Progress update
                if self.fetch_count % 30 == 0:
                    self.debug_log(f"📊 Stats: Fetches={self.fetch_count}, Alerts={self.alert_count}, Strikes={len(grouped_data)}")
                
                sleep(FETCH_INTERVAL)
                
            except Exception as e:
                self.debug_log(f"❌ Main loop error: {e}")
                sleep(1)

    def stop(self):
        self.running = False

# Flask app
bot = BTCOptionsBot()

@app.route('/')
def home():
    return f"""
    <h1>BTC Options Arbitrage Bot</h1>
    <p>Status: {'✅ Running' if bot.running else '🔴 Stopped'}</p>
    <p>Fetches: {bot.fetch_count}</p>
    <p>Alerts: {bot.alert_count}</p>
    <p>Expiry: {bot.current_expiry}</p>
    <p>Threshold: ${DELTA_THRESHOLD}</p>
    <p><a href="/debug">Debug Info</a></p>
    """

@app.route('/debug')
def debug():
    return {
        "status": "running" if bot.running else "stopped",
        "fetch_count": bot.fetch_count,
        "alert_count": bot.alert_count,
        "current_expiry": bot.current_expiry,
        "threshold": DELTA_THRESHOLD,
        "timestamp": datetime.now().isoformat()
    }

@app.route('/start')
def start_bot():
    if not bot.running:
        bot.running = True
        threading.Thread(target=bot.start_monitoring, daemon=True).start()
        return "Bot started"
    return "Bot already running"

@app.route('/stop')
def stop_bot():
    bot.stop()
    return "Bot stopped"

if __name__ == "__main__":
    print("🚀 Starting BTC Options Bot with Debug Logging")
    bot_thread = threading.Thread(target=bot.start_monitoring, daemon=True)
    bot_thread.start()
    
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
