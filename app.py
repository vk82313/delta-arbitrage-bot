import websocket
import json
import requests
import os
from datetime import datetime, timedelta, timezone
from time import sleep
from flask import Flask, request, render_template_string, redirect
import threading
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional
import time as time_module

# Initialize Flask app
app = Flask(__name__)

# -------------------------------
# Configuration & Global State
# -------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Global thresholds for arbitrage system
DELTA_THRESHOLD = {"ETH": 0.16, "BTC": 2}
ALERT_COOLDOWN = 60
PROCESS_INTERVAL = 2
EXPIRY_CHECK_INTERVAL = 60
BTC_FETCH_INTERVAL = 1

# -------------------------------
# System 2: Option Alert Configuration
# -------------------------------
@dataclass
class AlertConfig:
    strike: float = 0
    premium: float = 0
    is_monitoring: bool = False
    last_updated: str = ""
    active_expiry: str = ""

# Store alert configurations for System 2
alert_configs = {
    'btc_call': AlertConfig(),
    'btc_put': AlertConfig(),
    'eth_call': AlertConfig(),
    'eth_put': AlertConfig()
}

# Store previous configs to detect changes
previous_configs = {}

# System 2 monitoring status
new_system_active = False
last_check_time = None

# -------------------------------
# System 3: Dual Condition Spike Detection Configuration
# -------------------------------
@dataclass
class SpikeConfig:
    # Condition 1: Price Spike
    enabled_spike: bool = False
    min_spike_percent: float = 100.0  # No limits
    spike_min_premium: float = 1.0    # Minimum $ amount for spike check
    
    # Condition 2: Bid-Ask Spread
    enabled_spread: bool = False
    min_spread_percent: float = 100.0  # No limits
    spread_min_premium: float = 0.5    # Minimum $ amount for spread check
    
    # Asset filtering (applies to both conditions)
    monitor_eth: bool = True
    monitor_btc: bool = True
    monitor_calls: bool = True
    monitor_puts: bool = True

# System 3 configuration
spike_config = SpikeConfig()

# System 3 data storage
price_history = {}  # symbol: [last 10 prices] for Condition 1
last_spike_alert = {}  # symbol: timestamp for Condition 1
last_spread_alert = {}  # symbol: timestamp for Condition 2

# Fixed cooldown for both conditions (2 minutes)
SPIKE_COOLDOWN_SECONDS = 120

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

def send_config_update_telegram(config_id: str, old_config: Dict, new_config: Dict):
    """Send Telegram message when config is updated"""
    config_names = {
        'btc_call': 'BTC CALL',
        'btc_put': 'BTC PUT',
        'eth_call': 'ETH CALL',
        'eth_put': 'ETH PUT'
    }
    
    asset_type = config_names.get(config_id, config_id)
    
    # Check what changed
    changes = []
    
    if old_config.get('strike', 0) != new_config['strike']:
        changes.append(f"• Strike: {old_config.get('strike', 'Not set')} → {new_config['strike']}")
    
    if old_config.get('premium', 0) != new_config['premium']:
        changes.append(f"• Premium: ${old_config.get('premium', 0):.2f} → ${new_config['premium']:.2f}")
    
    if old_config.get('is_monitoring', False) != new_config['is_monitoring']:
        status = "✅ MONITORING" if new_config['is_monitoring'] else "⏸️ NOT MONITORING"
        changes.append(f"• Status: {status}")
    
    if not changes:
        return  # No actual changes
        
    message = f"""
⚙️ **ALERT CONFIGURATION UPDATED**

**{asset_type} ALERT**

**Changes:**
{"\n".join(changes)}

**New Configuration:**
• Strike: {new_config['strike']}
• Premium: ${new_config['premium']:.2f}
• Monitoring: {'✅ ACTIVE' if new_config['is_monitoring'] else '⏸️ INACTIVE'}
• Expiry: {new_config.get('active_expiry', 'Current')}

**Updated:** {get_ist_time()}
"""
    
    send_telegram(message)
    print(f"[{datetime.now()}] 📱 Telegram config update sent for {config_id}")

def send_alert_triggered_telegram(alert_data: Dict):
    """Send Telegram message when alert condition is met"""
    message = f"""
🚨 **{alert_data['asset']} {alert_data['type'].upper()} ALERT TRIGGERED!**

**Condition Met:**
• Looking for: Strike {'>' if alert_data['type'] == 'call' else '<'} {alert_data['config_strike']}
• Bid Price ≥ ${alert_data['threshold']:.2f}

**Found:**
• Strike Price: {alert_data['trigger_strike']}
• Current Bid: ${alert_data['bid_price']:.2f}
• Condition: ${alert_data['bid_price']:.2f} ≥ ${alert_data['threshold']:.2f} ✅

**Time:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
    
    send_telegram(message)

def send_spike_alert_telegram(symbol: str, current_price: float, historical_avg: float, spike_percent: float):
    """Send Telegram message for Condition 1: Premium spike"""
    # Extract symbol info
    parts = symbol.split('-')
    asset = "BTC" if "BTC" in symbol else "ETH"
    option_type = "CALL" if parts[0] == "C" else "PUT"
    strike = parts[2] if len(parts) > 2 else "Unknown"
    
    message = f"""
🚨 **PREMIUM SPIKE DETECTED!**

**{asset} {strike} {option_type}**
**Time:** {get_ist_time()}

**Price History:**
• Previous average: ${historical_avg:.2f}
• Current bid: ${current_price:.2f}
• Spike: +{spike_percent:.1f}%

**Alert:** Premium DOUBLED instantly!
"""
    
    send_telegram(message)
    print(f"[{datetime.now()}] 🚨 Condition 1: Spike alert sent for {symbol}: ${historical_avg:.2f} → ${current_price:.2f} (+{spike_percent:.1f}%)")

def send_spread_alert_telegram(symbol: str, bid_price: float, ask_price: float, spread_percent: float):
    """Send Telegram message for Condition 2: Bid-Ask spread"""
    # Extract symbol info
    parts = symbol.split('-')
    asset = "BTC" if "BTC" in symbol else "ETH"
    option_type = "CALL" if parts[0] == "C" else "PUT"
    strike = parts[2] if len(parts) > 2 else "Unknown"
    
    message = f"""
🚨 **BID-ASK SPREAD ALERT!**

**{asset} {strike} {option_type}**
**Time:** {get_ist_time()}

**Current Prices:**
• Bid: ${bid_price:.2f}
• Ask: ${ask_price:.2f}
• Spread: {spread_percent:.1f}%

**Alert:** Spread is {spread_percent:.1f}% (Bid: ${bid_price:.2f}, Ask: ${ask_price:.2f})
"""
    
    send_telegram(message)
    print(f"[{datetime.now()}] 🚨 Condition 2: Spread alert sent for {symbol}: Bid ${bid_price:.2f}, Ask ${ask_price:.2f}, Spread {spread_percent:.1f}%")

# -------------------------------
# System 3: Dual Condition Detection Functions
# -------------------------------
def check_premium_spikes_eth(eth_bot):
    """Check for both conditions in ETH options"""
    global price_history, last_spike_alert, last_spread_alert
    
    for symbol, price_data in eth_bot.options_prices.items():
        # Check if we should monitor this symbol based on asset filtering
        if not should_monitor_symbol(symbol):
            continue
        
        current_bid = price_data['bid']
        current_ask = price_data['ask']
        
        # Skip if no valid prices
        if current_bid <= 0 or current_ask <= 0:
            continue
        
        # CONDITION 1: PRICE SPIKE DETECTION
        if spike_config.enabled_spike and spike_config.monitor_eth:
            # Check premium filter first
            if current_bid >= spike_config.spike_min_premium:
                # Initialize price history for this symbol
                if symbol not in price_history:
                    price_history[symbol] = []
                
                # Add current price to history
                price_history[symbol].append(current_bid)
                
                # Keep only last 10 prices
                if len(price_history[symbol]) > 10:
                    price_history[symbol] = price_history[symbol][-10:]
                
                # Need at least 5 prices for meaningful average
                if len(price_history[symbol]) >= 5:
                    historical_avg = sum(price_history[symbol][:-1]) / (len(price_history[symbol]) - 1)
                    
                    if historical_avg > 0:
                        spike_percent = ((current_bid - historical_avg) / historical_avg) * 100
                        
                        if spike_percent >= spike_config.min_spike_percent:
                            # Check cooldown (2 minutes fixed)
                            now = datetime.now().timestamp()
                            last_alert = last_spike_alert.get(symbol, 0)
                            
                            if now - last_alert >= SPIKE_COOLDOWN_SECONDS:
                                # Send alert
                                send_spike_alert_telegram(symbol, current_bid, historical_avg, spike_percent)
                                last_spike_alert[symbol] = now
        
        # CONDITION 2: BID-ASK SPREAD DETECTION
        if spike_config.enabled_spread and spike_config.monitor_eth:
            # Check premium filter first
            if current_bid >= spike_config.spread_min_premium:
                if current_bid > 0:
                    spread_percent = ((current_ask - current_bid) / current_bid) * 100
                    
                    if spread_percent >= spike_config.min_spread_percent:
                        # Check cooldown (2 minutes fixed)
                        now = datetime.now().timestamp()
                        last_alert = last_spread_alert.get(symbol, 0)
                        
                        if now - last_alert >= SPIKE_COOLDOWN_SECONDS:
                            # Send alert
                            send_spread_alert_telegram(symbol, current_bid, current_ask, spread_percent)
                            last_spread_alert[symbol] = now

def check_premium_spikes_btc(btc_bot):
    """Check for both conditions in BTC options"""
    global price_history, last_spike_alert, last_spread_alert
    
    for symbol, price_data in btc_bot.options_prices.items():
        # Check if we should monitor this symbol based on asset filtering
        if not should_monitor_symbol(symbol):
            continue
        
        current_bid = price_data['bid']
        current_ask = price_data['ask']
        
        # Skip if no valid prices
        if current_bid <= 0 or current_ask <= 0:
            continue
        
        # CONDITION 1: PRICE SPIKE DETECTION
        if spike_config.enabled_spike and spike_config.monitor_btc:
            # Check premium filter first
            if current_bid >= spike_config.spike_min_premium:
                # Initialize price history for this symbol
                if symbol not in price_history:
                    price_history[symbol] = []
                
                # Add current price to history
                price_history[symbol].append(current_bid)
                
                # Keep only last 10 prices
                if len(price_history[symbol]) > 10:
                    price_history[symbol] = price_history[symbol][-10:]
                
                # Need at least 5 prices for meaningful average
                if len(price_history[symbol]) >= 5:
                    historical_avg = sum(price_history[symbol][:-1]) / (len(price_history[symbol]) - 1)
                    
                    if historical_avg > 0:
                        spike_percent = ((current_bid - historical_avg) / historical_avg) * 100
                        
                        if spike_percent >= spike_config.min_spike_percent:
                            # Check cooldown (2 minutes fixed)
                            now = datetime.now().timestamp()
                            last_alert = last_spike_alert.get(symbol, 0)
                            
                            if now - last_alert >= SPIKE_COOLDOWN_SECONDS:
                                # Send alert
                                send_spike_alert_telegram(symbol, current_bid, historical_avg, spike_percent)
                                last_spike_alert[symbol] = now
        
        # CONDITION 2: BID-ASK SPREAD DETECTION
        if spike_config.enabled_spread and spike_config.monitor_btc:
            # Check premium filter first
            if current_bid >= spike_config.spread_min_premium:
                if current_bid > 0:
                    spread_percent = ((current_ask - current_bid) / current_bid) * 100
                    
                    if spread_percent >= spike_config.min_spread_percent:
                        # Check cooldown (2 minutes fixed)
                        now = datetime.now().timestamp()
                        last_alert = last_spread_alert.get(symbol, 0)
                        
                        if now - last_alert >= SPIKE_COOLDOWN_SECONDS:
                            # Send alert
                            send_spread_alert_telegram(symbol, current_bid, current_ask, spread_percent)
                            last_spread_alert[symbol] = now

def should_monitor_symbol(symbol: str) -> bool:
    """Check if symbol should be monitored based on config"""
    if "BTC" in symbol and not spike_config.monitor_btc:
        return False
    if "ETH" in symbol and not spike_config.monitor_eth:
        return False
    
    parts = symbol.split('-')
    if len(parts) > 0:
        option_type = parts[0]
        if option_type == "C" and not spike_config.monitor_calls:
            return False
        if option_type == "P" and not spike_config.monitor_puts:
            return False
    
    return True

# -------------------------------
# Combined ETH WebSocket Bot (Systems 1, 2 & 3)
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
        self.last_user_alert_check = 0
        self.last_spike_check = 0
        
        # System 2 data
        self.option_chain_data = {'calls': {}, 'puts': {}}
        self.orderbook_data = {}  # Store orderbook data for quantity checks

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
        global price_history, last_spike_alert, last_spread_alert
        
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
                    
                    # Clear all systems' data
                    self.options_prices = {}
                    self.active_symbols = []
                    self.option_chain_data = {'calls': {}, 'puts': {}}
                    self.orderbook_data = {}
                    
                    # Update alert configs with new expiry
                    for config_id in alert_configs:
                        if alert_configs[config_id].is_monitoring:
                            alert_configs[config_id].active_expiry = self.active_expiry
                    
                    # Clear price history and alert timestamps for old expiry symbols
                    old_symbols = [s for s in price_history.keys() if 'ETH' in s]
                    for symbol in old_symbols:
                        if symbol in price_history:
                            del price_history[symbol]
                        if symbol in last_spike_alert:
                            del last_spike_alert[symbol]
                        if symbol in last_spread_alert:
                            del last_spread_alert[symbol]
                    
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
                    self.option_chain_data = {'calls': {}, 'puts': {}}
                    self.orderbook_data = {}
                    
                    # Update alert configs
                    for config_id in alert_configs:
                        if alert_configs[config_id].is_monitoring:
                            alert_configs[config_id].active_expiry = self.active_expiry
                    
                    # Clear price history and alert timestamps for old expiry symbols
                    old_symbols = [s for s in price_history.keys() if 'ETH' in s]
                    for symbol in old_symbols:
                        if symbol in price_history:
                            del price_history[symbol]
                        if symbol in last_spike_alert:
                            del last_spike_alert[symbol]
                        if symbol in last_spread_alert:
                            del last_spread_alert[symbol]
                    
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
                
                # Clear option chain data
                self.option_chain_data = {'calls': {}, 'puts': {}}
                
                for product in products:
                    symbol = product.get('symbol', '')
                    contract_type = product.get('contract_type', '')
                    
                    is_option = contract_type in ['call_options', 'put_options']
                    is_eth = 'ETH' in symbol
                    is_active_expiry = self.active_expiry in symbol
                    
                    if is_option and is_eth and is_active_expiry:
                        symbols.append(symbol)
                        
                        # Store strike data for dropdowns
                        strike = self.extract_strike(symbol)
                        if strike > 0:
                            if contract_type == 'call_options':
                                self.option_chain_data['calls'][strike] = symbol
                            else:
                                self.option_chain_data['puts'][strike] = symbol
                
                # Sort strikes
                self.option_chain_data['calls'] = dict(sorted(self.option_chain_data['calls'].items()))
                self.option_chain_data['puts'] = dict(sorted(self.option_chain_data['puts'].items()))
                
                symbols = sorted(list(set(symbols)))
                
                print(f"[{datetime.now()}] ✅ ETH: Found {len(symbols)} {self.active_expiry} expiry options symbols")
                print(f"[{datetime.now()}] 📊 ETH: Call strikes: {len(self.option_chain_data['calls'])}, Put strikes: {len(self.option_chain_data['puts'])}")
                
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
        """Handle incoming WebSocket messages - ALL SYSTEMS"""
        try:
            # Check expiry rollover
            self.check_and_update_expiry()
            
            message_json = json.loads(message)
            message_type = message_json.get('type')
            
            self.message_count += 1
            
            if self.message_count % 100 == 0:
                print(f"[{datetime.now()}] 📨 ETH: Message {self.message_count}")
            
            if message_type == 'l1_orderbook':
                self.process_l1_orderbook_data(message_json)
            elif message_type == 'l2_orderbook' or message_type == 'order_book':
                # Store full orderbook for quantity checks
                self.process_orderbook_data(message_json)
            elif message_type == 'subscriptions':
                print(f"[{datetime.now()}] ✅ ETH: Subscriptions confirmed for {self.active_expiry}")
                
        except Exception as e:
            print(f"[{datetime.now()}] ❌ ETH: Message processing error: {e}")

    def process_orderbook_data(self, message):
        """Process orderbook data for quantity checks"""
        try:
            symbol = message.get('symbol')
            if not symbol or 'ETH' not in symbol:
                return
                
            symbol_expiry = self.extract_expiry_from_symbol(symbol)
            if symbol_expiry != self.active_expiry:
                return
            
            # Store orderbook data for quantity checks
            self.orderbook_data[symbol] = message
            
        except Exception as e:
            print(f"[{datetime.now()}] ❌ ETH: Error processing orderbook data: {e}")

    def get_ask_quantity(self, symbol):
        """Get ask quantity from orderbook data"""
        try:
            if symbol in self.orderbook_data:
                orderbook = self.orderbook_data[symbol]
                
                # Check different possible structures
                if 'sell' in orderbook:
                    asks = orderbook.get('sell', [])
                    if asks and len(asks) > 0:
                        # Get best ask quantity
                        best_ask = asks[0]
                        if isinstance(best_ask, list) and len(best_ask) >= 2:
                            return float(best_ask[1])  # quantity is usually second element
                elif 'asks' in orderbook:
                    asks = orderbook.get('asks', [])
                    if asks and len(asks) > 0:
                        best_ask = asks[0]
                        if isinstance(best_ask, list) and len(best_ask) >= 2:
                            return float(best_ask[1])
                
        except Exception as e:
            print(f"[{datetime.now()}] ⚠️ ETH: Error getting ask quantity for {symbol}: {e}")
        
        return 0

    def process_l1_orderbook_data(self, message):
        """Process l1_orderbook data - ALL SYSTEMS USE THIS"""
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
                
                # Store data for ALL systems
                self.options_prices[symbol] = {
                    'bid': best_bid_price,
                    'ask': best_ask_price,
                    'symbol': symbol
                }
                
                current_time = datetime.now().timestamp()
                
                # Check ALL systems (every 2 seconds)
                if current_time - self.last_arbitrage_check >= PROCESS_INTERVAL:
                    # SYSTEM 1: Original arbitrage logic
                    self.check_arbitrage_opportunities()
                    
                    # SYSTEM 2: User alert logic
                    self.check_user_alerts()
                    
                    # SYSTEM 3: Dual condition detection
                    check_premium_spikes_eth(self)
                    
                    self.last_arbitrage_check = current_time
                    global last_check_time
                    last_check_time = datetime.now()
                
        except Exception as e:
            print(f"[{datetime.now()}] ❌ ETH: Error processing l1_orderbook data: {e}")

    def check_user_alerts(self):
        """SYSTEM 2: Check for user-configured alerts"""
        if not new_system_active:
            return
        
        # Check ETH calls
        eth_call_config = alert_configs['eth_call']
        if eth_call_config.is_monitoring and eth_call_config.strike > 0 and eth_call_config.premium > 0:
            alerts = []
            for strike, symbol in self.option_chain_data['calls'].items():
                if strike > eth_call_config.strike:
                    price_data = self.options_prices.get(symbol)
                    if price_data and price_data['bid'] >= eth_call_config.premium:
                        alert_key = f"ETH_CALL_ALERT_{strike}_{eth_call_config.strike}"
                        if self.can_alert(alert_key):
                            alerts.append({
                                'asset': 'ETH',
                                'type': 'call',
                                'trigger_strike': strike,
                                'bid_price': price_data['bid'],
                                'config_strike': eth_call_config.strike,
                                'threshold': eth_call_config.premium
                            })
            
            for alert in alerts:
                send_alert_triggered_telegram(alert)
                print(f"[{datetime.now()}] 🚨 ETH CALL Alert: Strike {alert['trigger_strike']} bid ${alert['bid_price']:.2f} ≥ ${alert['threshold']:.2f}")
        
        # Check ETH puts
        eth_put_config = alert_configs['eth_put']
        if eth_put_config.is_monitoring and eth_put_config.strike > 0 and eth_put_config.premium > 0:
            alerts = []
            for strike, symbol in self.option_chain_data['puts'].items():
                if strike < eth_put_config.strike:
                    price_data = self.options_prices.get(symbol)
                    if price_data and price_data['bid'] >= eth_put_config.premium:
                        alert_key = f"ETH_PUT_ALERT_{strike}_{eth_put_config.strike}"
                        if self.can_alert(alert_key):
                            alerts.append({
                                'asset': 'ETH',
                                'type': 'put',
                                'trigger_strike': strike,
                                'bid_price': price_data['bid'],
                                'config_strike': eth_put_config.strike,
                                'threshold': eth_put_config.premium
                            })
            
            for alert in alerts:
                send_alert_triggered_telegram(alert)
                print(f"[{datetime.now()}] 🚨 ETH PUT Alert: Strike {alert['trigger_strike']} bid ${alert['bid_price']:.2f} ≥ ${alert['threshold']:.2f}")

    def check_arbitrage_opportunities(self):
        """SYSTEM 1: Check for arbitrage opportunities - ONLY ETH"""
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
        """SYSTEM 1: Check for arbitrage opportunities within ACTIVE expiry"""
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
            call1_symbol = strikes[strike1]['call'].get('symbol', '')
            
            if call1_ask > 0 and call2_bid > 0 and call1_symbol:
                # Check ask quantity > 5 lots
                ask_quantity = self.get_ask_quantity(call1_symbol)
                
                call_diff = call1_ask - call2_bid
                if call_diff < 0 and abs(call_diff) >= DELTA_THRESHOLD["ETH"] and ask_quantity > 5:
                    alert_key = f"ETH_CALL_{strike1}_{strike2}_{self.active_expiry}"
                    if self.can_alert(alert_key):
                        profit = abs(call_diff)
                        expiry_display = format_expiry_display(self.active_expiry)
                        current_time = get_ist_time()
                        
                        alert_msg = f"🔵 ETH Alert Call\n{strike1} (B) → {strike2} (S)\n${call1_ask:.2f}    ${call2_bid:.2f}\nProfit: ${profit:.2f}\nQuantity: {ask_quantity} lots\n{expiry_display} | {current_time}"
                        alerts.append(alert_msg)
            
            # PUT arbitrage
            put2_ask = strikes[strike2]['put'].get('ask', 0)
            put1_bid = strikes[strike1]['put'].get('bid', 0)
            put2_symbol = strikes[strike2]['put'].get('symbol', '')
            
            if put1_bid > 0 and put2_ask > 0 and put2_symbol:
                # Check ask quantity > 5 lots
                ask_quantity = self.get_ask_quantity(put2_symbol)
                
                put_diff = put2_ask - put1_bid
                if put_diff < 0 and abs(put_diff) >= DELTA_THRESHOLD["ETH"] and ask_quantity > 5:
                    alert_key = f"ETH_PUT_{strike1}_{strike2}_{self.active_expiry}"
                    if self.can_alert(alert_key):
                        profit = abs(put_diff)
                        expiry_display = format_expiry_display(self.active_expiry)
                        current_time = get_ist_time()
                        
                        alert_msg = f"🔵 ETH Alert Put\n{strike2} (B) → {strike1} (S)\n${put2_ask:.2f}    ${put1_bid:.2f}\nProfit: ${profit:.2f}\nQuantity: {ask_quantity} lots\n{expiry_display} | {current_time}"
                        alerts.append(alert_msg)
        
        if alerts:
            for alert in alerts:
                send_telegram(alert)
                self.alert_count += 1
                print(f"[{datetime.now()}] ✅ ETH: Sent arbitrage alert (with quantity check)")

    def subscribe_to_options(self):
        """Subscribe to ACTIVE ETH expiry options"""
        symbols = self.get_all_options_symbols()
        
        if not symbols:
            print(f"[{datetime.now()}] ⚠️ ETH: No {self.active_expiry} expiry options symbols found")
            return
        
        self.active_symbols = symbols
        
        if symbols:
            # Subscribe to both L1 and L2 orderbooks for quantity data
            payload = {
                "type": "subscribe",
                "payload": {
                    "channels": [
                        {
                            "name": "l1_orderbook",
                            "symbols": symbols
                        },
                        {
                            "name": "order_book",  # For quantity data
                            "symbols": symbols
                        }
                    ]
                }
            }
            
            self.ws.send(json.dumps(payload))
            print(f"[{datetime.now()}] 📡 ETH: Subscribed to {len(symbols)} {self.active_expiry} expiry symbols (L1 + L2)")
            
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
# Combined BTC REST API Bot (Systems 1, 2 & 3)
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
        self.options_prices = {}
        self.last_arbitrage_check = 0
        self.last_spike_check = 0
        
        # System 2 data
        self.option_chain_data = {'calls': {}, 'puts': {}}
        self.orderbook_data = {}  # For quantity checks

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
        global price_history, last_spike_alert, last_spread_alert
        
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
                    
                    # Clear all systems' data
                    self.options_prices = {}
                    self.active_symbols = []
                    self.option_chain_data = {'calls': {}, 'puts': {}}
                    self.orderbook_data = {}
                    
                    # Update alert configs with new expiry
                    for config_id in alert_configs:
                        if alert_configs[config_id].is_monitoring:
                            alert_configs[config_id].active_expiry = self.active_expiry
                    
                    # Clear price history and alert timestamps for old expiry symbols
                    old_symbols = [s for s in price_history.keys() if 'BTC' in s]
                    for symbol in old_symbols:
                        if symbol in price_history:
                            del price_history[symbol]
                        if symbol in last_spike_alert:
                            del last_spike_alert[symbol]
                        if symbol in last_spread_alert:
                            del last_spread_alert[symbol]
                    
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
                    self.option_chain_data = {'calls': {}, 'puts': {}}
                    self.orderbook_data = {}
                    
                    # Update alert configs
                    for config_id in alert_configs:
                        if alert_configs[config_id].is_monitoring:
                            alert_configs[config_id].active_expiry = self.active_expiry
                    
                    # Clear price history and alert timestamps for old expiry symbols
                    old_symbols = [s for s in price_history.keys() if 'BTC' in s]
                    for symbol in old_symbols:
                        if symbol in price_history:
                            del price_history[symbol]
                        if symbol in last_spike_alert:
                            del last_spike_alert[symbol]
                        if symbol in last_spread_alert:
                            del last_spread_alert[symbol]
                    
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

    def fetch_orderbook(self, symbol):
        """Fetch orderbook for a specific symbol"""
        try:
            url = f"{self.base_url}/orderbook"
            params = {'symbol': symbol}
            response = requests.get(url, params=params, timeout=5)
            
            if response.status_code == 200:
                data = response.json()
                if data.get('success'):
                    return data.get('result', {})
        except Exception as e:
            self.debug_log(f"⚠️ BTC: Error fetching orderbook for {symbol}: {e}")
        
        return {}

    def get_ask_quantity(self, symbol):
        """Get ask quantity from orderbook"""
        try:
            if symbol not in self.orderbook_data:
                # Fetch orderbook if not cached
                self.orderbook_data[symbol] = self.fetch_orderbook(symbol)
            
            orderbook = self.orderbook_data.get(symbol, {})
            asks = orderbook.get('sell', [])
            
            if asks and len(asks) > 0:
                best_ask = asks[0]
                if isinstance(best_ask, list) and len(best_ask) >= 2:
                    quantity = float(best_ask[1])
                    return quantity
            
        except Exception as e:
            self.debug_log(f"⚠️ BTC: Error getting ask quantity for {symbol}: {e}")
        
        return 0

    def process_btc_options(self):
        """Process BTC options for ALL SYSTEMS"""
        tickers = self.fetch_tickers()
        if not tickers:
            self.debug_log("❌ BTC: No tickers received")
            return {}

        btc_tickers = [t for t in tickers if 'BTC' in str(t.get('symbol', '')).upper()]
        self.debug_log(f"🔍 BTC: Found {len(btc_tickers)} BTC tickers")
        
        current_expiry_tickers = []
        
        # Clear option chain data
        self.option_chain_data = {'calls': {}, 'puts': {}}
        
        for ticker in btc_tickers:
            symbol = ticker.get('symbol', '')
            parts = symbol.split('-')
            if len(parts) >= 4:
                expiry = parts[-1]
                if expiry == self.active_expiry:
                    current_expiry_tickers.append(ticker)
                    
                    # Store for System 2 dropdowns
                    strike = self.extract_strike(symbol)
                    if strike > 0:
                        # Check if it's a call or put based on symbol prefix
                        if symbol.startswith('C-'):
                            self.option_chain_data['calls'][strike] = symbol
                        elif symbol.startswith('P-'):
                            self.option_chain_data['puts'][strike] = symbol
        
        # Sort strikes
        self.option_chain_data['calls'] = dict(sorted(self.option_chain_data['calls'].items()))
        self.option_chain_data['puts'] = dict(sorted(self.option_chain_data['puts'].items()))
        
        self.active_symbols = [t.get('symbol', '') for t in current_expiry_tickers]
        self.debug_log(f"📅 BTC: Found {len(current_expiry_tickers)} tickers for expiry {self.active_expiry}")
        
        # Store prices for ALL systems
        for ticker in current_expiry_tickers:
            symbol = ticker.get('symbol', '')
            quotes = ticker.get('quotes', {})
            bid = float(quotes.get('best_bid', 0)) or 0
            ask = float(quotes.get('best_ask', 0)) or 0
            
            self.options_prices[symbol] = {
                'bid': bid,
                'ask': ask,
                'symbol': symbol
            }
        
        return self.group_by_strike(current_expiry_tickers)

    def group_by_strike(self, tickers):
        """Group tickers by strike price for System 1"""
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
                grouped[strike] = {'call': {'bid': 0, 'ask': 0, 'symbol': ''}, 'put': {'bid': 0, 'ask': 0, 'symbol': ''}}
            
            if option_type == 'call':
                grouped[strike]['call']['bid'] = bid
                grouped[strike]['call']['ask'] = ask
                grouped[strike]['call']['symbol'] = symbol
            else:  # put
                grouped[strike]['put']['bid'] = bid
                grouped[strike]['put']['ask'] = ask
                grouped[strike]['put']['symbol'] = symbol
        
        self.debug_log(f"💰 BTC: Grouped {len(grouped)} strikes with valid prices")
        return grouped

    def check_user_alerts(self):
        """SYSTEM 2: Check for user-configured BTC alerts"""
        if not new_system_active:
            return
        
        # Check BTC calls
        btc_call_config = alert_configs['btc_call']
        if btc_call_config.is_monitoring and btc_call_config.strike > 0 and btc_call_config.premium > 0:
            alerts = []
            for strike, symbol in self.option_chain_data['calls'].items():
                if strike > btc_call_config.strike:
                    price_data = self.options_prices.get(symbol)
                    if price_data and price_data['bid'] >= btc_call_config.premium:
                        alert_key = f"BTC_CALL_ALERT_{strike}_{btc_call_config.strike}"
                        if self.can_alert(alert_key):
                            alerts.append({
                                'asset': 'BTC',
                                'type': 'call',
                                'trigger_strike': strike,
                                'bid_price': price_data['bid'],
                                'config_strike': btc_call_config.strike,
                                'threshold': btc_call_config.premium
                            })
            
            for alert in alerts:
                send_alert_triggered_telegram(alert)
                print(f"[{datetime.now()}] 🚨 BTC CALL Alert: Strike {alert['trigger_strike']} bid ${alert['bid_price']:.2f} ≥ ${alert['threshold']:.2f}")
        
        # Check BTC puts
        btc_put_config = alert_configs['btc_put']
        if btc_put_config.is_monitoring and btc_put_config.strike > 0 and btc_put_config.premium > 0:
            alerts = []
            for strike, symbol in self.option_chain_data['puts'].items():
                if strike < btc_put_config.strike:
                    price_data = self.options_prices.get(symbol)
                    if price_data and price_data['bid'] >= btc_put_config.premium:
                        alert_key = f"BTC_PUT_ALERT_{strike}_{btc_put_config.strike}"
                        if self.can_alert(alert_key):
                            alerts.append({
                                'asset': 'BTC',
                                'type': 'put',
                                'trigger_strike': strike,
                                'bid_price': price_data['bid'],
                                'config_strike': btc_put_config.strike,
                                'threshold': btc_put_config.premium
                            })
            
            for alert in alerts:
                send_alert_triggered_telegram(alert)
                print(f"[{datetime.now()}] 🚨 BTC PUT Alert: Strike {alert['trigger_strike']} bid ${alert['bid_price']:.2f} ≥ ${alert['threshold']:.2f}")

    def check_arbitrage(self, grouped_data):
        """SYSTEM 1: Check for arbitrage opportunities with quantity check"""
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
            call1_symbol = grouped_data[strike1]['call']['symbol']
            
            if call1_ask > 0 and call2_bid > 0 and call1_symbol:
                # Check ask quantity > 5 lots
                ask_quantity = self.get_ask_quantity(call1_symbol)
                
                call_diff = call1_ask - call2_bid
                if call_diff < 0 and abs(call_diff) >= DELTA_THRESHOLD["BTC"] and ask_quantity > 5:
                    alert_key = f"BTC_CALL_{strike1}_{strike2}"
                    if self.can_alert(alert_key):
                        profit = abs(call_diff)
                        expiry_display = format_expiry_display(self.active_expiry)
                        current_time = get_ist_time()
                        
                        alert_msg = f"🔔 BTC Alert Call\n{strike1} (B) → {strike2} (S)\n${call1_ask:.2f}    ${call2_bid:.2f}\nProfit: ${profit:.2f}\nQuantity: {ask_quantity} lots\n{expiry_display} | {current_time}"
                        alerts.append(alert_msg)
            
            # PUT arbitrage
            put2_ask = grouped_data[strike2]['put']['ask']
            put1_bid = grouped_data[strike1]['put']['bid']
            put2_symbol = grouped_data[strike2]['put']['symbol']
            
            if put1_bid > 0 and put2_ask > 0 and put2_symbol:
                # Check ask quantity > 5 lots
                ask_quantity = self.get_ask_quantity(put2_symbol)
                
                put_diff = put2_ask - put1_bid
                if put_diff < 0 and abs(put_diff) >= DELTA_THRESHOLD["BTC"] and ask_quantity > 5:
                    alert_key = f"BTC_PUT_{strike1}_{strike2}"
                    if self.can_alert(alert_key):
                        profit = abs(put_diff)
                        expiry_display = format_expiry_display(self.active_expiry)
                        current_time = get_ist_time()
                        
                        alert_msg = f"🔔 BTC Alert Put\n{strike2} (B) → {strike1} (S)\n${put2_ask:.2f}    ${put1_bid:.2f}\nProfit: ${profit:.2f}\nQuantity: {ask_quantity} lots\n{expiry_display} | {current_time}"
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
                
                # Process data for ALL systems
                grouped_data = self.process_btc_options()
                
                current_time = datetime.now().timestamp()
                
                # Check ALL systems
                if current_time - self.last_arbitrage_check >= PROCESS_INTERVAL:
                    # SYSTEM 1: Original arbitrage logic with quantity check
                    alerts = self.check_arbitrage(grouped_data)
                    if alerts:
                        for alert in alerts:
                            send_telegram(alert)
                            self.alert_count += 1
                            self.debug_log(f"✅ BTC: Sent arbitrage alert (with quantity check)")
                    
                    # SYSTEM 2: User alert logic
                    self.check_user_alerts()
                    
                    # SYSTEM 3: Dual condition detection
                    check_premium_spikes_btc(self)
                    
                    self.last_arbitrage_check = current_time
                    global last_check_time
                    last_check_time = datetime.now()
                
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
# HTML Template - UPDATED FOR DUAL CONDITION SYSTEM 3
# -------------------------------
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Triple Alert System</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
            color: #333;
        }
        
        .container {
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            border-radius: 20px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            overflow: hidden;
        }
        
        .header {
            background: linear-gradient(135deg, #4a6ee0, #6a11cb);
            color: white;
            padding: 30px;
            text-align: center;
        }
        
        .header h1 {
            font-size: 2.5rem;
            margin-bottom: 10px;
        }
        
        .header .subtitle {
            font-size: 1.2rem;
            opacity: 0.9;
        }
        
        .tabs {
            display: flex;
            background: #f8f9fa;
            border-bottom: 2px solid #e9ecef;
        }
        
        .tab-btn {
            flex: 1;
            padding: 20px;
            border: none;
            background: none;
            font-size: 1.1rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s ease;
            color: #6c757d;
        }
        
        .tab-btn:hover {
            background: #e9ecef;
        }
        
        .tab-btn.active {
            background: white;
            color: #4a6ee0;
            border-bottom: 3px solid #4a6ee0;
        }
        
        .tab-content {
            display: none;
            padding: 30px;
        }
        
        .tab-content.active {
            display: block;
        }
        
        .alert-success {
            background: #d4edda;
            color: #155724;
            padding: 15px;
            border-radius: 10px;
            margin-bottom: 20px;
            border: 1px solid #c3e6cb;
        }
        
        .system-section {
            margin-bottom: 40px;
        }
        
        .section-title {
            font-size: 1.5rem;
            margin-bottom: 20px;
            color: #4a6ee0;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        
        .stat-card {
            background: #f8f9fa;
            padding: 25px;
            border-radius: 15px;
            border-left: 5px solid #4a6ee0;
        }
        
        .stat-card h3 {
            color: #333;
            margin-bottom: 15px;
            font-size: 1.3rem;
        }
        
        .stat-item {
            margin-bottom: 10px;
            font-size: 1.1rem;
            display: flex;
            justify-content: space-between;
        }
        
        .stat-label {
            color: #6c757d;
        }
        
        .stat-value {
            font-weight: 600;
            color: #333;
        }
        
        .threshold-card {
            background: white;
            padding: 25px;
            border-radius: 15px;
            box-shadow: 0 5px 20px rgba(0,0,0,0.1);
            margin-bottom: 20px;
        }
        
        .threshold-card h3 {
            color: #333;
            margin-bottom: 20px;
            font-size: 1.3rem;
        }
        
        .threshold-input {
            width: 100%;
            padding: 12px;
            font-size: 1.1rem;
            border: 2px solid #e0e0e0;
            border-radius: 10px;
            margin-bottom: 15px;
            transition: all 0.3s ease;
        }
        
        .threshold-input:focus {
            outline: none;
            border-color: #4a6ee0;
            box-shadow: 0 0 0 3px rgba(74, 110, 224, 0.1);
        }
        
        .update-btn {
            padding: 15px 30px;
            font-size: 1.1rem;
            font-weight: 600;
            border: none;
            border-radius: 10px;
            cursor: pointer;
            transition: all 0.3s ease;
            background: linear-gradient(135deg, #4a6ee0, #6a11cb);
            color: white;
            width: 100%;
        }
        
        .update-btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 25px rgba(74, 110, 224, 0.4);
        }
        
        .option-section {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        
        .option-card {
            background: #f8f9fa;
            padding: 25px;
            border-radius: 15px;
            border-top: 5px solid;
        }
        
        .btc-call { border-color: #3498db; }
        .btc-put { border-color: #e74c3c; }
        .eth-call { border-color: #2ecc71; }
        .eth-put { border-color: #9b59b6; }
        
        .option-card h4 {
            font-size: 1.2rem;
            margin-bottom: 15px;
            color: #333;
        }
        
        .select-input {
            width: 100%;
            padding: 12px;
            font-size: 1.1rem;
            border: 2px solid #e0e0e0;
            border-radius: 10px;
            margin-bottom: 15px;
            background: white;
        }
        
        .checkbox-group {
            display: flex;
            align-items: center;
            gap: 10px;
            margin-top: 15px;
        }
        
        .checkbox-group input[type="checkbox"] {
            width: 20px;
            height: 20px;
        }
        
        .activate-btn {
            padding: 20px;
            font-size: 1.3rem;
            font-weight: 700;
            border: none;
            border-radius: 15px;
            cursor: pointer;
            transition: all 0.3s ease;
            background: linear-gradient(135deg, #2ecc71, #27ae60);
            color: white;
            width: 100%;
            margin-top: 20px;
        }
        
        .activate-btn:hover {
            transform: translateY(-3px);
            box-shadow: 0 12px 30px rgba(46, 204, 113, 0.4);
        }
        
        .status-panel {
            background: #f8f9fa;
            padding: 25px;
            border-radius: 15px;
            margin-top: 30px;
        }
        
        .status-panel h3 {
            color: #333;
            margin-bottom: 20px;
            font-size: 1.3rem;
        }
        
        .status-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px 0;
            border-bottom: 1px solid #e9ecef;
        }
        
        .status-item:last-child {
            border-bottom: none;
        }
        
        .status-label {
            font-size: 1.1rem;
            color: #6c757d;
        }
        
        .status-value {
            font-weight: 600;
            font-size: 1.1rem;
        }
        
        .status-active {
            color: #2ecc71;
        }
        
        .status-inactive {
            color: #e74c3c;
        }
        
        /* System 3 Styles */
        .dual-condition-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin-bottom: 30px;
        }
        
        @media (max-width: 768px) {
            .dual-condition-grid {
                grid-template-columns: 1fr;
            }
        }
        
        .condition-panel {
            padding: 25px;
            border-radius: 15px;
            margin-bottom: 20px;
        }
        
        .condition-1 {
            background: linear-gradient(135deg, #3498db, #2980b9);
            color: white;
        }
        
        .condition-2 {
            background: linear-gradient(135deg, #9b59b6, #8e44ad);
            color: white;
        }
        
        .condition-panel h3 {
            color: white;
            margin-bottom: 20px;
            font-size: 1.5rem;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        
        .condition-status {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            padding: 15px;
            background: rgba(255, 255, 255, 0.1);
            border-radius: 10px;
        }
        
        .condition-controls {
            margin-top: 20px;
        }
        
        .control-buttons {
            display: flex;
            gap: 10px;
            margin-top: 15px;
        }
        
        .start-btn {
            padding: 15px;
            font-size: 1.1rem;
            font-weight: 600;
            border: none;
            border-radius: 10px;
            cursor: pointer;
            background: #2ecc71;
            color: white;
            flex: 1;
            transition: all 0.3s ease;
        }
        
        .stop-btn {
            padding: 15px;
            font-size: 1.1rem;
            font-weight: 600;
            border: none;
            border-radius: 10px;
            cursor: pointer;
            background: #e74c3c;
            color: white;
            flex: 1;
            transition: all 0.3s ease;
        }
        
        .start-btn:hover {
            background: #27ae60;
            transform: translateY(-2px);
        }
        
        .stop-btn:hover {
            background: #c0392b;
            transform: translateY(-2px);
        }
        
        .config-section {
            background: #f8f9fa;
            padding: 25px;
            border-radius: 15px;
            margin-bottom: 20px;
        }
        
        .config-section h4 {
            color: #333;
            margin-bottom: 20px;
            font-size: 1.3rem;
        }
        
        .condition-section {
            background: white;
            padding: 20px;
            border-radius: 10px;
            margin-bottom: 20px;
            border-left: 4px solid;
        }
        
        .condition-1-section {
            border-left-color: #3498db;
        }
        
        .condition-2-section {
            border-left-color: #9b59b6;
        }
        
        .condition-section h5 {
            font-size: 1.1rem;
            margin-bottom: 15px;
            color: #333;
        }
        
        .condition-section small {
            color: #666;
            display: block;
            margin-top: 5px;
            font-size: 0.9rem;
        }
        
        .config-row {
            margin-bottom: 20px;
        }
        
        .config-row label {
            display: block;
            margin-bottom: 8px;
            font-weight: 600;
            color: #555;
        }
        
        .checkbox-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 15px;
            margin-top: 20px;
        }
        
        .save-btn {
            padding: 15px 30px;
            font-size: 1.1rem;
            font-weight: 600;
            border: none;
            border-radius: 10px;
            cursor: pointer;
            background: #3498db;
            color: white;
            width: 100%;
            margin-top: 20px;
        }
        
        .save-btn:hover {
            background: #2980b9;
            transform: translateY(-2px);
        }
        
        .cooldown-note {
            background: rgba(255, 255, 255, 0.2);
            color: white;
            padding: 10px;
            border-radius: 5px;
            margin-top: 10px;
            font-size: 0.9rem;
            text-align: center;
        }
        
        .footer {
            text-align: center;
            padding: 20px;
            color: #6c757d;
            border-top: 1px solid #e9ecef;
            margin-top: 30px;
        }
        
        @media (max-width: 768px) {
            .header h1 {
                font-size: 2rem;
            }
            
            .tab-btn {
                padding: 15px;
                font-size: 1rem;
            }
            
            .tab-content {
                padding: 20px;
            }
            
            .stats-grid {
                grid-template-columns: 1fr;
            }
            
            .option-section {
                grid-template-columns: 1fr;
            }
            
            .control-buttons {
                flex-direction: column;
            }
            
            .checkbox-grid {
                grid-template-columns: 1fr;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🚀 Triple Alert System</h1>
            <div class="subtitle">Arbitrage + Option Alerts + Dual Condition Spike Detection</div>
        </div>
        
        <div class="tabs">
            <button class="tab-btn active" onclick="showTab('arbitrage')">Arbitrage System</button>
            <button class="tab-btn" onclick="showTab('option-alerts')">Option Alerts</button>
            <button class="tab-btn" onclick="showTab('spike-detector')">Spike Detector</button>
        </div>
        
        <!-- Success Message -->
        {% if success %}
        <div class="alert-success">
            ✅ {{ success }}
        </div>
        {% endif %}
        
        <!-- Tab 1: Arbitrage System -->
        <div id="arbitrage-tab" class="tab-content active">
            <div class="system-section">
                <h2 class="section-title">⚡ Arbitrage Alert System (with Quantity Check > 5 lots)</h2>
                
                <div class="stats-grid">
                    <!-- ETH Stats Card -->
                    <div class="stat-card">
                        <h3>🔵 ETH WebSocket Bot</h3>
                        <div class="stat-item">
                            <span class="stat-label">Status:</span>
                            <span class="stat-value">{{ "✅ Connected" if eth_bot.connected else "🔴 Disconnected" }}</span>
                        </div>
                        <div class="stat-item">
                            <span class="stat-label">Messages:</span>
                            <span class="stat-value">{{ eth_bot.message_count }}</span>
                        </div>
                        <div class="stat-item">
                            <span class="stat-label">ETH Symbols:</span>
                            <span class="stat-value">{{ len(eth_bot.options_prices) }}</span>
                        </div>
                        <div class="stat-item">
                            <span class="stat-label">Active Expiry:</span>
                            <span class="stat-value">{{ eth_bot.active_expiry }}</span>
                        </div>
                        <div class="stat-item">
                            <span class="stat-label">ETH Alerts:</span>
                            <span class="stat-value">{{ eth_bot.alert_count }}</span>
                        </div>
                    </div>
                    
                    <!-- BTC Stats Card -->
                    <div class="stat-card">
                        <h3>🟠 BTC REST API Bot</h3>
                        <div class="stat-item">
                            <span class="stat-label">Status:</span>
                            <span class="stat-value">{{ "✅ Running" if btc_bot.running else "🔴 Stopped" }}</span>
                        </div>
                        <div class="stat-item">
                            <span class="stat-label">Fetches:</span>
                            <span class="stat-value">{{ btc_bot.fetch_count }}</span>
                        </div>
                        <div class="stat-item">
                            <span class="stat-label">BTC Symbols:</span>
                            <span class="stat-value">{{ len(btc_bot.active_symbols) }}</span>
                        </div>
                        <div class="stat-item">
                            <span class="stat-label">Active Expiry:</span>
                            <span class="stat-value">{{ btc_bot.active_expiry }}</span>
                        </div>
                        <div class="stat-item">
                            <span class="stat-label">BTC Alerts:</span>
                            <span class="stat-value">{{ btc_bot.alert_count }}</span>
                        </div>
                    </div>
                </div>
                
                <div class="threshold-card">
                    <h3>⚙️ Update Arbitrage Thresholds</h3>
                    <div class="threshold-grid" style="display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px;">
                        <!-- ETH Threshold Form -->
                        <div>
                            <h4>ETH Threshold: ${{ "%.2f"|format(DELTA_THRESHOLD['ETH']) }}</h4>
                            <form action="/update_eth_threshold" method="POST">
                                <input type="number" name="threshold" value="{{ "%.2f"|format(DELTA_THRESHOLD['ETH']) }}" 
                                       step="0.01" min="0.01" max="10" class="threshold-input" required>
                                <button type="submit" class="update-btn">Update ETH Threshold</button>
                            </form>
                        </div>
                        
                        <!-- BTC Threshold Form -->
                        <div>
                            <h4>BTC Threshold: ${{ "%.2f"|format(DELTA_THRESHOLD['BTC']) }}</h4>
                            <form action="/update_btc_threshold" method="POST">
                                <input type="number" name="threshold" value="{{ "%.2f"|format(DELTA_THRESHOLD['BTC']) }}" 
                                       step="0.01" min="0.01" max="50" class="threshold-input" required>
                                <button type="submit" class="update-btn">Update BTC Threshold</button>
                            </form>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- Tab 2: Option Alerts System -->
        <div id="option-alerts-tab" class="tab-content">
            <div class="system-section">
                <h2 class="section-title">🎯 Option Strike Alert System</h2>
                <p style="margin-bottom: 20px; color: #666;">Configure alerts for specific strikes and premiums</p>
                
                <form action="/activate_alerts" method="POST">
                    <div class="option-section">
                        <!-- BTC CALL Card -->
                        <div class="option-card btc-call">
                            <h4>🔵 BTC CALL OPTIONS</h4>
                            <select name="btc_call_strike" class="select-input">
                                <option value="">Select Strike</option>
                                {% for strike in btc_bot.option_chain_data.calls.keys()|sort %}
                                <option value="{{ strike }}" {% if alert_configs['btc_call'].strike == strike %}selected{% endif %}>
                                    {{ strike }}
                                </option>
                                {% endfor %}
                            </select>
                            <input type="number" name="btc_call_premium" placeholder="Premium ($)" 
                                   value="{{ "%.2f"|format(alert_configs['btc_call'].premium) if alert_configs['btc_call'].premium > 0 else '' }}"
                                   step="0.01" min="0" class="threshold-input">
                            <div class="checkbox-group">
                                <input type="checkbox" name="btc_call_monitor" id="btc_call_monitor" 
                                       {% if alert_configs['btc_call'].is_monitoring %}checked{% endif %}>
                                <label for="btc_call_monitor">Monitor BTC Calls</label>
                            </div>
                            <small style="color: #666;">Found {{ btc_bot.option_chain_data.calls|length }} call strikes</small>
                        </div>
                        
                        <!-- BTC PUT Card -->
                        <div class="option-card btc-put">
                            <h4>🔴 BTC PUT OPTIONS</h4>
                            <select name="btc_put_strike" class="select-input">
                                <option value="">Select Strike</option>
                                {% for strike in btc_bot.option_chain_data.puts.keys()|sort %}
                                <option value="{{ strike }}" {% if alert_configs['btc_put'].strike == strike %}selected{% endif %}>
                                    {{ strike }}
                                </option>
                                {% endfor %}
                            </select>
                            <input type="number" name="btc_put_premium" placeholder="Premium ($)" 
                                   value="{{ "%.2f"|format(alert_configs['btc_put'].premium) if alert_configs['btc_put'].premium > 0 else '' }}"
                                   step="0.01" min="0" class="threshold-input">
                            <div class="checkbox-group">
                                <input type="checkbox" name="btc_put_monitor" id="btc_put_monitor"
                                       {% if alert_configs['btc_put'].is_monitoring %}checked{% endif %}>
                                <label for="btc_put_monitor">Monitor BTC Puts</label>
                            </div>
                            <small style="color: #666;">Found {{ btc_bot.option_chain_data.puts|length }} put strikes</small>
                        </div>
                        
                        <!-- ETH CALL Card -->
                        <div class="option-card eth-call">
                            <h4>🟢 ETH CALL OPTIONS</h4>
                            <select name="eth_call_strike" class="select-input">
                                <option value="">Select Strike</option>
                                {% for strike in eth_bot.option_chain_data.calls.keys()|sort %}
                                <option value="{{ strike }}" {% if alert_configs['eth_call'].strike == strike %}selected{% endif %}>
                                    {{ strike }}
                                </option>
                                {% endfor %}
                            </select>
                            <input type="number" name="eth_call_premium" placeholder="Premium ($)" 
                                   value="{{ "%.2f"|format(alert_configs['eth_call'].premium) if alert_configs['eth_call'].premium > 0 else '' }}"
                                   step="0.01" min="0" class="threshold-input">
                            <div class="checkbox-group">
                                <input type="checkbox" name="eth_call_monitor" id="eth_call_monitor"
                                       {% if alert_configs['eth_call'].is_monitoring %}checked{% endif %}>
                                <label for="eth_call_monitor">Monitor ETH Calls</label>
                            </div>
                            <small style="color: #666;">Found {{ eth_bot.option_chain_data.calls|length }} call strikes</small>
                        </div>
                        
                        <!-- ETH PUT Card -->
                        <div class="option-card eth-put">
                            <h4>🟣 ETH PUT OPTIONS</h4>
                            <select name="eth_put_strike" class="select-input">
                                <option value="">Select Strike</option>
                                {% for strike in eth_bot.option_chain_data.puts.keys()|sort %}
                                <option value="{{ strike }}" {% if alert_configs['eth_put'].strike == strike %}selected{% endif %}>
                                    {{ strike }}
                                </option>
                                {% endfor %}
                            </select>
                            <input type="number" name="eth_put_premium" placeholder="Premium ($)" 
                                   value="{{ "%.2f"|format(alert_configs['eth_put'].premium) if alert_configs['eth_put'].premium > 0 else '' }}"
                                   step="0.01" min="0" class="threshold-input">
                            <div class="checkbox-group">
                                <input type="checkbox" name="eth_put_monitor" id="eth_put_monitor"
                                       {% if alert_configs['eth_put'].is_monitoring %}checked{% endif %}>
                                <label for="eth_put_monitor">Monitor ETH Puts</label>
                            </div>
                            <small style="color: #666;">Found {{ eth_bot.option_chain_data.puts|length }} put strikes</small>
                        </div>
                    </div>
                    
                    <button type="submit" class="activate-btn">🚀 ACTIVATE ALERTS</button>
                </form>
                
                <div class="status-panel">
                    <h3>📊 Active Alerts Status</h3>
                    <div class="status-item">
                        <span class="status-label">BTC Calls:</span>
                        <span class="status-value {% if alert_configs['btc_call'].is_monitoring %}status-active{% else %}status-inactive{% endif %}">
                            {% if alert_configs['btc_call'].is_monitoring %}✅ ACTIVE{% else %}❌ INACTIVE{% endif %}
                        </span>
                    </div>
                    <div class="status-item">
                        <span class="status-label">BTC Puts:</span>
                        <span class="status-value {% if alert_configs['btc_put'].is_monitoring %}status-active{% else %}status-inactive{% endif %}">
                            {% if alert_configs['btc_put'].is_monitoring %}✅ ACTIVE{% else %}❌ INACTIVE{% endif %}
                        </span>
                    </div>
                    <div class="status-item">
                        <span class="status-label">ETH Calls:</span>
                        <span class="status-value {% if alert_configs['eth_call'].is_monitoring %}status-active{% else %}status-inactive{% endif %}">
                            {% if alert_configs['eth_call'].is_monitoring %}✅ ACTIVE{% else %}❌ INACTIVE{% endif %}
                        </span>
                    </div>
                    <div class="status-item">
                        <span class="status-label">ETH Puts:</span>
                        <span class="status-value {% if alert_configs['eth_put'].is_monitoring %}status-active{% else %}status-inactive{% endif %}">
                            {% if alert_configs['eth_put'].is_monitoring %}✅ ACTIVE{% else %}❌ INACTIVE{% endif %}
                        </span>
                    </div>
                    <div class="status-item">
                        <span class="status-label">Last Check:</span>
                        <span class="status-value">
                            {% if last_check_time %}
                                {{ (now - last_check_time).seconds }} seconds ago
                            {% else %}
                                Never
                            {% endif %}
                        </span>
                    </div>
                    <div class="status-item">
                        <span class="status-label">System Status:</span>
                        <span class="status-value {% if new_system_active %}status-active{% else %}status-inactive{% endif %}">
                            {% if new_system_active %}✅ RUNNING{% else %}❌ STOPPED{% endif %}
                        </span>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- Tab 3: Premium Spike Detector -->
        <div id="spike-detector-tab" class="tab-content">
            <div class="system-section">
                <h2 class="section-title">🚨 DUAL CONDITION SPIKE DETECTOR</h2>
                
                <!-- DUAL CONDITION PANELS -->
                <div class="dual-condition-grid">
                    <!-- CONDITION 1: PRICE SPIKE -->
                    <div class="condition-panel condition-1">
                        <h3>📊 CONDITION 1: PRICE SPIKE</h3>
                        
                        <div class="condition-status">
                            <div>
                                <strong>Status:</strong>
                                <span style="color: {% if spike_config.enabled_spike %}#2ecc71{% else %}#e74c3c{% endif %}; font-weight: bold;">
                                    {% if spike_config.enabled_spike %}🟢 RUNNING{% else %}🔴 STOPPED{% endif %}
                                </span>
                            </div>
                            <div class="cooldown-note">
                                ⏰ Cooldown: 120s
                            </div>
                        </div>
                        
                        <div class="condition-controls">
                            <form action="/start_spike_detection" method="POST" style="margin-bottom: 10px;">
                                <button type="submit" class="start-btn">▶️ START SPIKE</button>
                            </form>
                            <form action="/stop_spike_detection" method="POST">
                                <button type="submit" class="stop-btn">⏸️ STOP SPIKE</button>
                            </form>
                        </div>
                    </div>
                    
                    <!-- CONDITION 2: BID-ASK SPREAD -->
                    <div class="condition-panel condition-2">
                        <h3>📊 CONDITION 2: BID-ASK SPREAD</h3>
                        
                        <div class="condition-status">
                            <div>
                                <strong>Status:</strong>
                                <span style="color: {% if spike_config.enabled_spread %}#2ecc71{% else %}#e74c3c{% endif %}; font-weight: bold;">
                                    {% if spike_config.enabled_spread %}🟢 RUNNING{% else %}🔴 STOPPED{% endif %}
                                </span>
                            </div>
                            <div class="cooldown-note">
                                ⏰ Cooldown: 120s
                            </div>
                        </div>
                        
                        <div class="condition-controls">
                            <form action="/start_spread_detection" method="POST" style="margin-bottom: 10px;">
                                <button type="submit" class="start-btn">▶️ START SPREAD</button>
                            </form>
                            <form action="/stop_spread_detection" method="POST">
                                <button type="submit" class="stop-btn">⏸️ STOP SPREAD</button>
                            </form>
                        </div>
                    </div>
                </div>
                
                <!-- CONFIGURATION -->
                <div class="config-section">
                    <h4>Configuration Settings</h4>
                    <form action="/update_spike_config" method="POST">
                        
                        <!-- CONDITION 1: PRICE SPIKE -->
                        <div class="condition-section condition-1-section">
                            <h5>📊 CONDITION 1: PRICE SPIKE</h5>
                            <div class="config-row">
                                <label for="min_spike_percent">Minimum Spike Percentage:</label>
                                <input type="number" id="min_spike_percent" name="min_spike_percent" 
                                       value="{{ spike_config.min_spike_percent }}" step="0.1"
                                       class="threshold-input" required>
                                <small>
                                    Alert when bid price increases by this percentage (100% = price doubles)
                                </small>
                            </div>
                            <div class="config-row">
                                <label for="spike_min_premium">Minimum Premium Filter:</label>
                                <input type="number" id="spike_min_premium" name="spike_min_premium" 
                                       value="{{ spike_config.spike_min_premium }}" step="0.01" min="0"
                                       class="threshold-input" required>
                                <small>
                                    Only check spikes for options with bid price ≥ this amount ($)
                                </small>
                            </div>
                        </div>
                        
                        <!-- CONDITION 2: BID-ASK SPREAD -->
                        <div class="condition-section condition-2-section">
                            <h5>📊 CONDITION 2: BID-ASK SPREAD</h5>
                            <div class="config-row">
                                <label for="min_spread_percent">Minimum Spread Percentage:</label>
                                <input type="number" id="min_spread_percent" name="min_spread_percent" 
                                       value="{{ spike_config.min_spread_percent }}" step="0.1"
                                       class="threshold-input" required>
                                <small>
                                    Alert when (Ask-Bid)/Bid × 100 ≥ this percentage (100% = ask is double the bid)
                                </small>
                            </div>
                            <div class="config-row">
                                <label for="spread_min_premium">Minimum Premium Filter:</label>
                                <input type="number" id="spread_min_premium" name="spread_min_premium" 
                                       value="{{ spike_config.spread_min_premium }}" step="0.01" min="0"
                                       class="threshold-input" required>
                                <small>
                                    Only check spreads for options with bid price ≥ this amount ($)
                                </small>
                            </div>
                        </div>
                        
                        <!-- ASSET FILTERING -->
                        <div class="checkbox-grid">
                            <div class="checkbox-group">
                                <input type="checkbox" id="monitor_eth" name="monitor_eth" 
                                       {% if spike_config.monitor_eth %}checked{% endif %}>
                                <label for="monitor_eth">Monitor ETH</label>
                            </div>
                            <div class="checkbox-group">
                                <input type="checkbox" id="monitor_btc" name="monitor_btc"
                                       {% if spike_config.monitor_btc %}checked{% endif %}>
                                <label for="monitor_btc">Monitor BTC</label>
                            </div>
                            <div class="checkbox-group">
                                <input type="checkbox" id="monitor_calls" name="monitor_calls"
                                       {% if spike_config.monitor_calls %}checked{% endif %}>
                                <label for="monitor_calls">Include Calls</label>
                            </div>
                            <div class="checkbox-group">
                                <input type="checkbox" id="monitor_puts" name="monitor_puts"
                                       {% if spike_config.monitor_puts %}checked{% endif %}>
                                <label for="monitor_puts">Include Puts</label>
                            </div>
                        </div>
                        
                        <button type="submit" class="save-btn">💾 SAVE SETTINGS</button>
                    </form>
                </div>
                
                <!-- NOTE -->
                <div style="background: #fff3cd; color: #856404; padding: 15px; border-radius: 10px; margin-top: 20px; text-align: center;">
                    <strong>Note:</strong> Both conditions check: bid ≥ min premium, then percentage ≥ threshold
                </div>
            </div>
        </div>
        
        <div class="footer">
            <p>Auto-expiry at 5:30 PM IST • All systems running simultaneously</p>
            <p>Last Update: {{ get_ist_time() }} • <a href="/health" style="color: #4a6ee0;">Health Check</a></p>
        </div>
    </div>
    
    <script>
        function showTab(tabName) {
            // Hide all tabs
            document.querySelectorAll('.tab-content').forEach(tab => {
                tab.classList.remove('active');
            });
            
            // Remove active class from all buttons
            document.querySelectorAll('.tab-btn').forEach(btn => {
                btn.classList.remove('active');
            });
            
            // Show selected tab
            document.getElementById(tabName + '-tab').classList.add('active');
            
            // Activate selected button
            event.target.classList.add('active');
        }
        
        // Auto-refresh page every 30 seconds
        setTimeout(function() {
            window.location.reload();
        }, 30000);
    </script>
</body>
</html>
'''

# -------------------------------
# Flask Routes
# -------------------------------
@app.route('/')
def home():
    now = datetime.now()
    return render_template_string(HTML_TEMPLATE, 
                                 eth_bot=eth_bot,
                                 btc_bot=btc_bot,
                                 alert_configs=alert_configs,
                                 spike_config=spike_config,
                                 DELTA_THRESHOLD=DELTA_THRESHOLD,
                                 new_system_active=new_system_active,
                                 last_check_time=last_check_time,
                                 now=now,
                                 get_ist_time=get_ist_time,
                                 format_expiry_display=format_expiry_display,
                                 success=request.args.get('success'),
                                 len=len)

@app.route('/activate_alerts', methods=['POST'])
def activate_alerts():
    """Activate System 2: Option alerts"""
    global new_system_active, alert_configs
    
    try:
        # Store old configs for comparison
        old_configs = {}
        for config_id, config in alert_configs.items():
            old_configs[config_id] = asdict(config)
        
        # Update BTC Call config
        btc_call_strike_str = request.form.get('btc_call_strike', '')
        btc_call_strike = float(btc_call_strike_str) if btc_call_strike_str else 0
        btc_call_premium_str = request.form.get('btc_call_premium', '')
        btc_call_premium = float(btc_call_premium_str) if btc_call_premium_str else 0
        btc_call_monitor = 'btc_call_monitor' in request.form
        
        alert_configs['btc_call'].strike = btc_call_strike
        alert_configs['btc_call'].premium = btc_call_premium
        alert_configs['btc_call'].is_monitoring = btc_call_monitor
        alert_configs['btc_call'].last_updated = datetime.now().isoformat()
        alert_configs['btc_call'].active_expiry = btc_bot.active_expiry
        
        # Update BTC Put config
        btc_put_strike_str = request.form.get('btc_put_strike', '')
        btc_put_strike = float(btc_put_strike_str) if btc_put_strike_str else 0
        btc_put_premium_str = request.form.get('btc_put_premium', '')
        btc_put_premium = float(btc_put_premium_str) if btc_put_premium_str else 0
        btc_put_monitor = 'btc_put_monitor' in request.form
        
        alert_configs['btc_put'].strike = btc_put_strike
        alert_configs['btc_put'].premium = btc_put_premium
        alert_configs['btc_put'].is_monitoring = btc_put_monitor
        alert_configs['btc_put'].last_updated = datetime.now().isoformat()
        alert_configs['btc_put'].active_expiry = btc_bot.active_expiry
        
        # Update ETH Call config
        eth_call_strike_str = request.form.get('eth_call_strike', '')
        eth_call_strike = float(eth_call_strike_str) if eth_call_strike_str else 0
        eth_call_premium_str = request.form.get('eth_call_premium', '')
        eth_call_premium = float(eth_call_premium_str) if eth_call_premium_str else 0
        eth_call_monitor = 'eth_call_monitor' in request.form
        
        alert_configs['eth_call'].strike = eth_call_strike
        alert_configs['eth_call'].premium = eth_call_premium
        alert_configs['eth_call'].is_monitoring = eth_call_monitor
        alert_configs['eth_call'].last_updated = datetime.now().isoformat()
        alert_configs['eth_call'].active_expiry = eth_bot.active_expiry
        
        # Update ETH Put config
        eth_put_strike_str = request.form.get('eth_put_strike', '')
        eth_put_strike = float(eth_put_strike_str) if eth_put_strike_str else 0
        eth_put_premium_str = request.form.get('eth_put_premium', '')
        eth_put_premium = float(eth_put_premium_str) if eth_put_premium_str else 0
        eth_put_monitor = 'eth_put_monitor' in request.form
        
        alert_configs['eth_put'].strike = eth_put_strike
        alert_configs['eth_put'].premium = eth_put_premium
        alert_configs['eth_put'].is_monitoring = eth_put_monitor
        alert_configs['eth_put'].last_updated = datetime.now().isoformat()
        alert_configs['eth_put'].active_expiry = eth_bot.active_expiry
        
        # Activate system if any alerts are monitored
        new_system_active = any(config.is_monitoring for config in alert_configs.values())
        
        # Send Telegram notifications for changes
        for config_id in alert_configs:
            new_config = asdict(alert_configs[config_id])
            old_config = old_configs.get(config_id, {})
            
            # Check if config actually changed
            if (old_config.get('strike', 0) != new_config['strike'] or
                old_config.get('premium', 0) != new_config['premium'] or
                old_config.get('is_monitoring', False) != new_config['is_monitoring']):
                
                send_config_update_telegram(config_id, old_config, new_config)
        
        # Send activation message
        if new_system_active:
            active_count = sum(1 for config in alert_configs.values() if config.is_monitoring)
            send_telegram(f"🚀 OPTION ALERT SYSTEM ACTIVATED!\n\n📊 Active alerts: {active_count}/4\n⏰ Time: {get_ist_time()}\n\nSystem is now monitoring configured alerts!")
            print(f"[{datetime.now()}] ✅ Option alert system activated with {active_count} alerts")
        else:
            send_telegram(f"⏸️ OPTION ALERT SYSTEM DEACTIVATED\n\n⏰ Time: {get_ist_time()}\n\nNo alerts are currently monitored.")
            print(f"[{datetime.now()}] ⏸️ Option alert system deactivated")
        
        return redirect('/?success=Alert+system+activated+successfully!')
        
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Error activating alerts: {e}")
        return redirect('/?success=Error+activating+alerts')

@app.route('/update_eth_threshold', methods=['POST'])
def update_eth_threshold():
    """Update ETH threshold for System 1"""
    try:
        new_threshold = float(request.form['threshold'])
        if new_threshold <= 0:
            return "Threshold must be positive", 400
        
        old_threshold = DELTA_THRESHOLD['ETH']
        DELTA_THRESHOLD['ETH'] = new_threshold
        
        # Send Telegram notification
        current_time_str = get_ist_time()
        send_telegram(f"⚙️ ETH Arbitrage Threshold Updated\n\n📊 New Value: ${new_threshold:.2f}\n⏰ Time: {current_time_str}\n\nThreshold changed successfully!")
        
        print(f"[{datetime.now()}] ✅ ETH threshold updated: ${old_threshold:.2f} → ${new_threshold:.2f}")
        
        return redirect('/?success=ETH+threshold+updated+successfully!')
    except ValueError:
        return "Invalid threshold value", 400
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Error updating ETH threshold: {e}")
        return "Error updating threshold", 500

@app.route('/update_btc_threshold', methods=['POST'])
def update_btc_threshold():
    """Update BTC threshold for System 1"""
    try:
        new_threshold = float(request.form['threshold'])
        if new_threshold <= 0:
            return "Threshold must be positive", 400
        
        old_threshold = DELTA_THRESHOLD['BTC']
        DELTA_THRESHOLD['BTC'] = new_threshold
        
        # Send Telegram notification
        current_time_str = get_ist_time()
        send_telegram(f"⚙️ BTC Arbitrage Threshold Updated\n\n📊 New Value: ${new_threshold:.2f}\n⏰ Time: {current_time_str}\n\nThreshold changed successfully!")
        
        print(f"[{datetime.now()}] ✅ BTC threshold updated: ${old_threshold:.2f} → ${new_threshold:.2f}")
        
        return redirect('/?success=BTC+threshold+updated+successfully!')
    except ValueError:
        return "Invalid threshold value", 400
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Error updating BTC threshold: {e}")
        return "Error updating threshold", 500

@app.route('/start_spike_detection', methods=['POST'])
def start_spike_detection():
    """Start System 3 Condition 1: Spike detection"""
    global spike_config
    
    if not spike_config.enabled_spike:
        spike_config.enabled_spike = True
        send_telegram(f"🚨 PRICE SPIKE DETECTION STARTED!\n\n⚡ Minimum Spike: {spike_config.min_spike_percent}%\n💰 Minimum Premium: ${spike_config.spike_min_premium:.2f}\n⏰ Cooldown: 120 seconds\n⏰ Time: {get_ist_time()}\n\nPrice spike detection is now active!")
        print(f"[{datetime.now()}] ✅ Price spike detection started")
    
    return redirect('/?success=Spike+detection+started!')

@app.route('/stop_spike_detection', methods=['POST'])
def stop_spike_detection():
    """Stop System 3 Condition 1: Spike detection"""
    global spike_config
    
    if spike_config.enabled_spike:
        spike_config.enabled_spike = False
        send_telegram(f"⏸️ PRICE SPIKE DETECTION STOPPED\n\n⏰ Time: {get_ist_time()}\n\nPrice spike detection paused.")
        print(f"[{datetime.now()}] ⏸️ Price spike detection stopped")
    
    return redirect('/?success=Spike+detection+stopped!')

@app.route('/start_spread_detection', methods=['POST'])
def start_spread_detection():
    """Start System 3 Condition 2: Spread detection"""
    global spike_config
    
    if not spike_config.enabled_spread:
        spike_config.enabled_spread = True
        send_telegram(f"🚨 BID-ASK SPREAD DETECTION STARTED!\n\n⚡ Minimum Spread: {spike_config.min_spread_percent}%\n💰 Minimum Premium: ${spike_config.spread_min_premium:.2f}\n⏰ Cooldown: 120 seconds\n⏰ Time: {get_ist_time()}\n\nBid-ask spread detection is now active!")
        print(f"[{datetime.now()}] ✅ Bid-ask spread detection started")
    
    return redirect('/?success=Spread+detection+started!')

@app.route('/stop_spread_detection', methods=['POST'])
def stop_spread_detection():
    """Stop System 3 Condition 2: Spread detection"""
    global spike_config
    
    if spike_config.enabled_spread:
        spike_config.enabled_spread = False
        send_telegram(f"⏸️ BID-ASK SPREAD DETECTION STOPPED\n\n⏰ Time: {get_ist_time()}\n\nBid-ask spread detection paused.")
        print(f"[{datetime.now()}] ⏸️ Bid-ask spread detection stopped")
    
    return redirect('/?success=Spread+detection+stopped!')

@app.route('/update_spike_config', methods=['POST'])
def update_spike_config():
    """Update System 3 dual condition configuration"""
    global spike_config
    
    try:
        old_config = asdict(spike_config)
        
        # Update configuration
        spike_config.min_spike_percent = float(request.form.get('min_spike_percent', 100.0))
        spike_config.spike_min_premium = float(request.form.get('spike_min_premium', 1.0))
        spike_config.min_spread_percent = float(request.form.get('min_spread_percent', 100.0))
        spike_config.spread_min_premium = float(request.form.get('spread_min_premium', 0.5))
        spike_config.monitor_eth = 'monitor_eth' in request.form
        spike_config.monitor_btc = 'monitor_btc' in request.form
        spike_config.monitor_calls = 'monitor_calls' in request.form
        spike_config.monitor_puts = 'monitor_puts' in request.form
        
        # Send Telegram notification
        current_time_str = get_ist_time()
        eth_status = "✅" if spike_config.monitor_eth else "❌"
        btc_status = "✅" if spike_config.monitor_btc else "❌"
        calls_status = "✅" if spike_config.monitor_calls else "❌"
        puts_status = "✅" if spike_config.monitor_puts else "❌"
        
        send_telegram(f"⚙️ DUAL CONDITION CONFIG UPDATED\n\n📊 Condition 1 (Price Spike): {spike_config.min_spike_percent}%\n💰 Min Premium: ${spike_config.spike_min_premium:.2f}\n📊 Condition 2 (Bid-Ask Spread): {spike_config.min_spread_percent}%\n💰 Min Premium: ${spike_config.spread_min_premium:.2f}\n⏰ Cooldown: 120 seconds (2 minutes)\n\n📡 Assets:\n{eth_status} ETH | {btc_status} BTC\n{calls_status} Calls | {puts_status} Puts\n\n⏰ Time: {current_time_str}")
        
        print(f"[{datetime.now()}] ✅ Dual condition config updated")
        
        return redirect('/?success=Spike+detector+configuration+updated!')
        
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Error updating spike config: {e}")
        return redirect('/?success=Error+updating+configuration')

@app.route('/health')
def health():
    current_time_str = get_ist_time()
    
    return {
        "system_1_arbitrage": {
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
            }
        },
        "system_2_option_alerts": {
            "active": new_system_active,
            "configs": {
                config_id: asdict(config) for config_id, config in alert_configs.items()
            },
            "last_check": last_check_time.isoformat() if last_check_time else None
        },
        "system_3_spike_detector": {
            "condition_1_spike": {
                "active": spike_config.enabled_spike,
                "min_spike_percent": spike_config.min_spike_percent,
                "min_premium": spike_config.spike_min_premium
            },
            "condition_2_spread": {
                "active": spike_config.enabled_spread,
                "min_spread_percent": spike_config.min_spread_percent,
                "min_premium": spike_config.spread_min_premium
            },
            "asset_filtering": {
                "monitor_eth": spike_config.monitor_eth,
                "monitor_btc": spike_config.monitor_btc,
                "monitor_calls": spike_config.monitor_calls,
                "monitor_puts": spike_config.monitor_puts
            },
            "cooldown": "120 seconds (2 minutes)"
        },
        "current_time": current_time_str,
        "expiry_display": format_expiry_display(eth_bot.active_expiry)
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
# Start All Systems
# -------------------------------
def start_bots():
    print("="*60)
    print("TRIPLE ALERT SYSTEM")
    print("="*60)
    print(f"⚡ System 1: Arbitrage Alerts")
    print(f"   • ETH Threshold: ${DELTA_THRESHOLD['ETH']:.2f}")
    print(f"   • BTC Threshold: ${DELTA_THRESHOLD['BTC']:.2f}")
    print(f"   • Quantity Check: Ask > 5 lots")
    print(f"🎯 System 2: Option Strike Alerts")
    print(f"   • 4 independent sections")
    print(f"   • Fixed call/put separation")
    print(f"🚨 System 3: Dual Condition Spike Detection")
    print(f"   • Condition 1: Price spike ≥ {spike_config.min_spike_percent}%")
    print(f"   • Condition 1 Premium Filter: ≥ ${spike_config.spike_min_premium:.2f}")
    print(f"   • Condition 2: Bid-ask spread ≥ {spike_config.min_spread_percent}%")
    print(f"   • Condition 2 Premium Filter: ≥ ${spike_config.spread_min_premium:.2f}")
    print(f"   • Cooldown: 120 seconds (2 minutes) fixed")
    print(f"📅 Current expiry: {get_current_expiry()}")
    print(f"🔄 Auto-expiry at 5:30 PM IST")
    print("="*60)
    
    # Start ETH WebSocket bot (all systems)
    eth_bot.start()
    
    # Start BTC REST API bot (all systems)
    btc_thread = threading.Thread(target=btc_bot.start_monitoring, daemon=True)
    btc_thread.start()
    
    print(f"[{datetime.now()}] ✅ All three systems started")

if __name__ == "__main__":
    start_bots()
    sleep(2)
    
    port = int(os.environ.get("PORT", 10000))
    print(f"[{datetime.now()}] 🌐 Website: http://localhost:{port}")
    print(f"[{datetime.now()}] 🚀 Starting web server on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
