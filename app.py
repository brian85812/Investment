import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta, date
from flask import Flask, jsonify, render_template
from flask_cors import CORS
import logging
import time
import os
import threading
import urllib.request
import json
import ssl

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

# ========================================================
# 多層強健資料抓取層 (Multi-tier Robust Data Pipeline)
# 第 1 層: yfinance 套件標準抓取
# 第 2 層: Yahoo Direct Chart API (原生 HTTP 請求，繞過套件 Session/Cookie 阻擋)
# 第 3 層: 台灣證券交易所 (TWSE) 官方 API 自動對帳補齊
# ========================================================

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

def fetch_yahoo_direct(ticker):
    """第 2 層防禦：直接發送 HTTP 請求向 Yahoo Chart API 抓取 5 年 K 棒"""
    try:
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5y'
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
        })
        resp = urllib.request.urlopen(req, context=ssl_ctx, timeout=12)
        data = json.loads(resp.read().decode('utf-8'))
        result = data['chart']['result'][0]
        timestamps = result['timestamp']
        quote = result['indicators']['quote'][0]
        adj_close = result['indicators'].get('adjclose', [{}])[0].get('adjclose', quote['close'])
        
        df = pd.DataFrame({
            'Open': quote['open'],
            'High': quote['high'],
            'Low': quote['low'],
            'Close': adj_close if adj_close is not None else quote['close'],
            'Adj Close': adj_close,
            'Volume': quote['volume']
        }, index=pd.to_datetime(timestamps, unit='s'))
        df = df.dropna(subset=['Close'])
        if len(df) > 100:
            print(f"⚡ [Layer 2] Yahoo Direct API 備援成功抓取 {ticker}，共 {len(df)} 筆")
            return df
    except Exception as e:
        print(f"⚠️ [Layer 2] Yahoo Direct API 抓取失敗 ({ticker}): {e}")
    return None

def auto_patch_twse(ticker, df):
    """第 3 層防禦（台股專屬）：向台灣證券交易所 (TWSE) 官方即時比對本月與上月 K 棒，自動縫合缺漏日"""
    if not ticker.endswith('.TW') or df is None or df.empty:
        return df
        
    symbol = ticker.replace('.TW', '')
    now = datetime.now()
    dates_to_check = [now.strftime('%Y%m01')]
    first_day = now.replace(day=1)
    prev_month = (first_day - timedelta(days=1)).strftime('%Y%m01')
    dates_to_check.append(prev_month)
    
    patched_count = 0
    df_dates = set(df.index.strftime('%Y-%m-%d'))
    
    for d_str in dates_to_check:
        try:
            url = f'https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={d_str}&stockNo={symbol}'
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            resp = urllib.request.urlopen(req, context=ssl_ctx, timeout=8)
            data = json.loads(resp.read().decode('utf-8'))
            if 'data' not in data:
                continue
            for row in data['data']:
                parts = row[0].split('/')
                ad_year = int(parts[0]) + 1911
                iso_date = f'{ad_year}-{parts[1]}-{parts[2]}'
                
                # 若 Yahoo 漏掉此交易日，自動以證交所官方收盤價補齊
                if iso_date not in df_dates:
                    try:
                        o = float(row[3].replace(',', ''))
                        h = float(row[4].replace(',', ''))
                        l = float(row[5].replace(',', ''))
                        c = float(row[6].replace(',', ''))
                        vol = float(row[1].replace(',', ''))
                        ts = pd.Timestamp(iso_date) if df.index.tz is None else pd.Timestamp(iso_date, tz=df.index.tz)
                        bar_data = {'Open': o, 'High': h, 'Low': l, 'Close': c, 'Volume': vol}
                        if 'Adj Close' in df.columns:
                            bar_data['Adj Close'] = c
                        bar = pd.DataFrame([bar_data], index=[ts])
                        df = pd.concat([df, bar])
                        df_dates.add(iso_date)
                        patched_count += 1
                    except Exception:
                        pass
        except Exception:
            pass
            
    if patched_count > 0:
        df = df.sort_index()
        print(f"🏛️ [Layer 3] TWSE 官方 API 自動補齊 {symbol} 遺漏的 {patched_count} 天 K 棒！")
    return df

def fetch_robust_market_data(ticker):
    """整合 3 層防禦的資料抓取主函式"""
    df = None
    # 第 1 層：標準 yfinance
    for attempt in range(2):
        try:
            df = yf.download(ticker, period='5y', progress=False, auto_adjust=True)
            if df is not None and len(df) > 100:
                print(f"✅ [Layer 1] yfinance 抓取成功 ({ticker})，共 {len(df)} 筆")
                break
        except Exception as e:
            print(f"⚠️ [Layer 1] yfinance 第 {attempt+1} 次抓取失敗 ({ticker}): {e}")
        time.sleep(1)

    # 第 2 層：若 yfinance 失敗或回傳空值，切換 Yahoo Direct JSON API
    if df is None or len(df) < 100:
        print(f"🔄 正在啟用第二層防禦 (Yahoo Direct API) 抓取 {ticker}...")
        df = fetch_yahoo_direct(ticker)

    if df is None or len(df) == 0:
        print(f"❌ {ticker} 所有外部連線均告急")
        return None

    # 清理欄位格式
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # 第 3 層：台股向 TWSE 自動對帳補齊
    df = auto_patch_twse(ticker, df)

    # 執行已知手動補齊庫（雙重保險）
    df = apply_data_patches(ticker, df)

    # 針對最後一天即時報價 NaN 的容錯
    if len(df) > 0 and pd.isna(df['Close'].iloc[-1]):
        try:
            last_price = yf.Ticker(ticker).fast_info.last_price
            df.iloc[-1, df.columns.get_loc('Close')] = last_price
            for col in ['High', 'Low', 'Open']:
                if col in df.columns and pd.isna(df[col].iloc[-1]):
                    df.iloc[-1, df.columns.get_loc(col)] = last_price
        except Exception:
            pass

    return df

def compute_signal(name, ticker, base_leverage, max_leverage, fast_ma, slow_ma, breakout_window, cooldown, allocs, grace_days=0, extension_guard=None, bull_acceleration=None, deep_bear_guard=None, add_above_bear=False, pre_bull_boost=None, breakout_up=None, breakout_dn=None):
    """
    grace_days: 跌破年線容忍天數（美股 QQQ 為 0，台股 006208 為 1，即連續 2 天破線才確認）
    extension_guard: dict with {'threshold': 1.16, 'cap': 1.8} or None.
    bull_acceleration: dict with {'enable': True, 'accel_base': 1.2, 'accel_days': 252, 'min_bear_days': 40} or None.
    deep_bear_guard: dict with {'bear_ma': 60, 'ratio': 0.97, 'deep_base': 0.4, 'add_above_bear': True/False} or None.
    add_above_bear: 是否要求 FastMA >= 60MA 才允許向上突破升檔（美股 QQQ 啟用 D1-US 為 True，台股為 False）
    pre_bull_boost: E3b 季線右側總攻底倉（年線下若站穩 60MA 且突破 High_up，底倉升至美 1.0x / 台 1.2x）
    """
    df = fetch_robust_market_data(ticker)
    if df is None or len(df) == 0:
        return None

    if isinstance(breakout_window, (tuple, list)):
        w_up, w_dn = breakout_window[0], breakout_window[1]
    else:
        w_up = breakout_up if breakout_up is not None else breakout_window
        w_dn = breakout_dn if breakout_dn is not None else breakout_window

    max_idx = len(allocs) - 1
    in_trend = False
    step_idx = 0
    last_action_idx = -999
    target_history = []
    deep_bear_history = []
    pre_bull_history = []

    df['SMA_fast'] = df['Close'].rolling(window=fast_ma).mean()
    df['SMA_slow'] = df['Close'].rolling(window=slow_ma).mean()
    # 與 engine.py 回測完全一致，以收盤價 Close 計算 N 日突破與跌破通道（支援不對稱通道 w_up / w_dn）
    df['High_bw'] = df['Close'].shift(1).rolling(window=w_up).max()
    df['Low_bw']  = df['Close'].shift(1).rolling(window=w_dn).min()
    
    subset_cols = ['Close', 'SMA_slow', 'High_bw', 'Low_bw']
    if deep_bear_guard or add_above_bear or pre_bull_boost is not None:
        bear_w = deep_bear_guard.get('bear_ma', 60) if deep_bear_guard else 60
        df['SMA_bear'] = df['Close'].rolling(window=bear_w).mean()
        subset_cols.append('SMA_bear')

    df = df.dropna(subset=subset_cols)

    ext_guard_active = False  # 追蹤最新一天是否觸發過熱冷卻
    bear_counter = 0
    accel_active = False
    accel_timer = 0
    below_ma_counter = 0
    pre_bull_on = False

    for i in range(len(df)):
        current_close = df['Close'].iloc[i]
        sma_fast = df['SMA_fast'].iloc[i]
        sma_slow = df['SMA_slow'].iloc[i]
        high_bw  = df['High_bw'].iloc[i]
        low_bw   = df['Low_bw'].iloc[i]
        sma_bear = df['SMA_bear'].iloc[i] if 'SMA_bear' in df.columns else np.nan

        if current_close < sma_slow:
            below_ma_counter += 1
        else:
            below_ma_counter = 0

        # 1. 檢查是否觸發破線或死叉
        if sma_fast < sma_slow or below_ma_counter > grace_days:
            in_trend = False
            step_idx = 0
            accel_active = False
            accel_timer = 0

        # 2. 檢查是否滿足多頭重啟條件 (雙條件同時滿足)
        elif sma_fast >= sma_slow and current_close >= sma_slow and not in_trend:
            in_trend = True
            pre_bull_on = False
            if bull_acceleration and bull_acceleration.get('enable', True) and bear_counter >= bull_acceleration.get('min_bear_days', 40):
                accel_active = True
                accel_timer = 0
            bear_counter = 0

        # 3. 若處於防禦狀態 (not in_trend)，統一判定 C1 深熊減半、C1-A2 季線復原與 E3b 季線右側總攻
        if not in_trend:
            bear_counter += 1
            ext_guard_active = False
            cur_b = base_leverage
            is_deep = False
            if deep_bear_guard and not pd.isna(sma_bear) and sma_bear < sma_slow * deep_bear_guard.get('ratio', 0.97) and sma_fast < sma_bear:
                cur_b = deep_bear_guard.get('deep_base', base_leverage)
                is_deep = True
                pre_bull_on = False
            elif pre_bull_boost is not None and not pd.isna(sma_bear):
                if current_close >= sma_bear and sma_fast >= sma_bear:
                    if current_close > high_bw:
                        pre_bull_on = True
                else:
                    pre_bull_on = False
                if pre_bull_on:
                    cur_b = pre_bull_boost
            target_history.append(cur_b)
            deep_bear_history.append(is_deep)
            pre_bull_history.append(pre_bull_on)
            continue

        # 4. 以下為 in_trend == True (多頭進攻狀態)，deep_bear 與 pre_bull 強制為 False
        cur_base = base_leverage
        if accel_active and bull_acceleration:
            accel_timer += 1
            cur_base = bull_acceleration.get('accel_base', 1.2)
            if accel_timer >= bull_acceleration.get('accel_days', 252):
                accel_active = False
                cur_base = base_leverage

        req_bear = add_above_bear or (deep_bear_guard and deep_bear_guard.get('add_above_bear', False))
        if in_trend and (i - last_action_idx >= cooldown):
            if current_close > high_bw:
                if step_idx < max_idx and (not req_bear or pd.isna(sma_bear) or sma_fast >= sma_bear):
                    step_idx += 1
                    last_action_idx = i
            elif current_close < low_bw:
                if step_idx > 0:
                    step_idx -= 1
                    last_action_idx = i

        alloc_pct = allocs[step_idx]
        target = cur_base * (1 - alloc_pct) + max_leverage * alloc_pct

        # === 乖離過熱冷卻器 (Extension Guard) ===
        ext_guard_active = False
        if extension_guard and sma_slow > 0:
            ext_ratio = current_close / sma_slow
            if ext_ratio > extension_guard['threshold']:
                original_target = target
                target = min(target, extension_guard['cap'])
                if target < original_target:
                    ext_guard_active = True

        target_history.append(target)
        deep_bear_history.append(False)
        pre_bull_history.append(False)

    # === 計算前一天的目標槓桿 ===
    latest = df.iloc[-1]
    cur_p = round(float(latest['Close']), 2)
    f_ma = round(float(latest['SMA_fast']), 2)
    s_ma = round(float(latest['SMA_slow']), 2)
    h_bw = round(float(latest['High_bw']), 2)
    l_bw = round(float(latest['Low_bw']), 2)
    sma_bear_val = round(float(latest['SMA_bear']), 2) if 'SMA_bear' in latest and not pd.isna(latest['SMA_bear']) else None

    prev_target = round(target_history[-2], 2) if len(target_history) >= 2 else round(target_history[-1], 2)
    curr_target = round(target_history[-1], 2)
    trim_trigger = round(curr_target + 0.15, 2)
    lev_changed = curr_target != prev_target
    deep_bear_active = deep_bear_history[-1] if deep_bear_history else False
    pre_bull_active = pre_bull_history[-1] if pre_bull_history else False

    # === 實戰狀態 (Cases) 專屬戰術說明文案 ===
    bear_ratio = deep_bear_guard.get('ratio', 0.97) if deep_bear_guard else 0.97
    bear_w = deep_bear_guard.get('bear_ma', 60) if deep_bear_guard else 60
    bear_thresh = round(s_ma * bear_ratio, 2)
    ext_ratio_now = round(cur_p / s_ma, 4) if s_ma > 0 else 0.0
    ext_pct_now = round((cur_p / s_ma - 1) * 100, 1) if s_ma > 0 else 0.0
    ext_cap_val = extension_guard['cap'] if extension_guard else 1.8

    if not in_trend:
        # 【空頭防禦與季線總攻大類】
        if deep_bear_active and deep_bear_guard:
            if lev_changed and curr_target < prev_target:
                # Case 1: 剛觸發 C1 深熊二階段減半當日
                explanation = (
                    f"🚨【深熊二階段減碼 · {prev_target:.2f}x ➔ {curr_target:.2f}x】"
                    f"季線 {bear_w}MA ({sma_bear_val}) 已跌破年線 97% 深熊門檻 ({bear_thresh}) 且快線位於季線之下，"
                    f"確認進入系統性空頭深水區！今日請掛限價單將底倉減半至 {curr_target:.2f}x 保命。"
                )
            else:
                # Case 2: 維持 C1 深熊減半期間
                explanation = (
                    f"🛡️【深熊減半防禦中 · 維持 {curr_target:.2f}x】"
                    f"中期與長期均線持續空頭排列，維持二階段減半底倉 {curr_target:.2f}x 避險。"
                    f"無須動作；待快線 ({f_ma}) 重新站上季線 ({sma_bear_val}) 即刻復原常態底倉 {base_leverage:.1f}x。"
                )
        elif pre_bull_active and pre_bull_boost is not None:
            if lev_changed and curr_target > prev_target:
                # Case 3a: E3b 季線右側總攻觸發當日
                explanation = (
                    f"⚡【季線右側總攻啟動 (Stage 0) · {prev_target:.2f}x ➔ {curr_target:.2f}x】"
                    f"收盤價 ({cur_p}) 與 {fast_ma}MA 快線 ({f_ma}) 已雙雙站穩季線 {bear_w}MA ({sma_bear_val}) 並突破近 {w_up} 日高點 ({h_bw})！"
                    f"今日請掛限價單將底倉推升至準牛市總攻檔位 {curr_target:.2f}x，迎戰 {slow_ma}MA 年線 ({s_ma})。"
                )
            else:
                # Case 3b: E3b 季線右側總攻巡航期間
                diff_s = round(max(s_ma - cur_p, 0.0), 2)
                explanation = (
                    f"⚡【季線右側總攻巡航 (Stage 0) · 維持 {curr_target:.2f}x】"
                    f"目前已站穩季線 ({sma_bear_val}) 並完成右側突破，維持準牛市總攻底倉 {curr_target:.2f}x。"
                    f"距離正式站上 {slow_ma}MA 年線 ({s_ma}) 尚差 {diff_s} 點，今日無須動作。"
                )
        elif lev_changed and curr_target > prev_target:
            # Case 3: C1-A2 季線復原觸發當日 (快線站上 60MA，底倉由 0.4x/0.5x 恢復至 0.8x/1.0x)
            explanation = (
                f"🌤️【深熊底部復原 (Stage -1) · {prev_target:.2f}x ➔ {curr_target:.2f}x】"
                f"短線 {fast_ma}MA ({f_ma}) 已成功站上季線 {bear_w}MA ({sma_bear_val})，空頭最劇烈尋底期結束！"
                f"今日請掛限價單將底倉由深熊半倉恢復至常態 {curr_target:.2f}x；後續若突破 {w_up} 日高點 ({h_bw}) 將再升至 {pre_bull_boost:.1f}x 總攻。"
            )
        elif cur_p < s_ma:
            if lev_changed and curr_target < prev_target:
                # Case 4: 跌破 220MA (或跌破 60MA 退出 E3b) 確認退回常態底倉當日
                explanation = (
                    f"🚨【防禦降槓退回底倉 · {prev_target:.2f}x ➔ {curr_target:.2f}x】"
                    f"收盤價 ({cur_p}) 位於 {slow_ma}MA 年線護城河 ({s_ma}) 之下，動能退回常態防守底倉！"
                    f"今日請於最佳時段掛限價單退回常態底倉 {curr_target:.2f}x，嚴禁盤中手動抄底。"
                )
            else:
                # Case 5: 常態年線下防禦維持期間
                explanation = (
                    f"🛡️【年線下防禦待命 · 維持 {curr_target:.2f}x】"
                    f"收盤價距離 {slow_ma}MA 年線尚差 {round(s_ma - cur_p, 2)} 點（目前季線 {sma_bear_val}，深熊線 {bear_thresh}）。"
                    f"今日無須動作，維持常態底倉 {curr_target:.2f}x 靜待均線修復。"
                )
        else:
            # Case 6: 收盤已站上年線，但快線尚未黃金交叉慢線
            explanation = (
                f"👀【右側重啟觀察期 · 維持 {curr_target:.2f}x】"
                f"收盤價 ({cur_p}) 已率先站回年線 ({s_ma}) 之上，惟 {fast_ma}MA 快線 ({f_ma}) 尚差 {round(s_ma - f_ma, 2)} 點完成黃金交叉。"
                f"今日維持底倉 {curr_target:.2f}x 按兵不動，待雙條件同時滿足即刻重啟多頭。"
            )
    else:
        # 【多頭進攻大類：共 5 個 Cases】
        if cur_p < s_ma and below_ma_counter <= grace_days:
            # Case 7: 台股專屬 1-Day Grace Period 首日破年線觀察
            explanation = (
                f"⚠️【破年線容忍觀察 (第 1/{grace_days} 日) · 暫維 {curr_target:.2f}x】"
                f"收盤價 ({cur_p}) 首日微幅跌破年線 ({s_ma})，啟動台股防洗盤容忍保護，今日暫不砍倉！"
                f"明日收盤若未能站回 {s_ma} 之上，將正式確認破線並退回 {base_leverage:.1f}x 底倉。"
            )
        elif step_idx == 0:
            # Case 8: 剛站上年線多頭確立，處於 Step 0 底倉等待第一次突破
            diff_h = round(max(h_bw - cur_p, 0.01), 2)
            explanation = (
                f"🚀【多頭起跑待命 (Step 0/{max_idx}) · 目標 {curr_target:.2f}x】"
                f"年線護城河與快線多頭排列已確立！目前持守底倉 {curr_target:.2f}x，"
                f"距離第一階 {w_up} 日突破加碼門檻 ({h_bw}) 僅差 {diff_h} 點，突破前不提前追高。"
            )
        elif lev_changed and curr_target > prev_target:
            # Case 9: 突破 N 日高點（或脫離過熱區）觸發升檔加碼當日
            if step_idx == max_idx:
                explanation = (
                    f"🔥【突破滿載加碼 (Step {step_idx}/{max_idx}) · {prev_target:.2f}x ➔ {curr_target:.2f}x】"
                    f"收盤價 ({cur_p}) 創近 {w_up} 日收盤新高！動能階梯推進至最高滿載檔位。"
                    f"今日請掛限價單加碼至 {curr_target:.2f}x 天花板；後續不再加買，僅盯 {w_dn} 日下檔減碼線 ({l_bw}) 與修剪線 ({trim_trigger:.2f}x)。"
                )
            else:
                explanation = (
                    f"📈【順勢突破加碼 (Step {step_idx}/{max_idx}) · {prev_target:.2f}x ➔ {curr_target:.2f}x】"
                    f"收盤價 ({cur_p}) 成功突破近 {w_up} 日高點 ({h_bw})，趨勢動能確認！"
                    f"今日請於最佳時段掛限價單將部位推升至 {curr_target:.2f}x（冷卻期 {cooldown} 日後始可再升檔）。"
                )
        elif lev_changed and curr_target < prev_target:
            # Case 10: 跌破 N 日低點（或觸發過熱冷卻器壓回 1.8x）觸發降檔減碼當日
            if ext_guard_active and extension_guard:
                explanation = (
                    f"🌡️【乖離過熱鎖利減碼 · {prev_target:.2f}x ➔ {curr_target:.2f}x】"
                    f"年線正乖離達 +{ext_pct_now}%（超過 +16% 警戒線），啟動美股過熱保護閥！"
                    f"今日請掛限價單將槓桿上限主動壓回 {curr_target:.2f}x 鎖住高檔獲利，待乖離回落 ≤16% 再恢復 3.0x 權限。"
                )
            else:
                explanation = (
                    f"📉【跌破通道減碼 (Step {step_idx}/{max_idx}) · {prev_target:.2f}x ➔ {curr_target:.2f}x】"
                    f"收盤價 ({cur_p}) 跌破近 {w_dn} 日低點支撐 ({l_bw})，短線動能轉弱。"
                    f"若你目前帳戶實際槓桿高於 {curr_target:.2f}x，今日請掛限價單減碼壓回 {curr_target:.2f}x 目標。"
                )
        else:
            # Case 11: 多頭進攻維持期間（細分為：滿倉 3.0x vs 過熱封頂 1.8x vs 中段爬坡中）
            if ext_guard_active and extension_guard:
                explanation = (
                    f"🌡️【多頭過熱冷卻中 (Step {step_idx}/{max_idx}) · 封頂維持 {curr_target:.2f}x】"
                    f"目前年線正乖離 +{ext_pct_now}%（>16%），加碼上限鎖死於 {ext_cap_val:.1f}x。"
                    f"今日完全不動作；待股價橫盤或年線跟上使乖離 ≤16% 後，自動解鎖 3.0x 滿載空間。"
                )
            elif step_idx == max_idx:
                explanation = (
                    f"👑【滿載巡航奔跑中 (Step {step_idx}/{max_idx}) · 維持上限 {curr_target:.2f}x】"
                    f"已達系統最高槓桿天花板，上方無更高加碼檔位！上漲過程讓槓桿自然衰減、利潤奔跑，絕對不追高補倉；"
                    f"日常僅需防守 {w_dn} 日下檔減碼線 (<{l_bw}) 與被動超標修剪線 (>{trim_trigger:.2f}x)。"
                )
            else:
                diff_h = round(max(h_bw - cur_p, 0.01), 2)
                if req_bear and sma_bear_val is not None and f_ma < sma_bear_val:
                    explanation = (
                        f"🛡️【季線下整理凍結加碼 (Step {step_idx}/{max_idx}) · 維持 {curr_target:.2f}x】"
                        f"目前位於年線之上，但快線 {fast_ma}MA ({f_ma}) 尚未站回季線 {bear_w}MA ({sma_bear_val})，"
                        f"啟動 D1 夾層保護：暫時凍結向上突破升檔，僅防守 {w_dn} 日下檔減碼線 (<{l_bw})；待 {fast_ma}MA 站回 {sma_bear_val} 即解鎖加碼。"
                    )
                else:
                    explanation = (
                        f"⚡【多頭順勢巡航 (Step {step_idx}/{max_idx}) · 維持 {curr_target:.2f}x】"
                        f"趨勢穩健推進中，今日目標槓桿不變，無須任何操作。"
                        f"距下一階 {w_up} 日突破加碼門檻 (>{h_bw}) 差 {diff_h} 點，{w_dn} 日下檔減碼防線設於 <{l_bw}。"
                    )

    # === 附加狀態尾註（僅在牛初加速啟動時補充） ===
    bull_accel_active = accel_active if bull_acceleration else False
    bull_accel_days_left = max(0, bull_acceleration.get('accel_days', 252) - accel_timer) if (bull_acceleration and accel_active) else 0
    if bull_accel_active:
        explanation += f" 🚀【台股牛初加速生效中：底倉基準升為 {cur_base:.1f}x，剩餘 {bull_accel_days_left} 交易日】"

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
        'trim_trigger_lev': trim_trigger,
        'sma_fast_val': f_ma,
        'sma_slow_val': s_ma,
        'fast_ma_len': fast_ma,
        'slow_ma_len': slow_ma,
        'bo_up_len': w_up,
        'bo_dn_len': w_dn,
        'high_bw': h_bw,
        'low_bw': l_bw,
        'base_leverage': base_leverage,
        'max_leverage': max_leverage,
        'explanation': explanation,
        'ext_guard_active': ext_guard_active,
        'ext_cap': ext_cap_val if extension_guard else None,
        'ext_ratio': round(ext_ratio_now, 4),
        'bull_accel_active': bull_accel_active,
        'bull_accel_days_left': bull_accel_days_left,
        'deep_bear_active': deep_bear_active,
        'pre_bull_active': pre_bull_active,
        'sma_bear_val': sma_bear_val,
        'deep_bear_ratio': deep_bear_guard.get('ratio', 0.97) if deep_bear_guard else None
    }

def refresh_cache():
    """在背景 Thread 更新快取，不阻塞 web 請求"""
    if _cache['is_updating']:
        return
    _cache['is_updating'] = True
    print("🔄 正在更新市場資料快取...")

    try:
        # 保存既有快取資料，用於單一標的斷線時的保底防禦 (Layer 4)
        old_data_map = {}
        if _cache['data']:
            for item in _cache['data']:
                old_data_map[item['ticker']] = item

        qqq = compute_signal(
            name="美股 QQQ", ticker="QQQ",
            base_leverage=0.8, max_leverage=3.0,
            fast_ma=5, slow_ma=220, breakout_window=8, breakout_up=8, breakout_dn=8, cooldown=8,
            allocs=[0.0, 0.65, 0.85, 1.0],
            grace_days=0,
            extension_guard={'threshold': 1.16, 'cap': 1.8},
            deep_bear_guard={'bear_ma': 60, 'ratio': 0.97, 'deep_base': 0.4, 'add_above_bear': True},
            add_above_bear=True,
            pre_bull_boost=1.0
        )
        if qqq is None and 'QQQ' in old_data_map:
            print("🛡️ [Layer 4] QQQ 啟動離線快取保底，沿用前次有效訊號")
            qqq = old_data_map['QQQ'].copy()
            if not qqq['explanation'].startswith('⚠️【連線維護】'):
                qqq['explanation'] = "⚠️【連線維護中 · 沿用前次訊號】" + qqq['explanation']

        tw = compute_signal(
            name="台股 006208", ticker="006208.TW",
            base_leverage=1.0, max_leverage=3.0,
            fast_ma=8, slow_ma=220, breakout_window=15, breakout_up=15, breakout_dn=25, cooldown=10,
            allocs=[0.0, 0.50, 0.75, 0.90, 1.0],
            grace_days=1,
            bull_acceleration={'enable': True, 'accel_base': 1.2, 'accel_days': 252, 'min_bear_days': 40},
            deep_bear_guard={'bear_ma': 60, 'ratio': 0.97, 'deep_base': 0.5},
            add_above_bear=False,
            pre_bull_boost=1.2
        )
        if tw is None and '006208.TW' in old_data_map:
            print("🛡️ [Layer 4] 006208 啟動離線快取保底，沿用前次有效訊號")
            tw = old_data_map['006208.TW'].copy()
            if not tw['explanation'].startswith('⚠️【連線維護】'):
                tw['explanation'] = "⚠️【連線維護中 · 沿用前次訊號】" + tw['explanation']

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
# 🎯 期貨轉倉智能雷達 (Rollover Radar)
# ========================================================
def compute_rollover_radar():
    today = datetime.now().date()
    
    # 1. 台指期結算計算 (每月第 3 個週三)
    def third_wed(year, month):
        d1 = date(year, month, 1)
        w1 = d1 + timedelta(days=(2 - d1.weekday()) % 7)
        return w1 + timedelta(weeks=2)
        
    y, m = today.year, today.month
    tw_settlement = third_wed(y, m)
    current_m = m
    next_m = m + 1 if m < 12 else 1
    if today > tw_settlement:
        y, m = (y, m + 1) if m < 12 else (y + 1, 1)
        tw_settlement = third_wed(y, m)
        current_m = m
        next_m = m + 1 if m < 12 else 1
        
    tw_days_left = (tw_settlement - today).days
    tw_cur_code = f"{current_m:02d}月"
    tw_next_code = f"{next_m:02d}月"
    
    if tw_days_left > 5:
        tw_badge = "⚪ 常規持有"
        tw_class = "neutral"
        tw_phase = "hold"
        tw_action = f"距結算還有 {tw_days_left} 天，安心持倉無需理會價差。"
    elif tw_days_left >= 3:
        tw_badge = "📋 開始觀察"
        tw_class = "warning"
        tw_phase = "watch"
        tw_action = f"倒數 {tw_days_left} 天，次月增溫中，每天瞄一眼價差，夠甜就轉。"
    elif tw_days_left in [1, 2]:
        tw_badge = "🎯 黃金轉倉期"
        tw_class = "bull"
        tw_phase = "go"
        tw_action = "次月流動性超過 85%，建議立即使用【跨月價差單】掛限價換約！"
    else:
        tw_badge = "🚨 今日結算"
        tw_class = "bear"
        tw_phase = "deadline"
        tw_action = "今日 13:30 結算！不管價差多少，請立即以價差單換約避免斷倉！"

    # 2. 美股那指期 (NQ/MNQ) 結算計算 (3/6/9/12月 第 3 個週五)
    def third_fri(year, month):
        d1 = date(year, month, 1)
        f1 = d1 + timedelta(days=(4 - d1.weekday()) % 7)
        return f1 + timedelta(weeks=2)
        
    q_months = [3, 6, 9, 12]
    q_codes = {3: 'H', 6: 'M', 9: 'U', 12: 'Z'}
    us_y, us_m = today.year, today.month
    us_settlement = None
    us_cur_q = None
    us_next_q = None
    
    for i, qm in enumerate(q_months):
        if qm >= us_m:
            tf = third_fri(us_y, qm)
            if today <= tf:
                us_settlement = tf
                us_cur_q = qm
                us_next_q = q_months[(i + 1) % 4]
                break
    if us_settlement is None:
        us_settlement = third_fri(us_y + 1, 3)
        us_cur_q = 3
        us_next_q = 6
        
    us_days_left = (us_settlement - today).days
    roll_week_start = us_settlement - timedelta(days=8)
    watch_start = us_settlement - timedelta(days=14)
    cur_code_str = f"NQ{q_codes[us_cur_q]}{str(us_settlement.year)[-1]}"
    next_code_str = f"NQ{q_codes[us_next_q]}{str(us_settlement.year + (1 if us_next_q==3 else 0))[-1]}"
    
    if today < watch_start:
        us_badge = "⚪ 常規持有"
        us_class = "neutral"
        us_phase = "hold"
        days_to_watch = (watch_start - today).days
        us_action = f"距觀察期還有 {days_to_watch} 天，近月深度最佳，安心持有。"
    elif today < roll_week_start:
        us_badge = "📋 開始觀察"
        us_class = "warning"
        us_phase = "watch"
        days_to_roll = (roll_week_start - today).days
        us_action = f"距 Roll Week 還有 {days_to_roll} 天，可開始留意跨季價差與年化溢價率。"
    elif roll_week_start <= today < us_settlement:
        us_badge = "🎯 CME Roll Week"
        us_class = "bull"
        us_phase = "go"
        us_action = f"次季量能已超越當季，建議使用【Calendar Spread】掛 Mid-Price 換約！"
    else:
        us_badge = "🚨 四巫日結算"
        us_class = "bear"
        us_phase = "deadline"
        us_action = "今日到期結算！造市商抽單中，請立即以價差單完成轉倉！"

    return {
        'tw': {
            'market': '台指期 (TX / MTX / TMF)',
            'contract_info': f'當月 {tw_cur_code} ➔ 次月 {tw_next_code}',
            'settlement_date': tw_settlement.strftime('%Y-%m-%d'),
            'days_left': tw_days_left,
            'badge': tw_badge,
            'status_class': tw_class,
            'phase': tw_phase,
            'action': tw_action,
            'rule_tip': '💡 判定準則：若「遠月價格 < 加權現貨」（逆價差），為無腦送分題直接轉倉！'
        },
        'us': {
            'market': '那指期 (NQ / MNQ)',
            'contract_info': f'當季 {cur_code_str} ➔ 次季 {next_code_str}',
            'settlement_date': us_settlement.strftime('%Y-%m-%d'),
            'roll_start': roll_week_start.strftime('%Y-%m-%d'),
            'watch_start': watch_start.strftime('%Y-%m-%d'),
            'days_left': us_days_left,
            'badge': us_badge,
            'status_class': us_class,
            'phase': us_phase,
            'action': us_action,
            'rule_tip': '💡 判定準則：以 Spread 限價單掛在買賣中間價（Mid-Price），若年化溢價率 < 4.8% 即為合理區間。'
        }
    }

@app.route('/api/rollover')
def get_rollover():
    return jsonify(compute_rollover_radar())

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
