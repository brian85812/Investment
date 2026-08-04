import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from flask import Flask, jsonify, render_template
from flask_cors import CORS
import logging
import time
import os
import threading

# 關閉不必要的 Flask 輸出
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)
CORS(app)

# ========================================================
# 快取層：伺服器啟動時預先算好，之後每 30 分鐘更新一次
# 這樣網頁請求可以瞬間回應，不用等 yfinance 下載
# ========================================================
_cache = {
    'data': None,
    'last_updated': None,
    'is_updating': False
}
CACHE_TTL_MINUTES = 30

def compute_signal(name, ticker, base_leverage, max_leverage, fast_ma, slow_ma, breakout_window, cooldown, allocs):
    df = None
    for attempt in range(3):
        try:
            df = yf.download(
                ticker,
                period='5y',
                progress=False,
                auto_adjust=True
            )
            if df is not None and len(df) > 0:
                print(f"✅ {ticker} 抓取成功，共 {len(df)} 筆")
                break
        except Exception as e:
            print(f"⚠️ {ticker} 第 {attempt+1} 次失敗: {e}")
        time.sleep(3)

    if df is None or len(df) == 0:
        print(f"❌ {ticker} 最終抓取失敗")
        return None

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # 針對 Yahoo Finance Bug：最後一天如果有開盤/有量，但收盤價是 NaN，手動用即時報價補上
    if len(df) > 0 and pd.isna(df['Close'].iloc[-1]):
        try:
            last_price = yf.Ticker(ticker).fast_info.last_price
            df.iloc[-1, df.columns.get_loc('Close')] = last_price
            if pd.isna(df['High'].iloc[-1]):
                df.iloc[-1, df.columns.get_loc('High')] = last_price
            if pd.isna(df['Low'].iloc[-1]):
                df.iloc[-1, df.columns.get_loc('Low')] = last_price
            if pd.isna(df['Open'].iloc[-1]):
                df.iloc[-1, df.columns.get_loc('Open')] = last_price
            print(f"🔧 已使用即時報價 {last_price} 修補 {ticker} 的缺失資料")
        except Exception as e:
            print(f"⚠️ 嘗試修補最新價格失敗: {e}")

    max_idx = len(allocs) - 1
    in_trend = False
    step_idx = 0
    last_action_idx = -999
    target_history = []

    df['SMA_fast'] = df['Close'].rolling(window=fast_ma).mean()
    df['SMA_slow'] = df['Close'].rolling(window=slow_ma).mean()
    df['High_bw'] = df['High'].shift(1).rolling(window=breakout_window).max()
    df['Low_bw']  = df['Low'].shift(1).rolling(window=breakout_window).min()
    df = df.dropna()

    for i in range(len(df)):
        current_close = df['Close'].iloc[i]
        sma_fast = df['SMA_fast'].iloc[i]
        sma_slow = df['SMA_slow'].iloc[i]
        high_bw  = df['High_bw'].iloc[i]
        low_bw   = df['Low_bw'].iloc[i]

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
        target_history.append(base_leverage * (1 - alloc_pct) + max_leverage * alloc_pct)

    if current_close < sma_slow:
        explanation = f"目前價格低於長線 ({slow_ma}MA)，處於空頭防禦狀態，維持最低底倉 ({base_leverage}x)。"
    elif sma_fast < sma_slow:
        explanation = f"目前價格在長線之上，但短線 ({fast_ma}MA) 尚未黃金交叉，維持最低底倉 ({base_leverage}x)。"
    else:
        if step_idx == 0:
            explanation = f"多頭趨勢 (雙均線之上)，但尚未突破近 {breakout_window} 日高點，維持底倉 ({base_leverage}x) 等待發動。"
        else:
            alloc_pct = allocs[step_idx]
            target_lev = base_leverage * (1 - alloc_pct) + max_leverage * alloc_pct
            explanation = f"多頭趨勢確立，已觸發 {step_idx} 次突破向上。動能階梯 {step_idx}/{max_idx}，配置槓桿 {target_lev:.2f}x。"

    latest = df.iloc[-1]
    return {
        'id': ticker.replace('.', '_').lower(),
        'name': name,
        'ticker': ticker,
        'date': latest.name.strftime('%Y-%m-%d'),
        'close': round(float(latest['Close']), 2),
        'in_trend': in_trend,
        'step_idx': step_idx,
        'max_steps': max_idx,
        'target_today': round(target_history[-1], 2),
        'target_yesterday': round(target_history[-2], 2) if len(target_history) >= 2 else round(target_history[-1], 2),
        'sma_fast_val': round(float(latest['SMA_fast']), 2),
        'sma_slow_val': round(float(latest['SMA_slow']), 2),
        'fast_ma_len': fast_ma,
        'slow_ma_len': slow_ma,
        'explanation': explanation
    }

def refresh_cache():
    """在背景 Thread 更新快取，不阻塞 web 請求"""
    if _cache['is_updating']:
        return
    _cache['is_updating'] = True
    print("🔄 正在更新市場資料快取...")

    try:
        qqq = compute_signal(
            name="美股 QQQ", ticker="QQQ",
            base_leverage=0.6, max_leverage=3.0,
            fast_ma=20, slow_ma=200, breakout_window=40, cooldown=5,
            allocs=[0.0, 0.5, 0.8, 1.0]
        )
        tw = compute_signal(
            name="台股 006208", ticker="006208.TW",
            base_leverage=0.8, max_leverage=3.0,
            fast_ma=5, slow_ma=60, breakout_window=20, cooldown=3,
            allocs=[0.0, 0.4, 0.7, 0.9, 1.0]
        )

        results = [x for x in [qqq, tw] if x is not None]
        if results:
            _cache['data'] = results
            _cache['last_updated'] = datetime.now()
            print(f"✅ 快取更新完成：{_cache['last_updated'].strftime('%Y-%m-%d %H:%M:%S')}")
        else:
            print("❌ 快取更新失敗：所有資料均無法取得")
    except Exception as e:
        print(f"❌ 快取更新例外：{e}")
    finally:
        _cache['is_updating'] = False

def background_refresh():
    """每 30 分鐘自動重新整理一次"""
    while True:
        time.sleep(CACHE_TTL_MINUTES * 60)
        refresh_cache()

# ========================================================
# Flask Routes
# ========================================================
@app.route('/')
def index():
    return render_template('index.html')

_bg_thread_started = False

@app.route('/api/data')
def get_data():
    global _bg_thread_started
    
    # 如果快取是空的（首次請求），觸發立即更新
    if _cache['data'] is None:
        if not _cache['is_updating']:
            # 啟動抓資料 Thread
            thread = threading.Thread(target=refresh_cache)
            thread.daemon = True
            thread.start()
            
            # 確保 30 分鐘定時更新的 Thread 也有在跑
            if not _bg_thread_started:
                bg_thread = threading.Thread(target=background_refresh)
                bg_thread.daemon = True
                bg_thread.start()
                _bg_thread_started = True
                
        return jsonify({'status': 'loading', 'message': '資料正在載入中，請稍候 30 秒後重新整理...'}), 202

    return jsonify(_cache['data'])

@app.route('/api/refresh')
def force_refresh():
    """手動強制更新按鈕用"""
    thread = threading.Thread(target=refresh_cache)
    thread.daemon = True
    thread.start()
    return jsonify({'status': 'ok', 'message': '正在背景更新，約 30 秒後重新整理頁面即可'})

@app.route('/health')
def health():
    return jsonify({
        'status': 'ok',
        'cache_age': str(datetime.now() - _cache['last_updated']) if _cache['last_updated'] else 'no cache yet',
        'is_updating': _cache['is_updating']
    })

# ========================================================
# 啟動
# ========================================================
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
    port = int(os.environ.get('PORT', 5000))
    local_ip = get_local_ip()
    
    # 啟動時預熱快取（只在本地直接執行 python app.py 時觸發，避免 Render gunicorn fork 問題）
    print("🚀 伺服器啟動，開始背景預熱資料...")
    warmup_thread = threading.Thread(target=refresh_cache)
    warmup_thread.daemon = True
    warmup_thread.start()
    
    # 定時更新 thread
    bg_thread = threading.Thread(target=background_refresh)
    bg_thread.daemon = True
    bg_thread.start()
    _bg_thread_started = True

    print("\n" + "="*55)
    print(" 🚀 護城河 Web 伺服器啟動成功！ 🚀")
    print("="*55)
    print(f" 💻 電腦本機請用此網址: http://127.0.0.1:{port}")
    print(f" 📱 手機連線請用此網址: http://{local_ip}:{port}")
    print(" (確保手機與電腦連線至同一個 Wi-Fi)")
    print("="*55 + "\n")
    app.run(host='0.0.0.0', port=port, debug=False)
