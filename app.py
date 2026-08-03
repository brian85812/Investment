import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from flask import Flask, jsonify, render_template
from flask_cors import CORS
import logging

# 關閉不必要的 Flask 輸出
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)
CORS(app)

def get_signal_data(name, ticker, base_leverage, max_leverage, fast_ma, slow_ma, breakout_window, cooldown, allocs):
    end_date = datetime.now()
    start_date = end_date - timedelta(days=5*365)
    
    df = yf.Ticker(ticker).history(start=start_date)
    if len(df) == 0:
        return None
        
    max_idx = len(allocs) - 1
    in_trend = False
    step_idx = 0
    last_action_idx = -999
    target_history = []
    
    df['SMA_20'] = df['Close'].rolling(window=fast_ma).mean()
    df['SMA_200'] = df['Close'].rolling(window=slow_ma).mean()
    df['High_40'] = df['High'].shift(1).rolling(window=breakout_window).max()
    df['Low_40'] = df['Low'].shift(1).rolling(window=breakout_window).min()
    
    df = df.dropna()
    
    for i in range(len(df)):
        current_close = df['Close'].iloc[i]
        sma_fast = df['SMA_20'].iloc[i]
        sma_slow = df['SMA_200'].iloc[i]
        high_bw = df['High_40'].iloc[i]
        low_bw = df['Low_40'].iloc[i]
        
        if sma_fast < sma_slow or current_close < sma_slow:
            in_trend = False
            step_idx = 0
            target_history.append(base_leverage)
            continue
            
        if sma_fast >= sma_slow and not in_trend:
            in_trend = True
            
        if in_trend and (i - last_action_idx >= cooldown):
            if current_close > high_bw:
                if step_idx < max_idx:
                    step_idx += 1
                    last_action_idx = i
            elif current_close < low_bw:
                if step_idx > 0:
                    step_idx -= 1
                    last_action_idx = i
                    
        alloc_pct = allocs[step_idx]
        target_lev = base_leverage * (1 - alloc_pct) + max_leverage * alloc_pct
        target_history.append(target_lev)
                    
    latest = df.iloc[-1]
    return {
        'id': ticker.replace('.', '_').lower(),
        'name': name,
        'ticker': ticker,
        'date': latest.name.strftime('%Y-%m-%d'),
        'close': round(latest['Close'], 2),
        'in_trend': in_trend,
        'step_idx': step_idx,
        'max_steps': max_idx,
        'target_today': round(target_history[-1], 2),
        'target_yesterday': round(target_history[-2], 2)
    }

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/data')
def get_data():
    qqq_data = get_signal_data(
        name="美股 QQQ", ticker="QQQ", 
        base_leverage=0.6, max_leverage=3.0,
        fast_ma=20, slow_ma=200, breakout_window=40, cooldown=5,
        allocs=[0.0, 0.5, 0.8, 1.0]
    )
    
    tw_data = get_signal_data(
        name="台股 006208", ticker="006208.TW", 
        base_leverage=0.8, max_leverage=3.0,
        fast_ma=5, slow_ma=60, breakout_window=20, cooldown=3,
        allocs=[0.0, 0.4, 0.7, 0.9, 1.0]
    )
    
    return jsonify([qqq_data, tw_data])

import socket

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

import sys

if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

if __name__ == '__main__':
    local_ip = get_local_ip()
    print("\n" + "="*55, flush=True)
    print(" 🚀 護城河 Web 伺服器啟動成功！ 🚀", flush=True)
    print("="*55, flush=True)
    print(f" 💻 電腦本機請用此網址: http://127.0.0.1:5000", flush=True)
    print(f" 📱 手機連線請用此網址: http://{local_ip}:5000", flush=True)
    print(" (確保手機與電腦連線至同一個 Wi-Fi)", flush=True)
    print("="*55 + "\n", flush=True)
    app.run(host='0.0.0.0', port=5000, debug=False)
