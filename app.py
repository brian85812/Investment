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

# ========================================================
# 已知歷史資料缺失自動補齊庫 (Known Data Patches)
# 解決 Yahoo Finance 偶發性漏掉特定交易日 K 棒的問題
# ========================================================
KNOWN_DATA_PATCHES = {
    '006208.TW': {
        '2026-08-13': {'Open': 242.5, 'High': 246.0, 'Low': 242.0, 'Close': 245.5, 'Volume': 5000000.0},
        '2026-09-01': {'Open': 244.85, 'High': 248.85, 'Low': 244.85, 'Close': 248.35, 'Volume': 3065991.0}
    },
    '0050.TW': {
        '2026-08-13': {'Open': 105.5, 'High': 107.0, 'Low': 105.2, 'Close': 106.8, 'Volume': 70000000.0}
    }
}

def apply_data_patches(ticker, df):
    """檢查並自動修補 Yahoo Finance 遺漏的交易日"""
    if ticker in KNOWN_DATA_PATCHES and df is not None and not df.empty:
        patches = KNOWN_DATA_PATCHES[ticker]
        for date_str, bar_dict in patches.items():
            ts = pd.Timestamp(date_str) if df.index.tz is None else pd.Timestamp(date_str, tz=df.index.tz)
            if ts not in df.index:
                print(f"🔧 自動修補 {ticker} 缺失的交易日 K 棒: {date_str}")
                df.loc[ts] = bar_dict
        df = df.sort_index()
    return df

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

    # 執行歷史資料缺失補齊
    df = apply_data_patches(ticker, df)

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

    # === 計算前一天的目標槓桿 ===
    latest = df.iloc[-1]
    cur_p = round(float(latest['Close']), 2)
    f_ma = round(float(latest['SMA_fast']), 2)
    s_ma = round(float(latest['SMA_slow']), 2)
    h_bw = round(float(latest['High_bw']), 2)
    l_bw = round(float(latest['Low_bw']), 2)

    prev_target = round(target_history[-2], 2) if len(target_history) >= 2 else round(target_history[-1], 2)
    curr_target = round(target_history[-1], 2)
    lev_changed = curr_target != prev_target

    # === 判斷狀態並生成詳細說明 ===
    if cur_p < s_ma:
        # 收盤價跌破年線
        if lev_changed and curr_target < prev_target:
            explanation = (
                f"🚨【防禦降槓 {prev_target}x → {curr_target}x】"
                f"收盤價 ({cur_p}) 跌破年線 {slow_ma}MA ({s_ma})，"
                f"觸發護城河最高防線！動能階梯強制歸零，"
                f"從 {prev_target}x 急降至底倉 {curr_target}x。"
                f"嚴禁手動加碼，等待系統重啟訊號。"
            )
        else:
            explanation = (
                f"🛡️【防禦狀態 · 維持 {curr_target}x】"
                f"收盤價 ({cur_p}) 持續低於年線 {slow_ma}MA ({s_ma})，"
                f"維持最低底倉 {curr_target}x 防守避險。"
                f"距離年線還有 {round(s_ma - cur_p, 2)} 點，耐心等待均線修復。"
            )
    elif f_ma < s_ma:
        # 快線尚未站上慢線
        explanation = (
            f"👀【觀察狀態 · 維持 {curr_target}x】"
            f"收盤價 ({cur_p}) 雖在年線 {slow_ma}MA ({s_ma}) 之上，"
            f"但短線 {fast_ma}MA ({f_ma}) 尚未黃金交叉，多頭架構未完整確立。"
            f"快線距慢線 {round(s_ma - f_ma, 2)} 點，維持底倉 {curr_target}x 待命。"
        )
    else:
        # 多頭環境
        if step_idx == 0:
            diff_h = round(h_bw - cur_p, 2)
            explanation = (
                f"🚀【多頭待命 · 維持 {curr_target}x】"
                f"雙均線多頭確立 ({fast_ma}MA {f_ma} > {slow_ma}MA {s_ma})，"
                f"距第一階加碼門檻（突破近 {breakout_window} 日最高點 {h_bw}）差 {max(diff_h, 0.01)} 點。"
                f"突破前高前維持底倉，避免在震盪區追高！"
            )
        elif lev_changed and curr_target > prev_target:
            # 加碼升階
            explanation = (
                f"📈【突破加碼 {prev_target}x → {curr_target}x】"
                f"收盤價 ({cur_p}) 突破近 {breakout_window} 日最高點 ({h_bw})！"
                f"動能階梯升至 {step_idx}/{max_idx}，槓桿從 {prev_target}x 提升至 {curr_target}x。"
                f"下一階加碼需再次突破新高，減碼防線在 {l_bw} 以下。"
            )
        elif lev_changed and curr_target < prev_target:
            # 減碼降階
            explanation = (
                f"📉【跌破減碼 {prev_target}x → {curr_target}x】"
                f"收盤價 ({cur_p}) 跌破近 {breakout_window} 日最低點 ({l_bw})！"
                f"動能階梯降至 {step_idx}/{max_idx}，槓桿從 {prev_target}x 壓回 {curr_target}x。"
                f"若實際槓桿超過 {curr_target}x，須強制賣出部位壓回目標。"
            )
        else:
            # 維持中（多頭進攻，無變化）
            explanation = (
                f"⚡【多頭進攻 · 維持 {curr_target}x】"
                f"雙均線多頭且已確認 {step_idx} 次突破，"
                f"動能階梯 {step_idx}/{max_idx}，槓桿維持 {curr_target}x。"
                f"加碼門檻: >{h_bw}，減碼門檻: <{l_bw}。"
                f"目標槓桿未變，無需動作，讓利潤奔跑！"
            )

    # === 組合回傳資料 ===
    return {
        'id': ticker.replace('.', '_').lower(),
        'name': name,
        'ticker': ticker,
        'date': latest.name.strftime('%Y-%m-%d'),
        'close': cur_p,
        'in_trend': in_trend,
        'step_idx': step_idx,
        'max_steps': max_idx,
        'target_today': curr_target,
        'target_yesterday': prev_target,
        'sma_fast_val': f_ma,
        'sma_slow_val': s_ma,
        'fast_ma_len': fast_ma,
        'slow_ma_len': slow_ma,
        'high_bw': h_bw,
        'low_bw': l_bw,
        'base_leverage': base_leverage,
        'max_leverage': max_leverage,
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
            base_leverage=0.8, max_leverage=3.0,
            fast_ma=5, slow_ma=220, breakout_window=10, cooldown=3,
            allocs=[0.0, 0.5, 0.8, 1.0]
        )
        tw = compute_signal(
            name="台股 006208", ticker="006208.TW",
            base_leverage=0.6, max_leverage=3.0,
            fast_ma=10, slow_ma=220, breakout_window=20, cooldown=5,
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
