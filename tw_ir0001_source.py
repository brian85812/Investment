"""TWSE 官方發行量加權股價報酬指數 (IR0001) 四層強健資料抓取模組。

層級：
1. FinMind `TaiwanStockTotalReturnIndex` (`data_id=TAIEX`) 取得長期官方日線歷史。
2. TWSE 官方 `MFI94U` 月報端點逐日權威覆寫近期官方收盤，並在層級 1 失效時自動退回多月回補。
3. TWSE 官方 `MIS` 即時端點 (`tse_IR0001.tw|tse_t00.tw`) 補齊 13:30 當日正式收盤並附帶加權指數現貨參考 (`^TWII`)；
   含 F01 嚴格收盤時刻檢查與昨收連續性核對，絕不收盤前誤採盤中價或盲目跳接。
4. TWSE 官方盤後 `MI_INDEX` 端點於前三層仍缺預期交易日時執行最終官方補值。
"""
from datetime import date, datetime, timedelta, timezone
import json
import math
import ssl
import time
import urllib.request

import pandas as pd

TZ_TAIPEI = timezone(timedelta(hours=8))
FINMIND_URL = 'https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockTotalReturnIndex&data_id=TAIEX&start_date={start_date}'
TWSE_MFI94U_URL = 'https://www.twse.com.tw/rwd/zh/TAIEX/MFI94U?response=json&date={ym01}'
TWSE_MIS_URL = 'https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_IR0001.tw|tse_t00.tw&json=1&delay=0'
TWSE_MI_INDEX_URL = 'https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={ymd}&type=IND&response=json'
_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
}


def _default_ssl_context():
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def _fetch_json(url, ssl_context, timeout=15):
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, context=ssl_context, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _parse_roc_date(raw):
    """將民國日期 '115/11/24' 轉為 ISO '2026-11-24'。"""
    parts = str(raw or '').strip().split('/')
    if len(parts) != 3:
        return None
    try:
        ad_year = int(parts[0]) + 1911
        month = int(parts[1])
        day = int(parts[2])
        return date(ad_year, month, day).isoformat()
    except (TypeError, ValueError):
        return None


def _parse_yyyymmdd_date(raw):
    text = str(raw or '').strip()
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except (TypeError, ValueError):
        return None


def _parse_pos_float(raw):
    if raw is None:
        return None
    text = str(raw).replace(',', '').strip()
    if text in ('', '-', '--', 'N/A', 'nan', 'NaN', 'null', 'None'):
        return None
    try:
        val = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val) or val <= 0.0:
        return None
    return val


def _make_bar(close_val, open_val=None, high_val=None, low_val=None):
    return {
        'Open': open_val if open_val is not None else close_val,
        'High': high_val if high_val is not None else close_val,
        'Low': low_val if low_val is not None else close_val,
        'Close': close_val,
        'Volume': 1.0,
    }


def _month_starts_backwards(anchor_date, count):
    months = []
    cur = anchor_date.replace(day=1)
    for _ in range(count):
        months.append(cur.strftime('%Y%m01'))
        cur = (cur - timedelta(days=1)).replace(day=1)
    return months


def _is_eod_confirmed(bar_date, now_tw, is_tw_holiday_fn, require_time_str=None):
    today_tw = now_tw.date()
    if bar_date < today_tw:
        return True
    if bar_date > today_tw:
        return False
    if is_tw_holiday_fn is not None and is_tw_holiday_fn(bar_date):
        return False
    if now_tw.strftime('%H:%M') < '13:35':
        return False
    if require_time_str is not None and require_time_str < '13:30:00':
        return False
    return True


def fetch_tw_ir0001_data(
    expected_session,
    is_tw_holiday_fn,
    years=10,
    ssl_context=None,
    now_tw=None,
    sleep_fn=time.sleep,
    min_rows=225,
):
    """抓取 TWSE 官方發行量加權股價報酬指數 (IR0001) 日線 OHLC DataFrame。"""
    now_tw = now_tw if now_tw is not None else datetime.now(TZ_TAIPEI)
    if getattr(now_tw, 'tzinfo', None) is None:
        now_tw = now_tw.replace(tzinfo=TZ_TAIPEI)
    else:
        now_tw = now_tw.astimezone(TZ_TAIPEI)

    exp_date = expected_session.date() if isinstance(expected_session, datetime) else expected_session
    ctx = ssl_context if ssl_context is not None else _default_ssl_context()

    rows = {}
    layers = []
    mfi94u_dates = set()
    spot_ref = None
    unverified_mis_patch_skipped = False

    # === Layer 1: FinMind TaiwanStockTotalReturnIndex (TAIEX = IR0001) ===
    start_date = (now_tw.date() - timedelta(days=365 * years + 30)).isoformat()
    try:
        fm_json = _fetch_json(FINMIND_URL.format(start_date=start_date), ctx, timeout=18)
        fm_data = fm_json.get('data') if isinstance(fm_json, dict) else None
        if isinstance(fm_data, list):
            fm_count = 0
            for item in fm_data:
                if not isinstance(item, dict):
                    continue
                d_str = str(item.get('date', '')).strip()
                try:
                    d_obj = date.fromisoformat(d_str)
                except ValueError:
                    continue
                if d_obj > exp_date:
                    continue
                price = _parse_pos_float(item.get('price'))
                if price is not None:
                    rows[d_str] = _make_bar(price)
                    fm_count += 1
            if fm_count > 0:
                layers.append('finmind')
    except Exception as exc:
        print(f"⚠️ [IR0001 Layer 1] FinMind TAIEX 抓取失敗: {exc}")

    # === Layer 2: TWSE 官方 MFI94U 月報（近年權威覆寫，或 FinMind 失效時多月補全）===
    if len(rows) >= min_rows:
        mfi_months = _month_starts_backwards(now_tw.date(), 2)
    else:
        n_fallback = 16 if min_rows <= 260 else min(36, years * 12 + 2)
        mfi_months = _month_starts_backwards(now_tw.date(), n_fallback)

    mfi_added = 0
    for idx, ym01 in enumerate(mfi_months):
        if idx > 0 and sleep_fn is not None:
            sleep_fn(0.25)
        try:
            mfi_json = _fetch_json(TWSE_MFI94U_URL.format(ym01=ym01), ctx, timeout=10)
            mfi_data = mfi_json.get('data') if isinstance(mfi_json, dict) else None
            if not isinstance(mfi_data, list):
                continue
            for row in mfi_data:
                if not isinstance(row, (list, tuple)) or len(row) < 2:
                    continue
                iso = _parse_roc_date(row[0])
                val = _parse_pos_float(row[1])
                if iso is None or val is None:
                    continue
                if date.fromisoformat(iso) > exp_date:
                    continue
                rows[iso] = _make_bar(val)
                mfi94u_dates.add(iso)
                mfi_added += 1
        except Exception as exc:
            print(f"⚠️ [IR0001 Layer 2] TWSE MFI94U 查詢失敗 ({ym01}): {exc}")
    if mfi_added > 0:
        layers.append('twse_mfi94u')

    # === Layer 3: TWSE 官方 MIS 即時/收盤介面 (IR0001 + 加權指數 ^TWII 參考) ===
    try:
        mis_json = _fetch_json(TWSE_MIS_URL, ctx, timeout=10)
        msg_array = mis_json.get('msgArray') if isinstance(mis_json, dict) else None
        if isinstance(msg_array, list):
            for entry in msg_array:
                if not isinstance(entry, dict):
                    continue
                code = str(entry.get('c', '')).strip()
                bar_date = _parse_yyyymmdd_date(entry.get('d'))
                t_str = str(entry.get('t', '')).strip()
                z_val = _parse_pos_float(entry.get('z'))
                if bar_date is None or z_val is None or bar_date > exp_date:
                    continue

                if code == 't00':
                    if _is_eod_confirmed(bar_date, now_tw, is_tw_holiday_fn):
                        spot_ref = {
                            'label': '加權',
                            'ticker': '^TWII',
                            'close': round(z_val, 2),
                            'date': bar_date.isoformat(),
                        }
                    continue

                if code != 'IR0001':
                    continue
                if not _is_eod_confirmed(bar_date, now_tw, is_tw_holiday_fn, require_time_str=t_str):
                    continue

                bar_iso = bar_date.isoformat()
                if bar_iso in mfi94u_dates:
                    continue

                y_val = _parse_pos_float(entry.get('y'))
                prior_dates = [d for d in rows if d < bar_iso]
                if prior_dates and y_val is not None:
                    prev_iso = max(prior_dates)
                    prev_date = date.fromisoformat(prev_iso)
                    prev_close = rows[prev_iso]['Close']
                    if (bar_date - prev_date).days <= 14 and prev_close > 0:
                        if abs(y_val / prev_close - 1.0) > 0.005:
                            unverified_mis_patch_skipped = True
                            print(f"⚠️ [IR0001 Layer 3] 略過 MIS {bar_iso}：昨收 {y_val:.2f} 與前日 {prev_iso} ({prev_close:.2f}) 落差 > 0.5%")
                            continue

                o_val = _parse_pos_float(entry.get('o')) or z_val
                h_val = _parse_pos_float(entry.get('h')) or z_val
                l_val = _parse_pos_float(entry.get('l')) or z_val
                rows[bar_iso] = _make_bar(z_val, open_val=o_val, high_val=h_val, low_val=l_val)
                layers.append('twse_mis')
    except Exception as exc:
        print(f"⚠️ [IR0001 Layer 3] TWSE MIS 查詢失敗: {exc}")

    # === Layer 4: TWSE 官方 MI_INDEX 備援（預期交易日仍缺失時）===
    exp_iso = exp_date.isoformat()
    if exp_iso not in rows and _is_eod_confirmed(exp_date, now_tw, is_tw_holiday_fn):
        try:
            mi_json = _fetch_json(TWSE_MI_INDEX_URL.format(ymd=exp_date.strftime('%Y%m%d')), ctx, timeout=12)
            tables = mi_json.get('tables') if isinstance(mi_json, dict) else None
            if isinstance(tables, list):
                mi_filled = False
                for tbl in tables:
                    for row in (tbl.get('data') or []) if isinstance(tbl, dict) else []:
                        if not isinstance(row, (list, tuple)) or len(row) < 2:
                            continue
                        idx_name = str(row[0]).strip()
                        if '發行量加權股價報酬指數' in idx_name:
                            val = _parse_pos_float(row[1])
                            if val is not None:
                                rows[exp_iso] = _make_bar(val)
                                mi_filled = True
                        elif idx_name == '發行量加權股價指數' and spot_ref is None:
                            val = _parse_pos_float(row[1])
                            if val is not None:
                                spot_ref = {
                                    'label': '加權',
                                    'ticker': '^TWII',
                                    'close': round(val, 2),
                                    'date': exp_iso,
                                }
                if mi_filled:
                    layers.append('twse_mi_index')
        except Exception as exc:
            print(f"⚠️ [IR0001 Layer 4] TWSE MI_INDEX 查詢失敗 ({exp_iso}): {exc}")

    if len(rows) < min_rows:
        return None

    sorted_dates = sorted(rows.keys())
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in sorted_dates])
    df = pd.DataFrame([rows[d] for d in sorted_dates], index=idx, columns=['Open', 'High', 'Low', 'Close', 'Volume'])
    df = df[~df.index.duplicated(keep='last')].sort_index()
    df.attrs['source_layer'] = '+'.join(dict.fromkeys(layers)) if layers else 'twse_ir0001'
    if spot_ref is not None:
        df.attrs['spot_ref'] = spot_ref
    if unverified_mis_patch_skipped:
        df.attrs['unverified_mis_patch_skipped'] = True
    return df
