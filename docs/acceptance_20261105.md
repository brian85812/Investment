# 2026-11-05 最後驗收與通知修正

同一市場同一筆目標變更／模擬超標修剪只提醒一次。另一市場更新或歷史事件的 days_ago 改變，不再把舊事件當成新的調倉警報。日報仍可靜音呈現維持；新的超標修剪會正常提醒。

畫面與通知分列「最近目標變更」和「最近模擬超標修剪」，舊 last_action／last_trim API 保持相容。前者是策略目標，後者是零現金流 T+0 模擬，均不代表個人實帳已成交。

本次抓價建置：QQQ 資料日 2026-11-04，目標 0.8x；006208 資料日 2026-11-05，目標與前日均 2.8x。台股最近目標變更是 10/30 的 3.0→2.8x；11/5 為模擬自然槓桿 2.99→2.8x 修剪。目標沒變仍可能因下跌而自然超標，實帳應使用當前淨值及曝險試算。

## 修正

- 通知持久化按市場／事件種類／原事件日期及倍數識別，只有 API 確認送達才記 sent；缺設定與失敗可重試。舊狀態可識別者遷移，無狀態不假裝知道已送。
- workflow restore/save key 一致納入 run_id + run_attempt，補齊同一 run 重跑抓到新事件的狀態保存。
- build(notify=True) 與 notify-only 使用同一去重流程，build-only 保留既有狀態。
- 真實 Flask /health 在正常時 HTTP 200、無資料或降級時 503，保留原 JSON 狀態。
- 三份核心文件與根目錄鏡像同步；修正精確橋接表及輸入暫存／依賴／Bootstrap 備援起點描述。

## 已執行

- 24 個 Python 回歸測試：17 通知、4 訊號事件、3 真 Flask 路由。
- 6 個新前端情境，Node VM 執行實際 script.js。
- 原完整驗收套件重播成功，含原 13 個 Node 互動情境；Python 只把 jsonify 替身改成符合 Flask Response 的物件，所有 assert AST 不變；Node 一處「今日／本期」同義文案期待適配，保留橫幅並補兩個警示 class 斷言。
- 原 US 5,240 / TW 5,092 日 Full/H1/H2 目標配對零差異；降檔向上重置零違規，六核心策略／路由 AST 不變。
- Linux cp312/manylinux wheel 依賴完整 dry-run 解析；本機真行情 build-only 成功，未由本地驗收發真 Telegram。
- 凍結回測 cache SHA256 保持 3ef91f0fc386b06561cb62d48639912dba374443e5a69d7391195b2c52fe24e3，engine 與研究 suite 本輪未改。

遠端 Ubuntu 安裝／Pages 部署與真通知結果以本提交的 Actions run 為準；本地 dry-run 不當作 gunicorn 啟動驗收。429 retry_after 退避尚未實作，lost ACK 仍可能使傳輸重複；API delivered 不等於已讀。

## 重現

在儲存庫根目錄：

```powershell
python -B -m unittest discover -s tests -p "test_*.py" -v
node tests/test_event_ui.js
python -B -X utf8 build_static.py --build-only
```

受限 Windows 沙盒如不允許系統 Temp，可將 STOCK_TEST_TMPDIR 指向可寫研究目錄。GitHub Actions 在建置前執行同一組離線測試。完整原始驗收、雜湊與三方紀錄保存在 Stock/research/final_acceptance_20261105；此處 JSON 保存必要摘要。
