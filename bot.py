import sqlite3
import os
import json
import requests
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")

DATA_FILE = Path("triggers.json")

# How often to check prices, in seconds
CHECK_INTERVAL = 300  # 5 mins

# Default near-trigger rule
PCT_THRESHOLD = 0.002  # 0.2%

# Absolute buffers by symbol
# Adjust these based on how noisy each pair is
ABS_THRESHOLDS = {
    "EUR/USD": 0.0015,   # 15 pips
    "GBP/USD": 0.0015,
    "AUD/USD": 0.0015,
    "NZD/USD": 0.0015,
    "USD/JPY": 0.15,    # 15 pips
    "EUR/JPY": 0.15,
    "GBP/JPY": 0.20,
    "AUD/JPY": 0.15,
    "XAU/USD": 5.0,     # $5
    "BTC/USD": 300.0,
    "ETH/USD": 30.0,
}

def run_web_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Bot is running!")

    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()
    
def load_data():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text())
    return {}


def save_data(data):
    DATA_FILE.write_text(json.dumps(data, indent=2))


def normalize_symbol(symbol: str) -> str:
    symbol = symbol.upper().replace("-", "/")

    # Convert USDJPY to USD/JPY
    if "/" not in symbol and len(symbol) == 6:
        symbol = symbol[:3] + "/" + symbol[3:]

    # Convert XAUUSD to XAU/USD
    if symbol == "XAUUSD":
        symbol = "XAU/USD"

    return symbol


def get_price(symbol: str):
    url = "https://api.twelvedata.com/quote"
    params = {
        "symbol": symbol,
        "apikey": TWELVE_DATA_API_KEY,
    }

    response = requests.get(url, params=params, timeout=10)
    data = response.json()

    if "close" not in data:
        raise ValueError(f"Price not found for {symbol}: {data}")

    return float(data["close"])


def is_near_trigger(symbol, current_price, trigger_price):
    distance = abs(current_price - trigger_price)

    pct_condition = distance / trigger_price <= PCT_THRESHOLD

    abs_threshold = ABS_THRESHOLDS.get(symbol)
    abs_condition = abs_threshold is not None and distance <= abs_threshold

    return pct_condition or abs_condition, distance


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send /watch followed by your trigger list.\n\n"
        "Example:\n"
        "/watch\n"
        "USDJPY 155.20\n"
        "EURUSD 1.0800\n"
        "XAUUSD 2350\n"
        "BTCUSD 65000"
    )


async def watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    text = update.message.text.replace("/watch", "").strip()

    if not text:
        await update.message.reply_text("Send your trigger list after /watch.")
        return

    data = load_data()
    data[chat_id] = []

    lines = text.splitlines()

    for line in lines:
        parts = line.split()
        if len(parts) < 2:
            continue

        symbol = normalize_symbol(parts[0])
        trigger_price = float(parts[1])

        data[chat_id].append({
            "symbol": symbol,
            "trigger_price": trigger_price,
            "alerted": False,
        })

    save_data(data)

    await update.message.reply_text(
        f"Saved {len(data[chat_id])} trigger(s). I’ll check every {CHECK_INTERVAL // 60} mins."
    )


async def list_triggers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = load_data()
    triggers = data.get(chat_id, [])

    if not triggers:
        await update.message.reply_text("No triggers saved.")
        return

    msg = "Your triggers:\n\n"
    for t in triggers:
        status = "alerted" if t["alerted"] else "watching"
        msg += f'{t["symbol"]} near {t["trigger_price"]} — {status}\n'

    await update.message.reply_text(msg)


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = load_data()
    data[chat_id] = []
    save_data(data)

    await update.message.reply_text("Cleared all triggers.")


async def check_prices(context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    changed = False

    for chat_id, triggers in data.items():
        for t in triggers:
            symbol = t["symbol"]
            trigger_price = t["trigger_price"]

            try:
                current_price = get_price(symbol)
            except Exception as e:
                print(f"Error getting price for {symbol}: {e}")
                continue

            near, distance = is_near_trigger(symbol, current_price, trigger_price)

            # Send alert only once when price first becomes near
            if near and not t["alerted"]:
                await context.bot.send_message(
                    chat_id=int(chat_id),
                    text=(
                        f"Price near trigger\n\n"
                        f"{symbol}\n"
                        f"Trigger: {trigger_price}\n"
                        f"Current: {current_price}\n"
                        f"Distance: {distance:.5f}"
                    )
                )
                t["alerted"] = True
                changed = True

            # Reset alert if price moves far away again
            elif not near and t["alerted"]:
                t["alerted"] = False
                changed = True

    if changed:
        save_data(data)


def main():
    threading.Thread(target=run_web_server, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("watch", watch))
    app.add_handler(CommandHandler("list", list_triggers))
    app.add_handler(CommandHandler("clear", clear))

    app.job_queue.run_repeating(check_prices, interval=CHECK_INTERVAL, first=10)

    app.run_polling()


if __name__ == "__main__":
    main()