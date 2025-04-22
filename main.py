import os
import json
import datetime
import threading
import time
import csv
from collections import deque

import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from dotenv import load_dotenv
from binance.client import Client  # Binance API

# Загрузка токенов
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не найден в .env файле")

BINANCE_API_KEY = os.getenv("API_KEY_BINANCE")
BINANCE_API_SECRET = os.getenv("API_SECRET_BINANCE")
if not BINANCE_API_KEY or not BINANCE_API_SECRET:
    raise ValueError("Binance ключи не найдены в .env файле")

binance_client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

# Файлы
FEEDBACK_FILE = "feedback_data.json"
HISTORY_FILE = "signal_history.csv"

# Инициализация хранения
if not os.path.exists(FEEDBACK_FILE):
    with open(FEEDBACK_FILE, "w") as f:
        json.dump({}, f)

history = {}
if os.path.exists(HISTORY_FILE):
    with open(HISTORY_FILE, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            c = row["coin"]
            history.setdefault(c, deque(maxlen=1000)).append(float(row["change"]))

# Данные
def get_json(url):
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[ERROR] API error: {e}")
        return None

def get_html(url):
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"[ERROR] HTML error: {e}")
        return None

def get_coin_data(coin_id: str):
    d = get_json(f"https://api.coingecko.com/api/v3/coins/{coin_id}")
    if not d:
        return None
    m = d.get("market_data", {})
    return {
        "name": d.get("name"),
        "symbol": d.get("symbol"),
        "price": m.get("current_price", {}).get("usd"),
        "change_24h": m.get("price_change_percentage_24h"),
        "volume": m.get("total_volume", {}).get("usd"),
        "market_cap": m.get("market_cap", {}).get("usd"),
        "reddit": d.get("community_data", {}).get("reddit_subscribers"),
        "alexa": d.get("public_interest_stats", {}).get("alexa_rank"),
    }

def get_binance_price(symbol="BTCUSDT"):
    try:
        ticker = binance_client.get_symbol_ticker(symbol=symbol)
        return float(ticker["price"])
    except Exception as e:
        print(f"[ERROR] Binance API: {e}")
        return None

def get_news():
    html = get_html("https://cryptopanic.com/news/")
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    items = soup.find_all("h2")[:5]
    return [i.get_text(strip=True) for i in items]

def get_events(coin: str):
    html = get_html(f"https://coinmarketcal.com/en/coin/{coin}")
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    items = soup.find_all("h5")[:3]
    return [i.get_text(strip=True) for i in items]

def twitter_mentions(coin: str):
    html = get_html(f"https://nitter.net/search?f=tweets&q={coin}")
    if not html:
        return 0
    soup = BeautifulSoup(html, "lxml")
    return len(soup.find_all("div", {"class": "tweet-content"}))

def reddit_mentions(coin: str):
    html = get_html(f"https://www.reddit.com/search/?q={coin}")
    if not html:
        return 0
    soup = BeautifulSoup(html, "lxml")
    return len(soup.find_all("div", {"data-testid": "post-container"}))

def get_cointelegraph():
    html = get_html("https://cointelegraph.com/tags/bitcoin")
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    items = soup.find_all("span", {"class": "post-card-inline__title"})[:5]
    return [i.get_text(strip=True) for i in items]

def get_github_activity(coin: str):
    try:
        r = requests.get(f"https://github.com/{coin}", timeout=10)
        return r.status_code == 200
    except:
        return False

# Анализ
def analyze(data, news, events, tw_count, rd_count, extra_news, github):
    score = 0
    reasons = []
    now = datetime.datetime.utcnow()

    news = [n for n in news if any(unit in n for unit in ("hour", "day")) and int(n.split()[0]) <= 2]
    extra_news = [e for e in extra_news if ("2024" in e or "2025" in e)]

    ch = data["change_24h"]
    if isinstance(ch, (int, float)):
        if ch > 3:
            score += 2; reasons.append("Цена выросла >3% за 24ч")
        elif ch < -3:
            score -= 2; reasons.append("Цена упала < -3% за 24ч")
    if data["volume"] and data["volume"] > 1e8:
        score += 1; reasons.append("Высокий объем торгов")
    if data["reddit"] and data["reddit"] > 10000:
        score += 1; reasons.append("Большое сообщество Reddit")
    if data["alexa"] and data["alexa"] < 100000:
        score += 1; reasons.append("Популярность сайта")
    if news:
        score += 1; reasons.append("Свежие новости (CryptoPanic)")
    if extra_news:
        score += 1; reasons.append("Новости (CoinTelegraph)")
    if events:
        score += 1; reasons.append("Будущие события (CMC)")
    if tw_count > 10:
        score += 1; reasons.append("Активность в Twitter")
    if rd_count > 5:
        score += 1; reasons.append("Активность на Reddit")
    if github:
        score += 1; reasons.append("GitHub активность подтверждена")

    trend = "ВВЕРХ" if score >= 4 else "ВНИЗ" if score <= -2 else "ФЛЭТ"
    symbol = data["symbol"].lower()
    if symbol not in history:
        history[symbol] = deque(maxlen=1000)
    if isinstance(ch, (int, float)):
        history[symbol].append(ch)
        with open(HISTORY_FILE, "a", newline="") as f:
            w = csv.writer(f)
            if f.tell() == 0:
                w.writerow(["coin", "change"])
            w.writerow([symbol, ch])
    avg = sum(history[symbol]) / len(history[symbol]) if history[symbol] else 0

    confidence = min(100, abs(score) * 12 + 40)
    if confidence >= 75 and score > 2:
        truth = "ПРАВДА"
    elif confidence <= 55 or score <= 1:
        truth = "ЛОЖЬ"
    else:
        truth = "СОМНИТЕЛЬНО"

    ts = now.strftime("%Y-%m-%d %H:%M UTC")
    return trend, confidence, reasons, truth, avg, ts

# Команды Telegram
async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Укажи монету: /analyze bitcoin")
    coin = context.args[0].lower()
    await update.message.reply_text(f"Анализирую {coin} в реальном времени...")
    data = get_coin_data(coin)
    if not data:
        return await update.message.reply_text("Монета не найдена или данные временно недоступны.")
    news = get_news()
    events = get_events(coin)
    tw = twitter_mentions(coin)
    rd = reddit_mentions(coin)
    tg = get_cointelegraph()
    gh = get_github_activity(coin)

    trend, conf, reasons, truth, avg, ts = analyze(data, news, events, tw, rd, tg, gh)

    binance_symbol = data["symbol"].upper() + "USDT"
    binance_price = get_binance_price(binance_symbol)

    msg = (
        f"Монета: {data['name']} ({data['symbol'].upper()})\n"
        f"Цена (Coingecko): ${data['price']}\n"
        f"Цена (Binance): ${binance_price if binance_price else 'N/A'}\n"
        f"Изм.24ч: {data['change_24h']}%  Ср.изм.: {avg:.2f}%\n"
        f"Тренд: {trend} ({conf}% увер.)\n"
        f"Оценка: {truth}\n"
        f"Актуально: {ts}\n\n"
        "Причины:\n" + "\n".join(f"- {r}" for r in reasons)
    )
    await update.message.reply_text(msg[:4000])

async def feedback_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        return await update.message.reply_text("Формат: /feedback <coin> <correct/wrong>")
    coin, status = context.args[0].lower(), context.args[1].lower()
    db = json.load(open(FEEDBACK_FILE, "r"))
    db.setdefault(coin, []).append({"time": datetime.datetime.utcnow().isoformat(), "result": status})
    json.dump(db, open(FEEDBACK_FILE, "w"), indent=2)
    await update.message.reply_text(f"Обратная связь для {coin}: {status.upper()}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buttons = [
        [
            InlineKeyboardButton("Bitcoin", callback_data="analyze_bitcoin"),
            InlineKeyboardButton("Ethereum", callback_data="analyze_ethereum"),
        ],
        [
            InlineKeyboardButton("Solana", callback_data="analyze_solana"),
            InlineKeyboardButton("BNB", callback_data="analyze_binancecoin"),
        ],
    ]
    await update.message.reply_text("Выбери монету:", reply_markup=InlineKeyboardMarkup(buttons))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    coin = q.data.split("_", 1)[1]
    update.message = q.message  # фиксим объект для повторного использования
    context.args = [coin]
    await analyze_command(update, context)

def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("analyze", analyze_command))
    app.add_handler(CommandHandler("feedback", feedback_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    print("БОТ ЗАПУЩЕН")
    app.run_polling()

if __name__ == "__main__":
    main()
