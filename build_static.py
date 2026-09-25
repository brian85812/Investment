import os
import sys
import json
import shutil
from datetime import datetime

# 確保輸出支援 UTF-8
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

from app import compute_signal, compute_rollover_radar

def build():
    print("🚀 開始建置靜態網站 (GitHub Pages)...")
    base_dir = os.path.dirname(os.path.abspath(__file__))
    dist_dir = os.path.join(base_dir, "dist")
    
    if os.path.exists(dist_dir):
        shutil.rmtree(dist_dir)
    os.makedirs(dist_dir, exist_ok=True)

    # 1. 運算市場信號
    print("📊 正在抓取並計算市場數據...")
    qqq = compute_signal(
        name="美股 QQQ", ticker="QQQ",
        base_leverage=0.8, max_leverage=3.0,
        fast_ma=5, slow_ma=220, breakout_window=10, cooldown=3,
        allocs=[0.0, 0.5, 0.8, 1.0],
        extension_guard={'threshold': 1.16, 'cap': 2.2}
    )
    
    tw = compute_signal(
        name="台股 006208", ticker="006208.TW",
        base_leverage=0.6, max_leverage=3.0,
        fast_ma=10, slow_ma=220, breakout_window=20, cooldown=5,
        allocs=[0.0, 0.4, 0.7, 0.9, 1.0]
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
    shutil.copytree(static_src, static_dst)
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

    print(f"\n🎉 靜態建置完成！輸出目錄: {dist_dir}")
    print(f"🕒 建置時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

if __name__ == "__main__":
    build()
