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

US_P = dict(
    base=0.8, mx=3.0, f=5, s=220, bo=8, bo_up=8, bo_dn=8, cd=8,
    allocs=[0, .65, .85, 1], grace_days=0, ext=(1.16, 1.8), accel=None,
    deep_bear=(60, 0.97, 0.4), add_above_bear=True, pre_bull_boost=1.0
)
TW_P = dict(
    base=1.0, mx=3.0, f=8, s=220, bo=15, bo_up=15, bo_dn=25, cd=10,
    allocs=[0, .50, .75, .90, 1], grace_days=1, ext=None, accel=(40, 1.2, 252),
    deep_bear=(60, 0.97, 0.5), add_above_bear=False, pre_bull_boost=1.2
)

def target_lev(d, base, mx, f, s, bo=None, cd=5, allocs=None, grace_days=0,
               ext=None, accel=None, deep_bear=None, add_above_bear=False,
               pre_bull_boost=None, bo_up=None, bo_dn=None):
    if isinstance(bo, (tuple, list)):
        w_up, w_dn = bo[0], bo[1]
    else:
        w_up = bo_up if bo_up is not None else (bo if bo is not None else 10)
        w_dn = bo_dn if bo_dn is not None else (bo if bo is not None else 10)

    c = d['Close'].values
    F = d['Close'].rolling(f).mean().values
    S = d['Close'].rolling(s).mean().values
    H = d['Close'].shift(1).rolling(w_up).max().values
    Lo = d['Close'].shift(1).rolling(w_dn).min().values
    
    bear_ma = deep_bear[0] if deep_bear else (60 if (add_above_bear or pre_bull_boost is not None) else None)
    BMA = d['Close'].rolling(bear_ma).mean().values if bear_ma else None

    n = len(c)
    out = np.zeros(n)
    in_tr, step, last, below, def_days, acc_left = False, 0, -999, 0, 0, 0
    pre_bull_on = False
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
                pre_bull_on = False
                if accel and def_days >= accel[0]:
                    acc_left = accel[2]
                def_days = 0
            else:
                def_days += 1
        if in_tr and i - last >= cd:
            if p0 > h and step < len(allocs) - 1:
                if not add_above_bear or np.isnan(bma) or sf >= bma:
                    step += 1; last = i
            elif p0 < l and step > 0:
                step -= 1; last = i
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

def run(markets, start, end=None, rule='current', reb_rule=None, band=0.15, trim=0.15,
        cash_drag_us=False, init=15e6, cash=0, cost=1e-4, weights=None,
        semi_mode='adjust_notional', trim_lag=0):
    if reb_rule is not None:
        rule = reb_rule
    """
    統一資產負債表撮合引擎：
    - rule: 'current' (no_buy_down), 'legacy'
    - semi_mode: 'adjust_notional', 'cash_only', 'manual_sop'
    - trim_lag: 0 (當日修剪), 1 (隔日修剪，僅 L > t 才減倉，反彈不加買)
    """
    valid_rules = ('current', 'no_buy_down', 'legacy', 'symmetric', 'payday_full', 'daily')
    if rule not in valid_rules:
        raise ValueError(f"Unsupported rule: {rule}")
    valid_semi = ('adjust_notional', 'cash_only', 'band_50_70', 'manual_sop', 'pure_drift', 'semi_sub_reb')
    if semi_mode not in valid_semi:
        raise ValueError(f"Unsupported semi_mode: {semi_mode}")

    names = list(markets)
    weights = weights or ({'US': 0.6, 'TW': 0.4} if len(names) == 2 else {m: 1.0 / len(names) for m in names})
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
    trim_pending = {m: False for m in names}
    pv = np.zeros(len(idx)); flows = np.zeros(len(idx)); levs = np.zeros(len(idx))
    trades, turnover = 0, 0.0

    for i in range(len(idx)):
        # Step 1: 每日損益結算與自然漂移
        for m in names:
            sp = markets[m][3]
            rf_d = R[m][i] * gaps[i] / 365.0
            sp_d = sp * gaps[i] / 365.0
            r = P[m][i]
            rf_earn = (0.60 * rf_d) if (cash_drag_us and 'US' in m) else rf_d
            net = rf_earn + L[m] * (r - rf_d - sp_d)
            cap[m] *= (1.0 + net)
            L[m] = L[m] * (1.0 + r) / (1.0 + net) if (1.0 + net) > 0 else 0.0

        # ★ 關鍵：在任何本金移轉 (半年再平衡) 與新資金注入 (DCA) 之前，先鎖定各市場當日收盤的「真實舊名目部位」
        old_notional = {m: cap[m] * L[m] for m in names}
        total = sum(cap.values())

        # Step 2: 半年再平衡 (1月/7月初)
        if len(names) == 2 and semi[i]:
            w_us = cap['US'] / total if total > 0 else 0.6
            if semi_mode in ('adjust_notional', 'cash_only'):
                if w_us < 0.55 or w_us > 0.65:
                    for m, tw in [('US', 0.6), ('TW', 0.4)]:
                        new_c = total * tw
                        if semi_mode == 'cash_only':
                            L[m] = old_notional[m] / new_c if new_c > 0 else 0.0
                        cap[m] = new_c
            elif semi_mode in ('manual_sop', 'band_50_70'):
                if w_us < 0.50 or w_us > 0.70:
                    for m, tw in [('US', 0.6), ('TW', 0.4)]:
                        cap[m] = total * tw
        elif len(names) == 3 and semi[i]:
            w_us1 = cap['US1'] / total if total > 0 else 0.3
            w_us2 = cap['US2'] / total if total > 0 else 0.3
            w_us = w_us1 + w_us2
            if semi_mode == 'pure_drift':
                if w_us < 0.55 or w_us > 0.65:
                    ratio = (total * 0.6) / (cap['US1'] + cap['US2']) if (cap['US1'] + cap['US2']) > 0 else 0.0
                    cap['US1'] *= ratio
                    cap['US2'] *= ratio
                    cap['TW'] = total * 0.4
            elif semi_mode in ('adjust_notional', 'semi_sub_reb'):
                if w_us < 0.55 or w_us > 0.65 or abs(w_us1 - w_us2) > 0.05:
                    cap['US1'] = total * 0.3
                    cap['US2'] = total * 0.3
                    cap['TW'] = total * 0.4

        # Step 3: 月底新資金流分配 (DCA)
        recv = None
        if month_end[i] and cash > 0:
            total = sum(cap.values())
            if len(names) == 2:
                recv = 'US' if (cap['US'] / total < weights['US']) else 'TW'
            else:
                ratios = {m: cap[m] / (total * weights[m]) for m in names}
                recv = min(ratios, key=ratios.get)
            cap[recv] += cash
            L[recv] = old_notional[recv] / cap[recv] if cap[recv] > 0 else 0.0
            flows[i] = cash

        # Step 4: 換檔判定、DCA 計畫部位預算出、修剪封頂與統一費用結算
        total_before_fee = sum(cap.values())
        new_notional = {}
        for m in names:
            t = T[m][i]
            if rule in ('legacy', 'daily', 'payday_full', 'symmetric'):
                sig_reb = (last_t[m] is None) or (t != last_t[m])
                if rule == 'daily':
                    sig_reb = True
                elif rule == 'payday_full' and month_end[i]:
                    sig_reb = True
                elif rule == 'symmetric' and (L[m] < t - band):
                    sig_reb = True
            elif rule in ('current', 'no_buy_down'):
                sig_reb = (last_t[m] is None) or (t > last_t[m]) or (t < last_t[m] and L[m] > t)
            else:
                raise ValueError(f"Unsupported rule: {rule}")

            # 先計算若不觸發修剪時的「初步計畫名目部位 planned_N」與「計畫槓桿 planned_L」
            if sig_reb:
                planned_N = cap[m] * t
                trim_pending[m] = False
            elif m == recv and cash > 0:
                # 發薪日無換檔：初步計畫為「舊部位 + 新資金按目標 t 建倉」
                planned_N = old_notional[m] + cash * t
            else:
                planned_N = cap[m] * L[m]

            planned_L = planned_N / cap[m] if cap[m] > 0 else 0.0

            # 接著對 planned_L 進行超標修剪判定 (徹底防止入金稀釋後又加買導致突破 t + trim！)
            if not sig_reb:
                if trim_lag == 0:
                    if planned_L > t + trim:
                        planned_N = cap[m] * t
                else:  # trim_lag == 1 (隔日修剪 B3)
                    if trim_pending[m]:
                        if planned_L > t:
                            planned_N = cap[m] * t
                        trim_pending[m] = False
                        planned_L = planned_N / cap[m] if cap[m] > 0 else 0.0
                    if planned_L > t + trim:
                        trim_pending[m] = True

            new_notional[m] = planned_N
            last_t[m] = t

        if len(names) == 3 and 'US1' in names and 'US2' in names:
            delta_us = abs((new_notional['US1'] + new_notional['US2']) - (old_notional['US1'] + old_notional['US2']))
            if total_before_fee > 0 and (delta_us / total_before_fee) > 0.005:
                trades += 1
            turnover += delta_us / total_before_fee if total_before_fee > 0 else 0.0
            fee_us = delta_us * cost
            cap_us_tot = cap['US1'] + cap['US2']
            for um in ('US1', 'US2'):
                w_sub = cap[um] / cap_us_tot if cap_us_tot > 0 else 0.5
                cap[um] -= fee_us * w_sub
                L[um] = new_notional[um] / cap[um] if cap[um] > 0 else 0.0

            delta_tw = abs(new_notional['TW'] - old_notional['TW'])
            if total_before_fee > 0 and (delta_tw / total_before_fee) > 0.005:
                trades += 1
            turnover += delta_tw / total_before_fee if total_before_fee > 0 else 0.0
            cap['TW'] -= delta_tw * cost
            L['TW'] = new_notional['TW'] / cap['TW'] if cap['TW'] > 0 else 0.0
        else:
            for m in names:
                delta_n = abs(new_notional[m] - old_notional[m])
                if total_before_fee > 0 and (delta_n / total_before_fee) > 0.005:
                    trades += 1
                turnover += delta_n / total_before_fee if total_before_fee > 0 else 0.0
                cap[m] -= delta_n * cost
                L[m] = new_notional[m] / cap[m] if cap[m] > 0 else 0.0

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
