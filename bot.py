"""
markov_bot.py — Complete rewrite with trade lifecycle management.

Fixes:
  1. Tracks open positions and auto-closes on SL or TP hit
  2. Exposes /signal and /positions endpoints for the AngelOne bot
  3. Valid IST time window enforced
  4. Duplicate signal guard (unchanged, kept your logic)
  5. Removed invalid tickers TMPV.NS / TMCV.NS
"""

import yfinance as yf
import pandas as pd
import numpy as np
import time
import requests
import json
import threading
import os
import pytz
import warnings

from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

warnings.filterwarnings('ignore')

# =============================================================================
# CONFIGURATION
# =============================================================================
TICKERS = [
    "SBIN.NS", "WIPRO.NS", "RELIANCE.NS", "TCS.NS",
    "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS"
    # Removed: TMPV.NS, TMCV.NS (not valid yfinance symbols)
]

ENTRY_THRESH = 0.60
ATR_MULT     = 1.5
RR_RATIO     = 3.0

TELEGRAM_TOKEN   = "8701070280:AAHPIDZpQZLHGar0HEh6f84SEJcJGHbWQys"
TELEGRAM_CHAT_ID = "8125685903"

IST = pytz.timezone('Asia/Kolkata')

# =============================================================================
# SHARED STATE  (thread-safe via lock)
# =============================================================================
state_lock = threading.Lock()

# { symbol: { "direction", "entry", "sl", "tp", "time", "qty", "status" } }
open_positions: dict = {}

# Last signal time per symbol (duplicate guard)
last_signal_time: dict = {}

# Latest signal text for /signal endpoint
latest_signal_text: str = ""
latest_signal_ts:   str = ""

# Trade log for /history endpoint
trade_history: list = []

# =============================================================================
# TELEGRAM
# =============================================================================
def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Telegram error: {e}")

# =============================================================================
# MARKET DATA & MATH
# =============================================================================
def fetch_live_data(symbol: str) -> pd.DataFrame:
    df = yf.download(symbol, period="7d", interval="5m", progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.strip().lower() for c in df.columns]
    df.rename(columns={
        'open': 'Open', 'high': 'High',
        'low':  'Low',  'close': 'Close'
    }, inplace=True)
    return df.dropna()


def add_regime_labels(df: pd.DataFrame, atr_period=14, threshold=0.6) -> pd.DataFrame:
    df = df.copy()
    high_low    = df['High'] - df['Low']
    high_close  = np.abs(df['High'] - df['Close'].shift(1))
    low_close   = np.abs(df['Low']  - df['Close'].shift(1))
    tr          = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr']   = tr.rolling(atr_period).mean()
    df['prev_atr'] = df['atr'].shift(1)
    df['up_diff']   = (df['High'] - df['Open']) / df['prev_atr']
    df['down_diff'] = (df['Open'] - df['Low'])  / df['prev_atr']
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
    second_prob = {k: v[0] / v[1] for k, v in second.items() if v[1] > 0}
    overall_t   = sum(s) / n
    return second_prob, overall_t

# =============================================================================
# POSITION MANAGER  ← THE MISSING PIECE
# =============================================================================
def get_current_price(symbol: str) -> float | None:
    """Fetch the single latest close price."""
    try:
        df = yf.download(symbol, period="1d", interval="1m", progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.strip().lower() for c in df.columns]
        return float(df['close'].iloc[-1])
    except Exception:
        return None


def check_and_close_positions():
    """
    Called every scan cycle.
    For each open position, fetch current price and close if SL or TP is hit.
    """
    with state_lock:
        symbols = list(open_positions.keys())

    for symbol in symbols:
        with state_lock:
            pos = open_positions.get(symbol)
            if not pos:
                continue

        price = get_current_price(symbol)
        if price is None:
            continue

        direction = pos["direction"]
        entry     = pos["entry"]
        sl        = pos["sl"]
        tp        = pos["tp"]
        hit       = None
        pnl       = 0.0

        if direction == "LONG":
            if price <= sl:
                hit = "STOP LOSS"
                pnl = round(sl - entry, 2)
            elif price >= tp:
                hit = "TARGET"
                pnl = round(tp - entry, 2)
        else:  # SHORT
            if price >= sl:
                hit = "STOP LOSS"
                pnl = round(entry - sl, 2)
            elif price <= tp:
                hit = "TARGET"
                pnl = round(entry - tp, 2)

        if hit:
            emoji  = "✅" if hit == "TARGET" else "🛑"
            result = "WIN" if hit == "TARGET" else "LOSS"
            msg = (
                f"{emoji} *TRADE CLOSED — {result}*\n\n"
                f"*Symbol:* {symbol}\n"
                f"*Direction:* {direction}\n"
                f"*Entry:* {entry:.2f} → *Exit:* {price:.2f}\n"
                f"*Closed by:* {hit}\n"
                f"*P&L per share:* {pnl:+.2f}"
            )
            send_telegram(msg)
            print(f">>> CLOSED {symbol} [{result}] at {price:.2f} — {hit}")

            closed = {**pos, "exit_price": price, "exit_reason": hit,
                      "pnl": pnl, "result": result,
                      "closed_at": datetime.now(IST).strftime('%H:%M:%S')}
            with state_lock:
                trade_history.append(closed)
                del open_positions[symbol]

# =============================================================================
# FORCE CLOSE ALL POSITIONS AT 3:20 PM IST
# =============================================================================
def eod_square_off():
    """Close all open positions at end of day."""
    with state_lock:
        symbols = list(open_positions.keys())

    if not symbols:
        return

    print("⏰ EOD square-off triggered!")
    for symbol in symbols:
        price = get_current_price(symbol)
        with state_lock:
            pos = open_positions.get(symbol)
            if not pos:
                continue
            entry = pos["entry"]
            direction = pos["direction"]
            pnl = round((price - entry) if direction == "LONG" else (entry - price), 2) if price else 0

        msg = (
            f"⏰ *EOD SQUARE-OFF*\n\n"
            f"*Symbol:* {symbol}\n"
            f"*Direction:* {direction}\n"
            f"*Entry:* {entry:.2f} → *Exit:* {price:.2f if price else 'N/A'}\n"
            f"*P&L per share:* {pnl:+.2f}"
        )
        send_telegram(msg)
        print(f">>> EOD closed {symbol}")

        closed = {**pos, "exit_price": price, "exit_reason": "EOD",
                  "pnl": pnl, "result": "EOD",
                  "closed_at": datetime.now(IST).strftime('%H:%M:%S')}
        with state_lock:
            trade_history.append(closed)
            if symbol in open_positions:
                del open_positions[symbol]

# =============================================================================
# SCANNER
# =============================================================================
def scan_market(symbol: str):
    global latest_signal_text, latest_signal_ts

    now_ist      = datetime.now(IST)
    current_time = now_ist.strftime('%H:%M')
    print(f"[{now_ist.strftime('%H:%M:%S')}] Scanning {symbol}...")

    # ── EOD check ────────────────────────────────────────────────────────────
    if current_time >= '15:20':
        eod_square_off()
        return

    df = fetch_live_data(symbol)
    if len(df) < 200:
        print(f"  Not enough data for {symbol}.")
        return

    df = add_regime_labels(df)
    df['sma']          = df['Close'].rolling(200).mean()
    df['atr_median20'] = df['atr'].rolling(20).median()
    df['vol_ok']       = df['atr'] > 1.1 * df['atr_median20']
    df.dropna(inplace=True)

    second_prob, overall_t = compute_probabilities(df['regime'])

    last_bar = df.iloc[-1]
    prev_bar = df.iloc[-2]

    prev2_state = 1 if df['regime'].iloc[-3] == 't' else 0
    prev1_state = 1 if df['regime'].iloc[-2] == 't' else 0
    p_t = second_prob.get((prev2_state, prev1_state), overall_t)

    bar_time = last_bar.name
    if bar_time.tzinfo is None:
        bar_time = IST.localize(bar_time)
    else:
        bar_time = bar_time.astimezone(IST)

    candle_time      = bar_time.strftime('%H:%M')
    unique_candle_id = bar_time.strftime('%Y-%m-%d %H:%M')

    valid_time = '09:30' <= candle_time <= '14:45'

    price = float(last_bar['Close'])
    sma   = float(last_bar['sma'])
    atr   = float(last_bar['atr'])

    print(f"  -> {symbol} ₹{price:.2f} | Prob: {p_t:.2%} | Vol OK: {last_bar['vol_ok']}")

    # ── Check open position SL/TP ─────────────────────────────────────────────
    check_and_close_positions()

    # ── Skip if already in a position for this symbol ─────────────────────────
    with state_lock:
        already_open = symbol in open_positions

    if already_open:
        print(f"  -> {symbol} already has an open position. Skipping entry.")
        return

    # ── Entry logic ───────────────────────────────────────────────────────────
    if not (p_t >= ENTRY_THRESH and last_bar['vol_ok'] and valid_time):
        return

    if last_signal_time.get(symbol) == unique_candle_id:
        print(f"  -> Duplicate signal for {symbol} at {candle_time}. Skipping.")
        return

    direction = None
    if prev_bar['Close'] > prev_bar['sma']:
        direction = "LONG"
        sl = price - (ATR_MULT * atr)
        tp = price + (ATR_MULT * RR_RATIO * atr)
    elif prev_bar['Close'] < prev_bar['sma']:
        direction = "SHORT"
        sl = price + (ATR_MULT * atr)
        tp = price - (ATR_MULT * RR_RATIO * atr)

    if not direction:
        return

    emoji = "🟢" if direction == "LONG" else "🔴"

    # Plain-text format (parseable by angel_bot)
    signal_text = (
        f"{emoji} {direction} ALERT: {symbol}\n"
        f"Time: {candle_time} IST\n"
        f"Entry: {price:.2f}\n"
        f"Stop Loss: {sl:.2f}\n"
        f"Target (1:3): {tp:.2f}\n"
        f"Markov Prob: {p_t:.2%}"
    )

    # Markdown version for Telegram
    telegram_msg = (
        f"{emoji} *{direction} ALERT: {symbol}*\n\n"
        f"*Time:* {candle_time} IST\n"
        f"*Entry:* {price:.2f}\n"
        f"*Stop Loss:* {sl:.2f}\n"
        f"*Target (1:3):* {tp:.2f}\n"
        f"*Markov Prob:* {p_t:.2%}"
    )

    send_telegram(telegram_msg)
    print(f">>> SIGNAL FIRED: {direction} {symbol} at {price:.2f}")

    # Save signal for /signal endpoint
    with state_lock:
        latest_signal_text = signal_text
        latest_signal_ts   = now_ist.isoformat()
        last_signal_time[symbol] = unique_candle_id
        open_positions[symbol] = {
            "symbol":    symbol,
            "direction": direction,
            "entry":     price,
            "sl":        round(sl, 2),
            "tp":        round(tp, 2),
            "prob":      round(p_t * 100, 2),
            "opened_at": candle_time,
            "status":    "OPEN"
        }

# =============================================================================
# WEB SERVER  (UptimeRobot + AngelOne bot endpoints)
# =============================================================================
class SignalHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path == "/signal":
            self._json({
                "signal":    latest_signal_text,
                "timestamp": latest_signal_ts
            })
        elif self.path == "/positions":
            with state_lock:
                self._json(open_positions)
        elif self.path == "/history":
            with state_lock:
                self._json(trade_history)
        elif self.path == "/health":
            self._text("Markov Trading Bot is running ✅")
        else:
            self._text("Markov Trading Bot is running ✅")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def _json(self, data: dict | list):
        body = json.dumps(data, indent=2).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, text: str):
        body = text.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # suppress noisy access logs


def start_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), SignalHandler)
    print(f"🌐 Web server running on port {port}")
    server.serve_forever()

# =============================================================================
# MAIN LOOP
# =============================================================================
if __name__ == "__main__":
    threading.Thread(target=start_server, daemon=True).start()

    print("🚀 Markov Scanner started with full trade lifecycle management.")
    while True:
        ist_now = datetime.now(IST)
        current_time = ist_now.strftime('%H:%M')

        # Only scan during market hours
        if '09:15' <= current_time <= '15:25':
            for stock in TICKERS:
                try:
                    scan_market(stock)
                except Exception as e:
                    print(f"Error scanning {stock}: {e}")
                time.sleep(5)

            print("✅ Watchlist scan complete. Sleeping 5 mins...")
            time.sleep(300)
        else:
            print(f"[{current_time}] Market closed. Sleeping 10 mins...")
            time.sleep(600)
