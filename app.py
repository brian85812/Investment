import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS
import logging
import time
import os
import threading
import urllib.request
import json
import ssl

# 台北時區 (UTC+8)，防範 GitHub Actions (UTC) 時區偏差
TZ_TAIPEI = timezone(timedelta(hours=8))

# 關閉不必要的 Flask 輸出
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)
_allowed_origins = os.environ.get("CORS_ALLOWED_ORIGINS", "*")
CORS(app, resources={r"/api/*": {"origins": _allowed_origins}, r"/health": {"origins": _allowed_origins}})

# ========================================================
# 快取層：伺服器啟動時預先算好，之後每 30 分鐘更新一次
# ========================================================
_refresh_lock = threading.Lock()
_cache = {
    'data': None,
    'last_updated': None,
    'last_attempt_at': None,
    'last_success_at': None,
    'is_updating': False,
    'degraded': False,
    'last_refresh_trigger': 0.0
}
CACHE_TTL_MINUTES = 30
MIN_REFRESH_COOLDOWN_SECONDS = 30

# ========================================================
# 已知歷史資料缺失自動補齊庫 (Known Data Patches)
# 解決 Yahoo Finance 偶發性漏掉特定交易日 K 棒的問題
# ========================================================
KNOWN_DATA_PATCHES = {
    '006208.TW': {
        '2026-08-13': {'Open': 242.5, 'High': 246.0, 'Low': 242.0, 'Close': 245.5, 'Volume': 5000000.0},
        '2026-09-01': {'Open': 244.85, 'High': 248.85, 'Low': 244.85, 'Close': 248.35, 'Volume': 3065991.0},
        '2026-10-27': {'Open': 247.75, 'High': 247.75, 'Low': 242.95, 'Close': 244.60, 'Volume': 6234509.0}
    },
    '0050.TW': {
        '2026-08-13': {'Open': 105.5, 'High': 107.0, 'Low': 105.2, 'Close': 106.8, 'Volume': 70000000.0},
        '2026-10-27': {'Open': 107.10, 'High': 107.55, 'Low': 105.80, 'Close': 106.60, 'Volume': 153499719.0}
    }
}

def apply_data_patches(ticker, df):
    """檢查並自動修補 Yahoo Finance 遺漏的交易日 (F02/R05: 對齊還原價格口徑，缺可驗證因子時拒絕盲補)"""
    if ticker in KNOWN_DATA_PATCHES and df is not None and not df.empty:
        patches = KNOWN_DATA_PATCHES[ticker]
        for date_str, bar_dict in patches.items():
            ts = pd.Timestamp(date_str) if df.index.tz is None else pd.Timestamp(date_str, tz=df.index.tz)
            if ts not in df.index:
                factor = None
                if 'adj_factor' in bar_dict and bar_dict['adj_factor'] is not None:
                    factor = float(bar_dict['adj_factor'])
                elif 'Raw Close' in df.columns and 'Close' in df.columns:
                    before = df.loc[df.index < ts]
                    after = df.loc[df.index > ts]
                    if len(before) > 0 and len(after) > 0:
                        ref_b = before.iloc[-1]
                        ref_a = after.iloc[0]
                        if (pd.notna(ref_b.get('Raw Close')) and float(ref_b['Raw Close']) > 0 and pd.notna(ref_b.get('Close')) and float(ref_b['Close']) > 0
                                and pd.notna(ref_a.get('Raw Close')) and float(ref_a['Raw Close']) > 0 and pd.notna(ref_a.get('Close')) and float(ref_a['Close']) > 0):
                            fb = float(ref_b['Close']) / float(ref_b['Raw Close'])
                            fa = float(ref_a['Close']) / float(ref_a['Raw Close'])
                            if abs(fb - fa) <= 1e-4:
                                factor = 0.5 * (fb + fa)
                if factor is None or factor <= 0:
                    df.attrs['unverified_patch_skipped'] = True
                    print(f"⚠️ 略過 {ticker} 手動補丁 {date_str}：缺少可驗證之還原調整因子 (adj_factor)")
                    continue
                scaled = {}
                for k, v in bar_dict.items():
                    if k in ('Open', 'High', 'Low', 'Close', 'Adj Close'):
                        scaled[k] = float(v) * factor
                    elif k == 'Volume':
                        scaled[k] = float(v)
                if 'Raw Close' in df.columns and 'Close' in bar_dict:
                    scaled['Raw Close'] = float(bar_dict['Close'])
                for col in df.columns:
                    if col not in scaled:
                        scaled[col] = scaled.get('Close', np.nan)
                print(f"🔧 自動修補 {ticker} 缺失的交易日 K 棒: {date_str} (factor={factor:.6f})")
                df.loc[ts] = scaled
        df = df.sort_index()
    return df

# ========================================================
# 多層強健資料抓取層 (Multi-tier Robust Data Pipeline)
# 第 1 層: yfinance 套件標準抓取
# 第 2 層: Yahoo Direct Chart API (原生 HTTP 請求，繞過套件 Session/Cookie 阻擋)
# 第 3 層: 台灣證券交易所 (TWSE) 官方 API 自動對帳補齊
# ========================================================

try:
    import certifi
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
except Exception:
    ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = True
ssl_ctx.verify_mode = ssl.CERT_REQUIRED

def fetch_yahoo_direct(ticker):
    """第 2 層防禦：直接發送 HTTP 請求向 Yahoo Chart API 抓取 10 年 K 棒 (F02/F09/R01: 統一位置陣列對齊還原因子且驗收暖機長度)"""
    try:
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=10y'
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
        })
        resp = urllib.request.urlopen(req, context=ssl_ctx, timeout=12)
        data = json.loads(resp.read().decode('utf-8'))
        result = data['chart']['result'][0]
        timestamps = result['timestamp']
        quote = result['indicators']['quote'][0]
        adj_close = result['indicators'].get('adjclose', [{}])[0].get('adjclose', quote['close'])
        
        raw_close = np.asarray(quote['close'], dtype='float64')
        adj_arr = np.asarray(adj_close if adj_close is not None else quote['close'], dtype='float64')
        with np.errstate(divide='ignore', invalid='ignore'):
            valid_mask = (raw_close > 0) & (~np.isnan(raw_close)) & (~np.isnan(adj_arr))
            factor = np.where(valid_mask, adj_arr / raw_close, 1.0)

        open_arr = np.asarray(quote['open'], dtype='float64') * factor
        high_arr = np.asarray(quote['high'], dtype='float64') * factor
        low_arr = np.asarray(quote['low'], dtype='float64') * factor
        vol_arr = np.asarray(quote['volume'], dtype='float64')

        df = pd.DataFrame({
            'Open': open_arr,
            'High': high_arr,
            'Low': low_arr,
            'Close': adj_arr,
            'Adj Close': adj_arr,
            'Raw Close': raw_close,
            'Volume': vol_arr
        }, index=pd.to_datetime(timestamps, unit='s'))
        df = df.dropna(subset=['Close'])
        if len(df) >= 225:
            print(f"⚡ [Layer 2] Yahoo Direct API 備援成功抓取 {ticker}，共 {len(df)} 筆")
            return df
    except Exception as e:
        print(f"⚠️ [Layer 2] Yahoo Direct API 抓取失敗 ({ticker}): {e}")
    return None

def auto_patch_twse(ticker, df):
    """第 3 層防禦（台股專屬）：向台灣證券交易所 (TWSE) 官方即時比對近 45 天 K 棒，自動縫合缺漏日並清理無效假 K 棒 (F02/F08/R02-R04)"""
    if not ticker.endswith('.TW') or df is None or df.empty:
        return df
        
    symbol = ticker.replace('.TW', '')
    now = datetime.now(TZ_TAIPEI)
    cutoff_dt = now - timedelta(days=45)
    cutoff_date = cutoff_dt.strftime('%Y-%m-%d')
    today_str = now.strftime('%Y-%m-%d')

    # F08: 涵蓋近 45 天可能跨越之所有月份 (最多 3 個月)
    dates_to_check = []
    cur_m = now.replace(day=1)
    while cur_m.strftime('%Y-%m') >= cutoff_dt.strftime('%Y-%m') and len(dates_to_check) < 4:
        m_str = cur_m.strftime('%Y%m01')
        if m_str not in dates_to_check:
            dates_to_check.append(m_str)
        cur_m = (cur_m - timedelta(days=1)).replace(day=1)
    
    patched_count = 0
    updated_today = False
    
    twse_valid_dates = set()
    verified_months = set()
    parsed_twse_rows = {}
    
    for d_str in dates_to_check:
        try:
            url = f'https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={d_str}&stockNo={symbol}'
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            resp = urllib.request.urlopen(req, context=ssl_ctx, timeout=8)
            data = json.loads(resp.read().decode('utf-8'))
            if 'data' not in data or not isinstance(data['data'], list):
                continue
            month_prefix = f"{d_str[:4]}-{d_str[4:6]}"
            if len(data['data']) > 0:
                verified_months.add(month_prefix)
            for row in data['data']:
                parts = row[0].strip().split('/')
                ad_year = int(parts[0]) + 1911
                iso_date = f'{ad_year}-{parts[1]}-{parts[2]}'
                twse_valid_dates.add(iso_date)
                verified_months.add(iso_date[:7])
                
                try:
                    o = float(row[3].replace(',', ''))
                    h = float(row[4].replace(',', ''))
                    l = float(row[5].replace(',', ''))
                    c = float(row[6].replace(',', ''))
                    vol = float(row[1].replace(',', ''))
                except Exception:
                    continue

                chg_val = None
                if len(row) > 7 and isinstance(row[7], str):
                    raw_chg_str = row[7].strip()
                    if not any(tok in raw_chg_str.upper() for tok in ('X', '除', '無')):
                        cleaned_chg = raw_chg_str.replace(',', '').replace('+', '').strip()
                        try:
                            chg_val = float(cleaned_chg)
                        except Exception:
                            chg_val = None

                parsed_twse_rows[iso_date] = {
                    'o': o, 'h': h, 'l': l, 'c': c, 'vol': vol, 'chg': chg_val
                }
        except Exception as e:
            print(f"⚠️ [Layer 3] TWSE API 查詢失敗 ({symbol}, {d_str}): {e}")

    # F02/R02-R04/S01: 計算 TWSE 原始價格對 df (auto_adjust=True) 的還原調整因子
    overlap_factors = {}
    for iso_date, rinfo in parsed_twse_rows.items():
        ts = pd.Timestamp(iso_date)
        if ts in df.index and iso_date != today_str and rinfo['c'] > 0:
            df_c = float(df.loc[ts, 'Close'])
            if df_c > 0:
                overlap_factors[ts] = df_c / rinfo['c']

    _tw_hol_set = {
        '2025-01-23', '2025-01-24', '2025-01-27', '2025-01-28', '2025-01-29', '2025-01-30', '2025-01-31',
        '2025-04-03', '2025-05-30', '2025-09-29', '2025-10-06', '2025-10-24',
        '2026-02-12', '2026-02-13', '2026-02-16', '2026-02-17', '2026-02-18', '2026-02-19', '2026-02-20',
        '2026-02-27', '2026-04-03', '2026-04-06', '2026-06-19', '2026-09-25', '2026-09-28', '2026-10-09', '2026-10-26', '2026-12-25'
    }

    def _is_immediate_prev_tw_session(prev_d, target_d):
        if prev_d >= target_d or (target_d - prev_d).days > 14:
            return False
        cur_d = target_d - timedelta(days=1)
        while cur_d > prev_d:
            cur_iso = cur_d.strftime('%Y-%m-%d')
            is_non_trade = (
                cur_d.weekday() >= 5
                or (cur_d.month, cur_d.day) in {(1, 1), (2, 28), (4, 4), (4, 5), (5, 1), (10, 10), (12, 25)}
                or cur_iso in _tw_hol_set
            ) and (cur_iso not in twse_valid_dates)
            if not is_non_trade:
                return False
            cur_d -= timedelta(days=1)
        return True

    def _resolve_adj_factor(target_ts, rinfo):
        # 1. 優先以當日官方漲跌價差 (c - chg = 前一交易日收盤價/參考價) 對齊 df 真正前一交易日還原收盤價 (S01: 嚴格驗證無中間缺日且非無比價 X)
        if rinfo.get('chg') is not None:
            implied_prev_raw = rinfo['c'] - rinfo['chg']
            prev_idx = df.index[df.index < target_ts]
            if implied_prev_raw > 0 and len(prev_idx) > 0:
                prev_ts = prev_idx[-1]
                if _is_immediate_prev_tw_session(prev_ts.date(), target_ts.date()):
                    prev_df_c = float(df.loc[prev_ts, 'Close'])
                    if prev_df_c > 0:
                        return prev_df_c / implied_prev_raw
        # 2. 若無漲跌價差，僅在同日已有重疊因子或前後重疊因子一致（期間無除權息）時採用
        if target_ts in overlap_factors:
            return overlap_factors[target_ts]
        if overlap_factors:
            before_keys = [k for k in overlap_factors if k < target_ts]
            after_keys = [k for k in overlap_factors if k > target_ts]
            if before_keys and after_keys:
                f_before = overlap_factors[max(before_keys)]
                f_after = overlap_factors[min(after_keys)]
                if abs(f_before - f_after) <= 1e-4:
                    return 0.5 * (f_before + f_after)
        return None

    for iso_date, rinfo in sorted(parsed_twse_rows.items()):
        ts = pd.Timestamp(iso_date)
        need_patch = (ts not in df.index and iso_date >= cutoff_date)
        need_today_update = (ts in df.index and iso_date == today_str)
        if not (need_patch or need_today_update):
            continue

        factor = _resolve_adj_factor(ts, rinfo)
        if factor is None or factor <= 0:
            df.attrs['unverified_twse_patch_skipped'] = True
            print(f"⚠️ [Layer 3] 略過 {symbol} {iso_date} 補值：無法驗證除權息調整因子")
            continue

        o = rinfo['o'] * factor
        h = rinfo['h'] * factor
        l = rinfo['l'] * factor
        c = rinfo['c'] * factor
        vol = rinfo['vol']

        # 1. 若 Yahoo 漏掉此歷史交易日（近 45 天內），自動以還原後之證交所數據逐日原位補齊（支援連續缺日遞推 R04）
        if need_patch:
            bar_data = {'Open': o, 'High': h, 'Low': l, 'Close': c, 'Volume': vol}
            if 'Adj Close' in df.columns:
                bar_data['Adj Close'] = c
            if 'Raw Close' in df.columns:
                bar_data['Raw Close'] = rinfo['c']
            for col in df.columns:
                if col not in bar_data:
                    bar_data[col] = c
            df.loc[ts] = bar_data
            df = df.sort_index()
            overlap_factors[ts] = factor
            patched_count += 1
        # 2. 若今日證交所官方收盤資料已出爐，且 df 已有盤中 bar，以官方 EOD 正式收盤價更新覆寫
        elif need_today_update:
            df.loc[ts, 'Open'] = o
            df.loc[ts, 'High'] = h
            df.loc[ts, 'Low'] = l
            df.loc[ts, 'Close'] = c
            df.loc[ts, 'Volume'] = vol
            if 'Adj Close' in df.columns:
                df.loc[ts, 'Adj Close'] = c
            if 'Raw Close' in df.columns:
                df.loc[ts, 'Raw Close'] = rinfo['c']
            updated_today = True
        
    # 3. F08: 僅在該月份 TWSE 官方查詢已成功驗證 (verified_months) 時，才剔除不在官方交易日列表內的 Volume=0 假 K 棒
    ghost_indices = [
        idx for idx in df.index 
        if idx.strftime('%Y-%m-%d') >= cutoff_date 
        and idx.strftime('%Y-%m-%d') < today_str 
        and idx.strftime('%Y-%m') in verified_months
        and idx.strftime('%Y-%m-%d') not in twse_valid_dates 
        and df.loc[idx, 'Volume'] == 0
    ]
    if ghost_indices:
        df = df.drop(index=ghost_indices)
        print(f"🧹 [Layer 3] 已自動剔除 {symbol} 無效休市假 K 棒: {[i.strftime('%Y-%m-%d') for i in ghost_indices]}")

    df = df.sort_index()
    if patched_count > 0:
        print(f"🏛️ [Layer 3] TWSE 官方 API 自動補齊 {symbol} 遺漏的 {patched_count} 天 K 棒！")
    if updated_today:
        print(f"🎯 [Layer 3] TWSE 官方 API 已同步更新 {symbol} 今日最新官方收盤數據！")
    return df

def fetch_robust_market_data(ticker):
    """整合多層防禦的資料抓取主函式 (含 F01 休市防偽造、F07 保留原始浮點精度、F09 暖機與新鮮度驗證、F16 美股半日市支援、R06-R08 假日/Schema/未來K防呆)"""
    def _is_tw_holiday(d):
        if d.weekday() >= 5:
            return True
        if (d.month, d.day) in {(1, 1), (2, 28), (4, 4), (4, 5), (5, 1), (10, 10), (12, 25)}:
            return True
        known_tw_closures = {
            '2025-01-23', '2025-01-24', '2025-01-27', '2025-01-28', '2025-01-29', '2025-01-30', '2025-01-31',
            '2025-04-03', '2025-05-30', '2025-09-29', '2025-10-06', '2025-10-24',
            '2026-02-12', '2026-02-13', '2026-02-16', '2026-02-17', '2026-02-18', '2026-02-19', '2026-02-20',
            '2026-02-27', '2026-04-03', '2026-04-06', '2026-06-19', '2026-09-25', '2026-09-28', '2026-10-09', '2026-10-26', '2026-12-25'
        }
        return d.strftime('%Y-%m-%d') in known_tw_closures

    def _us_thanksgiving(year):
        d1 = date(year, 11, 1)
        first_thu = d1 + timedelta(days=(3 - d1.weekday()) % 7)
        return first_thu + timedelta(weeks=3)

    def _is_us_holiday(d):
        if d.weekday() >= 5:
            return True
        if d == _us_thanksgiving(d.year):
            return True
        # MLK Day (Jan 3rd Mon), Presidents' Day (Feb 3rd Mon), Memorial Day (May last Mon), Labor Day (Sep 1st Mon)
        if d.month == 1 and d.weekday() == 0 and 15 <= d.day <= 21:
            return True
        if d.month == 2 and d.weekday() == 0 and 15 <= d.day <= 21:
            return True
        if d.month == 5 and d.weekday() == 0 and d.day >= 25:
            return True
        if d.month == 9 and d.weekday() == 0 and d.day <= 7:
            return True
        # Good Friday (2 days before Easter Sunday via Butcher/Meeus Gregorian algorithm)
        y = d.year
        a = y % 19
        b = y // 100
        c = y % 100
        dd = b // 4
        e = b % 4
        f = (b + 8) // 25
        g = (b - f + 1) // 3
        h = (19 * a + b - dd - g + 15) % 30
        i = c // 4
        k = c % 4
        l = (32 + 2 * e + 2 * i - h - k) % 7
        m_e = (a + 11 * h + 22 * l) // 451
        easter_month = (h + l - 7 * m_e + 114) // 31
        easter_day = ((h + l - 7 * m_e + 114) % 31) + 1
        if d == date(y, easter_month, easter_day) - timedelta(days=2):
            return True
        for m_fixed, day_fixed in [(1, 1), (6, 19), (7, 4), (12, 25)]:
            hol = date(d.year, m_fixed, day_fixed)
            obs = hol - timedelta(days=1) if hol.weekday() == 5 else (hol + timedelta(days=1) if hol.weekday() == 6 else hol)
            if d == obs:
                return True
        return False

    def _get_us_close_hm(d):
        # F16: 美股感恩節隔日 (Black Friday) 與平安夜 (12/24)、獨立日前夕 (7/3) 為 13:00 ET 提前收盤 (+5分鐘緩衝 = 13:05)
        tg = _us_thanksgiving(d.year)
        if d == tg + timedelta(days=1):
            return '13:05'
        if d.month == 12 and d.day == 24 and not _is_us_holiday(d):
            return '13:05'
        if d.month == 7 and d.day == 3 and not _is_us_holiday(d) and date(d.year, 7, 4).weekday() < 5:
            return '13:05'
        return '16:05'

    def _expected_latest_session(is_tw):
        if is_tw:
            now_loc = datetime.now(ZoneInfo('Asia/Taipei'))
            cur_d = now_loc.date()
            if not _is_tw_holiday(cur_d) and now_loc.strftime('%H:%M') >= '13:35':
                return cur_d
            cur_d -= timedelta(days=1)
            while _is_tw_holiday(cur_d):
                cur_d -= timedelta(days=1)
            return cur_d
        else:
            now_loc = datetime.now(ZoneInfo('America/New_York'))
            cur_d = now_loc.date()
            if not _is_us_holiday(cur_d) and now_loc.strftime('%H:%M') >= _get_us_close_hm(cur_d):
                return cur_d
            cur_d -= timedelta(days=1)
            while _is_us_holiday(cur_d):
                cur_d -= timedelta(days=1)
            return cur_d

    MIN_REQUIRED_ROWS = 225
    REQUIRED_COLS = {'Open', 'High', 'Low', 'Close'}
    is_tw_ticker = ticker.endswith('.TW')
    expected_session = _expected_latest_session(is_tw_ticker)
    df = None
    source_layer = 'yfinance'

    # 第 1 層：標準 yfinance
    for attempt in range(2):
        try:
            cand = yf.download(ticker, period='10y', progress=False, auto_adjust=True)
            if cand is not None and len(cand) >= MIN_REQUIRED_ROWS:
                if isinstance(cand.columns, pd.MultiIndex):
                    cand.columns = cand.columns.get_level_values(0)
                if not REQUIRED_COLS.issubset(set(cand.columns)):
                    print(f"⚠️ [Layer 1] yfinance 回傳缺少必要 OHLC 欄位 ({ticker}): {list(cand.columns)}")
                    cand = None
                else:
                    cand_idx = pd.to_datetime(cand.index).tz_localize(None).normalize()
                    last_d = cand_idx[-1].date()
                    df = cand
                    print(f"✅ [Layer 1] yfinance 抓取成功 ({ticker})，共 {len(df)} 筆 (最新: {last_d})")
                    break
        except Exception as e:
            print(f"⚠️ [Layer 1] yfinance 第 {attempt+1} 次抓取失敗 ({ticker}): {e}")
        time.sleep(1)

    # 第 2 層：若 yfinance 失敗、筆數不足 225、或末筆資料嚴重過期 (>4 天)，嘗試切換 Yahoo Direct JSON API
    need_layer2 = (df is None or len(df) < MIN_REQUIRED_ROWS)
    if not need_layer2 and len(df) > 0:
        tmp_idx = pd.to_datetime(df.index).tz_localize(None).normalize()
        if (expected_session - tmp_idx[-1].date()).days > 4:
            need_layer2 = True

    if need_layer2:
        print(f"🔄 正在啟用第二層防禦 (Yahoo Direct API) 抓取 {ticker}...")
        direct_df = fetch_yahoo_direct(ticker)
        if direct_df is not None and len(direct_df) >= MIN_REQUIRED_ROWS and REQUIRED_COLS.issubset(set(direct_df.columns)):
            if df is None or len(df) < MIN_REQUIRED_ROWS or direct_df.index[-1] >= df.index[-1]:
                df = direct_df
                source_layer = 'yahoo_direct'

    if df is None or len(df) < MIN_REQUIRED_ROWS:
        print(f"❌ {ticker} 資料不足或所有外部連線均告急 (rows={0 if df is None else len(df)} < {MIN_REQUIRED_ROWS})")
        return None

    # 清理欄位格式與時間戳標準化
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if not REQUIRED_COLS.issubset(set(df.columns)):
        print(f"❌ {ticker} 資料表缺少必要 OHLC 欄位: {list(df.columns)}")
        return None

    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    df = df[~df.index.duplicated(keep='last')].sort_index()

    # 第 3 層：台股向 TWSE 自動對帳補齊與去假 K
    df = auto_patch_twse(ticker, df)

    # 執行已知手動補齊庫（雙重保險）
    df = apply_data_patches(ticker, df)

    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    df = df[~df.index.duplicated(keep='last')].sort_index()

    # F01: 針對台股交易日盤後，僅在「非休市日」且「fast_info 具備當日明確有效報價日期戳記」時才允許補今日 bar
    now = datetime.now(TZ_TAIPEI)
    today_str = now.strftime('%Y-%m-%d')
    today_ts = pd.Timestamp(today_str)
    if (is_tw_ticker and today_ts not in df.index and not _is_tw_holiday(now.date())
            and (now.hour > 13 or (now.hour == 13 and now.minute >= 35))):
        try:
            fi = yf.Ticker(ticker).fast_info
            obs_date = getattr(fi, 'observation_date', None) or getattr(fi, 'quote_date', None)
            stale_obs = getattr(fi, 'stale_observation_date', None)
            has_verified_today_quote = (obs_date == today_str) and (stale_obs is None or stale_obs == today_str)
            if has_verified_today_quote and fi.last_price is not None and not pd.isna(fi.last_price):
                c = float(fi.last_price)
                o = float(fi.open) if fi.open is not None and not pd.isna(fi.open) else c
                h = float(fi.day_high) if fi.day_high is not None and not pd.isna(fi.day_high) else c
                l = float(fi.day_low) if fi.day_low is not None and not pd.isna(fi.day_low) else c
                vol = float(fi.last_volume) if fi.last_volume is not None and not pd.isna(fi.last_volume) else 0.0
                bar_data = {'Open': o, 'High': h, 'Low': l, 'Close': c, 'Volume': vol}
                if 'Adj Close' in df.columns:
                    bar_data['Adj Close'] = c
                df = pd.concat([df, pd.DataFrame([bar_data], index=[today_ts])]).sort_index()
                print(f"⚡ [Layer 1.5] 已由經驗證之 fast_info 補上 {ticker} 今日 ({today_str}) 盤後收盤 K 棒！")
        except Exception as e:
            print(f"⚠️ fast_info 今日 K 棒補齊失敗 ({ticker}): {e}")

    # 針對最後一天即時報價 NaN 的容錯：若無法驗證為當日有效值則剔除該異常尾列
    if len(df) > 0 and pd.isna(df['Close'].iloc[-1]):
        df = df.iloc[:-1]

    # 1. 根據各市場當地時區與半日市日曆 (F16/R08)，剔除「今日尚未收盤」、「休市日誤植」或「未來日期異常」的 K 棒
    if len(df) > 0:
        if is_tw_ticker:
            now_local = datetime.now(ZoneInfo('Asia/Taipei'))
            if df.index[-1].date() == now_local.date() and (_is_tw_holiday(now_local.date()) or now_local.strftime('%H:%M') < '13:35'):
                print(f"⏳ [EOD 防呆] 偵測到 {ticker} 尚未收盤或為休市日 ({now_local.strftime('%H:%M')})，剔除盤中/無效 K 棒: {df.index[-1].date()}")
                df = df.iloc[:-1]
        else:
            now_ny = datetime.now(ZoneInfo('America/New_York'))
            close_hm = _get_us_close_hm(now_ny.date())
            if df.index[-1].date() == now_ny.date() and (_is_us_holiday(now_ny.date()) or now_ny.strftime('%H:%M') < close_hm):
                print(f"⏳ [EOD 防呆] 偵測到 {ticker} 尚未收盤或為休市日 ({now_ny.strftime('%H:%M')} ET < {close_hm})，剔除盤中 K 棒: {df.index[-1].date()}")
                df = df.iloc[:-1]

        future_mask = np.array([d > expected_session for d in df.index.date], dtype=bool)
        if future_mask.any():
            print(f"🧹 [EOD 防呆] 剔除 {ticker} 超越最新有效交易日 ({expected_session}) 之未來/異常 K 棒共 {int(future_mask.sum())} 筆")
            df = df.loc[~future_mask]
            df.attrs['future_bars_dropped'] = True

    # F07: 不在指標與突破通道計算前將 Close 強制 round(2)，以保留真實還原價格突破並與 engine.py 100% 對齊；
    # 同時清理非有限值並附加上游新鮮度 metadata (F09/C8)
    df = df.replace([np.inf, -np.inf], np.nan).dropna(subset=['Open', 'High', 'Low', 'Close'])
    df = df[(df['Close'] > 0) & (df['Open'] > 0) & (df['High'] > 0) & (df['Low'] > 0)]
    if len(df) < MIN_REQUIRED_ROWS:
        return None

    last_bar_date = df.index[-1].date()
    stale_days = max(0, (expected_session - last_bar_date).days)
    is_stale = stale_days > 0
    df.attrs['expected_session'] = expected_session.strftime('%Y-%m-%d')
    df.attrs['last_bar_date'] = last_bar_date.strftime('%Y-%m-%d')
    df.attrs['stale_days'] = int(stale_days)
    df.attrs['is_stale'] = bool(is_stale)
    df.attrs['data_status'] = 'stale' if is_stale else 'ok'
    df.attrs['source_layer'] = source_layer
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

    df_attrs = dict(getattr(df, 'attrs', {}) or {})

    if isinstance(breakout_window, (tuple, list)):
        w_up, w_dn = breakout_window[0], breakout_window[1]
    else:
        w_up = breakout_up if breakout_up is not None else breakout_window
        w_dn = breakout_dn if breakout_dn is not None else breakout_window

    if 'Close' not in df.columns or len(df) < max(slow_ma, w_up + 1, w_dn + 1):
        return None

    max_idx = len(allocs) - 1
    in_trend = False
    step_idx = 0
    last_action_idx = -999
    target_history = []
    deep_bear_history = []
    pre_bull_history = []

    sim_L = base_leverage
    last_action_event = None  # 記錄最近一次任何部位調整 (含加碼/減碼/+0.15x修剪)
    last_trim_event = None    # 專門記錄最近一次向下減碼或 +0.15x 超標修剪
    # 舊欄位保留相容；新欄位分列策略目標與 T+0 模擬自然漂移事件，均非實帳成交。
    last_signal_change_event = None
    last_excess_trim_event = None
    rf_ann = 0.045 if ticker == 'QQQ' else 0.013
    sp_ann = 0.003 if ticker == 'QQQ' else 0.005

    df = df.copy()
    df['SMA_fast'] = df['Close'].rolling(window=fast_ma).mean()
    df['SMA_slow'] = df['Close'].rolling(window=slow_ma).mean()
    # 與 engine.py 回測完全一致，以收盤價 Close 計算 N 日突破與跌破通道（支援不對稱通道 w_up / w_dn）
    df['High_bw'] = df['Close'].shift(1).rolling(window=w_up).max()
    df['Low_bw']  = df['Close'].shift(1).rolling(window=w_dn).min()
    
    subset_cols = ['Close', 'SMA_fast', 'SMA_slow', 'High_bw', 'Low_bw']
    if deep_bear_guard or add_above_bear or pre_bull_boost is not None:
        bear_w = deep_bear_guard.get('bear_ma', 60) if deep_bear_guard else 60
        df['SMA_bear'] = df['Close'].rolling(window=bear_w).mean()
        subset_cols.append('SMA_bear')

    df = df.replace([np.inf, -np.inf], np.nan).dropna(subset=subset_cols)
    if df.empty:
        return None

    ext_guard_active = False  # 追蹤最新一天是否觸發過熱冷卻
    bear_counter = 0
    accel_left = 0
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
            accel_left = 0

        # 2. 檢查是否滿足多頭重啟條件 (雙條件同時滿足)
        elif sma_fast >= sma_slow and current_close >= sma_slow and not in_trend:
            in_trend = True
            pre_bull_on = False
            if bull_acceleration and bull_acceleration.get('enable', True) and bear_counter >= bull_acceleration.get('min_bear_days', 40):
                accel_left = bull_acceleration.get('accel_days', 252)
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
            target = cur_b
        else:
            # 4. 以下為 in_trend == True (多頭進攻狀態)，deep_bear 與 pre_bull 強制為 False
            is_deep = False
            pre_bull_on = False
            cur_base = base_leverage
            if bull_acceleration and in_trend and accel_left > 0:
                cur_base = bull_acceleration.get('accel_base', 1.2)
                accel_left -= 1

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

        # F06: 以真實日曆天數差 (gap_days / 365.0) 計算單市場模擬自然漂移，並遵守 no_buy_down 降檔不補買鐵律
        if i > 0:
            prev_close = df['Close'].iloc[i - 1]
            r_day = (current_close / prev_close) - 1.0 if prev_close > 0 else 0.0
            gap_days = max(int((df.index[i] - df.index[i - 1]).days), 1)
            net_day = (rf_ann * gap_days / 365.0) + sim_L * (r_day - (rf_ann + sp_ann) * gap_days / 365.0)
            sim_L = sim_L * (1.0 + r_day) / (1.0 + net_day) if (1.0 + net_day) > 0 else 0.0

        prev_t = target_history[-1] if len(target_history) > 0 else base_leverage
        bar_date_str = df.index[i].strftime('%Y-%m-%d')

        if i > 0 and round(target, 2) != round(prev_t, 2):
            # 發生系統目標槓桿改變 (換檔)
            if not in_trend:
                if is_deep and target < prev_t:
                    act_label = "C1深熊減半"
                elif pre_bull_on and target > prev_t:
                    act_label = "E3b季線總攻"
                elif target > prev_t:
                    act_label = "C1季線復原"
                else:
                    act_label = "破年線退底倉"
            else:
                if target > prev_t:
                    act_label = "突破加碼" if not (prev_t <= 1.2 and step_idx == 0) else "站上年線重啟"
                elif ext_guard_active:
                    act_label = "過熱鎖利壓回"
                else:
                    act_label = "跌破通道減碼"

            is_step_down = round(target, 2) < round(prev_t, 2)
            no_buy_down_hold = bool(is_step_down and sim_L <= target)
            if no_buy_down_hold:
                act_label = f"{act_label}(實質已低免砍)"

            evt = {
                'date': bar_date_str,
                'idx': i,
                'from_lev': round(float(prev_t), 2),
                'to_lev': round(float(target), 2),
                'sim_pre_lev': round(float(sim_L), 2),
                'no_buy_down_hold': no_buy_down_hold,
                'type': act_label
            }
            last_action_event = evt
            last_signal_change_event = dict(
                evt,
                kind='signal_change',
                event_id=f"{ticker}|signal_change|{bar_date_str}|{evt['from_lev']:.2f}|{evt['to_lev']:.2f}"
            )
            if is_step_down:
                last_trim_event = evt
                if sim_L > target:
                    sim_L = target
            else:
                sim_L = target

        elif i > 0 and sim_L > target + 0.15:
            # 目標槓桿未變，但大跌導致自然槓桿突破 +0.15x 修剪線
            evt = {
                'date': bar_date_str,
                'idx': i,
                'from_lev': round(float(sim_L), 2),
                'to_lev': round(float(target), 2),
                'type': "+0.15x超標修剪"
            }
            last_action_event = evt
            last_trim_event = evt
            last_excess_trim_event = dict(
                evt,
                kind='excess_trim',
                event_id=f"{ticker}|excess_trim|{bar_date_str}|{evt['from_lev']:.2f}|{evt['to_lev']:.2f}"
            )
            sim_L = target
        elif i == 0:
            sim_L = target

        target_history.append(target)
        deep_bear_history.append(is_deep)
        pre_bull_history.append(pre_bull_on)

    total_bars = len(df)
    if last_action_event:
        last_action_event['days_ago'] = (total_bars - 1) - last_action_event['idx']
    if last_trim_event:
        last_trim_event['days_ago'] = (total_bars - 1) - last_trim_event['idx']
    for event in (last_signal_change_event, last_excess_trim_event):
        if event is not None:
            event['days_ago'] = (total_bars - 1) - event['idx']

    # === 計算前一天的目標槓桿與顯示欄位 ===
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
    ext_ratio_now = round(float(latest['Close']) / float(latest['SMA_slow']), 4) if float(latest['SMA_slow']) > 0 else 0.0
    ext_pct_now = round((float(latest['Close']) / float(latest['SMA_slow']) - 1) * 100, 1) if float(latest['SMA_slow']) > 0 else 0.0
    ext_cap_val = extension_guard['cap'] if extension_guard else 1.8

    if not in_trend:
        # 【空頭防禦與季線總攻大類】
        if deep_bear_active and deep_bear_guard:
            if lev_changed and curr_target < prev_target:
                explanation = (
                    f"🚨【深熊二階段減碼 · {prev_target:.2f}x ➔ {curr_target:.2f}x】"
                    f"季線 {bear_w}MA ({sma_bear_val}) 已跌破年線 97% 深熊門檻 ({bear_thresh}) 且快線位於季線之下，"
                    f"確認進入系統性空頭深水區！今日請掛限價單將底倉減半至 {curr_target:.2f}x 保命。"
                )
            else:
                explanation = (
                    f"🛡️【深熊減半防禦中 · 維持 {curr_target:.2f}x】"
                    f"中期與長期均線持續空頭排列，維持二階段減半底倉 {curr_target:.2f}x 避險。"
                    f"無須動作；待快線 ({f_ma}) 重新站上季線 ({sma_bear_val}) 即刻復原常態底倉 {base_leverage:.1f}x。"
                )
        elif pre_bull_active and pre_bull_boost is not None:
            if lev_changed and curr_target > prev_target:
                explanation = (
                    f"⚡【季線右側總攻啟動 (Stage 0) · {prev_target:.2f}x ➔ {curr_target:.2f}x】"
                    f"收盤價 ({cur_p}) 與 {fast_ma}MA 快線 ({f_ma}) 已雙雙站穩季線 {bear_w}MA ({sma_bear_val}) 並突破近 {w_up} 日高點 ({h_bw})！"
                    f"今日請掛限價單將底倉推升至準牛市總攻檔位 {curr_target:.2f}x，迎戰 {slow_ma}MA 年線 ({s_ma})。"
                )
            else:
                diff_s = round(max(s_ma - cur_p, 0.0), 2)
                explanation = (
                    f"⚡【季線右側總攻巡航 (Stage 0) · 維持 {curr_target:.2f}x】"
                    f"目前已站穩季線 ({sma_bear_val}) 並完成右側突破，維持準牛市總攻底倉 {curr_target:.2f}x。"
                    f"距離正式站上 {slow_ma}MA 年線 ({s_ma}) 尚差 {diff_s} 點，今日無須動作。"
                )
        elif lev_changed and curr_target > prev_target:
            explanation = (
                f"🌤️【深熊底部復原 (Stage -1) · {prev_target:.2f}x ➔ {curr_target:.2f}x】"
                f"短線 {fast_ma}MA ({f_ma}) 已成功站上季線 {bear_w}MA ({sma_bear_val})，空頭最劇烈尋底期結束！"
                f"今日請掛限價單將底倉由深熊半倉恢復至常態 {curr_target:.2f}x；後續若突破 {w_up} 日高點 ({h_bw}) 將再升至 {pre_bull_boost:.1f}x 總攻。"
            )
        elif cur_p < s_ma:
            if lev_changed and curr_target < prev_target:
                explanation = (
                    f"🚨【防禦降槓退回底倉 · {prev_target:.2f}x ➔ {curr_target:.2f}x】"
                    f"收盤價 ({cur_p}) 位於 {slow_ma}MA 年線護城河 ({s_ma}) 之下，動能退回常態防守底倉！"
                    f"今日請於最佳時段掛限價單退回常態底倉 {curr_target:.2f}x，嚴禁盤中手動抄底。"
                )
            else:
                explanation = (
                    f"🛡️【年線下防禦待命 · 維持 {curr_target:.2f}x】"
                    f"收盤價距離 {slow_ma}MA 年線尚差 {round(s_ma - cur_p, 2)} 點（目前季線 {sma_bear_val}，深熊線 {bear_thresh}）。"
                    f"今日無須動作，維持常態底倉 {curr_target:.2f}x 靜待均線修復。"
                )
        else:
            explanation = (
                f"👀【右側重啟觀察期 · 維持 {curr_target:.2f}x】"
                f"收盤價 ({cur_p}) 已率先站回年線 ({s_ma}) 之上，惟 {fast_ma}MA 快線 ({f_ma}) 尚差 {round(s_ma - f_ma, 2)} 點完成黃金交叉。"
                f"今日維持底倉 {curr_target:.2f}x 按兵不動，待雙條件同時滿足即刻重啟多頭。"
            )
    else:
        # 【多頭進攻大類：共 5 個 Cases】
        if cur_p < s_ma and below_ma_counter <= grace_days:
            explanation = (
                f"⚠️【破年線容忍觀察 (第 1/{grace_days} 日) · 暫維 {curr_target:.2f}x】"
                f"收盤價 ({cur_p}) 首日微幅跌破年線 ({s_ma})，啟動台股防洗盤容忍保護，今日暫不砍倉！"
                f"明日收盤若未能站回 {s_ma} 之上，將正式確認破線並退回 {base_leverage:.1f}x 底倉。"
            )
        elif step_idx == 0:
            diff_h = round(max(h_bw - cur_p, 0.01), 2)
            explanation = (
                f"🚀【多頭起跑待命 (Step 0/{max_idx}) · 目標 {curr_target:.2f}x】"
                f"年線護城河與快線多頭排列已確立！目前持守底倉 {curr_target:.2f}x，"
                f"距離第一階 {w_up} 日突破加碼門檻 ({h_bw}) 僅差 {diff_h} 點，突破前不提前追高。"
            )
        elif lev_changed and curr_target > prev_target:
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
    bull_accel_active = (accel_left > 0) if bull_acceleration else False
    bull_accel_days_left = accel_left if bull_acceleration else 0
    if bull_accel_active:
        explanation += f" 🚀【台股牛初加速生效中：底倉基準升為 {cur_base:.1f}x，剩餘 {bull_accel_days_left} 交易日】"

    latest_date_str = latest.name.strftime('%Y-%m-%d')
    expected_sess = df_attrs.get('expected_session', latest_date_str)
    is_stale = bool(df_attrs.get('is_stale', False))
    stale_days = int(df_attrs.get('stale_days', 0))

    # === 組合回傳資料 ===
    return {
        'id': ticker.replace('.', '_').lower(),
        'name': name,
        'ticker': ticker,
        'date': latest_date_str,
        'signal_asof': latest_date_str,
        'expected_session': expected_sess,
        'is_stale': is_stale,
        'stale_days': stale_days,
        'data_status': 'stale' if is_stale else 'ok',
        'source_layer': df_attrs.get('source_layer', 'yfinance'),
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
        'deep_bear_ratio': deep_bear_guard.get('ratio', 0.97) if deep_bear_guard else None,
        'last_action': last_action_event,
        'last_trim': last_trim_event,
        'last_signal_change': last_signal_change_event,
        'last_excess_trim': last_excess_trim_event,
        'signal_changed_today': bool(lev_changed),
        'excess_trim_today': bool(
            not lev_changed and last_excess_trim_event is not None
            and last_excess_trim_event['date'] == latest_date_str
            and last_excess_trim_event['days_ago'] == 0
        ),
        'sim_natural_lev': round(float(sim_L), 2),
        'sim_note': '模擬參考路徑（T+0簡化估算，非實帳）'
    }

def refresh_cache():
    """在背景 Thread 更新快取，不阻塞 web 請求 (F03: 嚴格區分新抓取成功與離線保底，且警告前綴冪等)"""
    lock = globals().get('_refresh_lock')
    if lock is not None:
        if not lock.acquire(blocking=False):
            return
    else:
        if _cache.get('is_updating'):
            return
    _cache['is_updating'] = True
    _cache['last_attempt_at'] = datetime.now()
    print("🔄 正在更新市場資料快取...")

    FALLBACK_PREFIX = "⚠️【連線維護中 · 沿用前次訊號】"

    try:
        old_data_map = {}
        if _cache.get('data'):
            for item in _cache['data']:
                old_data_map[item['ticker']] = item

        fresh_count = 0

        try:
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
        except Exception as e:
            print(f"⚠️ QQQ 訊號計算異常: {e}")
            qqq = None

        if qqq is not None:
            qqq['fallback_used'] = False
            if not qqq.get('is_stale', False) and qqq.get('data_status', 'ok') == 'ok':
                fresh_count += 1
        elif 'QQQ' in old_data_map:
            print("🛡️ [Layer 4] QQQ 啟動離線快取保底，沿用前次有效訊號")
            qqq = old_data_map['QQQ'].copy()
            qqq['fallback_used'] = True
            qqq['data_status'] = 'fallback_stale'
            if not qqq['explanation'].startswith(FALLBACK_PREFIX):
                qqq['explanation'] = FALLBACK_PREFIX + qqq['explanation']

        try:
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
        except Exception as e:
            print(f"⚠️ 006208.TW 訊號計算異常: {e}")
            tw = None

        if tw is not None:
            tw['fallback_used'] = False
            if not tw.get('is_stale', False) and tw.get('data_status', 'ok') == 'ok':
                fresh_count += 1
        elif '006208.TW' in old_data_map:
            print("🛡️ [Layer 4] 006208 啟動離線快取保底，沿用前次有效訊號")
            tw = old_data_map['006208.TW'].copy()
            tw['fallback_used'] = True
            tw['data_status'] = 'fallback_stale'
            if not tw['explanation'].startswith(FALLBACK_PREFIX):
                tw['explanation'] = FALLBACK_PREFIX + tw['explanation']

        results = [x for x in [qqq, tw] if x is not None]
        if results:
            _cache['data'] = results
            if fresh_count == 2:
                _cache['last_updated'] = datetime.now()
                _cache['last_success_at'] = _cache['last_updated']
                _cache['degraded'] = False
                print(f"✅ 快取完整更新完成：{_cache['last_updated'].strftime('%Y-%m-%d %H:%M:%S')}")
            elif fresh_count == 1:
                _cache['degraded'] = True
                print("⚠️ 快取部分更新完成（單一市場降級或過期，標記 degraded）")
            else:
                _cache['degraded'] = True
                print("⚠️ 快取更新失敗或雙市場皆過期：不重設 last_updated 時間戳，標記 degraded")
        else:
            _cache['degraded'] = True
            print("❌ 快取更新失敗：所有資料均無法取得")
    except Exception as e:
        _cache['degraded'] = True
        print(f"❌ 快取更新例外：{e}")
    finally:
        _cache['is_updating'] = False
        if lock is not None:
            try:
                lock.release()
            except Exception:
                pass

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
    
    if _cache['data'] is None:
        if not _cache['is_updating']:
            thread = threading.Thread(target=refresh_cache)
            thread.daemon = True
            thread.start()
            
            if not _bg_thread_started:
                bg_thread = threading.Thread(target=background_refresh)
                bg_thread.daemon = True
                bg_thread.start()
                _bg_thread_started = True
                
        return jsonify({'status': 'loading', 'message': '資料正在載入中，請稍候 30 秒後重新整理...'}), 202

    return jsonify(_cache['data'])

@app.route('/api/refresh', methods=['GET', 'POST'])
def force_refresh():
    """手動強制更新按鈕用 (F19: 加入權杖檢查與頻率節流保護)"""
    admin_token = os.environ.get('DASHBOARD_ADMIN_TOKEN', '').strip()
    if admin_token:
        req_token = request.headers.get('X-Admin-Token', '') or request.args.get('token', '')
        if req_token != admin_token:
            return jsonify({'status': 'forbidden', 'message': '未授權的強制更新請求'}), 403

    now_ts = time.time()
    last_trig = float(_cache.get('last_refresh_trigger', 0.0) or 0.0)
    if _cache.get('is_updating') or (now_ts - last_trig < MIN_REFRESH_COOLDOWN_SECONDS):
        return jsonify({'status': 'throttled', 'message': '更新已在進行中或冷卻時間內，請稍候'}), 429

    _cache['last_refresh_trigger'] = now_ts
    thread = threading.Thread(target=refresh_cache)
    thread.daemon = True
    thread.start()
    return jsonify({'status': 'ok', 'message': '正在背景更新，約 30 秒後重新整理頁面即可'})

@app.route('/health')
def health():
    """F03/R09/R10: 誠實反映雙市場完整性、快取真實年齡與呼叫當下是否已過期/降級"""
    def _is_tw_hol_live(d):
        if d.weekday() >= 5 or (d.month, d.day) in {(1, 1), (2, 28), (4, 4), (4, 5), (5, 1), (10, 10), (12, 25)}:
            return True
        return d.strftime('%Y-%m-%d') in {
            '2025-01-23', '2025-01-24', '2025-01-27', '2025-01-28', '2025-01-29', '2025-01-30', '2025-01-31',
            '2025-04-03', '2025-05-30', '2025-09-29', '2025-10-06', '2025-10-24',
            '2026-02-12', '2026-02-13', '2026-02-16', '2026-02-17', '2026-02-18', '2026-02-19', '2026-02-20',
            '2026-02-27', '2026-04-03', '2026-04-06', '2026-06-19', '2026-09-25', '2026-09-28', '2026-10-09', '2026-10-26', '2026-12-25'
        }

    def _is_us_hol_live(d):
        if d.weekday() >= 5:
            return True
        d1 = date(d.year, 11, 1)
        tg = d1 + timedelta(days=(3 - d1.weekday()) % 7) + timedelta(weeks=3)
        if d == tg:
            return True
        if (d.month == 1 and d.weekday() == 0 and 15 <= d.day <= 21) or (d.month == 2 and d.weekday() == 0 and 15 <= d.day <= 21):
            return True
        if (d.month == 5 and d.weekday() == 0 and d.day >= 25) or (d.month == 9 and d.weekday() == 0 and d.day <= 7):
            return True
        y = d.year
        a, b, c = y % 19, y // 100, y % 100
        dd, e = b // 4, b % 4
        f = (b + 8) // 25
        g = (b - f + 1) // 3
        h = (19 * a + b - dd - g + 15) % 30
        i, k = c // 4, c % 4
        l = (32 + 2 * e + 2 * i - h - k) % 7
        m_e = (a + 11 * h + 22 * l) // 451
        em, ed = (h + l - 7 * m_e + 114) // 31, ((h + l - 7 * m_e + 114) % 31) + 1
        if d == date(y, em, ed) - timedelta(days=2):
            return True
        for mf, dfix in [(1, 1), (6, 19), (7, 4), (12, 25)]:
            hol = date(d.year, mf, dfix)
            obs = hol - timedelta(days=1) if hol.weekday() == 5 else (hol + timedelta(days=1) if hol.weekday() == 6 else hol)
            if d == obs:
                return True
        return False

    def _live_expected_session(is_tw):
        if is_tw:
            now_loc = datetime.now(ZoneInfo('Asia/Taipei'))
            cur_d = now_loc.date()
            if not _is_tw_hol_live(cur_d) and now_loc.strftime('%H:%M') >= '13:35':
                return cur_d
            cur_d -= timedelta(days=1)
            while _is_tw_hol_live(cur_d):
                cur_d -= timedelta(days=1)
            return cur_d
        else:
            now_loc = datetime.now(ZoneInfo('America/New_York'))
            cur_d = now_loc.date()
            d1 = date(cur_d.year, 11, 1)
            tg = d1 + timedelta(days=(3 - d1.weekday()) % 7) + timedelta(weeks=3)
            close_hm = '13:05' if (cur_d == tg + timedelta(days=1) or (cur_d.month == 12 and cur_d.day == 24 and not _is_us_hol_live(cur_d)) or (cur_d.month == 7 and cur_d.day == 3 and not _is_us_hol_live(cur_d) and date(cur_d.year, 7, 4).weekday() < 5)) else '16:05'
            if not _is_us_hol_live(cur_d) and now_loc.strftime('%H:%M') >= close_hm:
                return cur_d
            cur_d -= timedelta(days=1)
            while _is_us_hol_live(cur_d):
                cur_d -= timedelta(days=1)
            return cur_d

    data = _cache.get('data') or []
    tickers = {item.get('ticker') for item in data if isinstance(item, dict)}
    has_both = ('QQQ' in tickers) and ('006208.TW' in tickers)
    any_fallback = any(item.get('fallback_used', False) for item in data if isinstance(item, dict))
    any_stale = any(item.get('is_stale', False) or item.get('data_status') in ('stale', 'fallback_stale') for item in data if isinstance(item, dict))

    for item in data:
        if not isinstance(item, dict):
            continue
        tkr = str(item.get('ticker', ''))
        d_str = item.get('date')
        try:
            bar_d = datetime.strptime(str(d_str), '%Y-%m-%d').date()
            exp_d = _live_expected_session(tkr.endswith('.TW'))
            if bar_d < exp_d:
                any_stale = True
        except Exception:
            any_stale = True

    is_degraded = bool(_cache.get('degraded', False)) or (not has_both) or any_fallback or any_stale

    if not data:
        status = 'error'
    elif is_degraded:
        status = 'degraded'
    else:
        status = 'ok'

    response = jsonify({
        'status': status,
        'market_count': len(data),
        'markets_complete': has_both,
        'degraded': is_degraded,
        'is_stale': any_stale,
        'cache_age': str(datetime.now() - _cache['last_updated']) if _cache.get('last_updated') else 'no cache yet',
        'last_updated': _cache['last_updated'].strftime('%Y-%m-%d %H:%M:%S') if _cache.get('last_updated') else None,
        'last_attempt_at': _cache['last_attempt_at'].strftime('%Y-%m-%d %H:%M:%S') if _cache.get('last_attempt_at') else None,
        'market_dates': {item.get('ticker'): item.get('date') for item in data if isinstance(item, dict)},
        'is_updating': _cache['is_updating']
    })
    response.status_code = 200 if status == 'ok' else 503
    return response

# ========================================================
# 🎯 期貨轉倉智能雷達 (Rollover Radar - F13/R11 時區/完整休市表/結算時刻修正)
# ========================================================
def compute_rollover_radar():
    now_tw = datetime.now(ZoneInfo('Asia/Taipei'))
    now_us = datetime.now(ZoneInfo('America/New_York'))
    today_tw = now_tw.date()
    today_us = now_us.date()
    
    known_tw_closures = {
        '2025-01-23', '2025-01-24', '2025-01-27', '2025-01-28', '2025-01-29', '2025-01-30', '2025-01-31',
        '2025-04-03', '2025-05-30', '2025-09-29', '2025-10-06', '2025-10-24',
        '2026-02-12', '2026-02-13', '2026-02-16', '2026-02-17', '2026-02-18', '2026-02-19', '2026-02-20',
        '2026-02-27', '2026-04-03', '2026-04-06', '2026-06-19', '2026-09-25', '2026-09-28', '2026-10-09', '2026-10-26', '2026-12-25'
    }

    # 1. 台指期結算計算 (每月第 3 個週三；遇休市順延至下一交易日；結算日 13:30 後切換下月；含農曆春節等完整期交所休市表 R11)
    def third_wed(year, month):
        d1 = date(year, month, 1)
        w1 = d1 + timedelta(days=(2 - d1.weekday()) % 7)
        d_settle = w1 + timedelta(weeks=2)
        while (d_settle.weekday() >= 5
               or (d_settle.month, d_settle.day) in {(1, 1), (2, 28), (4, 4), (4, 5), (5, 1), (10, 10), (12, 25)}
               or d_settle.strftime('%Y-%m-%d') in known_tw_closures):
            d_settle += timedelta(days=1)
        return d_settle
        
    y, m = today_tw.year, today_tw.month
    tw_settlement = third_wed(y, m)
    current_m = m
    next_m = m + 1 if m < 12 else 1
    if today_tw > tw_settlement or (today_tw == tw_settlement and now_tw.strftime('%H:%M') >= '13:30'):
        y, m = (y, m + 1) if m < 12 else (y + 1, 1)
        tw_settlement = third_wed(y, m)
        current_m = m
        next_m = m + 1 if m < 12 else 1
        
    tw_days_left = (tw_settlement - today_tw).days
    tw_cur_code = f"{current_m:02d}月"
    tw_next_code = f"{next_m:02d}月"
    
    if tw_days_left > 5:
        tw_badge = "⚪ 常規持有"
        tw_class = "neutral"
        tw_phase = "hold"
        tw_action = f"距結算還有 {tw_days_left} 個日曆天，安心持倉無需理會價差。"
    elif tw_days_left >= 3:
        tw_badge = "📋 開始觀察"
        tw_class = "warning"
        tw_phase = "watch"
        tw_action = f"結算倒數 {tw_days_left} 天，次月合約流動性逐步升溫，可每日觀察跨月價差。"
    elif tw_days_left in [1, 2]:
        tw_badge = "🎯 黃金轉倉窗"
        tw_class = "bull"
        tw_phase = "go"
        tw_action = "進入結算前 1~2 日慣例轉倉窗，請確認期貨商次月成交量與跨月價差後掛限價單換約！"
    else:
        tw_badge = "🚨 今日結算"
        tw_class = "bear"
        tw_phase = "deadline"
        tw_action = "今日 13:30 台北時間結算！請務必於結算前以跨月價差單完成換約避免部位中斷！"

    # 2. 美股那指期 (NQ/MNQ) 結算計算 (3/6/9/12月 第 3 個週五 09:30 ET；遇休市提前至前一營業日)
    def third_fri(year, month):
        d1 = date(year, month, 1)
        f1 = d1 + timedelta(days=(4 - d1.weekday()) % 7)
        d_settle = f1 + timedelta(weeks=2)
        y_e = d_settle.year
        a, b, c = y_e % 19, y_e // 100, y_e % 100
        dd, e = b // 4, b % 4
        f = (b + 8) // 25
        g = (b - f + 1) // 3
        h = (19 * a + b - dd - g + 15) % 30
        i_e, k = c // 4, c % 4
        l = (32 + 2 * e + 2 * i_e - h - k) % 7
        m_e = (a + 11 * h + 22 * l) // 451
        em, ed = (h + l - 7 * m_e + 114) // 31, ((h + l - 7 * m_e + 114) % 31) + 1
        good_friday = date(y_e, em, ed) - timedelta(days=2)
        while d_settle.weekday() >= 5 or (d_settle.month == 6 and d_settle.day == 19) or d_settle == good_friday:
            d_settle -= timedelta(days=1)
        return d_settle
        
    q_months = [3, 6, 9, 12]
    q_codes = {3: 'H', 6: 'M', 9: 'U', 12: 'Z'}
    us_y, us_m = today_us.year, today_us.month
    us_settlement = None
    us_cur_q = None
    us_next_q = None
    
    for i, qm in enumerate(q_months):
        if qm >= us_m:
            tf = third_fri(us_y, qm)
            if today_us < tf or (today_us == tf and now_us.strftime('%H:%M') < '09:30'):
                us_settlement = tf
                us_cur_q = qm
                us_next_q = q_months[(i + 1) % 4]
                break
    if us_settlement is None:
        us_settlement = third_fri(us_y + 1, 3)
        us_cur_q = 3
        us_next_q = 6
        
    us_days_left = (us_settlement - today_us).days
    roll_week_start = us_settlement - timedelta(days=8)
    watch_start = us_settlement - timedelta(days=14)
    cur_code_str = f"NQ{q_codes[us_cur_q]}{str(us_settlement.year)[-1]}"
    next_code_str = f"NQ{q_codes[us_next_q]}{str(us_settlement.year + (1 if us_next_q==3 else 0))[-1]}"
    
    weekday_names = ['週一', '週二', '週三', '週四', '週五', '週六', '週日']
    settle_wd_str = weekday_names[us_settlement.weekday()]
    prev_trade_day = us_settlement - timedelta(days=1)
    while prev_trade_day.weekday() >= 5:
        prev_trade_day -= timedelta(days=1)
    prev_wd_str = weekday_names[prev_trade_day.weekday()]

    if today_us < watch_start:
        us_badge = "⚪ 常規持有"
        us_class = "neutral"
        us_phase = "hold"
        days_to_watch = (watch_start - today_us).days
        us_action = f"距觀察期還有 {days_to_watch} 天，近月合約深度最佳，安心持有。"
    elif today_us < roll_week_start:
        us_badge = "📋 開始觀察"
        us_class = "warning"
        us_phase = "watch"
        days_to_roll = (roll_week_start - today_us).days
        us_action = f"距自訂轉倉觀察窗 (結算前 8 日曆天起) 還有 {days_to_roll} 天，可開始留意跨季價差。"
    elif us_days_left <= 1:
        us_badge = f"🚨 {settle_wd_str}開盤結算 · {prev_wd_str}必轉"
        us_class = "bear"
        us_phase = "deadline"
        us_action = f"⚠️ 警告：NQ/MNQ 於結算日 ({us_settlement.strftime('%Y-%m-%d')} {settle_wd_str}) 09:30 ET 美股開盤即停止交易結算！請務必於前一交易日 ({prev_wd_str}) 收盤前完成價差單轉倉！"
    else:
        us_badge = "🎯 轉倉觀察窗 (結算前8日曆天)"
        us_class = "bull"
        us_phase = "go"
        us_action = "已進入自訂轉倉觀察窗 (結算前 8 日曆天起，非 CME 官方固定日)，請核對近遠月實際成交量並以【Calendar Spread】掛 Mid-Price 換約！"

    return {
        'tw': {
            'market': '台指期 (TX / MTX / TMF)',
            'timezone': 'Asia/Taipei',
            'asof_date': today_tw.strftime('%Y-%m-%d'),
            'contract_info': f'當月 {tw_cur_code} ➔ 次月 {tw_next_code}',
            'settlement_date': tw_settlement.strftime('%Y-%m-%d'),
            'days_left': tw_days_left,
            'badge': tw_badge,
            'status_class': tw_class,
            'phase': tw_phase,
            'action': tw_action,
            'rule_tip': '💡 判定準則（日曆提醒，非即時期貨報價）：若遠月價格 < 近月/現貨（逆價差），對多頭轉倉有利。'
        },
        'us': {
            'market': '那指期 (NQ / MNQ)',
            'timezone': 'America/New_York',
            'asof_date': today_us.strftime('%Y-%m-%d'),
            'contract_info': f'當季 {cur_code_str} ➔ 次季 {next_code_str}',
            'settlement_date': us_settlement.strftime('%Y-%m-%d'),
            'roll_start': roll_week_start.strftime('%Y-%m-%d'),
            'watch_start': watch_start.strftime('%Y-%m-%d'),
            'days_left': us_days_left,
            'badge': us_badge,
            'status_class': us_class,
            'phase': us_phase,
            'action': us_action,
            'rule_tip': '💡 判定準則（日曆提醒，非即時 CME 量能）：以 Spread 限價單掛在買賣中間價（Mid-Price），核對年化隱含融資成本。'
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
    
    print("🚀 伺服器啟動，開始背景預熱資料...")
    warmup_thread = threading.Thread(target=refresh_cache)
    warmup_thread.daemon = True
    warmup_thread.start()
    
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
