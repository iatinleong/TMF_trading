# 手動驗證：兩個帳號的 worker 行程同時運行

前提：已經有一個真實的第二個群益期貨帳號，本人已完成 CA 憑證申請 +
SKCOMVerifyDJ 連線測試（跟你自己那次一樣的流程），且已經在這台機器上
用 Windows 憑證匯入精靈把他的憑證也裝進「目前使用者」憑證存放區。

## Step 1：存這個帳號的密碼

```
python scripts/set_broker_credentials.py --user-id <他的 Supabase user id> --capital-user-id <他的群益帳號>
```
（會提示輸入密碼，交給他自己在鍵盤上輸入，不要你自己打。）

## Step 2：幫他綁定至少一個策略（沿用既有的 bind_strategy.py）

```
python scripts/bind_strategy.py --user-id <他的 user id> --strategy-id breakout_long --product-code TM2609 --qty 1 --enabled
```

## Step 3：啟動你自己的共用主行程（跟平常一樣，port 8765）

```
python run_server.py
```

## Step 4：另開一個終端機，啟動他的 worker 行程（不同 port，設 WORKER_MODE + ACCOUNT_USER_ID）

Windows PowerShell：
```
$env:WORKER_MODE="1"
$env:ACCOUNT_USER_ID="<他的 Supabase user id>"
$env:TMF_PORT="8766"
python run_server.py
```

## Step 5：確認兩邊都連線成功

- 主行程（8765）：`http://127.0.0.1:8765/api/health`
- 他的 worker（8766）：`http://127.0.0.1:8766/api/health`

兩邊都要回 `{"status":"ok"}`。接著分別檢查兩邊的稽核 log，看有沒有出現
兩種不同的 `account=` 標記（`data/order_audit.log`/`data/position_audit.log`）——
這是驗證「兩個帳號真的各自連線成功、各自在跑」最直接的證據，比看
uvicorn console 輸出可靠（這個專案的 logging 設定，一般部署下不會把
`backend.*` 的 logger 輸出接到任何看得到的地方，這個坑這次 session 已經
踩過好幾次）。

## 這一步要看的是什麼

- 兩邊都 connect 成功、都看得到各自的持倉/委託 → 並發連線可行，Plan B-2
  （跨帳號請求路由，讓使用者登入後自動連到自己 worker，不用手動記 port）
  可以開始規劃。
- 只有一邊連得上，另一邊報錯或卡住 → 記下實際的錯誤訊息/行為，回來討論
  是不是要走「兩台機器」而不是「一台機器兩個行程」。

## 已知還沒解決的事（Plan B-2 才處理，這次驗證不用管）

- 使用者登入 Dashboard 後，前端目前完全不知道要連哪個 port——手動測試
  階段直接用 `http://127.0.0.1:8766` 開瀏覽器就好，不用等路由做完。
- worker 行程結束/當掉之後沒有自動重啟機制。
- 沒有「監控哪些帳號應該要有 worker 在跑、沒跑就自動啟動」的 supervisor，
  這次是手動各自開一個終端機視窗。
