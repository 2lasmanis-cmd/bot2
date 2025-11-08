import requests
import time
import schedule
from telegram import Bot
import os
from typing import Dict, List, Tuple, Optional

# === CONFIGURATION (Use Environment Variables on Render) ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
MARKET_CAP_LIMIT = 100_000_000   # 100 million USD
OI_ALERT_RATIO = 0.25            # 25% threshold

# Validate config
if not TELEGRAM_TOKEN or not CHAT_ID:
    raise EnvironmentError("Please set TELEGRAM_TOKEN and CHAT_ID as environment variables on Render.")

bot = Bot(token=TELEGRAM_TOKEN)


def get_market_data() -> Dict[str, Dict]:
    """Fetch coins and market caps from CoinGecko (top 250 by volume)"""
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": 250,
        "page": 1,
        "sparkline": False
    }
    
    try:
        print("Fetching market data from CoinGecko...")
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()

        if not isinstance(data, list):
            print(f"Unexpected CoinGecko response format: {type(data)}")
            return {}

        coins = {}
        for coin in data:
            symbol = coin.get("symbol", "").upper()
            market_cap = coin.get("market_cap")
            
            if not symbol or market_cap is None:
                continue
                
            if market_cap > 0 and market_cap < MARKET_CAP_LIMIT:
                coins[symbol] = {
                    "name": coin.get("name", "Unknown"),
                    "market_cap": float(market_cap)
                }
        
        print(f"Loaded {len(coins)} coins with market cap < ${MARKET_CAP_LIMIT/1e6:.0f}M")
        return coins

    except requests.exceptions.RequestException as e:
        print(f"CoinGecko API error: {e}")
        return {}
    except Exception as e:
        print(f"Unexpected error in get_market_data: {e}")
        return {}


def get_all_bybit_oi() -> Dict[str, float]:
    """Get Open Interest for ALL Bybit USDT perpetual futures"""
    oi_data = {}

    # Step 1: Get all linear (USDT) symbols
    url_info = "https://api.bybit.com/v5/market/instruments-info"
    try:
        print("Fetching Bybit instrument list...")
        resp = requests.get(url_info, params={"category": "linear"}, timeout=15)
        resp.raise_for_status()
        json_resp = resp.json()

        if json_resp.get("retCode") != 0:
            print(f"Bybit API error: {json_resp.get('retMsg')}")
            return {}

        symbols = [
            i["symbol"] for i in json_resp["result"]["list"]
            if i["symbol"].endswith("USDT") and i.get("status") == "Trading"
        ]
        print(f"Found {len(symbols)} active USDT perpetual contracts on Bybit.")

    except Exception as e:
        print(f"Error fetching Bybit symbols: {e}")
        return {}

    # Step 2: Fetch Open Interest for each symbol
    url_oi = "https://api.bybit.com/v5/market/open-interest"
    batch_size = 10
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        for sym in batch:
            try:
                params = {"category": "linear", "symbol": sym}
                r = requests.get(url_oi, params=params, timeout=10)
                r.raise_for_status()
                data = r.json()

                if data.get("retCode") != 0:
                    continue

                oi_list = data.get("result", {}).get("list", [])
                if oi_list:
                    oi_value = float(oi_list[0].get("openInterest", 0))
                    oi_usd = float(oi_list[0].get("openInterestUsd", 0))
                    base_coin = sym.replace("USDT", "")
                    oi_data[base_coin] = oi_usd

                time.sleep(0.12)  # Stay under Bybit rate limit (~8 req/sec)

            except Exception as e:
                print(f"Error fetching OI for {sym}: {e}")
                time.sleep(0.5)

        if i + batch_size < len(symbols):
            time.sleep(1)  # Be gentle on API

    print(f"Retrieved OI for {len(oi_data)} symbols.")
    return oi_data


def check_oi_ratio():
    """Compare OI vs Market Cap and send alerts"""
    print("Starting OI/Market Cap ratio check...")
    mc_data = get_market_data()
    oi_data = get_all_bybit_oi()

    if not mc_data:
        print("No market cap data available. Skipping this run.")
        return
    if not oi_data:
        print("No OI data available. Skipping this run.")
        return

    alerts: List[Tuple[str, float, float, float]] = []
    for symbol, coin in mc_data.items():
        if symbol in oi_data:
            oi = oi_data[symbol]
            mc = coin["market_cap"]
            if mc <= 0:
                continue
            ratio = oi / mc
            if ratio > OI_ALERT_RATIO:
                alerts.append((symbol, ratio, mc, oi))

    # Sort by ratio descending
    alerts.sort(key=lambda x: x[1], reverse=True)

    if alerts:
        msg = "High OI/Market Cap Ratio (Bybit USDT)\n"
        msg += f"Threshold: >{OI_ALERT_RATIO:.0%}\n\n"
        for sym, ratio, mc, oi in alerts[:10]:  # Top 10 only
            msg += f"{sym} • {ratio:.1%}\n"
            msg += f"   MC: ${mc/1e6:.1f}M • OI: ${oi/1e6:.1f}M\n"
        try:
            bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
            print(f"Sent {len(alerts)} alert(s) to Telegram.")
        except Exception as e:
            print(f"Failed to send Telegram message: {e}")
    else:
        print("No coins exceeded OI/MC threshold.")


def job():
    """Scheduled job wrapper"""
    print("\n" + "="*50)
    print("Checking Bybit OI ratios...")
    print("="*50)
    try:
        check_oi_ratio()
    except Exception as e:
        print(f"Job failed: {e}")
    print("Check complete.\n")


# === MAIN ===
if _name_ == "_main_":
    print("OI Alert Bot started (Bybit ALL pairs, 5min)...")
    
    # Run immediately on start
    job()
    
    # Schedule every 5 minutes
    schedule.every(5).minutes.do(job)
    
    print("Scheduler active. Waiting for next run...\n")
    
    # Keep alive loop
    while True:
        try:
            schedule.run_pending()
            time.sleep(5)
        except KeyboardInterrupt:
            print("\nBot stopped by user.")
            break
        except Exception as e:
            print(f"Unexpected error in main loop: {e}")
            time.sleep(10)