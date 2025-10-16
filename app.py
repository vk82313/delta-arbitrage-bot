import requests
import time
from datetime import datetime

# ===== Telegram Config =====
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN"
TELEGRAM_CHAT_ID = "YOUR_CHAT_ID"

# ===== Delta Exchange Config =====
BASE_URL = "https://api.india.delta.exchange/v2"
ASSETS = ["BTC", "ETH"]
MIN_DIFF = {"BTC": 2, "ETH": 0.16}

# ===== Store last alerts to prevent spam (1 per strike per minute) =====
last_alert_ts = {}

# ===== Helper Functions =====
def fetch_products():
    resp = requests.get(f"{BASE_URL}/products")
    return resp.json().get("result", []) if resp.status_code == 200 else []

def fetch_tickers():
    resp = requests.get(f"{BASE_URL}/tickers")
    return resp.json().get("result", []) if resp.status_code == 200 else []

def parse_expiry(code):
    if not code or len(code) != 6:
        return code
    dd, mm, yy = code[:2], code[2:4], code[4:]
    return f"20{yy}-{mm}-{dd}"

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"})

# ===== Build Options Table =====
def build_options_table(asset, products, tickers):
    asset_upper = asset.upper()
    options = []
    ticker_map = {t['symbol']: t for t in tickers}
    ticker_map.update({t['symbol'].upper(): t for t in tickers})
    ticker_map.update({t['symbol'].replace("-", "").upper(): t for t in tickers})

    # Filter options
    candidates = [p for p in products if p.get("contract_type", "").lower() in ["call", "put", "option"]
                  and (asset_upper in (p.get("symbol") or "") or asset_upper in (p.get("underlying_asset", {}).get("symbol") or ""))]

    expiries = sorted({p["symbol"].split("-")[-1] for p in candidates})
    selected_expiry = expiries[0] if expiries else None

    for p in candidates:
        sym = p["symbol"]
        if not sym.endswith(selected_expiry):
            continue
        parts = sym.split("-")
        strike = next((int(x) for x in parts if x.isdigit()), None)
        if strike is None:
            continue
        expiry = parse_expiry(parts[-1])
        ticker = ticker_map.get(sym) or ticker_map.get(sym.upper()) or ticker_map.get(sym.replace("-", "").upper())
        bid = ticker.get("best_bid_price") or ticker.get("best_bid") or ticker.get("quotes", {}).get("best_bid") or ticker.get("bid")
        ask = ticker.get("best_ask_price") or ticker.get("best_ask") or ticker.get("quotes", {}).get("best_ask") or ticker.get("ask")
        options.append({
            "strike": strike,
            "expiry": expiry,
            "type": p["contract_type"].lower(),
            "bid": float(bid) if bid else None,
            "ask": float(ask) if ask else None
        })

    # Group by strike
    strikes = {}
    for opt in options:
        s = opt["strike"]
        if s not in strikes:
            strikes[s] = {"call": None, "put": None, "expiry": opt["expiry"]}
        if "call" in opt["type"]:
            strikes[s]["call"] = opt
        elif "put" in opt["type"]:
            strikes[s]["put"] = opt

    return [strikes[s] for s in sorted(strikes.keys())]

# ===== Check and Send Alerts =====
def check_arbitrage(asset, table):
    now = int(time.time() * 1000)
    alerts = []

    for i in range(len(table) - 1):
        curr = table[i]
        nxt = table[i + 1]
        strike = curr["call"]["strike"] if curr["call"] else curr["put"]["strike"]

        # Call alert
        if curr["call"] and nxt["call"] and curr["call"]["ask"] is not None and nxt["call"]["bid"] is not None:
            diff = nxt["call"]["bid"] - curr["call"]["ask"]
            if diff < 0 and abs(diff) >= MIN_DIFF[asset]:
                key = f"{asset}_CALL_{strike}"
                last = last_alert_ts.get(key, 0)
                if now - last >= 60000:  # 1 alert per strike per minute
                    alerts.append(f"CALL {strike}: Ask {curr['call']['ask']} vs Next Bid {nxt['call']['bid']} → Δ {abs(diff):.2f}")
                    last_alert_ts[key] = now

        # Put alert
        if curr["put"] and nxt["put"] and curr["put"]["bid"] is not None and nxt["put"]["ask"] is not None:
            diff = curr["put"]["bid"] - nxt["put"]["ask"]
            if diff < 0 and abs(diff) >= MIN_DIFF[asset]:
                key = f"{asset}_PUT_{strike}"
                last = last_alert_ts.get(key, 0)
                if now - last >= 60000:  # 1 alert per strike per minute
                    alerts.append(f"PUT {strike}: Next Ask {nxt['put']['ask']} vs Bid {curr['put']['bid']} → Δ {abs(diff):.2f}")
                    last_alert_ts[key] = now

    if alerts:
        header = f"*{asset} OPTIONS ALERTS*\nUpdated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        send_telegram(header + "\n".join(alerts))

# ===== Main Loop: Fetch every second =====
def main_loop():
    while True:
        try:
            products = fetch_products()
            tickers = fetch_tickers()
            for asset in ASSETS:
                table = build_options_table(asset, products, tickers)
                check_arbitrage(asset, table)  # alerts only sent if conditions met
            time.sleep(1)  # fetch every second
        except Exception as e:
            print("Error:", e)
            time.sleep(1)

if __name__ == "__main__":
    main_loop()
