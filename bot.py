"""
markov_bot.py — IMPROVED VERSION 2.0
=====================================
Key improvements over v1:
  1. Markov threshold raised 0.60 → 0.68 (removes weak signals)
  2. Daily trend filter (only trade in direction of D1 SMA50 trend)
  3. Skip 09:30–09:44 (opening volatility protection)
  4. 30-min cooldown after stop loss (no revenge trading)
  5. One signal per symbol per session (no spam)
  6. Volume confirmation added
  7. ATR quality filter tightened
  8. RSI overbought/oversold filter added
  9. Max 3 simultaneous open positions
 10. Session P&L tracking and auto-pause if -3 losses in a row
 11. Web endpoints: /signal /positions /history /stats /health /myip
 12. Full Telegram alerts with P&L summary
"""

import yfinance as yf
import pandas as pd
import numpy as np
import time
import requests
import json
import threading
import os
import pyotp
import pytz
import warnings

from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

warnings.filterwarnings('ignore')

try:
    from smartapi import SmartConnect
    ANGEL_AVAILABLE = True
except ImportError:
    ANGEL_AVAILABLE = False
    print("⚠️  smartapi not installed — signals will fire but no orders placed.")

# =============================================================================
# CONFIGURATION
# =============================================================================
TICKERS = [
    "SBIN.NS", "WIPRO.NS", "RELIANCE.NS", "TCS.NS",
    "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS",
    "TMPV.NS", "TMCV.NS"
]

# ── IMPROVED THRESHOLDS ───────────────────────────────────────────────────────
ENTRY_THRESH    = 0.68    # ↑ was 0.60 — removes low-quality signals
ATR_MULT        = 1.5
RR_RATIO        = 3.0
MAX_POSITIONS   = 3       # NEW: max simultaneous open trades
MAX_CONSEC_LOSS = 3       # NEW: pause after this many consecutive losses

# ── TIMING ───────────────────────────────────────────────────────────────────
MARKET_START    = '09:45'  # ↑ was 09:30 — skip opening volatility
MARKET_END      = '14:30'  # ↓ was 14:45 — avoid last-hour chop
EOD_CLOSE       = '15:20'

# ── COOLDOWN ─────────────────────────────────────────────────────────────────
SL_COOLDOWN_MIN = 30       # NEW: minutes to wait after stop loss hit

# ── TELEGRAM ─────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",   "8701070280:AAHPIDZpQZLHGar0HEh6f84SEJcJGHbWQys")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8125685903")

# ── ANGELONE ─────────────────────────────────────────────────────────────────
ANGEL_API_KEY   = "J6epAYOQ"
ANGEL_CLIENT_ID = "AAAD532028"
ANGEL_PASSWORD  = "8688"
ANGEL_TOTP      = "AOYLGJBQFRZZLS5GWQJ7PX3YAY"
TRADE_QTY       = int(os.environ.get("TRADE_QTY",   "1"))

SYMBOL_TOKENS = {
    "INFY":      "1594",
    "SBIN":      "3045",
    "WIPRO":     "3787",
    "RELIANCE":  "2885",
    "TCS":       "11536",
    "HDFCBANK":  "1333",
    "ICICIBANK": "4963",
}

IST = pytz.timezone('Asia/Kolkata')

# =============================================================================
# SHARED STATE
# =============================================================================
state_lock       = threading.Lock()
open_positions   = {}          # { symbol: position_dict }
last_signal_time = {}          # { symbol: candle_id } — duplicate guard
sl_cooldown      = {}          # { symbol: datetime } — cooldown after SL
session_signals  = set()       # symbols already traded today
latest_signal    = {"text": "", "timestamp": ""}
trade_history    = []
angel_obj        = None

# Session stats
session_stats = {
    "wins": 0, "losses": 0, "eod": 0,
    "net_pnl": 0.0, "consec_loss": 0, "paused": False
}

# =============================================================================
# TELEGRAM
# =============================================================================
def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       message,
            "parse_mode": "Markdown"
        }, timeout=5)
    except Exception as e:
        print(f"Telegram error: {e}")


def send_daily_summary():
    with state_lock:
        w = session_stats["wins"]
        l = session_stats["losses"]
        e = session_stats["eod"]
        pnl = session_stats["net_pnl"]
        total = w + l + e
        wr = (w / total * 100) if total else 0

    msg = (
        f"📊 *Daily Summary*\n\n"
        f"*Total Trades:* {total}\n"
        f"*Wins:* {w} | *Losses:* {l} | *EOD:* {e}\n"
        f"*Win Rate:* {wr:.1f}%\n"
        f"*Net P&L:* {'+'if pnl>=0 else ''}{pnl:.2f} per share\n"
        f"*Session ended:* {datetime.now(IST).strftime('%H:%M IST')}"
    )
    send_telegram(msg)

# =============================================================================
# ANGELONE
# =============================================================================
def angel_login():
    global angel_obj
    if not ANGEL_AVAILABLE or not ANGEL_API_KEY:
        return None
    try:
        obj  = SmartConnect(api_key=ANGEL_API_KEY)
        totp = pyotp.TOTP(ANGEL_TOTP).now()
        data = obj.generateSession(ANGEL_CLIENT_ID, ANGEL_PASSWORD, totp)
        if data.get("status"):
            print(f"✅ AngelOne login OK | {ANGEL_CLIENT_ID}")
            angel_obj = obj
            return obj
        else:
            print(f"❌ Login failed: {data}")
            return None
    except Exception as e:
        print(f"❌ Login error: {e}")
        return None


def place_bracket_order(symbol, token, direction, entry, sl, tp):
    global angel_obj
    if not angel_obj:
        angel_obj = angel_login()
    if not angel_obj:
        print("⚠️  No AngelOne session. Order skipped.")
        return None

    sl_pts     = round(abs(entry - sl), 2)
    target_pts = round(abs(tp - entry), 2)

    params = {
        "variety":         "ROBO",
        "tradingsymbol":   symbol,
        "symboltoken":     token,
        "transactiontype": "BUY" if direction == "LONG" else "SELL",
        "exchange":        "NSE",
        "ordertype":       "LIMIT",
        "producttype":     "INTRADAY",
        "duration":        "DAY",
        "quantity":        str(TRADE_QTY),
        "price":           str(entry),
        "squareoff":       str(target_pts),
        "stoploss":        str(sl_pts),
    }
    try:
        result = angel_obj.placeOrder(params)
        print(f"✅ Order placed: {result}")
        return result
    except Exception as e:
        print(f"❌ Order error: {e}")
        angel_obj = angel_login()
        return None

# =============================================================================
# MARKET DATA
# =============================================================================
def fetch_live_data(symbol, period="7d", interval="5m"):
    df = yf.download(symbol, period=period, interval=interval, progress=False)
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.strip().lower() for c in df.columns]
    df.rename(columns={
        'open':'Open','high':'High','low':'Low',
        'close':'Close','volume':'Volume'
    }, inplace=True)
    return df.dropna()


def fetch_daily_data(symbol):
    """Fetch daily OHLCV for trend filter."""
    return fetch_live_data(symbol, period="60d", interval="1d")


def get_current_price(symbol):
    try:
        df = yf.download(symbol, period="1d", interval="1m", progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.strip().lower() for c in df.columns]
        return float(df['close'].iloc[-1])
    except Exception:
        return None


def add_regime_labels(df, atr_period=14, threshold=0.6):
    df = df.copy()
    high_low   = df['High'] - df['Low']
    high_close = np.abs(df['High'] - df['Close'].shift(1))
    low_close  = np.abs(df['Low']  - df['Close'].shift(1))
    tr         = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr']  = tr.rolling(atr_period).mean()
    df['prev_atr']  = df['atr'].shift(1)
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
    second_prob = {k: v[0]/v[1] for k, v in second.items() if v[1] > 0}
    return second_prob, sum(s)/n


# =============================================================================
# NEW: RSI INDICATOR
# =============================================================================
def compute_rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


# =============================================================================
# NEW: DAILY TREND FILTER
# =============================================================================
def get_daily_trend(symbol):
    """
    Returns 'UP', 'DOWN', or 'NEUTRAL'.
    Uses D1 SMA50 + SMA20 alignment.
    """
    try:
        df = fetch_daily_data(symbol)
        if len(df) < 50:
            return 'NEUTRAL'
        df['sma20'] = df['Close'].rolling(20).mean()
        df['sma50'] = df['Close'].rolling(50).mean()
        last = df.iloc[-1]
        if last['Close'] > last['sma20'] > last['sma50']:
            return 'UP'
        elif last['Close'] < last['sma20'] < last['sma50']:
            return 'DOWN'
        else:
            return 'NEUTRAL'
    except Exception:
        return 'NEUTRAL'


# =============================================================================
# POSITION MANAGER
# =============================================================================
def check_and_close_positions():
    with state_lock:
        symbols = list(open_positions.keys())

    for symbol in symbols:
        with state_lock:
            pos = open_positions.get(symbol)
        if not pos:
            continue

        price = get_current_price(symbol)
        if not price:
            continue

        direction = pos["direction"]
        entry, sl, tp = pos["entry"], pos["sl"], pos["tp"]
        hit = None

        if direction == "LONG":
            if price <= sl:   hit = "STOP LOSS"
            elif price >= tp: hit = "TARGET"
        else:
            if price >= sl:   hit = "STOP LOSS"
            elif price <= tp: hit = "TARGET"

        if hit:
            pnl    = round((price - entry) if direction == "LONG" else (entry - price), 2)
            emoji  = "✅" if hit == "TARGET" else "🛑"
            result = "WIN" if hit == "TARGET" else "LOSS"

            send_telegram(
                f"{emoji} *TRADE CLOSED — {result}*\n\n"
                f"*Symbol:* {symbol}\n"
                f"*Direction:* {direction}\n"
                f"*Entry:* {entry:.2f} → *Exit:* {price:.2f}\n"
                f"*Reason:* {hit}\n"
                f"*P&L per share:* {pnl:+.2f}"
            )
            print(f">>> CLOSED {symbol} [{result}] @ {price:.2f} | P&L: {pnl:+.2f}")

            with state_lock:
                # Update session stats
                session_stats["net_pnl"] += pnl
                if result == "WIN":
                    session_stats["wins"] += 1
                    session_stats["consec_loss"] = 0
                    session_stats["paused"] = False
                else:
                    session_stats["losses"] += 1
                    session_stats["consec_loss"] += 1
                    # Set cooldown for this symbol
                    sl_cooldown[symbol] = datetime.now(IST)
                    # Auto-pause after MAX_CONSEC_LOSS
                    if session_stats["consec_loss"] >= MAX_CONSEC_LOSS:
                        session_stats["paused"] = True
                        print(f"⚠️  {MAX_CONSEC_LOSS} consecutive losses — bot PAUSED for 30 min")
                        send_telegram(
                            f"⚠️ *Bot Paused*\n"
                            f"{MAX_CONSEC_LOSS} consecutive losses hit.\n"
                            f"Resuming in 30 minutes automatically."
                        )

                trade_history.append({
                    **pos,
                    "exit":   price,
                    "reason": hit,
                    "pnl":    pnl,
                    "result": result,
                    "closed_at": datetime.now(IST).strftime('%H:%M:%S')
                })
                del open_positions[symbol]


def eod_square_off():
    with state_lock:
        symbols = list(open_positions.keys())
    if not symbols:
        return

    print("⏰ EOD square-off...")
    for symbol in symbols:
        price = get_current_price(symbol)
        with state_lock:
            pos = open_positions.get(symbol)
            if not pos:
                continue
            pnl = round(
                (price - pos["entry"]) if pos["direction"] == "LONG"
                else (pos["entry"] - price), 2
            ) if price else 0

        send_telegram(
            f"⏰ *EOD SQUARE-OFF*\n\n"
            f"*Symbol:* {symbol} | *Direction:* {pos['direction']}\n"
            f"*Entry:* {pos['entry']:.2f} → *Exit:* {price:.2f if price else 'N/A'}\n"
            f"*P&L:* {pnl:+.2f}"
        )
        with state_lock:
            session_stats["eod"] += 1
            session_stats["net_pnl"] += pnl
            trade_history.append({
                **pos, "exit": price,
                "reason": "EOD", "pnl": pnl, "result": "EOD"
            })
            if symbol in open_positions:
                del open_positions[symbol]

    send_daily_summary()


# =============================================================================
# MAIN SCANNER
# =============================================================================
def scan_market(symbol):
    now_ist      = datetime.now(IST)
    current_time = now_ist.strftime('%H:%M')

    # ── EOD check ────────────────────────────────────────────────────────────
    if current_time >= EOD_CLOSE:
        eod_square_off()
        return

    # ── Check/close existing positions ───────────────────────────────────────
    check_and_close_positions()

    # ── Bot paused? ──────────────────────────────────────────────────────────
    with state_lock:
        if session_stats["paused"]:
            print(f"  ⏸  Bot paused after {MAX_CONSEC_LOSS} consecutive losses.")
            # Auto-resume after 30 min
            last_loss_time = max(sl_cooldown.values()) if sl_cooldown else None
            if last_loss_time:
                mins_since = (now_ist - last_loss_time).seconds / 60
                if mins_since >= 30:
                    session_stats["paused"] = False
                    session_stats["consec_loss"] = 0
                    print("▶️  Bot resumed.")
                    send_telegram("▶️ *Bot Resumed* — 30 min pause complete.")
            return

    print(f"[{now_ist.strftime('%H:%M:%S')}] Scanning {symbol}...")

    # ── Skip if already in position ───────────────────────────────────────────
    with state_lock:
        already_open = symbol in open_positions
        total_open   = len(open_positions)

    if already_open:
        print(f"  -> {symbol} already open. Skipping.")
        return

    # ── Max positions check ───────────────────────────────────────────────────
    if total_open >= MAX_POSITIONS:
        print(f"  -> Max {MAX_POSITIONS} positions open. Skipping {symbol}.")
        return

    # ── Already traded this symbol today? ─────────────────────────────────────
    with state_lock:
        if symbol in session_signals:
            print(f"  -> {symbol} already traded today. Skipping.")
            return

    # ── SL cooldown check ────────────────────────────────────────────────────
    with state_lock:
        cd_time = sl_cooldown.get(symbol)
    if cd_time:
        mins_since = (now_ist - cd_time).seconds / 60
        if mins_since < SL_COOLDOWN_MIN:
            print(f"  -> {symbol} in SL cooldown ({int(mins_since)}m/{SL_COOLDOWN_MIN}m). Skipping.")
            return

    # ── Fetch 5-min data ──────────────────────────────────────────────────────
    df = fetch_live_data(symbol)
    if len(df) < 200:
        print(f"  Not enough data for {symbol}.")
        return

    df = add_regime_labels(df)
    df['sma200']       = df['Close'].rolling(200).mean()
    df['sma50']        = df['Close'].rolling(50).mean()
    df['atr_median20'] = df['atr'].rolling(20).median()

    # ── NEW: Volume filter ───────────────────────────────────────────────────
    if 'Volume' in df.columns:
        df['vol_sma20'] = df['Volume'].rolling(20).mean()
        df['vol_ok']    = (df['atr'] > 1.1 * df['atr_median20']) & \
                          (df['Volume'] > 0.8 * df['vol_sma20'])
    else:
        df['vol_ok']    = df['atr'] > 1.1 * df['atr_median20']

    # ── NEW: RSI ──────────────────────────────────────────────────────────────
    df['rsi'] = compute_rsi(df['Close'])

    df.dropna(inplace=True)
    if len(df) < 5:
        return

    second_prob, overall_t = compute_probabilities(df['regime'])
    last_bar = df.iloc[-1]
    prev_bar = df.iloc[-2]

    prev2 = 1 if df['regime'].iloc[-3] == 't' else 0
    prev1 = 1 if df['regime'].iloc[-2] == 't' else 0
    p_t   = second_prob.get((prev2, prev1), overall_t)

    bar_time = last_bar.name
    bar_time = IST.localize(bar_time) if bar_time.tzinfo is None else bar_time.astimezone(IST)
    candle_time      = bar_time.strftime('%H:%M')
    unique_candle_id = bar_time.strftime('%Y-%m-%d %H:%M')

    valid_time = MARKET_START <= candle_time <= MARKET_END

    price = float(last_bar['Close'])
    atr   = float(last_bar['atr'])
    rsi   = float(last_bar['rsi'])

    print(f"  -> {symbol} ₹{price:.2f} | Prob: {p_t:.2%} | RSI: {rsi:.1f} | Vol OK: {last_bar['vol_ok']}")

    # ── ENTRY FILTER CHECKS ───────────────────────────────────────────────────
    if not (p_t >= ENTRY_THRESH and last_bar['vol_ok'] and valid_time):
        return

    if last_signal_time.get(symbol) == unique_candle_id:
        print(f"  -> Duplicate candle signal. Skipping.")
        return

    # ── NEW: Daily trend filter ───────────────────────────────────────────────
    daily_trend = get_daily_trend(symbol)
    print(f"  -> {symbol} Daily trend: {daily_trend}")

    # Determine direction from 5-min SMA
    if prev_bar['Close'] > prev_bar['sma200']:
        direction = "LONG"
    elif prev_bar['Close'] < prev_bar['sma200']:
        direction = "SHORT"
    else:
        return

    # ── NEW: Align with daily trend (allow NEUTRAL but block counter-trend) ───
    if daily_trend == "DOWN" and direction == "LONG":
        print(f"  -> Skipping LONG — daily trend is DOWN")
        return
    if daily_trend == "UP" and direction == "SHORT":
        print(f"  -> Skipping SHORT — daily trend is UP")
        return

    # ── NEW: RSI filter ───────────────────────────────────────────────────────
    if direction == "LONG" and rsi > 72:
        print(f"  -> Skipping LONG — RSI overbought ({rsi:.1f})")
        return
    if direction == "SHORT" and rsi < 28:
        print(f"  -> Skipping SHORT — RSI oversold ({rsi:.1f})")
        return

    # ── Calculate SL / TP ─────────────────────────────────────────────────────
    if direction == "LONG":
        sl = price - (ATR_MULT * atr)
        tp = price + (ATR_MULT * RR_RATIO * atr)
    else:
        sl = price + (ATR_MULT * atr)
        tp = price - (ATR_MULT * RR_RATIO * atr)

    emoji = "🟢" if direction == "LONG" else "🔴"

    # ── Plain-text signal (for /signal endpoint) ──────────────────────────────
    signal_text = (
        f"{emoji} {direction} ALERT: {symbol}\n"
        f"Time: {candle_time} IST\n"
        f"Entry: {price:.2f}\n"
        f"Stop Loss: {sl:.2f}\n"
        f"Target (1:3): {tp:.2f}\n"
        f"Markov Prob: {p_t:.2%}\n"
        f"RSI: {rsi:.1f} | Trend: {daily_trend}"
    )

    # ── Telegram (markdown) ───────────────────────────────────────────────────
    send_telegram(
        f"{emoji} *{direction} ALERT: {symbol}*\n\n"
        f"*Time:* {candle_time} IST\n"
        f"*Entry:* ₹{price:.2f}\n"
        f"*Stop Loss:* ₹{sl:.2f}\n"
        f"*Target (1:3):* ₹{tp:.2f}\n"
        f"*Markov Prob:* {p_t:.2%}\n"
        f"*RSI:* {rsi:.1f}\n"
        f"*Daily Trend:* {daily_trend}\n"
        f"*R:R:* 1:{RR_RATIO}"
    )
    print(f">>> SIGNAL: {direction} {symbol} @ ₹{price:.2f} | Prob: {p_t:.2%} | RSI: {rsi:.1f}")

    # ── Place AngelOne order ──────────────────────────────────────────────────
    sym   = symbol.replace(".NS","").replace(".BSE","").upper()
    token = SYMBOL_TOKENS.get(sym)
    if token:
        place_bracket_order(sym, token, direction, price, round(sl,2), round(tp,2))
    else:
        print(f"⚠️  No token for {sym}. Signal sent but no order placed.")

    # ── Update state ──────────────────────────────────────────────────────────
    with state_lock:
        latest_signal["text"]      = signal_text
        latest_signal["timestamp"] = now_ist.isoformat()
        last_signal_time[symbol]   = unique_candle_id
        session_signals.add(symbol)
        open_positions[symbol] = {
            "symbol":    symbol,
            "direction": direction,
            "entry":     price,
            "sl":        round(sl, 2),
            "tp":        round(tp, 2),
            "prob":      round(p_t * 100, 2),
            "rsi":       round(rsi, 1),
            "trend":     daily_trend,
            "opened_at": candle_time,
        }


# =============================================================================
# RESET SESSION AT EOD
# =============================================================================
def reset_session():
    """Resets per-day tracking. Called at start of each new trading day."""
    with state_lock:
        session_signals.clear()
        sl_cooldown.clear()
        session_stats.update({
            "wins": 0, "losses": 0, "eod": 0,
            "net_pnl": 0.0, "consec_loss": 0, "paused": False
        })
    print("🔄 Session reset for new trading day.")


# =============================================================================
# WEB SERVER
# =============================================================================
class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path == "/signal":
            self._json(latest_signal)

        elif self.path == "/positions":
            with state_lock:
                self._json(open_positions)

        elif self.path == "/history":
            with state_lock:
                self._json(trade_history)

        elif self.path == "/stats":
            with state_lock:
                total = session_stats["wins"] + session_stats["losses"] + session_stats["eod"]
                wr    = round(session_stats["wins"]/total*100, 1) if total else 0
                self._json({
                    **session_stats,
                    "total_trades": total,
                    "win_rate_pct": wr,
                    "open_positions": len(open_positions),
                })

        elif self.path == "/health":
            self._text("Markov Trading Bot v2.0 is running ✅")

        elif self.path == "/myip":
            try:
                ip = requests.get("https://api.ipify.org", timeout=5).text
            except Exception:
                ip = "Could not fetch IP"
            self._text(f"Render outbound IP: {ip}")

        else:
            self._text("Markov Trading Bot v2.0 ✅")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def _json(self, data):
        body = json.dumps(data, indent=2, default=str).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _text(self, text):
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(text.encode())

    def log_message(self, *args):
        pass  # suppress access logs


def start_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), Handler)
    print(f"🌐 Web server running on port {port}")
    server.serve_forever()


# =============================================================================
# MAIN LOOP
# =============================================================================
if __name__ == "__main__":
    threading.Thread(target=start_server, daemon=True).start()
    print("🚀 Markov Bot v2.0 started — improved filters active")
    print(f"   Threshold : {ENTRY_THRESH} (was 0.60)")
    print(f"   Market hrs: {MARKET_START} – {MARKET_END} IST (was 09:30–14:45)")
    print(f"   Max pos   : {MAX_POSITIONS}")
    print(f"   SL cooldown: {SL_COOLDOWN_MIN} min")
    print(f"   Pause after: {MAX_CONSEC_LOSS} consecutive losses")

    # AngelOne login on startup
    angel_login()

    last_reset_day = None

    while True:
        now_ist = datetime.now(IST)
        t       = now_ist.strftime('%H:%M')
        today   = now_ist.date()

        # Reset session at start of each new trading day
        if today != last_reset_day and t >= '09:00':
            reset_session()
            last_reset_day = today

        if '09:15' <= t <= '15:25':
            for stock in TICKERS:
                try:
                    scan_market(stock)
                except Exception as e:
                    print(f"Error scanning {stock}: {e}")
                time.sleep(5)

            print(f"✅ Scan complete [{t}]. Sleeping 5 mins...")
            time.sleep(300)
        else:
            print(f"[{t}] Market closed. Sleeping 10 mins...")
            time.sleep(600)
