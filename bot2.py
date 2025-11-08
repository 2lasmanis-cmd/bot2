# o_alert_bot.py   (nom du fichier sur Render : bot2.py ou o_alert_bot.py)
import requests
import time
import schedule
from telegram import Bot
import os
from typing import Dict, List, Tuple
from flask import Flask, jsonify
from threading import Thread

# ---------- CONFIG ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID        = os.getenv("CHAT_ID")
MARKET_CAP_LIMIT = 100_000_000      # 100 M$
OI_ALERT_RATIO   = 0.25             # 25 %

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise EnvironmentError("TELEGRAM_TOKEN et CHAT_ID obligatoires (variables d’environnement).")

bot = Bot(token=TELEGRAM_TOKEN)

# ---------- FONCTIONS (identiques à la version précédente) ----------
def get_market_data() -> Dict[str, Dict]:
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": 250,
        "page": 1,
        "sparkline": False
    }
    try:
        print("Récupération CoinGecko...")
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            return {}
        coins = {}
        for c in data:
            sym = c.get("symbol", "").upper()
            mc  = c.get("market_cap")
            if sym and mc and 0 < mc < MARKET_CAP_LIMIT:
                coins[sym] = {"name": c.get("name","?"), "market_cap": float(mc)}
        print(f"{len(coins)} monnaies < ${MARKET_CAP_LIMIT/1e6:.0f}M")
        return coins
    except Exception as e:
        print("CoinGecko error:", e)
        return {}

def get_all_bybit_oi() -> Dict[str, float]:
    oi = {}
    # 1. Liste des contrats USDT
    try:
        r = requests.get("https://api.bybit.com/v5/market/instruments-info",
                         params={"category":"linear"}, timeout=15)
        r.raise_for_status()
        symbols = [i["symbol"] for i in r.json()["result"]["list"]
                   if i["symbol"].endswith("USDT") and i.get("status")=="Trading"]
        print(f"{len(symbols)} contrats USDT trouvés")
    except Exception as e:
        print("Bybit symbols error:", e)
        return {}

    # 2. OI pour chaque symbole
    url_oi = "https://api.bybit.com/v5/market/open-interest"
    for sym in symbols:
        try:
            r = requests.get(url_oi, params={"category":"linear","symbol":sym}, timeout=10)
            r.raise_for_status()
            d = r.json()
            if d.get("retCode") != 0: continue
            lst = d.get("result",{}).get("list",[])
            if lst:
                oi_usd = float(lst[0].get("openInterestUsd",0))
                oi[sym.replace("USDT","")] = oi_usd
            time.sleep(0.12)               # respect des limites Bybit
        except Exception as e:
            print(f"OI {sym} error:", e)
    print(f"OI récupéré pour {len(oi)} symboles")
    return oi

def check_oi_ratio():
    mc = get_market_data()
    oi = get_all_bybit_oi()
    if not mc or not oi: return

    alerts = []
    for sym, coin in mc.items():
        if sym in oi:
            ratio = oi[sym] / coin["market_cap"]
            if ratio > OI_ALERT_RATIO:
                alerts.append((sym, ratio, coin["market_cap"], oi[sym]))
    alerts.sort(key=lambda x: x[1], reverse=True)

    if alerts:
        msg = "Ratio OI/MC élevé (Bybit USDT)\n_Seuil >25%_\n\n"
        for sym, r, mc, oi_val in alerts[:10]:
            msg += f"{sym} • {r:.1%}\n   MC ${mc/1e6:.1f}M • OI ${oi_val/1e6:.1f}M\n"
        try:
            bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
            print(f"{len(alerts)} alerte(s) envoyée(s)")
        except Exception as e:
            print("Telegram error:", e)
    else:
        print("Aucun dépassement de seuil")

def job():
    print("\n"+"="*50)
    print("Vérification OI / MC")
    print("="*50)
    try:
        check_oi_ratio()
    except Exception as e:
        print("Job échoué:", e)

# ---------- FLASK (keep-alive) ----------
app = Flask(__name__)          # <-- __name_ (pas name)

@app.route('/health')
def health():
    return jsonify(status="ok", bot="running")

@app.route('/')
def home():
    return jsonify(message="Bot OI actif – checks toutes les 5 min")

# ---------- LANCEMENT ----------
def run_scheduler():
    job()                                 # 1er run immédiat
    schedule.every(5).minutes.do(job)
    while True:
        schedule.run_pending()
        time.sleep(5)

if _name_ == "_main_":
    Thread(target=run_scheduler, daemon=True).start()
    print("Thread bot démarré – lancement Flask")
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

