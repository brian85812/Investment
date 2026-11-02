import os
import sys
import json
import math
import shutil
import argparse
import urllib.request
import urllib.error
from datetime import datetime

# 確保輸出支援 UTF-8
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

from app import compute_signal, compute_rollover_radar


def send_push_notification(signals, rollover):
    bot_token = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TG_CHAT_ID", "").strip()
    if not bot_token or not chat_id:
        print(f"ℹ️ 未偵測到完整 TG 設定 (TG_BOT_TOKEN: {'已設定' if bot_token else '未設定'}, TG_CHAT_ID: {'已設定' if chat_id else '未設定'})，略過 Telegram 推播。")
        return {"ok": True, "skipped": True, "attempts": 0}

    # F03/F11: 嚴格驗證雙市場完整性、日期格式與槓桿邊界，拒絕單一市場或異常訊號發送推播
    is_valid, val_errors = validate_signals_payload(signals)
    if not is_valid:
        print(f"❌ 訊號列表未通過完整性驗證 ({'; '.join(val_errors)})，拒絕發送推播！")
        return {"ok": False, "reason": "invalid_or_incomplete_signals", "errors": val_errors, "attempts": 0}

    lev_changed = any(
        (s['target_today'] != s['target_yesterday']) or s.get('deep_bear_active', False)
        for s in signals
    )
    trim_today = any(
        (s.get('last_trim') or {}).get('days_ago') == 0
        and '+0.15x' in str((s.get('last_trim') or {}).get('type', ''))
        for s in signals
    )
    rollover_urgent = any(
        rollover.get(k, {}).get('days_left', 99) <= 2 for k in ('tw', 'us')
    )
    stale_warning = any(
        s.get('is_stale', False) or s.get('data_status') in ('stale', 'fallback_stale')
        for s in signals
    )
    urgent = lev_changed or trim_today or rollover_urgent or stale_warning

    if stale_warning:
        header = "⚠️ <b>【動態槓桿 · 資料新鮮度降級警告】</b>"
    elif lev_changed:
        header = "🚨 <b>【動態槓桿 · 調倉指令警報】</b>"
    elif trim_today:
        header = "⚠️ <b>【動態槓桿 · 模擬超標修剪提醒（請核對實帳槓桿）】</b>"
    else:
        header = "🟢 <b>【動態槓桿 EOD 戰情 · 今日維持】</b>"

    lines = [header, ""]
    for s in signals:
        lt = s.get('last_trim')
        is_trim_today = bool(lt and lt.get('days_ago') == 0 and '+0.15x' in str(lt.get('type', '')))
        if s['target_today'] != s['target_yesterday']:
            chg = f"⚡ <b>{s['target_yesterday']:.2f}x ➔ {s['target_today']:.2f}x</b>"
        elif is_trim_today:
            chg = f"⚠️ 維持 <b>{s['target_today']:.2f}x</b> (今日觸發模擬修剪提醒，實帳若 &gt;{s['trim_trigger_lev']:.2f}x 請砍回目標)"
        else:
            chg = f"維持 <b>{s['target_today']:.2f}x</b>"

        if not s['in_trend']:
            if s.get('pre_bull_active', False):
                bear_v = s.get('sma_bear_val') or s['sma_slow_val']
                add_str = f"<code>季線總攻中(守60MA&gt;={bear_v:.2f}/攻220MA&gt;{s['sma_slow_val']:.2f})</code>"
            else:
                add_str = f"<code>站上年線&gt;{s['sma_slow_val']:.2f}</code>"
            sub_str = "<code>已處底倉</code>"
        else:
            if s.get('ext_guard_active', False):
                cap_v = s.get('ext_cap') or 1.8
                add_str = f"<code>過熱封頂{cap_v:.1f}x</code>"
            elif s['step_idx'] >= s['max_steps']:
                add_str = "<code>已滿載鎖定</code>"
            else:
                add_str = f"<code>&gt;{s['high_bw']:.2f}</code>"
            sub_str = "<code>已處底倉</code>" if s['step_idx'] == 0 else f"<code>&lt;{s['low_bw']:.2f}</code>"

        la = s.get('last_action')
        la_str = (
            f"{la['date']} ({'今日' if la['days_ago']==0 else str(la['days_ago'])+'日前'}) "
            f"<code>{la['from_lev']:.2f}x ➔ {la['to_lev']:.2f}x</code> [{la['type']}]"
            if la else "無"
        )
        lt_str = (
            f"{lt['date']} ({'今日' if lt['days_ago']==0 else str(lt['days_ago'])+'日前'}) "
            f"<code>{lt['from_lev']:.2f}x ➔ {lt['to_lev']:.2f}x</code> [{lt['type']}]"
            if lt else "無"
        )
        hist_line = f"  🕒 上次調倉(模擬參考): {la_str}"
        if lt and (not la or lt['date'] != la['date'] or lt['type'] != la['type']):
            hist_line += f"\n  ✂️ 上次修剪(模擬參考): {lt_str}"
        else:
            hist_line = f"  ✂️ 上次調倉/修剪(模擬參考): {la_str}"

        lines.append(
            f"• <b>{s['name']}</b> ({s['date']})｜收盤 <code>{s['close']:.2f}</code>\n"
            f"  目標: {chg} (階梯 {s['step_idx']}/{s['max_steps']} · 修剪線 &gt;{s['trim_trigger_lev']:.2f}x)\n"
            f"  加碼 {add_str} ｜ 減碼 {sub_str} ｜ 220MA <code>{s['sma_slow_val']:.2f}</code>\n"
            f"{hist_line}"
        )
    for k in ('tw', 'us'):
        ro = rollover.get(k, {})
        if ro and ro.get('days_left', 99) <= 5:
            lines.append(f"\n🎯 <b>轉倉日曆提醒</b>：{ro['market']} 結算倒數 <b>{ro['days_left']} 天</b> ({ro['badge']})")

    payload = json.dumps({
        "chat_id": chat_id,
        "text": "\n".join(lines),
        "parse_mode": "HTML",
        "disable_notification": (not urgent)
    }).encode("utf-8")

    # F17: 結構化有限次重試、檢查 JSON ok:true 並回傳明確狀態
    max_retries = 3
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp_data = resp.read().decode('utf-8')
                try:
                    resp_json = json.loads(resp_data)
                except Exception:
                    resp_json = {}
                if resp_json.get("ok") is not True:
                    last_error = f"Telegram API returned ok=false: {resp_data[:120]}"
                    print(f"❌ Telegram API 應用層拒絕 (第 {attempt} 次): {last_error}")
                    return {"ok": False, "error": last_error, "attempts": attempt}
                print(f"✅ Telegram 戰情推播發送成功 (第 {attempt} 次嘗試)！API 回應: {resp_data[:80]}...")
                return {"ok": True, "attempts": attempt}
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode('utf-8', errors='ignore')
            last_error = f"HTTP {e.code}: {err_msg}"
            print(f"❌ Telegram 推播 HTTP 錯誤 (第 {attempt}/{max_retries} 次) [{e.code}]: {err_msg}")
        except Exception as e:
            last_error = str(e)
            print(f"❌ Telegram 推播發送失敗 (第 {attempt}/{max_retries} 次): {e}")

    return {"ok": False, "error": last_error, "attempts": max_retries}


def validate_signals_payload(signals):
    """F03: 驗證雙市場完整性、欄位齊備、ISO 日期可解析與數值有限/合理性"""
    from datetime import datetime as _dt
    errors = []
    if not isinstance(signals, list) or len(signals) != 2:
        errors.append(f"Expected 2 market signals (QQQ and 006208.TW), got {0 if not isinstance(signals, list) else len(signals)}")
        return False, errors

    expected_tickers = {'QQQ', '006208.TW'}
    actual_tickers = {s.get('ticker') for s in signals if isinstance(s, dict)}
    if actual_tickers != expected_tickers:
        errors.append(f"Missing required tickers: expected {expected_tickers}, got {actual_tickers}")

    required_numeric = ['close', 'target_today', 'target_yesterday', 'trim_trigger_lev',
                        'sma_fast_val', 'sma_slow_val', 'high_bw', 'low_bw', 'step_idx', 'max_steps']
    for s in signals:
        if not isinstance(s, dict):
            errors.append("Signal entry is not a dict")
            continue
        tkr = s.get('ticker', 'UNKNOWN')
        d_str = s.get('date')
        if not d_str or not isinstance(d_str, str):
            errors.append(f"{tkr}: missing date")
        else:
            try:
                _dt.strptime(d_str, '%Y-%m-%d')
            except Exception:
                errors.append(f"{tkr}: unparseable ISO date '{d_str}'")
        for k in required_numeric:
            val = s.get(k)
            if val is None or not isinstance(val, (int, float)) or not math.isfinite(float(val)):
                errors.append(f"{tkr}: invalid or non-finite field '{k}'={val}")
        if (s.get('close', 0) or 0) <= 0 or (s.get('sma_slow_val', 0) or 0) <= 0 or (s.get('high_bw', 0) or 0) <= 0 or (s.get('low_bw', 0) or 0) <= 0:
            errors.append(f"{tkr}: non-positive price, channel, or slow MA")
        t_today = s.get('target_today', 0)
        t_yest = s.get('target_yesterday', 0)
        if not isinstance(t_today, (int, float)) or not isinstance(t_yest, (int, float)) or t_today <= 0 or t_today > 3.0 or t_yest <= 0 or t_yest > 3.0:
            errors.append(f"{tkr}: target leverage out of valid range (0, 3.0]: today={t_today}, yesterday={t_yest}")
        s_idx = s.get('step_idx', -1)
        m_steps = s.get('max_steps', -1)
        if not isinstance(s_idx, (int, float)) or not isinstance(m_steps, (int, float)) or s_idx < 0 or m_steps <= 0 or s_idx > m_steps:
            errors.append(f"{tkr}: invalid step_idx/max_steps ({s_idx}/{m_steps})")

    return len(errors) == 0, errors


def write_json_atomic(path, payload):
    import time as _time
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    for _retry_idx in range(5):
        try:
            os.replace(tmp_path, path)
            break
        except PermissionError:
            if _retry_idx == 4:
                raise
            _time.sleep(0.05)


def build(notify=True):
    print("🚀 開始建置靜態網站 (GitHub Pages)...")
    base_dir = os.path.dirname(os.path.abspath(__file__))
    dist_dir = os.path.join(base_dir, "dist")
    
    os.makedirs(dist_dir, exist_ok=True)

    # 1. 運算市場信號
    print("📊 正在抓取並計算市場數據...")
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
    
    # F03: 嚴格要求雙市場皆非 None 才構成有效 signals，拒絕單市場缺漏或全空覆寫
    signals = [qqq, tw] if (qqq is not None and tw is not None) else []
    is_valid, val_errors = validate_signals_payload(signals)
    if not is_valid:
        err_summary = "; ".join(val_errors)
        print(f"❌ [F03 Gate] 雙市場訊號驗收未通過，拒絕寫入 api_data.json 與發送綠燈推播: {err_summary}")
        raise RuntimeError(f"Signal validation failed: {err_summary}")
    
    # 2. 運算期貨轉倉雷達
    print("🎯 正在計算轉倉雷達數據...")
    rollover = compute_rollover_radar()

    # 3. 原子寫入 JSON API 資料
    data_path = os.path.join(dist_dir, "api_data.json")
    write_json_atomic(data_path, signals)
    print(f"✅ 已原子輸出: {data_path}")

    rollover_path = os.path.join(dist_dir, "api_rollover.json")
    write_json_atomic(rollover_path, rollover)
    print(f"✅ 已原子輸出: {rollover_path}")

    # 4. 複製靜態資源
    static_src = os.path.join(base_dir, "static")
    static_dst = os.path.join(dist_dir, "static")
    shutil.copytree(static_src, static_dst, dirs_exist_ok=True)
    print(f"✅ 已複製 static 目錄到: {static_dst}")

    # 5. 處理 index.html (轉為相對路徑)
    html_src = os.path.join(base_dir, "templates", "index.html")
    with open(html_src, "r", encoding="utf-8") as f:
        html_content = f.read()

    html_content = html_content.replace('href="/static/', 'href="./static/')
    html_content = html_content.replace('src="/static/', 'src="./static/')

    html_dst = os.path.join(dist_dir, "index.html")
    tmp_html = html_dst + ".tmp"
    with open(tmp_html, "w", encoding="utf-8") as f:
        f.write(html_content)
    os.replace(tmp_html, html_dst)
    print(f"✅ 已輸出 index.html 到: {html_dst}")

    # 6. 建立 .nojekyll 防止 GitHub 忽略特定檔案
    nojekyll_path = os.path.join(dist_dir, ".nojekyll")
    with open(nojekyll_path, "w") as f:
        pass
    print("✅ 已建立 .nojekyll")

    # 重置推播事件去重紀錄（新一次建置完成後允許首次推播，防止同一份 dist/ 重複觸發發送兩次）
    dedup_path = os.path.join(dist_dir, ".last_notified_event.json")
    write_json_atomic(dedup_path, {"last_event_sig": None})

    # 7. 若啟用同步推播通知 (預設在本地單獨執行時可用；CI 部署時改由部署成功後執行 --notify-only)
    if notify:
        push_res = send_push_notification(signals, rollover)
        if not push_res.get("ok", False):
            raise RuntimeError(f"Telegram push notification failed: {push_res}")

    print(f"\n🎉 靜態建置完成！輸出目錄: {dist_dir}")
    print(f"🕒 建置時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


def notify_from_dist():
    """F11/F17: 從已建置並部署完成之 dist/ 讀取訊號發送 Telegram 推播，確保推播與線上畫面一致且同一事件快照不重複推播"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    dist_dir = os.path.join(base_dir, "dist")
    data_path = os.path.join(dist_dir, "api_data.json")
    rollover_path = os.path.join(dist_dir, "api_rollover.json")
    dedup_path = os.path.join(dist_dir, ".last_notified_event.json")

    if not os.path.exists(data_path) or not os.path.exists(rollover_path):
        raise RuntimeError("Missing dist/api_data.json or dist/api_rollover.json for post-deploy notification")

    with open(data_path, "r", encoding="utf-8") as f:
        signals = json.load(f)
    with open(rollover_path, "r", encoding="utf-8") as f:
        rollover = json.load(f)

    is_valid, val_errors = validate_signals_payload(signals)
    if not is_valid:
        raise RuntimeError(f"Invalid dist/api_data.json: {'; '.join(val_errors)}")

    event_sig = json.dumps({
        "signals": [
            {
                "ticker": s.get("ticker"),
                "date": s.get("date"),
                "target_today": s.get("target_today"),
                "target_yesterday": s.get("target_yesterday"),
                "is_stale": s.get("is_stale", False),
                "last_trim": s.get("last_trim")
            }
            for s in signals
        ],
        "rollover": {k: (rollover.get(k) or {}).get("days_left") for k in ("tw", "us")}
    }, sort_keys=True)

    if os.path.exists(dedup_path):
        try:
            with open(dedup_path, "r", encoding="utf-8") as f:
                dedup_state = json.load(f)
            if isinstance(dedup_state, dict) and dedup_state.get("last_event_sig") == event_sig:
                print("ℹ️ 相同 EOD 事件快照已完成推播，略過重複發送。")
                return {"ok": True, "dedup_skipped": True, "attempts": 0}
        except Exception:
            pass

    push_res = send_push_notification(signals, rollover)
    if not push_res.get("ok", False):
        raise RuntimeError(f"Telegram push failed after retries: {push_res}")

    write_json_atomic(dedup_path, {"last_event_sig": event_sig})
    return push_res


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build static dashboard and/or send EOD Telegram notification")
    parser.add_argument("--build-only", action="store_true", help="Only build dist/ artifacts without sending Telegram push")
    parser.add_argument("--notify-only", action="store_true", help="Only send Telegram push from existing dist/ artifacts")
    args = parser.parse_args()

    if args.notify_only:
        notify_from_dist()
    elif args.build_only:
        build(notify=False)
    else:
        build(notify=True)
