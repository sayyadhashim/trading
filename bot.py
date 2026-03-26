import yfinance as yf
import pandas as pd
import numpy as np
import time
import requests
from datetime import datetime
import warnings
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
import pytz # ADDED FOR INDIAN TIMEZONE

warnings.filterwarnings('ignore')

# =============================================================================
# CONFIGURATION & TELEGRAM SETUP
# =============================================================================
# WATCHLIST
TICKERS = ["SBIN.NS", "WIPRO.NS", "RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS", "TMPV.NS", "TMCV.NS"]


ENTRY_THRESH = 0.60
ATR_MULT = 1.5
RR_RATIO = 3.0

# YOUR TELEGRAM CREDENTIALS
TELEGRAM_TOKEN = "8701070280:AAHPIDZpQZLHGar0HEh6f84SEJcJGHbWQys"
TELEGRAM_CHAT_ID = "8125685903"

# NEW: Bot Memory so it doesn't spam you twice for the same trade!
last_signal_time = {}

def send_telegram_alert(message):
    """Sends the alert directly to your phone via Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Failed to send Telegram alert: {e}")

# =============================================================================
# PURE PANDAS MATH
# =============================================================================
def fetch_live_data(symbol):
    df = yf.download(symbol, period="7d", interval="5m", progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.strip().lower() for c in df.columns]
    df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'}, inplace=True)
    return df.dropna()

def add_regime_labels(df, atr_period=14, threshold=0.6):
    df = df.copy()
    high_low = df['High'] - df['Low']
    high_close = np.abs(df['High'] - df['Close'].shift(1))
    low_close = np.abs(df['Low'] - df['Close'].shift(1))
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = tr.rolling(atr_period).mean()
    
    df['prev_atr'] = df['atr'].shift(1)
    df['up_diff'] = (df['High'] - df['Open']) / df['prev_atr']
    df['down_diff'] = (df['Open'] - df['Low']) / df['prev_atr']
    
    df['regime'] = 'r'
    mask = (df['up_diff'] >= threshold) | (df['down_diff'] >= threshold)
    df.loc[mask, 'regime'] = 't'
    return df.dropna(subset=['prev_atr'])

def compute_probabilities(states):
    s = [1 if x == 't' else 0 for x in states]
    n = len(s)
    second = {}
    for i in range(2, n):
        key = (s[i-2], s[i-1])
        second.setdefault(key, [0, 0])[1] += 1
        if s[i] == 1:
            second[key][0] += 1
    second_prob = {k: v[0]/v[1] for k, v in second.items() if v[1] > 0}
    overall_t = sum(s) / n
    return second_prob, overall_t

# =============================================================================
# THE LIVE SCANNER ENGINE
# =============================================================================
def scan_market(symbol):
    # Lock the server clock to India
    ist = pytz.timezone('Asia/Kolkata')
    print(f"[{datetime.now(ist).strftime('%H:%M:%S')}] Scanning {symbol}...")
    
    df = fetch_live_data(symbol)
    if len(df) < 200:
        print(f"Not enough data for {symbol}. Waiting...")
        return

    df = add_regime_labels(df)
    df['sma'] = df['Close'].rolling(200).mean()
    df['atr_median20'] = df['atr'].rolling(20).median()
    df['vol_ok'] = df['atr'] > 1.1 * df['atr_median20']
    
    df.dropna(inplace=True)
    
    second_prob, overall_t = compute_probabilities(df['regime'])
    
    last_bar = df.iloc[-1]
    prev_bar = df.iloc[-2]
    
    prev2_state = 1 if df['regime'].iloc[-3] == 't' else 0
    prev1_state = 1 if df['regime'].iloc[-2] == 't' else 0
    p_t = second_prob.get((prev2_state, prev1_state), overall_t)
    
    # NEW: Force the market candle data into correct IST time
    bar_time = last_bar.name
    if bar_time.tzinfo is None:
        bar_time = ist.localize(bar_time)
    else:
        bar_time = bar_time.astimezone(ist)
        
    current_time = bar_time.strftime('%H:%M')
    unique_candle_id = bar_time.strftime('%Y-%m-%d %H:%M') # Used for memory
    
    valid_time = '09:30' <= current_time <= '14:45'
    
    price = last_bar['Close']
    sma = last_bar['sma']
    atr = last_bar['atr']
    
    print(f"  -> {symbol} Close: {price:.2f} | Prob: {p_t:.2f} | Vol OK: {last_bar['vol_ok']}")
    
    if p_t >= ENTRY_THRESH and last_bar['vol_ok'] and valid_time:
        
        # CHECK MEMORY: Have we already alerted for this specific timestamp?
        if last_signal_time.get(symbol) == unique_candle_id:
            print(f"  -> Already sent alert for {symbol} at {current_time}. Skipping duplicate.")
            return

        if prev_bar['Close'] > prev_bar['sma']:
            sl = price - (ATR_MULT * atr)
            tp = price + (ATR_MULT * RR_RATIO * atr)
            msg = f"🟢 **LONG ALERT: {symbol}**\n\n*Time:* {current_time} IST\n*Entry:* {price:.2f}\n*Stop Loss:* {sl:.2f}\n*Target (1:3):* {tp:.2f}\n*Markov Prob:* {p_t:.2%}"
            send_telegram_alert(msg)
            print(f">>> SIGNAL FIRED for {symbol}!")
            last_signal_time[symbol] = unique_candle_id # SAVE TO MEMORY
            
        elif prev_bar['Close'] < prev_bar['sma']:
            sl = price + (ATR_MULT * atr)
            tp = price - (ATR_MULT * RR_RATIO * atr)
            msg = f"🔴 **SHORT ALERT: {symbol}**\n\n*Time:* {current_time} IST\n*Entry:* {price:.2f}\n*Stop Loss:* {sl:.2f}\n*Target (1:3):* {tp:.2f}\n*Markov Prob:* {p_t:.2%}"
            send_telegram_alert(msg)
            print(f">>> SIGNAL FIRED for {symbol}!")
            last_signal_time[symbol] = unique_candle_id # SAVE TO MEMORY

# =============================================================================
# DUMMY WEB SERVER (UptimeRobot Safe)
# =============================================================================
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(b"Markov Trading Bot is actively scanning the market!")
        
    def do_HEAD(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()

def keep_alive():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), DummyHandler)
    server.serve_forever()

if __name__ == "__main__":
    threading.Thread(target=keep_alive, daemon=True).start()
    
    print("🚀 Starting Multi-Ticker Markov Scanner...")
    while True:
        for stock in TICKERS:
            try:
                scan_market(stock)
            except Exception as e:
                print(f"Error scanning {stock}: {e}")
            
            time.sleep(5) 
            
        print("✅ Finished scanning watchlist. Sleeping for 5 minutes...")
        time.sleep(300)
