import websocket
import json
import brotli
import base64
import requests
import os
from datetime import datetime
from time import sleep

# -------------------------------
# Telegram Configuration
# -------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Minimum Δ threshold for alerts
DELTA_THRESHOLD = {"BTC": 2, "ETH": 0.16}

# Minimum time between alerts per strike in seconds
ALERT_COOLDOWN = 60

# -------------------------------
# Delta WebSocket Client
# -------------------------------
class DeltaOptionsBot:
    def __init__(self):
        self.ws_url = "wss://socket.india.delta.exchange"
        self.ws = None
        self.last_alert_time = {}  # Track last alert per strike
        self.options_prices = {}   # Current bid/ask
        self.assets = ["BTC", "ETH"]

    # ---------------------------
    # WebSocket Callbacks
    # ---------------------------
    def on_open(self, ws):
        print(f"[{datetime.now()}] Connected to Delta Exchange WebSocket")
        self.subscribe_all_options()

    def on_close(self, ws, close_status_code, close_msg):
        print(f"[{datetime.now()}] WebSocket closed: {close_status_code} - {close_msg}")

    def on_error(self, ws, error):
        print(f"[{datetime.now()}] WebSocket error: {error}")

    def on_message(self, ws, message):
        try:
            msg = json.loads(message)
            if msg.get("type") == "l1ob_c":
                self.process_bid_ask(msg)
            elif msg.get("type") in ["success", "error"]:
                print(f"[{datetime.now()}] {msg.get('type').upper()}: {msg.get('message')}")
        except Exception as e:
            print(f"[{datetime.now()}] Message processing error: {e}")

    # ---------------------------
    # Subscribe to current options
    # ---------------------------
    def subscribe_all_options(self):
        expiries = self.get_current_expiries()
        if not expiries:
            print(f"[{datetime.now()}] No expiries found, retrying in 10s...")
            sleep(10)
            expiries = self.get_current_expiries()

        payload = {
            "type": "subscribe",
            "payload": {
                "channels": [{"name": "l1ob_c", "symbols": expiries}]
            }
        }
        self.ws.send(json.dumps(payload))
        print(f"[{datetime.now()}] Subscribed to options: {expiries}")

    # ---------------------------
    # Fetch current expiries dynamically
    # ---------------------------
    def get_current_expiries(self):
        expiries = []
        try:
            resp = requests.get("https://api.india.delta.exchange/v2/products").json()
            products = resp.get("result", [])
            for p in products:
                sym = p.get("symbol", "")
                underlying = p.get("underlying_asset", {}).get("symbol", "")
                ctype = str(p.get("contract_type", "")).lower()
                if ctype in ["call", "put", "option"]:
                    for asset in self.assets:
                        if asset in sym or asset in underlying:
                            expiries.append(sym)
        except Exception as e:
            print(f"[{datetime.now()}] Error fetching expiries: {e}")
        return expiries

    # ---------------------------
    # Brotli Decompression
    # ---------------------------
    def decompress_brotli(self, compressed):
        try:
            decoded = base64.b64decode(compressed)
            decompressed = brotli.decompress(decoded)
            return json.loads(decompressed.decode("utf-8"))
        except Exception as e:
            print(f"[{datetime.now()}] Decompression error: {e}")
            return []

    # ---------------------------
    # Process bid/ask updates
    # ---------------------------
    def process_bid_ask(self, msg):
        data = self.decompress_brotli(msg.get("c", ""))
        if not data:
            return

        asset_options = {asset: [] for asset in self.assets}

        # Update current prices
        for option in data:
            sym = option.get("s")
            d = option.get("d", [])
            if len(d) < 4:
                continue
            best_ask = float(d[0]) if d[0] else None
            best_bid = float(d[2]) if d[2] else None
            self.options_prices[sym] = {"bid": best_bid, "ask": best_ask}

            for asset in self.assets:
                if sym.startswith(("C-"+asset, "P-"+asset)):
                    asset_options[asset].append({
                        "symbol": sym,
                        "bid": best_bid,
                        "ask": best_ask
                    })

        # Generate and send alerts per asset
        for asset in self.assets:
            self.generate_alert(asset, asset_options[asset])

    # ---------------------------
    # Generate Telegram Alerts
    # ---------------------------
    def generate_alert(self, asset, options):
        if not options or len(options) < 2:
            return

        options_sorted = sorted(options, key=lambda x: x["symbol"])
        alerts = []

        for i in range(len(options_sorted)-1):
            curr = options_sorted[i]
            nxt = options_sorted[i+1]
            delta_threshold = DELTA_THRESHOLD[asset]

            # Call option alert logic
            if curr["symbol"].startswith("C-"+asset):
                if curr["ask"] is not None and nxt["bid"] is not None:
                    delta = curr["ask"] - nxt["bid"]
                    if delta >= delta_threshold:
                        key = curr["symbol"]
                        now = datetime.now().timestamp()
                        last_time = self.last_alert_time.get(key, 0)
                        if now - last_time >= ALERT_COOLDOWN:
                            alerts.append({
                                "type": "CALL",
                                "strike": curr["symbol"].split("-")[2],
                                "next_strike": nxt["symbol"].split("-")[2],
                                "ask": curr["ask"],
                                "next_bid": nxt["bid"],
                                "delta": delta
                            })
                            self.last_alert_time[key] = now

            # Put option alert logic
            if curr["symbol"].startswith("P-"+asset):
                if curr["bid"] is not None and nxt["ask"] is not None:
                    delta = curr["bid"] - nxt["ask"]
                    if delta >= delta_threshold:
                        key = curr["symbol"]
                        now = datetime.now().timestamp()
                        last_time = self.last_alert_time.get(key, 0)
                        if now - last_time >= ALERT_COOLDOWN:
                            alerts.append({
                                "type": "PUT",
                                "strike": curr["symbol"].split("-")[2],
                                "next_strike": nxt["symbol"].split("-")[2],
                                "bid": curr["bid"],
                                "next_ask": nxt["ask"],
                                "delta": delta
                            })
                            self.last_alert_time[key] = now

        # Sort alerts by delta descending
        alerts = sorted(alerts, key=lambda x: x["delta"], reverse=True)

        # Send message if any alerts
        if alerts:
            msg_lines = [f"*{asset} OPTIONS ALERT*\nUpdated: {datetime.now().strftime('%d-%m-%Y %H:%M:%S')}\n"]
            for a in alerts:
                if a["type"] == "CALL":
                    msg_lines.append(f'CALL ⚡\nStrike: {a["strike"]} → Next: {a["next_strike"]}\nAsk: {a["ask"]} | Next Bid: {a["next_bid"]}\nΔ: {a["delta"]:.2f}\n')
                else:
                    msg_lines.append(f'PUT ⚡\nStrike: {a["strike"]} → Next: {a["next_strike"]}\nBid: {a["bid"]} | Next Ask: {a["next_ask"]}\nΔ: {a["delta"]:.2f}\n')

            message = "\n".join(msg_lines)
            self.send_telegram(message)

    # ---------------------------
    # Send Telegram
    # ---------------------------
    def send_telegram(self, message):
        if not TELEGRA_MBOT_TOKEN or not TELEGRAM_CHAT_ID:
            print(f"[{datetime.now()}] Telegram not configured.")
            return
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"})
            if resp.status_code == 200:
                print(f"[{datetime.now()}] Telegram alert sent.")
            else:
                print(f"[{datetime.now()}] Telegram error: {resp.text}")
        except Exception as e:
            print(f"[{datetime.now()}] Telegram send error: {e}")

    # ---------------------------
    # Connect WebSocket
    # ---------------------------
    def connect(self):
        self.ws = websocket.WebSocketApp(
            self.ws_url,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        self.ws.run_forever()


# -------------------------------
# Main
# -------------------------------
if __name__ == "__main__":
    print("="*50)
    print("Delta Options Bid/Ask Monitor with Telegram Alerts")
    print("="*50)
    bot = DeltaOptionsBot()
    try:
        bot.connect()
    except KeyboardInterrupt:
        print("\nStopping bot...")
    except Exception as e:
        print(f"Error: {e}")
