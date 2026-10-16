import os
import sys
import json
import shutil
import urllib.request
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
        return

    urgent = any(
        (s['target_today'] != s['target_yesterday']) or s.get('deep_bear_active', False)
        for s in signals
    ) or any(
        rollover.get(k, {}).get('days_left', 99) <= 2 for k in ('tw', 'us')
    )

    header = "🚨 <b>【動態槓桿 · 調倉指令警報】</b>" if urgent else "🟢 <b>【動態槓桿 EOD 戰情 · 今日維持】</b>"
    lines = [header, ""]
    for s in signals:
        chg = (
            f"⚡ <b>{s['target_yesterday']:.2f}x ➔ {s['target_today']:.2f}x</b>"
            if s['target_today'] != s['target_yesterday']
            else f"維持 <b>{s['target_today']:.2f}x</b>"
        )
        if not s['in_trend']:
            add_str = f"<code>站上年線&gt;{s['sma_slow_val']:.2f}</code>"
            sub_str = "<code>已處底倉</code>"
        else:
            if s.get('ext_guard_active', False):
                add_str = "<code>過熱封頂2.0x</code>"
            elif s['step_idx'] >= s['max_steps']:
                add_str = "<code>已滿載鎖定</code>"
            else:
                add_str = f"<code>&gt;{s['high_bw']:.2f}</code>"
            sub_str = "<code>已處底倉</code>" if s['step_idx'] == 0 else f"<code>&lt;{s['low_bw']:.2f}</code>"

        lines.append(
            f"• <b>{s['name']}</b> ({s['date']})｜收盤 <code>{s['close']:.2f}</code>\n"
            f"  目標: {chg} (階梯 {s['step_idx']}/{s['max_steps']} · 修剪線 &gt;{s['trim_trigger_lev']:.2f}x)\n"
            f"  加碼 {add_str} ｜ 減碼 {sub_str} ｜ 220MA <code>{s['sma_slow_val']:.2f}</code>"
        )
    for k in ('tw', 'us'):
        ro = rollover.get(k, {})
        if ro and ro.get('days_left', 99) <= 5:
            lines.append(f"\n🎯 <b>轉倉提醒</b>：{ro['market']} 結算倒數 <b>{ro['days_left']} 天</b> ({ro['badge']})")

    payload = json.dumps({
        "chat_id": chat_id,
        "text": "\n".join(lines),
        "parse_mode": "HTML",
        "disable_notification": False
    }).encode("utf-8")

    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp_data = resp.read().decode('utf-8')
            print(f"✅ Telegram 戰情推播發送成功！API 回應: {resp_data[:80]}...")
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode('utf-8', errors='ignore')
        print(f"❌ Telegram 推播 HTTP 錯誤 [{e.code}]: {err_msg}")
    except Exception as e:
        print(f"❌ Telegram 推播發送失敗: {e}")

def build():
    print("🚀 開始建置靜態網站 (GitHub Pages)...")
    base_dir = os.path.dirname(os.path.abspath(__file__))
    dist_dir = os.path.join(base_dir, "dist")
    
    os.makedirs(dist_dir, exist_ok=True)

    # 1. 運算市場信號
    print("📊 正在抓取並計算市場數據...")
    qqq = compute_signal(
        name="美股 QQQ", ticker="QQQ",
        base_leverage=0.8, max_leverage=3.0,
        fast_ma=5, slow_ma=220, breakout_window=10, cooldown=3,
        allocs=[0.0, 0.5, 0.8, 1.0],
        grace_days=0,
        extension_guard={'threshold': 1.16, 'cap': 2.0},
        deep_bear_guard={'bear_ma': 60, 'ratio': 0.97, 'deep_base': 0.4}
    )
    
    tw = compute_signal(
        name="台股 006208", ticker="006208.TW",
        base_leverage=1.0, max_leverage=3.0,
        fast_ma=10, slow_ma=220, breakout_window=20, cooldown=5,
        allocs=[0.0, 0.4, 0.7, 0.9, 1.0],
        grace_days=1,
        bull_acceleration={'enable': True, 'accel_base': 1.2, 'accel_days': 252, 'min_bear_days': 40},
        deep_bear_guard={'bear_ma': 60, 'ratio': 0.97, 'deep_base': 0.5}
    )
    
    signals = [x for x in [qqq, tw] if x is not None]
    
    # 2. 運算期貨轉倉雷達
    print("🎯 正在計算轉倉雷達數據...")
    rollover = compute_rollover_radar()

    # 3. 輸出 JSON API 資料
    data_path = os.path.join(dist_dir, "api_data.json")
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(signals, f, ensure_ascii=False, indent=2)
    print(f"✅ 已輸出: {data_path}")

    rollover_path = os.path.join(dist_dir, "api_rollover.json")
    with open(rollover_path, "w", encoding="utf-8") as f:
        json.dump(rollover, f, ensure_ascii=False, indent=2)
    print(f"✅ 已輸出: {rollover_path}")

    # 4. 複製靜態資源
    static_src = os.path.join(base_dir, "static")
    static_dst = os.path.join(dist_dir, "static")
    shutil.copytree(static_src, static_dst, dirs_exist_ok=True)
    print(f"✅ 已複製 static 目錄到: {static_dst}")

    # 5. 處理 index.html (轉為相對路徑)
    html_src = os.path.join(base_dir, "templates", "index.html")
    with open(html_src, "r", encoding="utf-8") as f:
        html_content = f.read()

    # 將 /static/ 改為 ./static/ 確保在 GitHub Pages 子路徑 (/Investment/) 也能正常載入
    html_content = html_content.replace('href="/static/', 'href="./static/')
    html_content = html_content.replace('src="/static/', 'src="./static/')

    html_dst = os.path.join(dist_dir, "index.html")
    with open(html_dst, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"✅ 已輸出 index.html 到: {html_dst}")

    # 6. 建立 .nojekyll 防止 GitHub 忽略特定檔案
    nojekyll_path = os.path.join(dist_dir, ".nojekyll")
    with open(nojekyll_path, "w") as f:
        pass
    print("✅ 已建立 .nojekyll")

    # 7. 發送推播通知
    send_push_notification(signals, rollover)

    print(f"\n🎉 靜態建置完成！輸出目錄: {dist_dir}")
    print(f"🕒 建置時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

if __name__ == "__main__":
    build()
