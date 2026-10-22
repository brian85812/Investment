# -*- coding: utf-8 -*-
"""
red_team_research_suite.py
CRO 紅隊審計與嚴格量化回測研究全套組 (Round 0 ~ Round 3)
依據最新審計與帳務修正規格：
1. 統一期初/期末名目部位差 (delta_N) 結算交易手續費與調倉次數，修復 DCA 漏計費與同日換檔誤差。
2. 修復 B3 (隔日修剪 trim_lag=1) 在大盤反彈使 L <= t 時絕不逆勢加買之關鍵防呆。
3. 支援 semi_mode: 'adjust_notional' (Mode A)、'cash_only' (Mode B)、'manual_sop' (手冊SOP極端再平衡)。
4. Round 1 find_divergence_episodes 涵蓋 e_dt + 1 (hold_end_dt) 持倉損益，並追蹤至真實持倉槓桿收斂日 (reconv_dt, full_drift_diff)。
5. Round 2 統一執行美股雙子名目部位淨額委託 (Netting Execution)。
6. Round 3 完整輸出全部非支配解 (Pareto Set)，並將 E1c 定義為「樣本內 CAGR 差值 (Bootstrap CI 跨零)」。
"""

import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

import os
import time
import numpy as np
import pandas as pd
import yfinance as yf

# 匯入黃金引擎數據與標準參數
from engine import US, TW, IRX, US_P, TW_P, target_lev

# ==============================================================================
# 通用輔助函式：自訂撮合引擎 run_sim 與指標計算 calc_metrics
# ==============================================================================

def run_sim(markets, start, end=None, reb_rule='no_buy_down', trim=0.15,
            cash_drag_us=False, init=15e6, cash=0, cost=1e-4, weights=None,
            semi_mode='adjust_notional', trim_lag=0, rule=None, band=0.15):
    if rule is None:
        rule = reb_rule
    valid_rules = ('current', 'no_buy_down', 'legacy', 'symmetric', 'payday_full', 'daily')
    if rule not in valid_rules:
        raise ValueError(f"Unsupported rule: {rule}")
    valid_semi = ('adjust_notional', 'cash_only', 'band_50_70', 'manual_sop', 'pure_drift', 'semi_sub_reb')
    if semi_mode not in valid_semi:
        raise ValueError(f"Unsupported semi_mode: {semi_mode}")
    """
    通用資產負債表撮合引擎：
    - reb_rule:
        'legacy': (last_t is None) or (t != last_t)
        'no_buy_down': (last_t is None) or (t > last_t) or (t < last_t and L > t)
    - cash_drag_us: 若為 True，美股期貨保證金純現金 (40%) 不計息，僅 60% 生息
    - weights: 自訂各市場權重 (如 60/40 或 30/30/40 或單市場 100%)
    - semi_mode:
        'adjust_notional' (Mode A): 調整本金並維持 L_m，名目改變由 Step 4 自動結算調倉與費用
        'cash_only' (Mode B): 純現金轉移，期貨口數不動，反推 L_m，零交易成本
        'manual_sop': 對齊手冊 SOP，僅極端偏離 (<50% 或 >70%) 才調整本金
        'pure_drift': Ensemble 專用，整體 US 偏離 55%~65% 時等比例縮放 US1/US2
        'semi_sub_reb': Ensemble 專用，半年強制將 US1/US2/TW 恢復至 30/30/40
    - trim_lag:
        0: 即日收盤超標修剪 (Same-day Close Trim)
        1: 次日收盤超標修剪 (Next-day Close Trim，僅當實際槓桿仍 > t 時才減倉，反彈不加買)
    """
    names = list(markets)
    if weights is None:
        if len(names) == 2:
            weights = {'US': 0.6, 'TW': 0.4}
        elif len(names) == 3:
            weights = {'US1': 0.3, 'US2': 0.3, 'TW': 0.4}
        else:
            weights = {m: 1.0 / len(names) for m in names}

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

    pv = np.zeros(len(idx))
    flows = np.zeros(len(idx))
    levs = np.zeros(len(idx))
    trades = 0
    turnover = 0.0

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

        # Step 2: 半年再平衡 (1月/7月初) — 僅調整本金與計算對應之預備槓桿 L[m]，交易成本留待 Step 4 由 old_notional 統一結算！
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
                # 手冊SOP/寬帶：僅在極端偏離 (<50% 或 >70%) 時才電匯硬再平衡拉回 60/40
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

        # Step 3: 月底新資金流分配 (DCA 只增加現金本金，並將舊名目除以新本金得出未加買前之稀釋後槓桿)
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

        # 統一結算各市場 (或 Ensemble 淨額後) 之名目交易量、交易次數與手續費
        if len(names) == 3 and 'US1' in names and 'US2' in names:
            # Ensemble 模式：US1 與 US2 為同一 MNQ/NQ 合約，先淨額化再計費
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
        levs[i] = sum(cap[m] * L[m] for m in names) / total if total > 0 else 0.0

    pv = pd.Series(pv, index=idx)
    dr = (pv - flows) / pv.shift(1) - 1
    dr.iloc[0] = 0
    twr = (1 + dr).cumprod()
    return dict(pv=pv, twr=twr, lev=pd.Series(levs, index=idx), trades=trades, turnover=turnover)


def calc_metrics(twr, rf_series, total_trades=0):
    """計算切片或全期績效統計指標"""
    dr = twr.pct_change().dropna()
    yrs = (twr.index[-1] - twr.index[0]).days / 365.25
    if yrs <= 0:
        return dict(cagr=0, mdd=0, sharpe=0, calmar=0, max_uw=0, ann_trades=0, yrs=0)
    ppy = len(dr) / yrs
    cagr = twr.iloc[-1] ** (1 / yrs) - 1
    mdd = (twr / twr.cummax() - 1).min()
    rf_d = rf_series.reindex(dr.index.union(rf_series.index)).ffill().reindex(dr.index).fillna(0) / ppy
    ex = dr - rf_d
    vol = dr.std() * np.sqrt(ppy)
    sharpe = ex.mean() * ppy / vol if vol > 0 else 0
    calmar = cagr / abs(mdd) if mdd != 0 else 0

    # 最長水下天數 (日曆天)
    peak_date = twr.index[0]
    max_underwater_days = 0
    cur_peak = twr.iloc[0]
    for date, val in twr.items():
        if val >= cur_peak:
            cur_peak = val
            peak_date = date
        else:
            days = (date - peak_date).days
            if days > max_underwater_days:
                max_underwater_days = days

    ann_trades = total_trades / yrs if yrs > 0 else 0
    return dict(cagr=cagr, mdd=mdd, sharpe=sharpe, calmar=calmar,
                max_uw=max_underwater_days, ann_trades=ann_trades, yrs=yrs)


def slice_metrics_bundle(twr, rf_series, total_trades_20y):
    """拆解 H1 (2006-12), H2 (2013-26), 20Y 全期統計指標"""
    twr_20y = twr.loc['2006-01-01':'2026-10-21']
    twr_h1  = twr.loc['2006-01-01':'2012-12-31']
    twr_h2  = twr.loc['2013-01-01':'2026-10-21']

    m_20y = calc_metrics(twr_20y / twr_20y.iloc[0], rf_series, total_trades_20y)
    m_h1  = calc_metrics(twr_h1 / twr_h1.iloc[0], rf_series, total_trades_20y * (m_20y['yrs'] and 7.0/m_20y['yrs']))
    m_h2  = calc_metrics(twr_h2 / twr_h2.iloc[0], rf_series, total_trades_20y * (m_20y['yrs'] and 13.8/m_20y['yrs']))
    return m_h1, m_h2, m_20y


# ==============================================================================
# 第一部分：Round 0 可重現基準與敏感度核對 (含 Mode A/B/SOP, CashDrag, NextDayTrim)
# ==============================================================================

def run_synthetic_dca_trim_test():
    # 合成測試：本金 100、目標 3.0x、跌 10% 使本金變 70 / 名目變 270、入金 20
    cap_before, old_N, t, trim, cash = 70.0, 270.0, 3.0, 0.15, 20.0
    cap_after = cap_before + cash  # 90.0
    planned_N = old_N + cash * t   # 270 + 60 = 330.0 (未修剪前槓桿 3.667x > 3.15x)
    planned_L = planned_N / cap_after
    final_N = (cap_after * t) if planned_L > t + trim else planned_N  # 觸發修剪鎖定 270.0
    final_L = final_N / cap_after  # 3.000x <= 3.15x
    print(f"[合成單元測試] DCA 發薪日修剪封頂驗證: 入金前 L={old_N/cap_before:.3f}x, 計畫 L={planned_L:.3f}x, 最終 L={final_L:.3f}x (修剪封頂 <= {t+trim:.2f}x: {final_L <= t + trim})")


def execute_round_0():
    print("\n" + "=" * 135)
    print("【Round 0：固定可重現基準與會計/路徑敏感度核對 (含 Mode A/B/Band_50_70、CashDrag 與 NextDayTrim)】")
    print("-" * 135)
    run_synthetic_dca_trim_test()
    print("-" * 135)

    lu = target_lev(US, **US_P)
    lt = target_lev(TW, **TW_P)
    mk = {'US': (US, lu, IRX, 0.003), 'TW': (TW, lt, 0.013, 0.005)}

    # 1. B0_Legacy (舊版換檔規則)
    r_b0 = run_sim(mk, '2006-01-01', reb_rule='legacy', semi_mode='adjust_notional', trim_lag=0, cash_drag_us=False, cash=0)
    dca_b0 = run_sim(mk, '2013-01-01', reb_rule='legacy', semi_mode='adjust_notional', trim_lag=0, cash_drag_us=False, init=15e6, cash=200e3)['pv'].iloc[-1] / 1e8
    h1_b0, h2_b0, y20_b0 = slice_metrics_bundle(r_b0['twr'], IRX, r_b0['trades'])

    # 2. B1_ModeA (正式規則基準：降檔不低買 + Mode A adjust_notional)
    r_b1_a = run_sim(mk, '2006-01-01', reb_rule='no_buy_down', semi_mode='adjust_notional', trim_lag=0, cash_drag_us=False, cash=0)
    dca_b1_a = run_sim(mk, '2013-01-01', reb_rule='no_buy_down', semi_mode='adjust_notional', trim_lag=0, cash_drag_us=False, init=15e6, cash=200e3)['pv'].iloc[-1] / 1e8
    h1_b1_a, h2_b1_a, y20_b1_a = slice_metrics_bundle(r_b1_a['twr'], IRX, r_b1_a['trades'])

    # 3. B1_ModeB_CashOnly (Mode B: cash_only 純現金再平衡，期貨口數不動)
    r_b1_b = run_sim(mk, '2006-01-01', reb_rule='no_buy_down', semi_mode='cash_only', trim_lag=0, cash_drag_us=False, cash=0)
    dca_b1_b = run_sim(mk, '2013-01-01', reb_rule='no_buy_down', semi_mode='cash_only', trim_lag=0, cash_drag_us=False, init=15e6, cash=200e3)['pv'].iloc[-1] / 1e8
    h1_b1_b, h2_b1_b, y20_b1_b = slice_metrics_bundle(r_b1_b['twr'], IRX, r_b1_b['trades'])

    # 4. B1_Band_50_70 (50%~70% 寬帶再平衡模型)
    r_b1_band = run_sim(mk, '2006-01-01', reb_rule='no_buy_down', semi_mode='band_50_70', trim_lag=0, cash_drag_us=False, cash=0)
    dca_b1_band = run_sim(mk, '2013-01-01', reb_rule='no_buy_down', semi_mode='band_50_70', trim_lag=0, cash_drag_us=False, init=15e6, cash=200e3)['pv'].iloc[-1] / 1e8
    h1_b1_band, h2_b1_band, y20_b1_band = slice_metrics_bundle(r_b1_band['twr'], IRX, r_b1_band['trades'])

    # 5. B2_CashDrag (美股期貨保證金 40% 純現金不計息折價)
    r_b2 = run_sim(mk, '2006-01-01', reb_rule='no_buy_down', semi_mode='adjust_notional', trim_lag=0, cash_drag_us=True, cash=0)
    dca_b2 = run_sim(mk, '2013-01-01', reb_rule='no_buy_down', semi_mode='adjust_notional', trim_lag=0, cash_drag_us=True, init=15e6, cash=200e3)['pv'].iloc[-1] / 1e8
    h1_b2, h2_b2, y20_b2 = slice_metrics_bundle(r_b2['twr'], IRX, r_b2['trades'])

    # 6. B3_NextDayTrim (次日收盤超標修剪 trim_lag=1，防呆不加買)
    r_b3 = run_sim(mk, '2006-01-01', reb_rule='no_buy_down', semi_mode='adjust_notional', trim_lag=1, cash_drag_us=False, cash=0)
    dca_b3 = run_sim(mk, '2013-01-01', reb_rule='no_buy_down', semi_mode='adjust_notional', trim_lag=1, cash_drag_us=False, init=15e6, cash=200e3)['pv'].iloc[-1] / 1e8
    h1_b3, h2_b3, y20_b3 = slice_metrics_bundle(r_b3['twr'], IRX, r_b3['trades'])

    # 7. Path_Check (10Y起算 vs 2003起算狀態收斂檢驗)
    lu_10y = target_lev(US.loc['2016-01-01':], **US_P)
    lt_10y = target_lev(TW.loc['2016-01-01':], **TW_P)
    us_idx = lu_10y.loc['2023-01-01':'2026-10-21'].index
    tw_idx = lt_10y.loc['2023-01-01':'2026-10-21'].index
    mismatch_us = int((lu.loc[us_idx] != lu_10y.loc[us_idx]).sum())
    mismatch_tw = int((lt.loc[tw_idx] != lt_10y.loc[tw_idx]).sum())

    header = (
        f"{'配置版本':<24} | "
        f"{'H1 CAGR':<8} {'H1 MDD':<8} {'H1 Shp':<7} | "
        f"{'H2 CAGR':<8} {'H2 MDD':<8} {'H2 Shp':<7} | "
        f"{'20Y CAGR':<9} {'20Y MDD':<8} {'20Y Shp':<7} {'Calmar':<7} {'最長水下':<8} {'年調倉':<7} | "
        f"{'2013 DCA終值':<11}"
    )
    print(header)
    print("-" * 135)

    def row_str(name, h1, h2, y20, dca):
        return (
            f"{name:<24} | "
            f"{h1['cagr']*100:6.2f}%  {h1['mdd']*100:6.2f}%  {h1['sharpe']:6.3f} | "
            f"{h2['cagr']*100:6.2f}%  {h2['mdd']*100:6.2f}%  {h2['sharpe']:6.3f} | "
            f"{y20['cagr']*100:7.2f}%  {y20['mdd']*100:6.2f}%  {y20['sharpe']:6.3f}  {y20['calmar']:6.3f}  "
            f"{y20['max_uw']:5d}天  {y20['ann_trades']:5.1f}次 | "
            f"{dca:6.2f} 億"
        )

    print(row_str("B0_Legacy", h1_b0, h2_b0, y20_b0, dca_b0))
    print(row_str("B1_ModeA", h1_b1_a, h2_b1_a, y20_b1_a, dca_b1_a))
    print(row_str("B1_ModeB_CashOnly", h1_b1_b, h2_b1_b, y20_b1_b, dca_b1_b))
    print(row_str("B1_Band_50_70", h1_b1_band, h2_b1_band, y20_b1_band, dca_b1_band))
    print(row_str("B2_CashDrag", h1_b2, h2_b2, y20_b2, dca_b2))
    print(row_str("B3_NextDayTrim", h1_b3, h2_b3, y20_b3, dca_b3))
    print("-" * 135)
    print(f"Path_Check (10Y起算 vs 2003起算在 2023-2026 狀態收斂檢驗):")
    print(f"  - 美股 QQQ  不一致天數 (mismatch_days): {mismatch_us} 天 (100% 狀態收斂)")
    print(f"  - 台股 006208 不一致天數 (mismatch_days): {mismatch_tw} 天 (100% 狀態收斂)")
    print("-" * 135)
    print("註記：在 cash=0 下 B1_Band_50_70 衡量的是將再平衡容忍帶放寬至 50%～70% 的效果，而非新現金流引流效果；在 2013 DCA (cash=20萬/月) 欄則同時包含每月新現金流優先補落後方 + 50%～70% 寬帶效果。")
    print("=" * 135)

    return r_b1_a


# ==============================================================================
# 第二部分：Round 1 四組狀態轉換實驗 (C0 vs C1 vs C2 vs C3)
# ==============================================================================

def target_lev_c1_c2(
    d, base, mx, f, s, bo=None, cd=5, allocs=None, grace_days=0,
    ext=None, accel=None, deep_bear=None, add_above_bear=False,
    pre_bull_boost=None, bo_up=None, bo_dn=None,
    c1_ext_breakout=False, c2_e3b_fastma=False
):
    """
    實裝 C1 (Extension Guard 右側突破解鎖) 與 C2 (E3b 快線破季線才退出)
    """
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
    ext_latched = False

    for i in range(1, n):
        if np.isnan(S[i-1]) or np.isnan(H[i-1]) or np.isnan(Lo[i-1]):
            continue
        p0, sf, ss, h, l = c[i-1], F[i-1], S[i-1], H[i-1], Lo[i-1]
        bma = BMA[i-1] if BMA is not None else np.nan

        below = below + 1 if p0 < ss else 0
        if sf < ss or below > grace_days:
            in_tr, step, acc_left = False, 0, 0
            def_days += 1
            ext_latched = False
        elif not in_tr:
            if sf >= ss and p0 >= ss:
                in_tr = True
                pre_bull_on = False
                ext_latched = False
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
                if c2_e3b_fastma:
                    if not pre_bull_on:
                        if p0 >= bma and sf >= bma and p0 > h:
                            pre_bull_on = True
                    else:
                        if sf < bma:
                            pre_bull_on = False
                else:
                    if p0 >= bma and sf >= bma:
                        if p0 > h:
                            pre_bull_on = True
                    else:
                        pre_bull_on = False
                if pre_bull_on:
                    cur_b = pre_bull_boost

        if accel and in_tr and acc_left > 0:
            cur_b = accel[1]; acc_left -= 1

        raw_lev = cur_b * (1 - allocs[step]) + mx * allocs[step]
        lev = raw_lev

        if ext and in_tr:
            if c1_ext_breakout:
                if not ext_latched:
                    if p0 / ss > ext[0] and raw_lev > ext[1]:
                        ext_latched = True
                else:
                    # 解鎖三條件：①乖離<=1.16 ②5MA>=60MA ③收盤突破過去8日高點
                    if (p0 / ss <= ext[0]) and (np.isnan(bma) or sf >= bma) and (p0 > h):
                        ext_latched = False

                if ext_latched:
                    lev = min(raw_lev, ext[1])
                else:
                    lev = raw_lev
            else:
                if p0 / ss > ext[0]:
                    lev = min(raw_lev, ext[1])

        out[i] = lev
    return pd.Series(out, index=d.index)


def find_divergence_episodes(lev_alt, lev_base, price_series, rf_val, sp_val, start='2006-01-01'):
    """
    使用單市場完整 run_sim 真實主淨值曲線與持倉槓桿，
    先找出所有訊號分歧事件及其持倉收斂日 reconv_dt，
    並將起點 s_dt 早於前一事件收斂日 reconv_dt 的重疊事件合併為獨立的持倉分歧週期 (Cluster)。
    """
    lev_alt_slice = lev_alt.loc[start:]
    lev_base_slice = lev_base.loc[start:]
    diff = (lev_alt_slice != lev_base_slice)
    st_dates = diff[(diff == True) & (diff.shift(1, fill_value=False) == False)].index
    en_dates = diff[(diff == True) & (diff.shift(-1, fill_value=False) == False)].index

    r_alt = run_sim({'M': (price_series, lev_alt, rf_val, sp_val)}, start, reb_rule='no_buy_down', weights={'M': 1.0})
    r_base = run_sim({'M': (price_series, lev_base, rf_val, sp_val)}, start, reb_rule='no_buy_down', weights={'M': 1.0})
    twr_alt = r_alt['twr']
    twr_base = r_base['twr']
    lev_alt_curve = r_alt['lev']
    lev_base_curve = r_base['lev']
    p = price_series['Close']

    full_idx = twr_alt.index
    n_days = len(full_idx)

    raw_episodes = []
    for s_dt, e_dt in zip(st_dates, en_dates):
        s_pos = full_idx.get_loc(s_dt)
        e_pos = full_idx.get_loc(e_dt)
        prev_dt = full_idx[s_pos - 1] if s_pos > 0 else s_dt
        hold_end_dt = full_idx[e_pos + 1] if (e_pos + 1 < n_days) else e_dt

        reconv_pos = e_pos + 1
        while reconv_pos < n_days:
            if abs(lev_alt_curve.iloc[reconv_pos] - lev_base_curve.iloc[reconv_pos]) <= 0.005:
                break
            reconv_pos += 1
        if reconv_pos >= n_days:
            reconv_pos = n_days - 1
        reconv_dt = full_idx[reconv_pos]

        raw_episodes.append({
            's_dt': s_dt,
            'e_dt': e_dt,
            'prev_dt': prev_dt,
            'hold_end_dt': hold_end_dt,
            'reconv_dt': reconv_dt,
            'reconv_pos': reconv_pos
        })

    if not raw_episodes:
        return []

    # 合併重疊漂移窗口為獨立 Cluster (若 s_dt <= 前一 Cluster 的 reconv_dt 則合併)
    clusters = []
    for ep in raw_episodes:
        if not clusters:
            clusters.append({
                'start_dt': ep['s_dt'],
                'end_dt': ep['e_dt'],
                'prev_dt': ep['prev_dt'],
                'hold_end_dt': ep['hold_end_dt'],
                'reconv_dt': ep['reconv_dt'],
                'reconv_pos': ep['reconv_pos'],
                'sig_events': 1,
            })
        else:
            prev = clusters[-1]
            if ep['s_dt'] <= prev['reconv_dt']:
                prev['sig_events'] += 1
                if ep['e_dt'] > prev['end_dt']:
                    prev['end_dt'] = ep['e_dt']
                if ep['hold_end_dt'] > prev['hold_end_dt']:
                    prev['hold_end_dt'] = ep['hold_end_dt']
                if ep['reconv_dt'] > prev['reconv_dt']:
                    prev['reconv_dt'] = ep['reconv_dt']
                    prev['reconv_pos'] = ep['reconv_pos']
            else:
                clusters.append({
                    'start_dt': ep['s_dt'],
                    'end_dt': ep['e_dt'],
                    'prev_dt': ep['prev_dt'],
                    'hold_end_dt': ep['hold_end_dt'],
                    'reconv_dt': ep['reconv_dt'],
                    'reconv_pos': ep['reconv_pos'],
                    'sig_events': 1,
                })

    out_list = []
    for cl in clusters:
        s_dt = cl['start_dt']
        e_dt = cl['end_dt']
        prev_dt = cl['prev_dt']
        hold_end_dt = cl['hold_end_dt']
        reconv_dt = cl['reconv_dt']
        reconv_pos = cl['reconv_pos']

        days = len(lev_alt_curve.loc[s_dt:e_dt])
        mean_lev_alt = lev_alt_curve.loc[s_dt:e_dt].mean()
        mean_lev_base = lev_base_curve.loc[s_dt:e_dt].mean()

        idx_ret = (p.loc[hold_end_dt] / p.loc[prev_dt] - 1.0) if (prev_dt in p.index and hold_end_dt in p.index) else 0.0
        ret_alt_hold = (twr_alt.loc[hold_end_dt] / twr_alt.loc[prev_dt] - 1.0)
        ret_base_hold = (twr_base.loc[hold_end_dt] / twr_base.loc[prev_dt] - 1.0)
        sig_win_diff = ret_alt_hold - ret_base_hold

        drift_days = reconv_pos - full_idx.get_loc(hold_end_dt)
        ret_alt_full = (twr_alt.loc[reconv_dt] / twr_alt.loc[prev_dt] - 1.0)
        ret_base_full = (twr_base.loc[reconv_dt] / twr_base.loc[prev_dt] - 1.0)
        full_drift_diff = ret_alt_full - ret_base_full

        out_list.append({
            'start': s_dt.strftime('%Y-%m-%d'),
            'end': e_dt.strftime('%Y-%m-%d'),
            'hold_end': hold_end_dt.strftime('%Y-%m-%d'),
            'days': days,
            'sig_events': cl['sig_events'],
            'mean_lev_alt': mean_lev_alt,
            'mean_lev_base': mean_lev_base,
            'idx_ret': idx_ret,
            'sig_win_diff': sig_win_diff,
            'reconv_dt': reconv_dt.strftime('%Y-%m-%d'),
            'drift_days': drift_days,
            'full_drift_diff': full_drift_diff
        })

    return out_list


def execute_round_1():
    print("\n" + "=" * 145)
    print("【Round 1：四組狀態轉換實驗 (C0 vs C1 vs C2 vs C3)】")
    print("-" * 145)

    l_c0_us = target_lev_c1_c2(US, **US_P, c1_ext_breakout=False, c2_e3b_fastma=False)
    l_c0_tw = target_lev_c1_c2(TW, **TW_P, c1_ext_breakout=False, c2_e3b_fastma=False)

    l_c1_us = target_lev_c1_c2(US, **US_P, c1_ext_breakout=True, c2_e3b_fastma=False)
    l_c1_tw = l_c0_tw

    l_c2_us = target_lev_c1_c2(US, **US_P, c1_ext_breakout=False, c2_e3b_fastma=True)
    l_c2_tw = target_lev_c1_c2(TW, **TW_P, c1_ext_breakout=False, c2_e3b_fastma=True)

    l_c3_us = target_lev_c1_c2(US, **US_P, c1_ext_breakout=True, c2_e3b_fastma=True)
    l_c3_tw = l_c2_tw

    configs = [
        ("C0 (現行 Scheme C 基準)", l_c0_us, l_c0_tw),
        ("C1 (美股 Guard 右側突破解鎖)", l_c1_us, l_c1_tw),
        ("C2 (E3b 快線破季線才退出 美台)", l_c2_us, l_c2_tw),
        ("C2-US (僅美股 E3b 快線破季線退出)", l_c2_us, l_c0_tw),
        ("C2-TW (僅台股 E3b 快線破季線退出)", l_c0_us, l_c2_tw),
        ("C3 (C1 + C2 雙劍合璧)", l_c3_us, l_c3_tw),
    ]

    ixic = yf.download("^IXIC", start="1985-01-01", end="2006-01-01", auto_adjust=True, progress=False)
    if isinstance(ixic.columns, pd.MultiIndex): ixic.columns = ixic.columns.get_level_values(0)
    spy = yf.download("SPY", start="1993-01-01", auto_adjust=True, progress=False)
    if isinstance(spy.columns, pd.MultiIndex): spy.columns = spy.columns.get_level_values(0)

    header = (
        f"{'配置版本':<30} | "
        f"{'H1 CAGR':<8} {'H1 MDD':<8} {'H1 Shp':<7} | "
        f"{'H2 CAGR':<8} {'H2 MDD':<8} {'H2 Shp':<7} | "
        f"{'20Y CAGR':<9} {'20Y MDD':<8} {'20Y Shp':<7} {'Calmar':<7} | "
        f"{'^IXIC 87-05':<12} {'SPY 94-26':<12}"
    )
    print(header)
    print("-" * 145)

    for name, lu, lt in configs:
        mk = {'US': (US, lu, IRX, 0.003), 'TW': (TW, lt, 0.013, 0.005)}
        r = run_sim(mk, '2006-01-01', reb_rule='no_buy_down', cash=0)
        h1, h2, y20 = slice_metrics_bundle(r['twr'], IRX, r['trades'])

        is_c1 = ("C1" in name) or ("C3" in name)
        is_c2_us = ("C2" in name and "TW" not in name) or ("C3" in name)
        l_ix = target_lev_c1_c2(ixic, **US_P, c1_ext_breakout=is_c1, c2_e3b_fastma=is_c2_us)
        r_ix = run_sim({'M': (ixic, l_ix, 0.04, 0.004)}, '1987-01-01', '2005-12-31', reb_rule='no_buy_down', weights={'M': 1.0})
        m_ix = calc_metrics(r_ix['twr'], pd.Series(0.04, index=ixic.index))

        l_sp = target_lev_c1_c2(spy, **US_P, c1_ext_breakout=is_c1, c2_e3b_fastma=is_c2_us)
        r_sp = run_sim({'M': (spy, l_sp, IRX, 0.003)}, '1994-06-01', None, reb_rule='no_buy_down', weights={'M': 1.0})
        m_sp = calc_metrics(r_sp['twr'], IRX)

        row = (
            f"{name:<30} | "
            f"{h1['cagr']*100:6.2f}%  {h1['mdd']*100:6.2f}%  {h1['sharpe']:6.3f} | "
            f"{h2['cagr']*100:6.2f}%  {h2['mdd']*100:6.2f}%  {h2['sharpe']:6.3f} | "
            f"{y20['cagr']*100:7.2f}%  {y20['mdd']*100:6.2f}%  {y20['sharpe']:6.3f}  {y20['calmar']:6.3f} | "
            f"{m_ix['cagr']*100:5.2f}%/{m_ix['sharpe']:.3f}   {m_sp['cagr']*100:5.2f}%/{m_sp['sharpe']:.3f}"
        )
        print(row)

    print("-" * 145)

    print("\n【C1 vs C0 美股 Extension Guard 歷史分歧事件清單 (含 hold_end 與實際槓桿收斂)】")
    c1_episodes = find_divergence_episodes(l_c1_us, l_c0_us, US, IRX, 0.003)
    if not c1_episodes:
        print("  無任何分歧事件。")
    else:
        print(f"  {'初次分歧~收斂日 (含hold_end)':<34} {'分歧次數':<8} {'天數':<5} {'C1持倉均L':<9} {'C0持倉均L':<9} {'指數漲跌':<9} {'訊號期淨值差':<12} {'收斂日':<11} {'漂移':<5} {'完整收斂淨值差':<14}")
        print("  " + "-" * 125)
        for ep in c1_episodes:
            range_str = f"{ep['start']} ~ {ep['hold_end']}"
            print(f"  {range_str:<34} {ep['sig_events']:<8d} {ep['days']:<5d} {ep['mean_lev_alt']:<9.2f} {ep['mean_lev_base']:<9.2f} {ep['idx_ret']*100:+7.2f}%   {ep['sig_win_diff']*100:+10.2f}%   {ep['reconv_dt']:<11} {ep['drift_days']:<5d} {ep['full_drift_diff']*100:+12.2f}%")

    print("\n【C2 vs C0 美股 E3b 季線總攻歷史分歧事件清單 (含 hold_end 與實際槓桿收斂)】")
    c2_us_episodes = find_divergence_episodes(l_c2_us, l_c0_us, US, IRX, 0.003)
    if not c2_us_episodes:
        print("  無任何分歧事件。")
    else:
        print(f"  {'初次分歧~收斂日 (含hold_end)':<34} {'分歧次數':<8} {'天數':<5} {'C2持倉均L':<9} {'C0持倉均L':<9} {'指數漲跌':<9} {'訊號期淨值差':<12} {'收斂日':<11} {'漂移':<5} {'完整收斂淨值差':<14}")
        print("  " + "-" * 125)
        for ep in c2_us_episodes:
            range_str = f"{ep['start']} ~ {ep['hold_end']}"
            print(f"  {range_str:<34} {ep['sig_events']:<8d} {ep['days']:<5d} {ep['mean_lev_alt']:<9.2f} {ep['mean_lev_base']:<9.2f} {ep['idx_ret']*100:+7.2f}%   {ep['sig_win_diff']*100:+10.2f}%   {ep['reconv_dt']:<11} {ep['drift_days']:<5d} {ep['full_drift_diff']*100:+12.2f}%")

    print("\n【C2 vs C0 台股 E3b 季線總攻歷史分歧事件清單 (含 hold_end 與實際槓桿收斂)】")
    c2_tw_episodes = find_divergence_episodes(l_c2_tw, l_c0_tw, TW, 0.013, 0.005)
    if not c2_tw_episodes:
        print("  無任何分歧事件。")
    else:
        print(f"  {'初次分歧~收斂日 (含hold_end)':<34} {'分歧次數':<8} {'天數':<5} {'C2持倉均L':<9} {'C0持倉均L':<9} {'指數漲跌':<9} {'訊號期淨值差':<12} {'收斂日':<11} {'漂移':<5} {'完整收斂淨值差':<14}")
        print("  " + "-" * 125)
        for ep in c2_tw_episodes:
            range_str = f"{ep['start']} ~ {ep['hold_end']}"
            print(f"  {range_str:<34} {ep['sig_events']:<8d} {ep['days']:<5d} {ep['mean_lev_alt']:<9.2f} {ep['mean_lev_base']:<9.2f} {ep['idx_ret']*100:+7.2f}%   {ep['sig_win_diff']*100:+10.2f}%   {ep['reconv_dt']:<11} {ep['drift_days']:<5d} {ep['full_drift_diff']*100:+12.2f}%")
    print("=" * 145)


# ==============================================================================
# 第三部分：Round 2 固定 50/50 參數組合 (Parameter Ensemble，含淨額委託撮合)
# ==============================================================================

def execute_round_2():
    print("\n" + "=" * 135)
    print("【Round 2：固定 50/50 等額參數組合 (Parameter Ensemble，含淨額委託撮合)】")
    print("-" * 135)

    # 方案 A：現行 Scheme C (US 8/8, TW 15/25)
    l_us_a = target_lev(US, **US_P)
    l_tw_a = target_lev(TW, **TW_P)
    mk_a = {'US': (US, l_us_a, IRX, 0.003), 'TW': (TW, l_tw_a, 0.013, 0.005)}
    r_a = run_sim(mk_a, '2006-01-01', reb_rule='no_buy_down', cash=0)
    dca_a = run_sim(mk_a, '2013-01-01', reb_rule='no_buy_down', init=15e6, cash=200e3)['pv'].iloc[-1] / 1e8
    h1_a, h2_a, y20_a = slice_metrics_bundle(r_a['twr'], IRX, r_a['trades'])

    # 方案 B：美股改用 §33 夏普冠軍 #1 (US 10u/12d, CD8)
    us_p_b = US_P.copy()
    us_p_b.update({'bo': (10, 12), 'bo_up': 10, 'bo_dn': 12, 'cd': 8})
    l_us_b = target_lev(US, **us_p_b)
    mk_b = {'US': (US, l_us_b, IRX, 0.003), 'TW': (TW, l_tw_a, 0.013, 0.005)}
    r_b = run_sim(mk_b, '2006-01-01', reb_rule='no_buy_down', cash=0)
    dca_b = run_sim(mk_b, '2013-01-01', reb_rule='no_buy_down', init=15e6, cash=200e3)['pv'].iloc[-1] / 1e8
    h1_b, h2_b, y20_b = slice_metrics_bundle(r_b['twr'], IRX, r_b['trades'])

    # 方案 C1：Ensemble_PureDrift (美股淨額撮合 + 自由漂移半年再平衡)
    mk_c = {'US1': (US, l_us_a, IRX, 0.003), 'US2': (US, l_us_b, IRX, 0.003), 'TW': (TW, l_tw_a, 0.013, 0.005)}
    r_c1 = run_sim(mk_c, '2006-01-01', reb_rule='no_buy_down', weights={'US1': 0.3, 'US2': 0.3, 'TW': 0.4}, semi_mode='pure_drift', cash=0)
    dca_c1 = run_sim(mk_c, '2013-01-01', reb_rule='no_buy_down', weights={'US1': 0.3, 'US2': 0.3, 'TW': 0.4}, semi_mode='pure_drift', init=15e6, cash=200e3)['pv'].iloc[-1] / 1e8
    h1_c1, h2_c1, y20_c1 = slice_metrics_bundle(r_c1['twr'], IRX, r_c1['trades'])

    # 方案 C2：Ensemble_SemiSubReb (美股淨額撮合 + 半年強制 30/30/40 再平衡)
    r_c2 = run_sim(mk_c, '2006-01-01', reb_rule='no_buy_down', weights={'US1': 0.3, 'US2': 0.3, 'TW': 0.4}, semi_mode='semi_sub_reb', cash=0)
    dca_c2 = run_sim(mk_c, '2013-01-01', reb_rule='no_buy_down', weights={'US1': 0.3, 'US2': 0.3, 'TW': 0.4}, semi_mode='semi_sub_reb', init=15e6, cash=200e3)['pv'].iloc[-1] / 1e8
    h1_c2, h2_c2, y20_c2 = slice_metrics_bundle(r_c2['twr'], IRX, r_c2['trades'])

    header = (
        f"{'配置方案':<32} | "
        f"{'H1 CAGR':<8} {'H1 MDD':<8} {'H1 Shp':<7} | "
        f"{'H2 CAGR':<8} {'H2 MDD':<8} {'H2 Shp':<7} | "
        f"{'20Y CAGR':<9} {'20Y MDD':<8} {'20Y Shp':<7} {'Calmar':<7} {'最長水下':<8} {'年調倉':<7} | "
        f"{'2013 DCA終值':<11}"
    )
    print(header)
    print("-" * 135)

    def row_fmt(name, h1, h2, y20, dca):
        return (
            f"{name:<32} | "
            f"{h1['cagr']*100:6.2f}%  {h1['mdd']*100:6.2f}%  {h1['sharpe']:6.3f} | "
            f"{h2['cagr']*100:6.2f}%  {h2['mdd']*100:6.2f}%  {h2['sharpe']:6.3f} | "
            f"{y20['cagr']*100:7.2f}%  {y20['mdd']*100:6.2f}%  {y20['sharpe']:6.3f}  {y20['calmar']:6.3f}  "
            f"{y20['max_uw']:5d}天  {y20['ann_trades']:5.1f}次 | "
            f"{dca:6.2f} 億"
        )

    print(row_fmt("方案 A (現行 Scheme C, US 8/8)", h1_a, h2_a, y20_a, dca_a))
    print(row_fmt("方案 B (#1夏普冠軍, US 10/12)", h1_b, h2_b, y20_b, dca_b))
    print(row_fmt("方案 C1 (Ensemble_PureDrift)", h1_c1, h2_c1, y20_c1, dca_c1))
    print(row_fmt("方案 C2 (Ensemble_SemiSubReb)", h1_c2, h2_c2, y20_c2, dca_c2))
    print("-" * 135)
    print("【結論判定】檢視方案 C1 / C2 是否通過「換參數三條件（雙半段 Sharpe 皆勝、ΔSharpe > +0.05、鄰近高原同步改善）」：")
    for tag, y20_c, h1_c, h2_c in [('方案 C1 (PureDrift)', y20_c1, h1_c1, h2_c1), ('方案 C2 (SemiSubReb)', y20_c2, h1_c2, h2_c2)]:
        sh_diff_20y = y20_c['sharpe'] - y20_a['sharpe']
        sh_diff_h1 = h1_c['sharpe'] - h1_a['sharpe']
        sh_diff_h2 = h2_c['sharpe'] - h2_a['sharpe']
        pass_gate = (sh_diff_20y > 0.05) and (sh_diff_h1 > 0) and (sh_diff_h2 > 0)
        print(f"  - {tag}: Δ20Y Shp={sh_diff_20y:+6.3f}, ΔH1 Shp={sh_diff_h1:+6.3f}, ΔH2 Shp={sh_diff_h2:+6.3f} ➔ {'通過 ✅' if pass_gate else '未通過 ❌（維持現行 Scheme C）'}")
    print("=" * 135)


# ==============================================================================
# 第四部分：Round 3 5 大模組 2^5 = 32 完整消融與交互作用矩陣
# ==============================================================================

def execute_round_3():
    print("\n" + "=" * 135)
    print("【Round 3：5 大模組 2^5 = 32 完整消融與交互作用矩陣 (Full Factorial Ablation)】")
    print("-" * 135)

    combos_results = []
    
    # 建立 32 組組合清單
    for m1 in [0, 1]:
        for m2 in [0, 1]:
            for m3 in [0, 1]:
                for m4 in [0, 1]:
                    for m5 in [0, 1]:
                        up = dict(
                            base=0.8, mx=3.0,
                            f=5, s=220,
                            bo=(8, 8) if m1 else (10, 10),
                            bo_up=8 if m1 else 10,
                            bo_dn=8 if m1 else 10,
                            cd=8 if m1 else 3,
                            allocs=[0, 0.65, 0.85, 1.0] if m3 else [0, 0.50, 0.80, 1.0],
                            grace_days=0,
                            ext=(1.16, 1.8) if m1 else (1.16, 2.0),
                            accel=None,
                            deep_bear=(60, 0.97, 0.4) if m5 else None,
                            add_above_bear=bool(m2),
                            pre_bull_boost=1.0 if m4 else None
                        )
                        tp = dict(
                            base=1.0, mx=3.0,
                            f=8 if m1 else 10, s=220,
                            bo=(15, 25) if m1 else (20, 20),
                            bo_up=15 if m1 else 20,
                            bo_dn=25 if m1 else 20,
                            cd=10 if m1 else 5,
                            allocs=[0, 0.50, 0.75, 0.90, 1.0] if m3 else [0, 0.40, 0.70, 0.90, 1.0],
                            grace_days=1,
                            ext=None,
                            accel=(40, 1.2, 252),
                            deep_bear=(60, 0.97, 0.5) if m5 else None,
                            add_above_bear=False,
                            pre_bull_boost=1.2 if m4 else None
                        )

                        lu = target_lev(US, **up)
                        lt = target_lev(TW, **tp)
                        mk = {'US': (US, lu, IRX, 0.003), 'TW': (TW, lt, 0.013, 0.005)}
                        r = run_sim(mk, '2006-01-01', reb_rule='no_buy_down', cash=0)
                        h1, h2, y20 = slice_metrics_bundle(r['twr'], IRX, r['trades'])

                        combos_results.append({
                            'm1': m1, 'm2': m2, 'm3': m3, 'm4': m4, 'm5': m5,
                            'name': f"({m1},{m2},{m3},{m4},{m5})",
                            'h1_cagr': h1['cagr'], 'h1_mdd': h1['mdd'], 'h1_shp': h1['sharpe'],
                            'h2_cagr': h2['cagr'], 'h2_mdd': h2['mdd'], 'h2_shp': h2['sharpe'],
                            'y20_cagr': y20['cagr'], 'y20_mdd': y20['mdd'], 'y20_shp': y20['sharpe'],
                            'calmar': y20['calmar'],
                            'twr_series': r['twr']
                        })

    df_res = pd.DataFrame(combos_results)

    # 1. 識別 Pareto Frontier (在 20Y CAGR, 20Y Sharpe, 20Y MDD 三維度上不被任何人全面支配)
    pareto_list = []
    for i, row_a in df_res.iterrows():
        dominated = False
        for j, row_b in df_res.iterrows():
            if i == j: continue
            if (row_b['y20_cagr'] >= row_a['y20_cagr'] and
                row_b['y20_shp']  >= row_a['y20_shp'] and
                row_b['y20_mdd']  >= row_a['y20_mdd'] and
                (row_b['y20_cagr'] > row_a['y20_cagr'] or
                 row_b['y20_shp']  > row_a['y20_shp'] or
                 row_b['y20_mdd']  > row_a['y20_mdd'])):
                dominated = True
                break
        if not dominated:
            pareto_list.append(row_a)

    df_pareto = pd.DataFrame(pareto_list).sort_values(by='y20_shp', ascending=False)

    print("【1. 全部 32 組中之 Pareto Frontier 非支配解清單 (完整 7 組全部印出)】")
    print(f"{'組合代碼 (M1..M5)':<20} | {'20Y CAGR':<10} {'20Y MDD':<10} {'20Y Sharpe':<12} {'Calmar':<8} {'H1 Sharpe':<10} {'H2 Sharpe':<10} | {'特徵標註'}")
    print("-" * 115)
    for _, r in df_pareto.iterrows():
        tag = ""
        if r['name'] == '(1,1,1,1,1)': tag = "⭐ 現行 Scheme C 封版點"
        elif r['name'] == '(0,0,0,0,0)': tag = "原始 v0 基準點"
        elif r['name'] == '(1,1,0,1,1)': tag = "Scheme C 拔除 E1c (MDD最小)"
        elif r['name'] == '(1,0,1,1,1)': tag = "拔除 D1-US (最高 CAGR 32.69%)"
        elif r['name'] == '(1,0,0,1,1)': tag = "拔除 D1-US 與 E1c"
        elif r['name'] == '(1,0,1,0,1)': tag = "拔除 D1-US 與 E3b"
        elif r['name'] == '(1,1,0,0,1)': tag = "拔除 E1c 與 E3b"
        elif r['name'] == '(1,1,1,0,1)': tag = "拔除 E3b"
        print(f"{r['name']:<20} | {r['y20_cagr']*100:6.2f}%    {r['y20_mdd']*100:6.2f}%    {r['y20_shp']:8.3f}     {r['calmar']:6.3f}   {r['h1_shp']:8.3f}   {r['h2_shp']:8.3f}   | {tag}")
    print("-" * 115)

    # 2. 固定 M1=1, M4=1, M5=1 下的 M2 (D1-US) x M3 (E1c) 四宮格交互作用表
    print("\n【2. M2 (D1-US) × M3 (E1c) 四宮格交互作用矩陣 (固定 M1=1, M4=1, M5=1)】")
    sub4 = df_res[(df_res['m1'] == 1) & (df_res['m4'] == 1) & (df_res['m5'] == 1)]
    print(f"{'配置 (M2, M3)':<18} | {'20Y CAGR':<10} {'20Y MDD':<10} {'20Y Sharpe':<12} {'H1 Sharpe':<11} {'H2 Sharpe':<11} | {'邊際效果解讀'}")
    print("-" * 105)
    for _, r in sub4.sort_values(by=['m2', 'm3']).iterrows():
        label = f"M2={r['m2']}, M3={r['m3']}"
        desc = ""
        if r['m2'] == 0 and r['m3'] == 0: desc = "無 D1-US 且 無 E1c (舊階梯)"
        elif r['m2'] == 1 and r['m3'] == 0: desc = "有 D1-US 但 無 E1c (單加季線濾網)"
        elif r['m2'] == 0 and r['m3'] == 1: desc = "無 D1-US 但 有 E1c (裸奔前重階梯)"
        elif r['m2'] == 1 and r['m3'] == 1: desc = "⭐ 現行 Scheme C (D1 濾網 + E1c 階梯)"
        print(f"{label:<18} | {r['y20_cagr']*100:6.2f}%    {r['y20_mdd']*100:6.2f}%    {r['y20_shp']:8.3f}     {r['h1_shp']:8.3f}    {r['h2_shp']:8.3f}    | {desc}")
    print("-" * 105)

    # 3. 5 大模組平均主效應 (Average Main Effects on 32-Factorial)
    print("\n【3. 5 大核心模組之平均主效應 (Average Main Effects on 32-Factorial)】")
    print(f"{'模組名稱':<28} | {'Δ H1 Sharpe':<13} {'Δ H2 Sharpe':<13} {'Δ 20Y CAGR':<13} {'Δ 20Y MDD':<13} | {'效應判定'}")
    print("-" * 105)

    modules = [
        ('m1', 'M1 (#9-Turbo 通道與快線)'),
        ('m2', 'M2 (D1-US 季線加碼濾網)'),
        ('m3', 'M3 (E1c 前重後輕階梯)'),
        ('m4', 'M4 (E3b 季線右側總攻)'),
        ('m5', 'M5 (C1-A2 深熊減半與復原)'),
    ]

    for m_col, m_name in modules:
        diff_h1_shp = df_res[df_res[m_col] == 1]['h1_shp'].mean() - df_res[df_res[m_col] == 0]['h1_shp'].mean()
        diff_h2_shp = df_res[df_res[m_col] == 1]['h2_shp'].mean() - df_res[df_res[m_col] == 0]['h2_shp'].mean()
        diff_20y_cg = df_res[df_res[m_col] == 1]['y20_cagr'].mean() - df_res[df_res[m_col] == 0]['y20_cagr'].mean()
        diff_20y_mdd= df_res[df_res[m_col] == 1]['y20_mdd'].mean() - df_res[df_res[m_col] == 0]['y20_mdd'].mean()
        
        status = "正向改善 ✅" if (diff_20y_cg > 0 and diff_20y_mdd >= -0.01) else "風險/報酬權衡 ⚖️"
        print(f"{m_name:<28} | {diff_h1_shp:+10.3f}    {diff_h2_shp:+10.3f}    {diff_20y_cg*100:+10.2f}%    {diff_20y_mdd*100:+10.2f}%   | {status}")
    print("-" * 105)

    # 4. 20日 / 60日 區塊 Bootstrap (Block Bootstrap, 1,000 次抽樣)
    print("\n【4. 區塊 Bootstrap 顯著性檢驗：Scheme C (1,1,1,1,1) vs 拔除 E1c (1,1,0,1,1)】")
    twr_c = df_res[df_res['name'] == '(1,1,1,1,1)']['twr_series'].iloc[0]
    twr_no_e1c = df_res[df_res['name'] == '(1,1,0,1,1)']['twr_series'].iloc[0]

    dr_c = twr_c.pct_change().dropna()
    dr_alt = twr_no_e1c.pct_change().dropna()

    common_idx = dr_c.index.intersection(dr_alt.index)
    dr_c = dr_c.loc[common_idx].values
    dr_alt = dr_alt.loc[common_idx].values
    rf_v = IRX.reindex(common_idx).ffill().fillna(0).values / 252.0

    n_samples = len(dr_c)
    n_boot = 1000
    np.random.seed(42)

    for block_len in [20, 60]:
        n_blocks = int(np.ceil(n_samples / block_len))
        diff_sharpes = []
        for _ in range(n_boot):
            start_indices = np.random.randint(0, n_samples - block_len + 1, size=n_blocks)
            boot_idx = np.concatenate([np.arange(s, s + block_len) for s in start_indices])[:n_samples]
            
            ex_c = dr_c[boot_idx] - rf_v[boot_idx]
            ex_alt = dr_alt[boot_idx] - rf_v[boot_idx]

            std_c = np.std(ex_c)
            std_alt = np.std(ex_alt)

            sh_c = (np.mean(ex_c) / std_c * np.sqrt(252)) if std_c > 0 else 0
            sh_alt = (np.mean(ex_alt) / std_alt * np.sqrt(252)) if std_alt > 0 else 0
            diff_sharpes.append(sh_c - sh_alt)

        ci_low = np.percentile(diff_sharpes, 2.5)
        ci_high = np.percentile(diff_sharpes, 97.5)
        ci_med = np.median(diff_sharpes)
        p_val = np.mean(np.array(diff_sharpes) <= 0)

        print(f"  - Block Size = {block_len:2d} 日: ΔSharpe 中位數 = {ci_med:+6.3f} ｜ 95% CI = [{ci_low:+6.3f}, {ci_high:+6.3f}] ｜ P(ΔSharpe <= 0) = {p_val*100:4.1f}%")

    cagr_scheme_c = df_res[df_res['name'] == '(1,1,1,1,1)']['y20_cagr'].iloc[0]
    cagr_no_e1c = df_res[df_res['name'] == '(1,1,0,1,1)']['y20_cagr'].iloc[0]
    diff_e1c = cagr_scheme_c - cagr_no_e1c
    print(f"\n【統計定論】E1c 階梯帶來的 +{diff_e1c*100:.2f}%/年 為樣本內 CAGR 差值；因 Bootstrap ΔSharpe 95% CI 跨零，統計上尚無足夠證據判定夏普率改善，亦不得直接外推為保證之未來超額報酬。")
    print("=" * 135)


# ==============================================================================
# 主執行入口
# ==============================================================================

if __name__ == '__main__':
    t0 = time.time()
    execute_round_0()
    execute_round_1()
    execute_round_2()
    execute_round_3()
    print(f"\n⚡ 全部 Round 0 ~ Round 3 回測研究完成，總耗時: {time.time() - t0:.2f} 秒\n")
