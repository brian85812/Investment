# QQQ 富途收盤備援與授權（2026-11-12）

## 完成與限制

已接入正式 `fetch_robust_market_data`。Yahoo 完整收盤落後時才嘗試富途，只補 QQQ 最後完整日之後最多 5 個交易日，不覆寫有效舊列或以短歷史重設十年狀態。台股沿用 Yahoo＋TWSE，策略參數、結算成本、冷卻、資金池及研究凍結行情不變。

尚未完成富途帳戶授權及真實行情口徑驗收。無憑證時不呼叫富途；憑證不足／HTTP 401／權限失敗時保留 Yahoo 舊資料及 stale 警示，API metadata 的 `secondary_source.status` 提供不含秘密的診斷結果。**程式部署不等於富途已啟用，也不代表缺失收盤已補齊。**

Yahoo 若完全無法取得至少225列歷史，仍維持原先拒絕輸出；本次沒有引入十年歷史快取或完整替代資料庫。需要另案處理長時間全站斷線。

## 資料驗證

富途取得 US.QQQ 正常時段日 K（`ktype=2, extended_time=0`）的未復權與前復權兩組資料；查詢前500日、每組最多370列。回覆要通過日期／紐約時間戳、範圍、交易日、四價關係、非有限值、成交量、重複列及原價／還原價日期相同等檢查。

所有有效重疊列的還原 OHLC 須與 Yahoo 逐格比對，至少60個交易日；允許1美分加百萬分之一相對誤差。若 Yahoo 有 Raw Close，也比對原始收盤。這是拒絕來源口徑錯誤的資料品質閘門，不是策略最佳化參數。前復權若採不同公式、公司行動尚未同步、日期有缺漏或價格不合，就不補價。沒有以一個錨點／平均倍率硬拉齊兩來源。

補價成功才標記來源 `yfinance+futu` 或 `yahoo_direct+futu`，以富途還原 OHLC、原始成交量及 Raw Close 建立新列；完整歷史仍由原狀態機重新計算。資訊不足或衝突時保持降級，不能只靠抓到單一報價解除警示。

## 為何選 AppKey 簽章

官方提供 OAuth 及 AppKey；本次用適合固定伺服器排程的 AppKey，支援 Ed25519 與 RSA-SHA256。OAuth access token 會過期，本次沒有實作 OAuth 登入／自動續期，也不要求交易授權。

所有請求都是固定富途主機的 QQQ 歷史 GET；簽章包含實際 query 順序與編碼、時間戳，另有 nonce；TLS 必須驗證，拒絕重新導向，錯誤與公開 JSON 不含私鑰、AppKey、簽章或供應商原始錯誤。

## 使用者授權步驟

1. 本機產生加密 Ed25519 金鑰：

   ```powershell
   python tools/setup_futu_key.py
   ```

   預設寫入 `%LOCALAPPDATA%\DynamicLeverage\futu`，不寫入 OneDrive 專案。會要求私鑰加密密碼，不顯示或儲存密碼，不覆寫既有金鑰。本機私鑰檔加密，權限依本機帳戶／作業系統管理。

2. 自行登入 [富途 Console](https://open.futunn.com/dashboard)，在 AppKey 管理建立行情讀取權限的 AppKey，選 Ed25519，**只上傳公開金鑰 `futu_public_key.pem`**。實際行情權限、配額、費用依帳戶結果核對，不假設 App 權限等於 API 權限。無須開啟下單或帳戶資產存取。

3. 在 GitHub 專案 Settings → Secrets and variables → Actions 設定：

   | Secret | 內容 |
   |---|---|
   | `FUTU_API_KEY` | AppKey ID |
   | `FUTU_PRIVATE_KEY_PEM` | 加密的私鑰 PEM 檔案內容 |
   | `FUTU_PRIVATE_KEY_PASSWORD` | 私鑰加密密碼 |

   金鑰僅在建置步驟由 Secrets 注入，不進入靜態網站、快取、通知或 Git。不要把私鑰／密碼貼進對話。尚未替使用者建立 AppKey 或儲存 Secrets。

4. 本機測試時可設 `FUTU_API_KEY`、`FUTU_PRIVATE_KEY_FILE`（指向加密私鑰）及 `FUTU_PRIVATE_KEY_PASSWORD`，以私密輸入方式注入；PEM 與 FILE 只選一種，不把密碼寫進 PowerShell 歷史。權限測試：

   ```powershell
   python tools/check_futu_source.py --end 2026-11-11
   ```

   請將日期換成實際已完成交易日。此命令只查行情，不建置、不推播；未授權 exit 2。取得日K不等於已通過兩來源比對，最後還須執行正式 build-only 查驗 `secondary_source`、日期、價格與策略輸入。

5. 真實授權後先跑行情／重疊價格驗收，通過再確認線上補價。若閘門失敗，先查資料口徑，不為消除警示而放寬門檻。

## 驗證界線

新測試涵蓋簽章驗證、無授權零HTTP、加密私鑰、錯誤遮罩、重導向阻擋、錯日期／未來／假日／空值／異常四價、還原價與原價衝突、除息未同步、錨點不足、多日全成或全拒、健康Yahoo不觸發備援，以及完整資料管線。

測試行情均為合成 fixture，不冒充富途真實歷史；先前無授權的真實探測回401，保存於 `Stock/research/us_data_20261112/futu_probe_manifest.json`。本輪未登入帳戶、未建立金鑰、未下單、未直接發送本機 Telegram。

官方規格：

- [授權與簽章](https://open.futunn.com/api/overview/getting-started)
- [歷史日K](https://open.futunn.com/api/quote/basic-data/history-kline)
- [行情權限](https://open.futunn.com/api/overview/api-authority)
