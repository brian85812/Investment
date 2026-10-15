"""
共用回測引擎 (import 時不印任何東西)
模型:
- 期貨報酬 = rf + L * (r_total - rf - spread)
- 美: ^IRX 真實利率, spread 0.3%/年; 台: rf 1.3%, spread 0.5%/年
- 台股資料: ^TWII + 3.5%/年 股息還原 (yfinance 0050.TW 資料損壞)
- CAGR 一律 TWR
"""
import os, pickle, time
import yfinance as yf
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

_CACHE = os.path.join(os.path.dirname(__file__), 'data_cache.pkl')

def _dl(t, start='2003-01-01'):
    d = yf.download(t, start=start, auto_adjust=True, progress=False)
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    return d[['Close', 'High', 'Low']].dropna()

def load_data():
    if os.path.exists(_CACHE) and time.time() - os.path.getmtime(_CACHE) < 86400:
        with open(_CACHE, 'rb') as f:
            return pickle.load(f)
    us = _dl('QQQ')
    tw_raw = _dl('^TWII')
    f = pd.Series((1 + 0.035) ** (1 / 252), index=tw_raw.index).cumprod()
    tw = tw_raw.mul(f, axis=0)
    irx = _dl('^IRX')['Close'] / 100
    data = (us, tw, irx)
    with open(_CACHE, 'wb') as fh:
        pickle.dump(data, fh)
    return data

US, TW, IRX = load_data()

US_P = dict(base=0.8, mx=3.0, f=5, s=220, bo=10, cd=3, allocs=[0, .5, .8, 1], grace_days=0, ext=(1.16, 2.0), accel=None, deep_bear=(60, 0.97, 0.4))
TW_P = dict(base=1.0, mx=3.0, f=10, s=220, bo=20, cd=5, allocs=[0, .4, .7, .9, 1], grace_days=1, ext=None, accel=(40, 1.2, 252), deep_bear=(60, 0.97, 0.5))

def target_lev(d, base, mx, f, s, bo, cd, allocs, grace_days=0, ext=None, accel=None, deep_bear=None):
    c = d['Close'].values
    F = d['Close'].rolling(f).mean().values
    S = d['Close'].rolling(s).mean().values
    H = d['Close'].shift(1).rolling(bo).max().values
    Lo = d['Close'].shift(1).rolling(bo).min().values
    
    bear_ma = deep_bear[0] if deep_bear else None
    BMA = d['Close'].rolling(bear_ma).mean().values if bear_ma else None

    n = len(c)
    out = np.zeros(n)
    in_tr, step, last, below, def_days, acc_left = False, 0, -999, 0, 0, 0
    for i in range(1, n):
        if np.isnan(S[i-1]) or np.isnan(H[i-1]) or np.isnan(Lo[i-1]):
            continue
        p0, sf, ss, h, l = c[i-1], F[i-1], S[i-1], H[i-1], Lo[i-1]
        bma = BMA[i-1] if BMA is not None else np.nan

        below = below + 1 if p0 < ss else 0
        if sf < ss or below > grace_days:
            in_tr, step, acc_left = False, 0, 0
            def_days += 1
        elif not in_tr:
            if sf >= ss and p0 >= ss:
                in_tr = True
                if accel and def_days >= accel[0]:
                    acc_left = accel[2]
                def_days = 0
            else:
                def_days += 1
        if in_tr and i - last >= cd:
            if p0 > h and step < len(allocs) - 1:
                step += 1; last = i
            elif p0 < l and step > 0:
                step -= 1; last = i
        cur_b = base
        if not in_tr and deep_bear is not None and not np.isnan(bma):
            if bma < ss * deep_bear[1]:
                cur_b = deep_bear[2]
        if accel and in_tr and acc_left > 0:
            cur_b = accel[1]; acc_left -= 1
        lev = cur_b * (1 - allocs[step]) + mx * allocs[step]
        if ext and in_tr and p0 / ss > ext[0]:
            lev = min(lev, ext[1])
        out[i] = lev
    return pd.Series(out, index=d.index)

def build_markets(us_p=None, tw_p=None):
    us_p = us_p or US_P
    tw_p = tw_p or TW_P
    return {'US': (US, target_lev(US, **us_p), IRX, 0.003),
            'TW': (TW, target_lev(TW, **tw_p), 0.013, 0.005)}

def run(markets, start, end=None, rule='current', band=0.15, trim=0.15,
        init=15e6, cash=200e3, cost=1e-4, weights=None):
    """
    rule:
      current     = 位階改變才動 + 超過 目標+trim 修剪 + 新資金開目標 (舊部位不補)
      symmetric   = current + 低於 目標-band 補回目標
      payday_full = current + 每月最後交易日把全部拉回目標
      daily       = 每天拉回目標 (參考上限)
    回傳 dict: pv, twr, lev, trades(實際調倉次數), turnover(累積調倉名目/資本)
    """
    names = list(markets)
    weights = weights or {'US': 0.6, 'TW': 0.4}
    idx = pd.DatetimeIndex(sorted(set().union(*[markets[m][0].index for m in names])))
    idx = idx[idx >= pd.Timestamp(start)]
    if end:
        idx = idx[idx <= pd.Timestamp(end)]
    P, T, R = {}, {}, {}
    for m in names:
        px, tl, rf, sp = markets[m]
        P[m] = px['Close'].reindex(idx.union(px.index)).ffill().reindex(idx).pct_change().fillna(0).values
        T[m] = tl.reindex(idx.union(tl.index)).ffill().reindex(idx).fillna(0).values
        R[m] = (rf.reindex(idx.union(rf.index)).ffill().reindex(idx).fillna(0).values
                if isinstance(rf, pd.Series) else np.full(len(idx), rf))
    gaps = np.r_[1, np.diff(idx.values).astype('timedelta64[D]').astype(int)]
    month_end = np.r_[idx[1:].month != idx[:-1].month, True]
    semi = np.r_[False, (idx[1:].month != idx[:-1].month) & np.isin(idx[1:].month, [1, 7])]

    cap = {m: init * weights[m] for m in names}
    L = {m: 0.0 for m in names}
    last_t = {m: None for m in names}
    pv = np.zeros(len(idx)); flows = np.zeros(len(idx)); levs = np.zeros(len(idx))
    trades, turnover = 0, 0.0
    for i in range(len(idx)):
        for m in names:
            sp = markets[m][3]
            rf_d = R[m][i] * gaps[i] / 365
            sp_d = sp * gaps[i] / 365
            r = P[m][i]
            net = rf_d + L[m] * (r - rf_d - sp_d)
            cap[m] *= (1 + net)
            L[m] = L[m] * (1 + r) / (1 + net) if (1 + net) > 0 else 0
        total = sum(cap.values())
        if len(names) > 1 and semi[i]:
            w_us = cap['US'] / total
            if w_us < 0.55 or w_us > 0.65:
                cap['US'], cap['TW'] = total * 0.6, total * 0.4
        recv = None
        if month_end[i] and cash > 0:
            total = sum(cap.values())
            recv = 'US' if cap['US'] / total < weights['US'] else 'TW'
            cap[recv] += cash
            flows[i] = cash
        total_before = sum(cap.values())
        for m in names:
            t = T[m][i]
            reb = (last_t[m] is None) or (t != last_t[m])
            if L[m] > t + trim:
                reb = True
            if rule == 'symmetric' and L[m] < t - band:
                reb = True
            if rule == 'payday_full' and month_end[i]:
                reb = True
            if rule == 'daily':
                reb = True
            if m == recv and not reb and cap[m] > 0:
                L[m] = ((cap[m] - cash) * L[m] + cash * t) / cap[m]
                turnover += cash * t / total_before
            if reb:
                diff = abs(L[m] - t)
                if diff > 0.005:
                    trades += 1
                turnover += cap[m] * diff / total_before
                cap[m] -= cap[m] * diff * cost
                L[m] = t
            last_t[m] = t
        total = sum(cap.values())
        pv[i] = total
        levs[i] = sum(cap[m] * L[m] for m in names) / total if total > 0 else 0
    pv = pd.Series(pv, index=idx)
    dr = (pv - flows) / pv.shift(1) - 1
    dr.iloc[0] = 0
    twr = (1 + dr).cumprod()
    return dict(pv=pv, twr=twr, lev=pd.Series(levs, index=idx), trades=trades, turnover=turnover)

def metrics(twr, rf=IRX):
    dr = twr.pct_change().dropna()
    yrs = (twr.index[-1] - twr.index[0]).days / 365.25
    ppy = len(dr) / yrs
    cagr = twr.iloc[-1] ** (1 / yrs) - 1
    mdd = (twr / twr.cummax() - 1).min()
    rf_d = rf.reindex(dr.index.union(rf.index)).ffill().reindex(dr.index).fillna(0) / ppy
    ex = dr - rf_d
    vol = dr.std() * np.sqrt(ppy)
    sharpe = ex.mean() * ppy / vol if vol > 0 else 0
    down = ex[ex < 0].std() * np.sqrt(ppy)
    sortino = ex.mean() * ppy / down if down > 0 else 0
    yr = twr.resample('YE').last()
    yr = pd.concat([pd.Series([twr.iloc[0]], index=[twr.index[0]]), yr]).pct_change().dropna()
    return dict(cagr=cagr, mdd=mdd, vol=vol, sharpe=sharpe, sortino=sortino,
                calmar=cagr / abs(mdd) if mdd else 0, worst_year=yr.min(), yrs=yrs, yearly=yr)
