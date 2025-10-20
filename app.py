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
EXPIRY_CHECK_INTERVAL = 60  # Check every 1 minute for expiry rollover

# -------------------------------
# BTC Options Arbitrage Bot (API Only - Fixed)
# -------------------------------
class BTCOptionsArbitrageBot:
    def __init__(self):
        self.base_url = "https://api.india.delta.exchange/v2"
        self.last_alert_time = {}
        self.options_prices = {}
        self.running = True
        self.current_expiry = self.get_current_expiry()
        self.active_expiry = self.get_initial_active_expiry()
        self.fetch_count = 0
        self.alert_count = 0
        self.expiry_rollover_count = 0
        self.last_expiry_check = 0

    def get_current_expiry(self):
        """Get current date in DDMMYY format"""
        now = datetime.now(timezone.utc)
        ist_now = now + timedelta(hours=5, minutes=30)
        return ist_now.strftime("%d%m%y")

    def get_initial_active_expiry(self):
        """Determine which expiry should be active right now"""
        now = datetime.now(timezone.utc)
        ist_now = now + timedelta(hours=5, minutes=30)
        
        # If it's after 5:30 PM IST, we should already be on next day's expiry
        if ist_now.hour >= 17 and ist_now.minute >= 30:
            next_day = ist_now + timedelta(days=1)
            next_expiry = next_day.strftime("%d%m%y")
            print(f"[{datetime.now()}] 🕠 After 5:30 PM IST, starting with next expiry: {next_expiry}")
            return next_expiry
        else:
            print(f"[{datetime.now()}] 📅 Starting with today's expiry: {self.current_expiry}")
            return self.current_expiry

    def should_rollover_expiry(self):
        """Check if we should move to next expiry"""
        now = datetime.now(timezone.utc)
        ist_now = now + timedelta(hours=5, minutes=30)
        
        # After 5:30 PM IST, move to next day's expiry
        if ist_now.hour >= 17 and ist_now.minute >= 30:
            next_expiry = (ist_now + timedelta(days=1)).strftime("%d%m%y")
            return next_expiry
        return None

    def get_available_expiries(self):
        """Get all available BTC expiries from the API - FIXED"""
        try:
            # Use the correct endpoint and parameters
            url = f"{self.base_url}/tickers"
            params = {
                'contract_types': 'call_options,put_options',
                'underlying_asset_symbols': 'BTC'  # Required for BTC options
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
            print(f"[{datetime.now()}] ❌ Error fetching BTC expiries: {e}")
            return []

    def get_next_available_expiry(self, current_expiry):
        """Get the next available expiry after current one"""
        available_expiries = self.get_available_expiries()
        if not available_expiries:
            return current_expiry
        
        print(f"[{datetime.now()}] 📊 Available BTC expiries: {available_expiries}")
        
        # Find the first expiry that is > current expiry
        for expiry in available_expiries:
            if expiry > current_expiry:
                return expiry
        
        # If no future expiry found, return the last available one
        return available_expiries[-1]

    def check_and_update_expiry(self):
        """Check if we need to update the active expiry"""
        current_time = datetime.now().timestamp()
        if current_time - self.last_expiry_check >= EXPIRY_CHECK_INTERVAL:
            self.last_expiry_check = current_time
            
            # Get current time in IST
            now = datetime.now(timezone.utc)
            ist_now = now + timedelta(hours=5, minutes=30)
            current_time_ist = ist_now.strftime("%H:%M:%S")
            
            print(f"[{datetime.now()}] 🔄 Checking BTC expiry rollover... (Current: {self.active_expiry}, Time: {current_time_ist} IST)")
            
            # Check if we should rollover to next expiry
            next_expiry = self.should_rollover_expiry()
            if next_expiry and next_expiry != self.active_expiry:
                print(f"[{datetime.now()}] 🎯 BTC EXPIRY ROLLOVER TRIGGERED!")
                print(f"[{datetime.now()}] 📅 Changing from {self.active_expiry} to {next_expiry}")
                
                # Get the actual next available expiry from API
                actual_next_expiry = self.get_next_available_expiry(self.active_expiry)
                
                if actual_next_expiry != self.active_expiry:
                    self.active_expiry = actual_next_expiry
                    self.expiry_rollover_count += 1
                    
                    # Reset data for new expiry
                    self.options_prices = {}
                    
                    # Send Telegram notification
                    self.send_telegram(f"🔄 *BTC Expiry Rollover Complete!*\n\n📅 Now monitoring: {self.active_expiry}\n⏰ Time: {current_time_ist} IST\n\nBot automatically switched to new expiry! ✅")
                    return True
                else:
                    print(f"[{datetime.now()}] ⚠️ No new BTC expiry available yet, keeping: {self.active_expiry}")
            
            # Also check if current expiry is still available
            available_expiries = self.get_available_expiries()
            if available_expiries and self.active_expiry not in available_expiries:
                print(f"[{datetime.now()}] ⚠️ Current BTC expiry {self.active_expiry} no longer available!")
                next_available = self.get_next_available_expiry(self.active_expiry)
                if next_available != self.active_expiry:
                    print(f"[{datetime.now()}] 🔄 Switching to available BTC expiry: {next_available}")
                    self.active_expiry = next_available
                    self.expiry_rollover_count += 1
                    
                    # Reset data
                    self.options_prices = {}
                    
                    self.send_telegram(f"🔄 *BTC Expiry Update*\n\n📅 Now monitoring: {self.active_expiry}\n⏰ Time: {current_time_ist} IST\n\nPrevious expiry no longer available! ✅")
                    return True
        
        return False

    def extract_expiry_from_symbol(self, symbol):
        """Extract expiry date from symbol string"""
        try:
            parts = symbol.split('-')
            if len(parts) >= 4:
                return parts[3]  # Format: C-BTC-STRIKE-EXPIRY or P-BTC-STRIKE-EXPIRY
            return None
        except:
            return None

    def extract_strike(self, symbol):
        """Extract strike price from symbol"""
        try:
            parts = symbol.split('-')
            if len(parts) >= 4:
                return int(parts[2])  # Strike is the third part
            return 0
        except:
            return 0

    def fetch_btc_options_data(self):
        """Fetch BTC options data from API every second - FIXED ENDPOINT"""
        try:
            # Use the correct endpoint with proper parameters
            url = f"{self.base_url}/tickers"
            params = {
                'contract_types': 'call_options,put_options',
                'underlying_asset_symbols': 'BTC'  # Required parameter for BTC options
            }
            
            response = requests.get(url, params=params, timeout=5)
            
            if response.status_code == 200:
                data = response.json()
                if data.get('success'):
                    tickers = data.get('result', [])
                    
                    # Process all BTC options (we'll filter by expiry in process_tickers_data)
                    self.process_tickers_data(tickers)
                    self.fetch_count += 1
                    
                    if self.fetch_count % 30 == 0:  # Log every 30 fetches
                        print(f"[{datetime.now()}] 🔄 Fetched BTC data {self.fetch_count} times, tracking {len(self.options_prices)} {self.active_expiry} symbols")
                    
                    return True
                else:
                    print(f"[{datetime.now()}] ❌ API response not successful: {data}")
                    return False
            else:
                print(f"[{datetime.now()}] ❌ API Error: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Error fetching BTC data: {e}")
            return False

    def process_tickers_data(self, tickers):
        """Process tickers data and update options prices - FIXED"""
        try:
            current_prices = {}
            
            for ticker in tickers:
                symbol = ticker.get('symbol', '')
                
                # Only process BTC options with active expiry
                if 'BTC' in symbol:
                    symbol_expiry = self.extract_expiry_from_symbol(symbol)
                    if symbol_expiry == self.active_expiry:
                        mark_price = ticker.get('mark_price')
                        
                        # Use mark_price as reference, fallback to bid/ask
                        if mark_price and float(mark_price) > 0:
                            current_prices[symbol] = {
                                'bid': float(ticker.get('quotes', {}).get('best_bid', 0)) or float(mark_price) * 0.99,
                                'ask': float(ticker.get('quotes', {}).get('best_ask', 0)) or float(mark_price) * 1.01,
                                'mark_price': float(mark_price)
                            }
                        else:
                            # If no mark price, use bid/ask directly
                            bid_price = float(ticker.get('quotes', {}).get('best_bid', 0))
                            ask_price = float(ticker.get('quotes', {}).get('best_ask', 0))
                            if bid_price > 0 and ask_price > 0:
                                current_prices[symbol] = {
                                    'bid': bid_price,
                                    'ask': ask_price,
                                    'mark_price': (bid_price + ask_price) / 2
                                }
            
            # Update options prices
            self.options_prices = current_prices
            
            # Check for arbitrage opportunities
            self.check_arbitrage_opportunities()
            
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Error processing BTC tickers data: {e}")

    def check_arbitrage_opportunities(self):
        """Check for arbitrage opportunities - BTC ONLY"""
        if len(self.options_prices) < 10:
            return
            
        btc_options = []
        
        for symbol, prices in self.options_prices.items():
            # Only process BTC symbols with active expiry
            if 'BTC' in symbol:
                option_data = {
                    'symbol': symbol,
                    'bid': prices['bid'],
                    'ask': prices['ask']
                }
                btc_options.append(option_data)
        
        # Check arbitrage for BTC only
        if btc_options:
            self.check_arbitrage_same_expiry(btc_options)

    def check_arbitrage_same_expiry(self, options):
        """Check for arbitrage opportunities within ACTIVE expiry - BTC ONLY"""
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
            
            # CALL arbitrage: Buy lower strike call, sell higher strike call
            call1_ask = strikes[strike1]['call'].get('ask', 0)
            call2_bid = strikes[strike2]['call'].get('bid', 0)
            
            if call1_ask > 0 and call2_bid > 0:
                call_diff = call1_ask - call2_bid
                if call_diff < 0 and abs(call_diff) >= DELTA_THRESHOLD:
                    alert_key = f"BTC_CALL_{strike1}_{strike2}_{self.active_expiry}"
                    if self.can_alert(alert_key):
                        profit = abs(call_diff)
                        alerts.append(f"🔷 BTC CALL {strike1:,} Ask: ${call1_ask:.2f} vs {strike2:,} Bid: ${call2_bid:.2f} → Profit: ${profit:.2f}")
            
            # PUT arbitrage: Buy higher strike put, sell lower strike put
            put1_bid = strikes[strike1]['put'].get('bid', 0)
            put2_ask = strikes[strike2]['put'].get('ask', 0)
            
            if put1_bid > 0 and put2_ask > 0:
                put_diff = put2_ask - put1_bid
                if put_diff < 0 and abs(put_diff) >= DELTA_THRESHOLD:
                    alert_key = f"BTC_PUT_{strike1}_{strike2}_{self.active_expiry}"
                    if self.can_alert(alert_key):
                        profit = abs(put_diff)
                        alerts.append(f"🟣 BTC PUT {strike1:,} Bid: ${put1_bid:.2f} vs {strike2:,} Ask: ${put2_ask:.2f} → Profit: ${profit:.2f}")
        
        if alerts:
            ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
            current_time_ist = ist_now.strftime("%H:%M:%S")
            
            message = f"🚨 *BTC {self.active_expiry} ARBITRAGE ALERTS* 🚨\n\n" + "\n".join(alerts)
            message += f"\n\n_Expiry: {self.active_expiry}_"
            message += f"\n_Time: {current_time_ist} IST_"
            message += f"\n_Threshold: ${DELTA_THRESHOLD}_"
            self.send_telegram(message)
            self.alert_count += len(alerts)
            print(f"[{datetime.now()}] ✅ Sent {len(alerts)} BTC arbitrage alerts for {self.active_expiry}")

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
                print(f"[{datetime.now()}] ❌ Telegram error {resp.status_code}: {resp.text}")
        except Exception as e:
            print(f"[{datetime.now()}] ❌ Telegram error: {e}")

    def start_fetching(self):
        """Start fetching BTC data every second"""
        print(f"[{datetime.now()}] 🤖 Starting BTC Options Arbitrage Bot (API Mode)...")
        print(f"[{datetime.now()}] 📅 Active BTC expiry: {self.active_expiry}")
        print(f"[{datetime.now()}] ⚡ BTC Threshold: ${DELTA_THRESHOLD}")
        
        # Send startup notification
        ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
        current_time_ist = ist_now.strftime("%H:%M:%S IST")
        self.send_telegram(f"🔗 *BTC Bot Started (API Mode)*\n\n📅 Monitoring: {self.active_expiry}\n⚡ Threshold: ${DELTA_THRESHOLD}\n⏰ Time: {current_time_ist}\n\nBTC Bot is now live! 🚀")
        
        while self.running:
            try:
                # Check expiry rollover
                self.check_and_update_expiry()
                
                # Fetch BTC options data
                success = self.fetch_btc_options_data()
                
                if not success:
                    print(f"[{datetime.now()}] ⚠️ Failed to fetch BTC data, retrying...")
                
                # Wait for 1 second before next fetch
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
bot = BTCOptionsArbitrageBot()

@app.route('/')
def home():
    status = "✅ Running" if bot.running else "🔴 Stopped"
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    current_time_ist = ist_now.strftime("%Y-%m-%d %H:%M:%S IST")
    
    calls_count = len([s for s in bot.options_prices.keys() if 'C-' in s])
    puts_count = len([s for s in bot.options_prices.keys() if 'P-' in s])
    
    return f"""
    <h1>BTC Options Arbitrage Bot (API Mode)</h1>
    <p>Status: {status}</p>
    <p>API Fetches: {bot.fetch_count}</p>
    <p>BTC Symbols Tracked: {len(bot.options_prices)} (Calls: {calls_count}, Puts: {puts_count})</p>
    <p>Active BTC Expiry: {bot.active_expiry}</p>
    <p>BTC Alerts Sent: {bot.alert_count}</p>
    <p>BTC Expiry Rollovers: {bot.expiry_rollover_count}</p>
    <p>BTC Threshold: ${DELTA_THRESHOLD}</p>
    <p>Last Update: {current_time_ist}</p>
    <p><a href="/health">Health Check</a> | <a href="/debug">Debug Info</a></p>
    """

@app.route('/health')
def health():
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    current_time_ist = ist_now.strftime("%Y-%m-%d %H:%M:%S IST")
    
    calls_count = len([s for s in bot.options_prices.keys() if 'C-' in s])
    puts_count = len([s for s in bot.options_prices.keys() if 'P-' in s])
    
    return {
        "status": "healthy", 
        "bot_running": bot.running, 
        "api_fetches": bot.fetch_count,
        "btc_symbols_tracked": len(bot.options_prices),
        "btc_calls_tracked": calls_count,
        "btc_puts_tracked": puts_count,
        "active_btc_expiry": bot.active_expiry,
        "btc_alerts_sent": bot.alert_count,
        "btc_expiry_rollovers": bot.expiry_rollover_count,
        "btc_threshold": DELTA_THRESHOLD,
        "current_time_ist": current_time_ist
    }, 200

@app.route('/debug')
def debug():
    """Debug endpoint"""
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    current_time_ist = ist_now.strftime("%Y-%m-%d %H:%M:%S IST")
    
    available_expiries = bot.get_available_expiries()
    sample_prices = dict(list(bot.options_prices.items())[:5])
    
    return {
        "bot_running": bot.running,
        "api_fetches": bot.fetch_count,
        "btc_symbols_tracked": len(bot.options_prices),
        "active_btc_expiry": bot.active_expiry,
        "available_btc_expiries": available_expiries,
        "btc_expiry_rollovers": bot.expiry_rollover_count,
        "btc_alerts_sent": bot.alert_count,
        "btc_threshold": DELTA_THRESHOLD,
        "current_time_ist": current_time_ist,
        "sample_btc_prices": sample_prices
    }

@app.route('/start')
def start_bot():
    if not bot.running:
        bot.running = True
        bot_thread = threading.Thread(target=bot.start_fetching)
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
def start_bot():
    print("="*60)
    print("BTC Options Arbitrage Bot - API Mode (FIXED)")
    print("="*60)
    print(f"⚡ BTC Threshold: ${DELTA_THRESHOLD}")
    print(f"📅 Starting BTC expiry: {bot.active_expiry}")
    print(f"🔄 Fetch interval: {FETCH_INTERVAL} second")
    print("="*60)
    
    bot_thread = threading.Thread(target=bot.start_fetching)
    bot_thread.daemon = True
    bot_thread.start()
    print(f"[{datetime.now()}] ✅ BTC Bot thread started")

if __name__ == "__main__":
    start_bot()
    sleep(2)
    
    port = int(os.environ.get("PORT", 10000))
    print(f"[{datetime.now()}] 🚀 Starting web server on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
