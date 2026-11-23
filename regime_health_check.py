"""每月 1 號體制健康哨兵（涵蓋鬧鐘 ②・③・④、第四層三大紅線、台積電權重與 CI 失敗告警）。

本模組刻意將「純統計評估」與「外部網路抓取」完全解耦，且 `--notify-ci-failure`
僅使用 Python 標準函式庫即可運行（不匯入 numpy / pandas / yfinance）。
"""
import argparse
from datetime import datetime, date as date_cls, timedelta, timezone
from html import escape
import json
import math
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

TZ_TAIPEI = ZoneInfo('Asia/Taipei')

# 100% 對齊 engine.py / app.py / build_static.py 定案參數（grace_days=0、Scheme C、70/30 ±10%）
US_P = dict(
    base=0.8, mx=3.0, f=5, s=220, bo_up=8, bo_dn=8, cd=8,
    allocs=(0.0, 0.65, 0.85, 1.0), grace_days=0, ext=(1.16, 1.8), accel=None,
    deep_bear=(60, 0.97, 0.4), add_above_bear=True, pre_bull_boost=1.0,
)
TW_P = dict(
    base=1.0, mx=3.0, f=8, s=220, bo_up=15, bo_dn=25, cd=10,
    allocs=(0.0, 0.50, 0.75, 0.90, 1.0), grace_days=0, ext=None, accel=(40, 1.2, 252),
    deep_bear=(60, 0.97, 0.5), add_above_bear=False, pre_bull_boost=1.2,
)
PORTFOLIO_WEIGHTS = {'US': 0.70, 'TW': 0.30}
SEMI_BAND = 0.10  # 60% ~ 80% 寬帶
TRIM_BUFFER = 0.15
SPREAD_US = 0.003
RF_TW = 0.013
SPREAD_TW = 0.005
TX_COST = 1e-4

STATUS_EMOJI = {
    'green': '🟢',
    'yellow': '🟡',
    'red': '🔴',
    'unknown': '⚪',
}


def _ssl_context():
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def _clean_close_series(raw_series, allow_zero=False):
    import numpy as np
    import pandas as pd

    if isinstance(raw_series, pd.DataFrame):
        if 'Close' in raw_series.columns:
            s = raw_series['Close']
        else:
            s = raw_series.iloc[:, 0]
    else:
        s = raw_series
    if isinstance(s, pd.DataFrame):
        s = s.iloc[:, 0]
    s = pd.to_numeric(s, errors='coerce')
    idx = pd.to_datetime(s.index)
    if getattr(idx, 'tz', None) is not None:
        idx = idx.tz_localize(None)
    s.index = idx.normalize()
    s = s.replace([np.inf, -np.inf], np.nan).dropna()
    s = s[~s.index.duplicated(keep='last')].sort_index()
    if allow_zero:
        s = s[s >= -1.0]
    else:
        s = s[s > 0.0]
    s.name = 'Close'
    return s


def _drop_incomplete_eod_bar(s, ticker, now_dt=None):
    """剔除尚未收盤之當日盤中報價或未來日期異常 K 棒，確保只用已完成 EOD 收盤價。"""
    if len(s) == 0:
        return s
    is_tw = ticker.endswith('.TW') or ticker == '^TWII'
    if is_tw:
        local_now = now_dt if now_dt is not None else datetime.now(TZ_TAIPEI)
        cutoff_hm = '13:35'
    else:
        local_now = now_dt if now_dt is not None else datetime.now(ZoneInfo('America/New_York'))
        cutoff_hm = '16:00'

    today_local = local_now.date()
    s = s[[d <= today_local for d in s.index.date]]
    if len(s) > 0 and s.index[-1].date() == today_local and local_now.strftime('%H:%M') < cutoff_hm:
        s = s.iloc[:-1]
    return s


def fetch_close_series(ticker, period='10y', allow_zero=False, min_rows=260):
    """兩層唯讀日線抓價：yfinance 主抓，失敗或筆數不足則退回 Yahoo Direct Chart API。"""
    import numpy as np
    import pandas as pd
    import yfinance as yf

    last_err = None
    try:
        df = yf.download(ticker, period=period, auto_adjust=True, progress=False)
        if df is not None and not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            if 'Close' in df.columns:
                s = _drop_incomplete_eod_bar(_clean_close_series(df['Close'], allow_zero=allow_zero), ticker)
                if len(s) >= min_rows:
                    return s
    except Exception as exc:
        last_err = exc

    try:
        quoted = urllib.parse.quote(ticker, safe='')
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{quoted}?interval=1d&range={period}'
        req = urllib.request.Request(
            url,
            headers={
                'User-Agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
                )
            },
        )
        with urllib.request.urlopen(req, context=_ssl_context(), timeout=15) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
        res = payload['chart']['result'][0]
        timestamps = res['timestamp']
        quote = res['indicators']['quote'][0]
        adj_block = res['indicators'].get('adjclose') or [{}]
        raw_vals = adj_block[0].get('adjclose') if not allow_zero else quote.get('close')
        if raw_vals is None:
            raw_vals = quote['close']
        arr = np.asarray([np.nan if v is None else float(v) for v in raw_vals], dtype='float64')
        idx = pd.to_datetime(timestamps, unit='s', utc=True).tz_convert(None).normalize()
        s = _drop_incomplete_eod_bar(_clean_close_series(pd.Series(arr, index=idx), allow_zero=allow_zero), ticker)
        if len(s) >= min_rows:
            return s
        raise RuntimeError(f'{ticker}: Yahoo Direct returned only {len(s)} valid rows (< {min_rows})')
    except Exception as exc:
        raise RuntimeError(f'Failed to fetch {ticker}: layer1={last_err}; layer2={exc}') from exc


def fetch_tsmc_weight():
    """項目 5a：抓 006208.TW 台積電權重（失敗時改讀 0050.TW 代理；兩者皆失敗優雅降級）。"""
    import yfinance as yf

    errors = []
    for tkr, src in (('006208.TW', '006208.TW'), ('0050.TW', '0050.TW_proxy')):
        try:
            top_h = yf.Ticker(tkr).funds_data.top_holdings
            if top_h is not None and '2330.TW' in top_h.index:
                val = float(top_h.loc['2330.TW', 'Holding Percent']) * 100.0
                if math.isfinite(val) and 0.0 < val < 100.0:
                    return {'weight_pct': round(val, 2), 'source': src, 'error': None}
            errors.append(f'{tkr}: 2330.TW missing in top_holdings')
        except Exception as exc:
            errors.append(f'{tkr}: {exc}')
    return {'weight_pct': None, 'source': None, 'error': '; '.join(errors)}


def evaluate_alarm4_volatility(close_s, label):
    """項目 1：鬧鐘 ④（1~2 年常態年化波動率 >30%~35% 警戒 / <25% 且站上 220MA 恢復）。"""
    import numpy as np

    if len(close_s) < 260:
        raise ValueError(f'{label}: need at least 260 rows for alarm4, got {len(close_s)}')

    r = close_s.pct_change().dropna()
    vol_1y_pct = float(r.tail(252).std(ddof=1) * math.sqrt(252.0) * 100.0)
    win_2y = min(len(r), 504)
    vol_2y_pct = float(r.tail(win_2y).std(ddof=1) * math.sqrt(252.0) * 100.0)
    sma220 = float(close_s.rolling(220).mean().iloc[-1])
    last_close = float(close_s.iloc[-1])
    above_220ma = bool(np.isfinite(sma220) and last_close >= sma220)
    recovery_ready = bool(vol_1y_pct < 25.0 and above_220ma)

    if (vol_1y_pct >= 35.0) or (vol_1y_pct >= 30.0 and vol_2y_pct >= 30.0):
        status = 'red'
    elif (vol_1y_pct >= 28.0) or (vol_2y_pct >= 28.0):
        status = 'yellow'
    else:
        status = 'green'

    return {
        'label': label,
        'status': status,
        'vol_1y_pct': round(vol_1y_pct, 2),
        'vol_2y_pct': round(vol_2y_pct, 2),
        'above_220ma': above_220ma,
        'recovery_ready': recovery_ready,
        'last_date': str(close_s.index[-1].date()),
    }


def evaluate_alarm2_whipsaw(close_s, fast_ma, slow_ma, max_breaks_2y, label):
    """項目 2：鬧鐘 ②（滾動 2 年破 220MA 次數突破極限 且 2Y 報酬 < 0%）。"""
    import numpy as np
    import pandas as pd

    if len(close_s) < 505:
        raise ValueError(f'{label}: need at least 505 rows for alarm2, got {len(close_s)}')

    c = close_s.to_numpy(dtype=float)
    f_arr = close_s.rolling(fast_ma).mean().to_numpy(dtype=float)
    s_arr = close_s.rolling(slow_ma).mean().to_numpy(dtype=float)

    in_tr = False
    brk = np.zeros(len(c), dtype=int)
    for i in range(len(c)):
        if np.isnan(s_arr[i]) or np.isnan(f_arr[i]):
            continue
        was = in_tr
        if f_arr[i] < s_arr[i] or c[i] < s_arr[i]:
            if was:
                brk[i] = 1
            in_tr = False
        elif not in_tr and f_arr[i] >= s_arr[i] and c[i] >= s_arr[i]:
            in_tr = True

    brk_s = pd.Series(brk, index=close_s.index)
    breaks_1y = int(brk_s.tail(252).sum())
    breaks_2y = int(brk_s.tail(504).sum())
    ret_2y_pct = (float(close_s.iloc[-1]) / float(close_s.iloc[-505]) - 1.0) * 100.0

    if breaks_2y > max_breaks_2y and ret_2y_pct < 0.0:
        status = 'red'
    elif breaks_2y >= max_breaks_2y - 1 and ret_2y_pct < 5.0:
        status = 'yellow'
    else:
        status = 'green'

    return {
        'label': label,
        'status': status,
        'breaks_1y': breaks_1y,
        'breaks_2y': breaks_2y,
        'limit_2y': int(max_breaks_2y),
        'ret_2y_pct': round(ret_2y_pct, 2),
        'in_trend_now': bool(in_tr),
    }


def evaluate_alarm3_us_rates(irx_pct_s, qqq_close_s):
    """項目 3：鬧鐘 ③（僅限美股 QQQ：^IRX 連 12 個月 > 6.0% 且美股趨勢轉弱）。"""
    if len(irx_pct_s) < 252 or len(qqq_close_s) < 253:
        raise ValueError('Insufficient ^IRX or QQQ rows for alarm3')

    irx_1y = irx_pct_s.tail(252)
    latest_pct = float(irx_1y.iloc[-1])
    min_1y_pct = float(irx_1y.min())
    avg_1y_pct = float(irx_1y.mean())
    pct_days_gt6 = float((irx_1y > 6.0).mean() * 100.0)
    sustained_gt6 = bool(pct_days_gt6 >= 95.0)

    sma220 = qqq_close_s.rolling(220).mean().dropna()
    prev_idx = -21 if len(sma220) >= 21 else 0
    sma220_down = bool(float(sma220.iloc[-1]) < float(sma220.iloc[prev_idx]))
    ret_1y_pct = (float(qqq_close_s.iloc[-1]) / float(qqq_close_s.iloc[-253]) - 1.0) * 100.0
    us_weak = bool(sma220_down or (ret_1y_pct < 0.0))

    if sustained_gt6 and us_weak:
        status = 'red'
    elif latest_pct >= 5.5 or pct_days_gt6 >= 50.0:
        status = 'yellow'
    else:
        status = 'green'

    return {
        'status': status,
        'latest_pct': round(latest_pct, 2),
        'min_1y_pct': round(min_1y_pct, 2),
        'avg_1y_pct': round(avg_1y_pct, 2),
        'pct_days_gt6': round(pct_days_gt6, 1),
        'sustained_gt6': sustained_gt6,
        'us_weak': us_weak,
        'sma220_down': sma220_down,
        'qqq_ret_1y_pct': round(ret_1y_pct, 2),
        'last_date': str(irx_pct_s.index[-1].date()),
    }


def compute_target_lev_series(close_s, params):
    """嚴格對齊 engine.target_lev（lag1 訊號、G0、C1-A2、E3b、D1-US、Ext 1.16/1.8、牛初加速）。"""
    import numpy as np
    import pandas as pd

    c = close_s.to_numpy(dtype=float)
    f_arr = close_s.rolling(params['f']).mean().to_numpy(dtype=float)
    s_arr = close_s.rolling(params['s']).mean().to_numpy(dtype=float)
    h_arr = close_s.shift(1).rolling(params['bo_up']).max().to_numpy(dtype=float)
    lo_arr = close_s.shift(1).rolling(params['bo_dn']).min().to_numpy(dtype=float)

    deep_bear = params.get('deep_bear')
    add_above_bear = params.get('add_above_bear', False)
    pre_bull_boost = params.get('pre_bull_boost')
    bear_ma = deep_bear[0] if deep_bear else (60 if (add_above_bear or pre_bull_boost is not None) else None)
    bma_arr = close_s.rolling(bear_ma).mean().to_numpy(dtype=float) if bear_ma else None

    allocs = params['allocs']
    base = params['base']
    mx = params['mx']
    cd = params['cd']
    grace_days = params.get('grace_days', 0)
    ext = params.get('ext')
    accel = params.get('accel')

    n = len(c)
    out = np.zeros(n, dtype=float)
    in_tr = False
    step = 0
    last = -999
    below = 0
    def_days = 0
    acc_left = 0
    pre_bull_on = False

    for i in range(1, n):
        if np.isnan(s_arr[i - 1]) or np.isnan(h_arr[i - 1]) or np.isnan(lo_arr[i - 1]):
            continue
        p0, sf, ss, h, lo = c[i - 1], f_arr[i - 1], s_arr[i - 1], h_arr[i - 1], lo_arr[i - 1]
        bma = bma_arr[i - 1] if bma_arr is not None else np.nan

        below = below + 1 if p0 < ss else 0
        if sf < ss or below > grace_days:
            in_tr = False
            step = 0
            acc_left = 0
            def_days += 1
        elif not in_tr:
            if sf >= ss and p0 >= ss:
                in_tr = True
                pre_bull_on = False
                if accel and def_days >= accel[0]:
                    acc_left = accel[2]
                def_days = 0
            else:
                def_days += 1

        if in_tr and (i - last >= cd):
            if p0 > h and step < len(allocs) - 1:
                if not add_above_bear or np.isnan(bma) or sf >= bma:
                    step += 1
                    last = i
            elif p0 < lo and step > 0:
                step -= 1
                last = i

        cur_b = base
        if not in_tr:
            if deep_bear is not None and not np.isnan(bma) and bma < ss * deep_bear[1] and sf < bma:
                cur_b = deep_bear[2]
                pre_bull_on = False
            elif pre_bull_boost is not None and not np.isnan(bma):
                if p0 >= bma and sf >= bma:
                    if p0 > h:
                        pre_bull_on = True
                else:
                    pre_bull_on = False
                if pre_bull_on:
                    cur_b = pre_bull_boost

        if accel and in_tr and acc_left > 0:
            cur_b = accel[1]
            acc_left -= 1

        lev = cur_b * (1.0 - allocs[step]) + mx * allocs[step]
        if ext and in_tr and (p0 / ss) > ext[0]:
            lev = min(lev, ext[1])
        out[i] = lev

    return pd.Series(out, index=close_s.index)


def _simulate_single_book(idx, rets, targets, rates, spreads, cash_drag_us=True):
    """在固定聯集時間軸 idx 執行 70/30 ±10% 半年寬帶再平衡撮合（no_buy_down + +0.15x 修剪）。"""
    import numpy as np
    import pandas as pd

    names = ('US', 'TW')
    semi = np.r_[False, (idx[1:].month != idx[:-1].month) & np.isin(idx[1:].month, [1, 7])]
    gaps = np.r_[1, np.diff(idx.values).astype('timedelta64[D]').astype(int)]

    cap = {m: PORTFOLIO_WEIGHTS[m] for m in names}
    lev_now = {m: 0.0 for m in names}
    last_t = {m: None for m in names}
    market_insolvent = {m: False for m in names}
    insolvent = False
    pv = np.zeros(len(idx), dtype=float)

    for i in range(len(idx)):
        for m in names:
            if market_insolvent[m] or cap[m] <= 0.0:
                cap[m] = 0.0
                lev_now[m] = 0.0
                continue
            rf_d = rates[m][i] * gaps[i] / 365.0
            sp_d = spreads[m] * gaps[i] / 365.0
            r = rets[m][i]
            rf_earn = (0.60 * rf_d) if (cash_drag_us and m == 'US') else rf_d
            net = rf_earn + lev_now[m] * (r - rf_d - sp_d)
            if (1.0 + net) <= 0.0:
                cap[m] = 0.0
                lev_now[m] = 0.0
                market_insolvent[m] = True
                insolvent = True
            else:
                cap[m] *= (1.0 + net)
                lev_now[m] = lev_now[m] * (1.0 + r) / (1.0 + net)

        total = sum(cap.values())
        if total <= 0.0:
            insolvent = True

        if not insolvent and semi[i]:
            w_us = cap['US'] / total
            if w_us < PORTFOLIO_WEIGHTS['US'] - SEMI_BAND or w_us > PORTFOLIO_WEIGHTS['US'] + SEMI_BAND:
                for m in names:
                    cap[m] = total * PORTFOLIO_WEIGHTS[m]

        for m in names:
            if market_insolvent[m] or cap[m] <= 0.0:
                continue
            t = targets[m][i]
            sig_reb = (last_t[m] is None) or (t > last_t[m]) or (t < last_t[m] and lev_now[m] > t)
            if sig_reb:
                planned_n = cap[m] * t
            else:
                planned_n = cap[m] * lev_now[m]
                if planned_n / cap[m] > t + TRIM_BUFFER:
                    planned_n = cap[m] * t

            old_n = cap[m] * lev_now[m]
            fee = abs(planned_n - old_n) * TX_COST
            if cap[m] - fee <= 0.0:
                cap[m] = 0.0
                lev_now[m] = 0.0
                market_insolvent[m] = True
                insolvent = True
            else:
                cap[m] -= fee
                lev_now[m] = planned_n / cap[m]
                last_t[m] = t

        pv[i] = sum(cap.values())

    return pd.Series(pv, index=idx), insolvent


def simulate_portfolio_70_30(us_close, tw_close, irx_pct_s):
    """以純 10Y 近端序列模擬 Scheme C (70/30 ±10%) 與 1x (70/30 ±10%) 淨值曲線。"""
    import numpy as np
    import pandas as pd

    us_tl = compute_target_lev_series(us_close, US_P)
    tw_tl = compute_target_lev_series(tw_close, TW_P)
    us_ready = us_tl[us_tl > 0.0].index
    tw_ready = tw_tl[tw_tl > 0.0].index
    if len(us_ready) == 0 or len(tw_ready) == 0:
        raise ValueError('Insufficient warm-up bars for 220MA strategy simulation')
    start_dt = max(us_ready[0], tw_ready[0])

    idx = pd.DatetimeIndex(sorted(set(us_close.index).union(set(tw_close.index))))
    idx = idx[idx >= start_dt]
    if len(idx) < 2:
        raise ValueError('Insufficient aligned trading days after 220MA warm-up')

    us_al = us_close.reindex(idx.union(us_close.index)).ffill().reindex(idx)
    tw_al = tw_close.reindex(idx.union(tw_close.index)).ffill().reindex(idx)
    irx_dec = (irx_pct_s / 100.0).clip(lower=0.0)
    irx_al = irx_dec.reindex(idx.union(irx_dec.index)).ffill().bfill().reindex(idx).fillna(0.0)

    rets = {
        'US': us_al.pct_change().fillna(0.0).to_numpy(dtype=float),
        'TW': tw_al.pct_change().fillna(0.0).to_numpy(dtype=float),
    }
    rates = {
        'US': irx_al.to_numpy(dtype=float),
        'TW': np.full(len(idx), RF_TW, dtype=float),
    }
    targets_strat = {
        'US': us_tl.reindex(idx.union(us_tl.index)).ffill().reindex(idx).fillna(0.0).to_numpy(dtype=float),
        'TW': tw_tl.reindex(idx.union(tw_tl.index)).ffill().reindex(idx).fillna(0.0).to_numpy(dtype=float),
    }
    targets_1x = {
        'US': np.ones(len(idx), dtype=float),
        'TW': np.ones(len(idx), dtype=float),
    }

    twr_strat, insolv_strat = _simulate_single_book(
        idx, rets, targets_strat, rates, {'US': SPREAD_US, 'TW': SPREAD_TW}, cash_drag_us=True
    )
    twr_1x, _ = _simulate_single_book(
        idx, rets, targets_1x, rates, {'US': 0.0, 'TW': 0.0}, cash_drag_us=False
    )
    return {'twr_strategy': twr_strat, 'twr_1x': twr_1x, 'insolvent': insolv_strat}


def evaluate_layer4_redlines(us_close, tw_close, irx_pct_s):
    """項目 4：第四層三大紅線（3Y 滾動落後 1x <-12%/yr、水下 >880 日、MDD <-50%/-70%）。"""
    import pandas as pd

    sim = simulate_portfolio_70_30(us_close, tw_close, irx_pct_s)
    twr_c = sim['twr_strategy']
    twr_1x = sim['twr_1x']
    insolvent = bool(sim['insolvent'])
    idx = twr_c.index
    end_dt = idx[-1]

    # 紅線 A：精準日曆 3 年視窗（避免美台聯集每年 ~258 日導致 756 筆不足 3 年）
    target_3y_dt = end_dt - pd.DateOffset(years=3)
    if target_3y_dt >= idx[0]:
        pos = int(idx.searchsorted(target_3y_dt))
        pos = min(pos, len(idx) - 2)
        yrs = max((end_dt - idx[pos]).days / 365.25, 1e-6)
        cagr_3y_strat = (float(twr_c.iloc[-1]) / float(twr_c.iloc[pos])) ** (1.0 / yrs) - 1.0
        cagr_3y_1x = (float(twr_1x.iloc[-1]) / float(twr_1x.iloc[pos])) ** (1.0 / yrs) - 1.0
        diff_3y_pct = (cagr_3y_strat - cagr_3y_1x) * 100.0
        if diff_3y_pct < -12.0:
            status_a = 'red'
        elif diff_3y_pct < -6.0:
            status_a = 'yellow'
        else:
            status_a = 'green'
    else:
        cagr_3y_strat = None
        cagr_3y_1x = None
        diff_3y_pct = None
        status_a = 'unknown'

    # 紅線 B：當前連續未創新高水下交易日數（與 880 日 / 3.5 年標尺對齊）
    peak = twr_c.cummax()
    is_uw = twr_c < peak
    uw_streak = is_uw.groupby((~is_uw).cumsum()).cumsum().astype(int)
    current_uw_days = int(uw_streak.iloc[-1])
    max_uw_days = int(uw_streak.max())
    if current_uw_days > 880:
        status_b = 'red'
    elif current_uw_days > 560:
        status_b = 'yellow'
    else:
        status_b = 'green'

    # 紅線 C：當前與近 1 個月（22 日）回撤深度（避免多年已復原舊股災永遠亮紅燈）
    dd_pct_s = (twr_c / peak - 1.0) * 100.0
    current_dd_pct = float(dd_pct_s.iloc[-1])
    worst_1m_dd_pct = float(dd_pct_s.tail(22).min())
    active_dd_pct = min(current_dd_pct, worst_1m_dd_pct)
    max_dd_pct = float(dd_pct_s.min())
    tsunami_breached = bool(insolvent or active_dd_pct <= -70.0)
    if insolvent or active_dd_pct <= -50.0:
        status_c = 'red'
    elif active_dd_pct <= -40.0:
        status_c = 'yellow'
    else:
        status_c = 'green'

    statuses = [status_a, status_b, status_c]
    if 'red' in statuses:
        overall = 'red'
    elif 'yellow' in statuses:
        overall = 'yellow'
    elif all(s == 'green' for s in statuses):
        overall = 'green'
    else:
        overall = 'yellow' if 'yellow' in statuses else 'green'

    return {
        'status': overall,
        'insolvent': insolvent,
        'sim_start': str(idx[0].date()),
        'sim_end': str(end_dt.date()),
        'redline_a': {
            'status': status_a,
            'diff_3y_pct': round(diff_3y_pct, 2) if diff_3y_pct is not None else None,
            'strat_3y_cagr_pct': round(cagr_3y_strat * 100.0, 2) if cagr_3y_strat is not None else None,
            'bench1x_3y_cagr_pct': round(cagr_3y_1x * 100.0, 2) if cagr_3y_1x is not None else None,
            'limit_pct': -12.0,
        },
        'redline_b': {
            'status': status_b,
            'current_uw_days': current_uw_days,
            'current_uw_years': round(current_uw_days / 252.0, 2),
            'max_uw_days': max_uw_days,
            'limit_days': 880,
        },
        'redline_c': {
            'status': status_c,
            'current_dd_pct': round(current_dd_pct, 2),
            'worst_1m_dd_pct': round(worst_1m_dd_pct, 2),
            'active_dd_pct': round(active_dd_pct, 2),
            'max_dd_pct': round(max_dd_pct, 2),
            'limit_normal_pct': -50.0,
            'limit_tsunami_pct': -70.0,
            'tsunami_breached': tsunami_breached,
        },
    }


def evaluate_tsmc_weight(tsmc_info):
    """項目 5a：台積電在 006208.TW 權重（< 30% 紅燈、< 35% 黃燈；無法取得為 unknown）。"""
    weight_pct = None if tsmc_info is None else tsmc_info.get('weight_pct')
    source = None if tsmc_info is None else tsmc_info.get('source')
    error = None if tsmc_info is None else tsmc_info.get('error')
    if weight_pct is None or not math.isfinite(float(weight_pct)):
        return {'status': 'unknown', 'weight_pct': None, 'source': source, 'error': error}
    val = float(weight_pct)
    if val < 30.0:
        status = 'red'
    elif val < 35.0:
        status = 'yellow'
    else:
        status = 'green'
    return {'status': status, 'weight_pct': round(val, 2), 'source': source, 'error': error}


def evaluate_all(us_close, tw_close, irx_pct_s, tsmc_info, as_of_date=None):
    """彙總全部 1~5a 體制健康指標與總燈號。"""
    if as_of_date is None:
        as_of_date = datetime.now(TZ_TAIPEI).date()

    alarm4_us = evaluate_alarm4_volatility(us_close, '美股 QQQ')
    alarm4_tw = evaluate_alarm4_volatility(tw_close, '台股 006208')
    alarm2_us = evaluate_alarm2_whipsaw(us_close, US_P['f'], US_P['s'], 10, '美股 QQQ')
    alarm2_tw = evaluate_alarm2_whipsaw(tw_close, TW_P['f'], TW_P['s'], 6, '台股 006208')
    alarm3 = evaluate_alarm3_us_rates(irx_pct_s, us_close)
    layer4 = evaluate_layer4_redlines(us_close, tw_close, irx_pct_s)
    tsmc = evaluate_tsmc_weight(tsmc_info)

    checks = [
        ('鬧鐘④ 美股高波', alarm4_us['status']),
        ('鬧鐘④ 台股高波', alarm4_tw['status']),
        ('鬧鐘② 美股箱型', alarm2_us['status']),
        ('鬧鐘② 台股箱型', alarm2_tw['status']),
        ('鬧鐘③ 美股高息', alarm3['status']),
        ('紅線A 3Y落後1x', layer4['redline_a']['status']),
        ('紅線B 水下天數', layer4['redline_b']['status']),
        ('紅線C 回撤深度', layer4['redline_c']['status']),
        ('5a 台積電權重', tsmc['status']),
    ]
    red_items = [name for name, st in checks if st == 'red']
    yellow_items = [name for name, st in checks if st == 'yellow']

    if red_items:
        overall = 'red'
    elif yellow_items:
        overall = 'yellow'
    else:
        overall = 'green'

    return {
        'as_of_date': as_of_date.isoformat(),
        'data_dates': {
            'us': str(us_close.index[-1].date()),
            'tw': str(tw_close.index[-1].date()),
            'irx': str(irx_pct_s.index[-1].date()),
        },
        'overall_status': overall,
        'has_red': bool(red_items),
        'has_yellow': bool(yellow_items),
        'red_items': red_items,
        'yellow_items': yellow_items,
        'alarm4': {'us': alarm4_us, 'tw': alarm4_tw},
        'alarm2': {'us': alarm2_us, 'tw': alarm2_tw},
        'alarm3': alarm3,
        'layer4': layer4,
        'tsmc': tsmc,
    }


def should_notify(report, mode, as_of_date=None):
    """推播節制：auto 下有紅燈立刻響、1 月送年報、7 月送半年報，其餘靜音不打擾。"""
    if as_of_date is None:
        as_of_date = date_cls.fromisoformat(report['as_of_date'])

    if mode == 'dry-run':
        return False, 'dry_run'
    if mode == 'force-notify':
        if report.get('has_red'):
            return True, 'manual_force_red'
        return True, 'manual_force'
    if mode == 'auto':
        if report.get('has_red'):
            return True, 'red_alarm'
        if as_of_date.month == 1:
            return True, 'annual_report'
        if as_of_date.month == 7:
            return True, 'semiannual_report'
        return False, 'silent_sentinel_ok'
    raise ValueError(f'Unsupported mode: {mode}')


def _worst_status(*statuses):
    for target in ('red', 'yellow', 'unknown', 'green'):
        if target in statuses:
            return target
    return 'green'


def format_health_report_html(report, trigger_reason):
    """格式化繁體中文 Telegram HTML 體檢報表。"""
    as_of_str = report['as_of_date']
    as_of_month = int(as_of_str.split('-')[1])
    has_red = bool(report['has_red'])

    if has_red:
        header = f"🚨 <b>【體制鬧鐘觸發】({escape(as_of_str)})</b>"
    elif trigger_reason == 'annual_report' or as_of_month == 1:
        header = f"📋 <b>【年度策略體檢＆半年再平衡】({escape(as_of_str)})</b>"
    elif trigger_reason == 'semiannual_report' or as_of_month == 7:
        header = f"📋 <b>【年中體制快報＆半年再平衡】({escape(as_of_str)})</b>"
    else:
        header = f"🩺 <b>【手動體制體檢報告】({escape(as_of_str)})</b>"

    a4u = report['alarm4']['us']
    a4t = report['alarm4']['tw']
    a4_emoji = STATUS_EMOJI[_worst_status(a4u['status'], a4t['status'])]

    def _alarm4_note(item):
        ma_txt = '≥220MA' if item['above_220ma'] else '&lt;220MA'
        if item['status'] == 'red':
            return f'（{ma_txt}；🔴建議評估降2.5x）'
        if item['status'] == 'yellow':
            return f'（{ma_txt}；🟡近警戒）'
        if item['recovery_ready']:
            return f'（{ma_txt}；達3.0x標準）'
        if item['vol_1y_pct'] < 25.0:
            return f'（{ma_txt}；低波&lt;25%）'
        return f'（{ma_txt}；正常區間）'

    a4_note_u = _alarm4_note(a4u)
    a4_note_t = _alarm4_note(a4t)

    a2u = report['alarm2']['us']
    a2t = report['alarm2']['tw']
    a2_emoji = STATUS_EMOJI[_worst_status(a2u['status'], a2t['status'])]

    a3 = report['alarm3']
    a3_emoji = STATUS_EMOJI[a3['status']]
    a3_weak_txt = '弱勢' if a3['us_weak'] else '未轉弱'

    l4 = report['layer4']
    la = l4['redline_a']
    lb = l4['redline_b']
    lc = l4['redline_c']
    l4_emoji = STATUS_EMOJI[l4['status']]
    diff_txt = f"{la['diff_3y_pct']:+.2f}%/yr" if la['diff_3y_pct'] is not None else '資料不足'
    tsunami_note = ' ⚠️破-70%海嘯線' if lc['tsunami_breached'] else ''

    tsmc = report['tsmc']
    tsmc_emoji = STATUS_EMOJI[tsmc['status']]
    if tsmc['weight_pct'] is not None:
        src_note = ' (0050代理)' if tsmc.get('source') == '0050.TW_proxy' else ''
        tsmc_txt = f"<b>{tsmc['weight_pct']:.2f}%</b>{escape(src_note)}（&lt;30% 紅線）"
    else:
        tsmc_txt = '暫無法讀取（降級略過）'

    dd = report['data_dates']
    lines = [
        header,
        (
            f"總燈號：{STATUS_EMOJI[report['overall_status']]} <b>{report['overall_status'].upper()}</b>"
            f"｜資料日 US {escape(dd['us'])} / TW {escape(dd['tw'])}"
        ),
        '',
        f"{a4_emoji} <b>1. 鬧鐘④ 高波動（&gt;30%~35% 降2.5x / &lt;25%回220MA 恢復）</b>",
        f"• 美股 1Y/2Y：<b>{a4u['vol_1y_pct']:.2f}% / {a4u['vol_2y_pct']:.2f}%</b>{a4_note_u}",
        f"• 台股 1Y/2Y：<b>{a4t['vol_1y_pct']:.2f}% / {a4t['vol_2y_pct']:.2f}%</b>{a4_note_t}",
        '',
        f"{a2_emoji} <b>2. 鬧鐘② 箱型死魚（美&gt;10 / 台&gt;6 次且 2Y&lt;0%）</b>",
        f"• 美股：2Y 破線 <b>{a2u['breaks_2y']}</b>/{a2u['limit_2y']} 次｜2Y報酬 <b>{a2u['ret_2y_pct']:+.2f}%</b>",
        f"• 台股：2Y 破線 <b>{a2t['breaks_2y']}</b>/{a2t['limit_2y']} 次｜2Y報酬 <b>{a2t['ret_2y_pct']:+.2f}%</b>",
        '',
        f"{a3_emoji} <b>3. 鬧鐘③ 美股高息（^IRX 連12M&gt;6% 且弱勢）</b>",
        f"• ^IRX 現值 <b>{a3['latest_pct']:.2f}%</b>（1Y均 {a3['avg_1y_pct']:.2f}%，&gt;6% 佔 {a3['pct_days_gt6']:.1f}%）｜美股{a3_weak_txt}",
        '',
        f"{l4_emoji} <b>4. 第四層三大紅線（70/30 ±10% 模型，{escape(l4['sim_start'])} 起）</b>",
        f"• {STATUS_EMOJI[la['status']]} 紅線A 3Y超額1x：<b>{escape(diff_txt)}</b>（&lt;-12%/yr 紅線）",
        f"• {STATUS_EMOJI[lb['status']]} 紅線B 當前水下：<b>{lb['current_uw_days']}</b>/{lb['limit_days']} 日（歷史最長 {lb['max_uw_days']} 日）",
        f"• {STATUS_EMOJI[lc['status']]} 紅線C 當前/近1M回撤：<b>{lc['current_dd_pct']:.2f}% / {lc['worst_1m_dd_pct']:.2f}%</b>{tsunami_note}",
        '',
        f"{tsmc_emoji} <b>5a. 台積電權重：</b>{tsmc_txt}",
    ]

    if as_of_month in (1, 7) or trigger_reason in ('annual_report', 'semiannual_report'):
        lines.append('')
        lines.append('📅 <b>本月為半年再平衡檢核期</b>：請開網站 <code>#rebal-panel</code> 核對台美 70/30 ±10%（60%~80%）寬帶。')
    if as_of_month == 1 or trigger_reason == 'annual_report':
        lines.append('📝 年度人工勾稽：順手核對實帳與網站追蹤誤差、以及是否跨過 4,000 萬 / 1 億里程碑。')
    if has_red:
        lines.append('')
        lines.append(f"🚨 <b>觸發項目</b>：{escape('、'.join(report['red_items']))}")
        lines.append('💡 請將本報表貼給 AI 評估是否啟動降載（嚴禁盤中恐慌操作）。')

    return '\n'.join(lines)


def send_telegram_html(text, urgent=False):
    """純標準庫發送 Telegram HTML 推播（3 次重試，嚴格檢查 ok=true）。"""
    bot_token = os.environ.get('TG_BOT_TOKEN', '').strip()
    chat_id = os.environ.get('TG_CHAT_ID', '').strip()
    if not bot_token or not chat_id:
        print(
            f"ℹ️ 未偵測到完整 TG 設定 (TG_BOT_TOKEN: {'已設定' if bot_token else '未設定'}, "
            f"TG_CHAT_ID: {'已設定' if chat_id else '未設定'})，略過 Telegram 推播。"
        )
        return {'ok': True, 'skipped': True, 'delivered': False, 'status': 'skipped_unconfigured', 'attempts': 0}

    payload = json.dumps({
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'HTML',
        'disable_notification': (not urgent),
    }).encode('utf-8')

    ctx = _ssl_context()
    url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
    last_error = None
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
                resp_data = resp.read().decode('utf-8')
                try:
                    resp_json = json.loads(resp_data)
                except Exception:
                    resp_json = {}
                if resp_json.get('ok') is not True:
                    last_error = f'Telegram API returned ok=false: {resp_data[:120]}'
                    print(f'❌ Telegram API 應用層拒絕 (第 {attempt} 次): {last_error}')
                    return {
                        'ok': False, 'skipped': False, 'delivered': False,
                        'status': 'failed', 'error': last_error, 'attempts': attempt,
                    }
                print(f'✅ Telegram 體檢/警報發送成功 (第 {attempt} 次嘗試)！')
                return {'ok': True, 'skipped': False, 'delivered': True, 'status': 'delivered', 'attempts': attempt}
        except urllib.error.HTTPError as exc:
            err_msg = exc.read().decode('utf-8', errors='ignore')
            last_error = f'HTTP {exc.code}: {err_msg}'
            print(f'❌ Telegram 推播 HTTP 錯誤 (第 {attempt}/3 次) [{exc.code}]: {err_msg}')
        except Exception as exc:
            last_error = str(exc)
            print(f'❌ Telegram 推播發送失敗 (第 {attempt}/3 次): {exc}')

    return {'ok': False, 'skipped': False, 'delivered': False, 'status': 'failed', 'error': last_error, 'attempts': 3}


def format_ci_failure_html(env=None):
    """項目 6b：純標準庫格式化 GitHub Actions 排程失敗緊急警報。"""
    src = os.environ if env is None else env
    workflow = src.get('GITHUB_WORKFLOW', 'Investment Automation')
    event_name = src.get('GITHUB_EVENT_NAME', 'unknown')
    repo = src.get('GITHUB_REPOSITORY', '')
    run_id = src.get('GITHUB_RUN_ID', '')
    server = src.get('GITHUB_SERVER_URL', 'https://github.com').rstrip('/')
    sha = src.get('GITHUB_SHA', '')[:7]
    now_tpe = datetime.now(TZ_TAIPEI).strftime('%Y-%m-%d %H:%M TPE')

    lines = [
        '🚨 <b>【排程／建置失敗警報 (6b)】</b>',
        f'• Workflow：<b>{escape(workflow)}</b>',
        f'• 觸發方式：{escape(event_name)}｜時間：{escape(now_tpe)}',
    ]
    if sha:
        lines.append(f'• Commit：<code>{escape(sha)}</code>')
    if repo and run_id:
        run_url = f'{server}/{repo}/actions/runs/{run_id}'
        lines.append(f'• 日誌：<a href="{escape(run_url, quote=True)}">檢視 GitHub Actions Run #{escape(run_id)}</a>')
    lines.append('⚠️ 請確認行情 API（Yahoo／TWSE／Futu）或測試是否連續異常。')
    return '\n'.join(lines)


def notify_ci_failure():
    """項目 6b CLI 入口（不依賴第三方套件）。"""
    text = format_ci_failure_html()
    print(text)
    res = send_telegram_html(text, urgent=True)
    if not res.get('ok'):
        return 1
    return 0


def run_health_check(mode='auto', as_of_str=None, json_out=None):
    """執行線上 1~5a 體制健康檢查並依 mode / 日期決定是否發送 Telegram 報表。"""
    as_of_date = date_cls.fromisoformat(as_of_str) if as_of_str else datetime.now(TZ_TAIPEI).date()

    print(f'🩺 開始執行體制健康檢查 (mode={mode}, as_of={as_of_date.isoformat()})...')
    us_close = fetch_close_series('QQQ', period='10y', allow_zero=False)
    tw_close = fetch_close_series('006208.TW', period='10y', allow_zero=False)
    irx_pct_s = fetch_close_series('^IRX', period='10y', allow_zero=True)
    tsmc_info = fetch_tsmc_weight()

    report = evaluate_all(us_close, tw_close, irx_pct_s, tsmc_info, as_of_date=as_of_date)
    should_send, reason = should_notify(report, mode=mode, as_of_date=as_of_date)
    html_text = format_health_report_html(report, reason)

    report['notify'] = {'should_send': should_send, 'reason': reason}
    if json_out:
        out_dir = os.path.dirname(os.path.abspath(json_out))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(json_out, 'w', encoding='utf-8') as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print(f'✅ 已輸出體檢 JSON：{json_out}')

    print('--- 報表預覽 ---')
    print(html_text)
    print(f'--- 判定結果：overall={report["overall_status"]}, notify={should_send} ({reason}) ---')

    if should_send:
        res = send_telegram_html(html_text, urgent=report['has_red'])
        if not res.get('ok'):
            raise RuntimeError(f"Telegram notification failed: {res.get('error')}")
    return report


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8')
        except Exception:
            pass

    parser = argparse.ArgumentParser(description='Monthly Regime Health Check & CI Failure Notifier')
    parser.add_argument(
        '--mode',
        choices=('auto', 'force-notify', 'dry-run'),
        default='auto',
        help='auto: push on red or Jan/Jul; force-notify: always push; dry-run: print & save JSON only',
    )
    parser.add_argument('--as-of', default=None, help='Override Taipei date YYYY-MM-DD for testing')
    parser.add_argument('--json-out', default=None, help='Optional JSON report output path')
    parser.add_argument(
        '--notify-ci-failure',
        action='store_true',
        help='Send urgent CI/data pipeline failure Telegram alert using stdlib only',
    )
    args = parser.parse_args(argv)

    if args.notify_ci_failure:
        return notify_ci_failure()

    run_health_check(mode=args.mode, as_of_str=args.as_of, json_out=args.json_out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
