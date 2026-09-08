# VM / 遠端部署踩坑紀錄

> 這份文件記的是**環境層面**的坑（Windows/GCP/群益 SKCOM 的行為），不是專案邏輯 bug——
> 邏輯層的修復都直接寫在對應程式碼的日期註解裡（`grep -rn "2026-0[0-9]-[0-9][0-9]" backend/`
> 可以列出全部）。這份文件的目的是：下次要在一台新機器（新的 GCP VM、或任何一台之前
> 沒跑過這套系統的 Windows 機器）上部署時，不用重新踩一次已知的坑。

## 1. SKCOM 登入回傳 602

官方訊息是「憑證可能過期或是未安裝憑證」，但實際遇過兩種完全不同的根因，症狀一模一樣，
必須先分清楚是哪一種：

### 1a. 這台機器/這個帳號根本沒做過 CA 憑證申請
群益策略王 API 自 V2.13.35 起強制雙因子登入 + 綁下單憑證。全新機器一定要先完成：
1. 群益金融網 →「憑證專區」→「AP軟體憑證」→ 下載「憑證精靈 RAWinApp」
2. 用身分證字號 + 密碼登入 → 申請 → 簡訊驗證碼 → 確認顯示「此電腦已安裝您的有效憑證」
3. 下載 `SKCOMVerifyDJ.exe`（API 下載專區），做一次雙因子登入 + 連線測試

**注意**：步驟 2 的「檢測憑證」多半只是檢查憑證檔案存不存在，不是真的做一次登入驗證。
步驟 3 的 `SKCOMVerifyDJ.exe` 才是唯一會真正觸發登入驗證的動作，兩步都要做，
只做步驟 2 不代表登入一定會成功。這步驟只能帳號本人在該台機器上做，不能代勞（見
`docs/deploy_end_user_pc.md` 階段 0）。

### 1b. 排程任務用 SYSTEM 身分執行
即使憑證已經裝好、且在互動式桌面登入下測試完全正常，**排程任務若用 SYSTEM 帳號 +
`-AtStartup` 觸發**，SKCOM 登入還是固定回 602。研判是群益的雙因子/憑證綁定認的是
「目前登入使用者的 session context」，不是純機器層級，SYSTEM 這個特殊帳號讀不到。

修法：排程任務一律用 `-LogonType Interactive -UserId <實際使用者>` +
`-AtLogOn -User <該使用者>`，不能用 `-LogonType ServiceAccount -UserId SYSTEM`。
`installer/post_install.ps1` 已依此設定，若之後改 `scripts/gcp/vm-bootstrap.ps1` 之類
自己寫的排程邏輯，要套用同樣的修正。

## 2. GCP Console「Set Windows Password」會弄壞已裝好的憑證

**這是一個容易忽略、但破壞性很大的坑**：如果憑證已經裝好、SKCOM 也連線成功過，之後
透過 GCP Console 的「重設 Windows 密碼」功能強制重設密碼，很可能會讓 602 重新出現。

原因：Windows 對使用者帳戶本地安裝的憑證私鑰是用 DPAPI 加密，DPAPI 金鑰是從**該帳戶的
密碼**推導出來的。

- 在已登入狀態下用「Ctrl+Alt+End → 變更密碼」（需要輸入舊密碼）改密碼，Windows 會
  順便把 DPAPI 保護的資料用新密碼重新加密一次，私鑰照樣能用。
- GCP Console 的強制重設是**外部重設**，不知道舊密碼，沒辦法幫你遷移 DPAPI 保護的資料。
  密碼重設前裝好的憑證私鑰會變成孤兒，新密碼底下讀不到 → 602 重新出現，即使憑證精靈
  再跑一次顯示「已安裝」（因為那只是檢查檔案存在，見上面 1a 的注意事項）。

**How to apply**：RDP 密碼忘記/需要重設時，優先用「已登入狀態下正常改密碼」的方式；
真的需要用 GCP Console 強制重設，重設完之後要當作**憑證作廢**處理，回群益的憑證精靈
做「憑證展延/重新申請」（不是只做檢測），並重新跑一次 `SKCOMVerifyDJ.exe` 連線測試
確認登入是真的成功，不能只看憑證精靈顯示成功就當作沒事。

## 3. Google Cloud SDK Shell 是 32 位元程序

GCP VM 上常見的「Google Cloud SDK Shell」桌面捷徑開出來的是 **32 位元**的 shell，
在裡面執行任何跟這個專案相關的安裝/註冊指令都可能踩坑：

- `C:\Windows\System32` 會被 WOW64 重導向到 `SysWOW64`，檢查 `msvcr100.dll`
  是否存在會得到假的 `False`（vcredist 明明裝了，還是誤判成沒裝）。
- `regsvr32` 會把 SKCOM.dll 的 COM 註冊寫進 32 位元登錄區（`WOW6432Node`），
  64 位元的 `tmf-backend.exe` 讀不到，永遠是 `Class not registered`。
- 症狀特徵：安裝程序回報 exit code 0（成功），但 `Test-Path` 檢查不到理應存在的檔案。

**How to apply**：一律從開始功能表開原生的「Windows PowerShell」（管理員），不要用
Cloud SDK Shell 跑任何跟本專案安裝/註冊相關的指令。`installer/post_install.ps1` 已經
用 `[Environment]::Is64BitProcess` 自動偵測並用 Sysnative 重跑本身來防呆；`.iss` 的
`[Run]`/`[UninstallRun]` 也都已改用 `{sys}\...` + `64bit` flag。手動在 VM 上跑指令時
沒有這層防呆，要自己注意開的是哪個 shell。

## 4. 系統時區

GCP Windows VM 預設時區常常不是台灣。這個專案所有 K 棒/交易時間戳都假設系統跑在
Asia/Taipei，時區設錯會直接影響 K 線資料正確性（見下一節）。

```cmd
tzutil /g                        REM 查目前時區
tzutil /s "Taipei Standard Time" REM 設成台灣時區
```

## 5. K 線時間戳「naive 時間戳被當成 UTC」bug 家族

這是同一類 bug 在專案裡出現過三次，記錄下來避免第四次：

**根因**：`pandas.Timestamp.timestamp()` 對沒有時區資訊（naive）的時間戳，一律當成
**UTC** 處理——這跟 Python 內建 `datetime.timestamp()`（用系統本地時區）行為不一樣，
非常容易誤用。這個專案所有 K 棒/交易時間都存成 naive 的**台灣本地時間**，任何地方
直接呼叫 `pd.Timestamp(...).timestamp()` 都會把時間標籤往前多算 8 小時。

**已修復的三個發生點**：
1. `api.py::_to_unix_seconds`（2026-08-21，回測報表用）
2. `kline_engine.py::LiveKlineStore.on_tick`、`capital_parse.py::_parse_kline_datetime`
   （2026-08-24，即時 K 線用——第 1 點修好之後才發現這兩個地方各自重複實作、
   沒有跟著改，導致「最新 K 棒時間標錯」的症狀又重現一次）
3. `broker/capital_futures.py::_on_tick_for_kline`（2026-08-25）——這個是不同性質的
   變體：`request_stocks()` 輪詢路徑沒有券商給的交易所時間可用，原本退回
   `pd.Timestamp.now()`，這個會讀**作業系統本地時區**。在系統時區不是台灣的機器
   （例如剛開的全新 GCP VM，預設常常是 UTC，對照上面第 4 點）上，輪詢推送的那批
   K 棒會被貼上錯誤時間，混進本來正確的成交 tick 資料裡，症狀是「已連線、能看到
   K 線，但最近幾根不見或時間標錯」。

**修法**：全部改用單一共用實作 `backend/timeutil.py::to_unix_seconds()`（naive 時間戳
一律先標記成 `Asia/Taipei` 再轉換），系統時鐘相關的呼叫則一律用
`datetime.now(TAIPEI_TZ)`（不依賴 OS 本身時區設定為何）取代 `pd.Timestamp.now()`
或裸的 `datetime.now()`。`tests/test_timeutil.py`、
`tests/test_capital_futures_broker.py::test_on_tick_for_kline_polling_path_uses_taipei_tz_not_system_clock`
是這類 bug 的回歸測試。

**How to apply**：以後任何地方要把時間戳轉成 unix 秒數，或要取得「現在的台灣時間」，
一律呼叫 `backend/timeutil.py` 的 `to_unix_seconds()` / `TAIPEI_TZ`，不要自己重新
實作一次轉換邏輯——這個 bug 就是因為同一段邏輯在三個檔案各自重複實作才反覆出現。

## 6. RDP session 登出會連帶砍掉排程任務（跟 1b 的 602 是不同症狀）

`TMF-Trading-API`／`TMF-Trading-Tunnel` 都用 `-LogonType Interactive -UserId <實際
使用者>` + `-AtLogOn` 註冊（見上面第 1b 點，這是解 602 必要的設定）。但這個設定本身
有個副作用：**這個排程任務綁定在「目前這個使用者的登入 session」上，session 一旦
被登出（不是單純 RDP 斷線／中斷連線，是真的 log off），Windows 會把綁在上面的任務
process 一起中止**，`Get-ScheduledTaskInfo` 查出來的 `LastTaskResult` 會是
**`267014`（十六進位 `0x41306`，官方定義是 `SCHED_S_TASK_TERMINATED`）**。

2026-09-03 實測：同一天內發生兩次，兩次 `TMF-Trading-API` 跟 `TMF-Trading-Tunnel`
都在完全相同的時間點一起變成 `267014`，代表不是個別任務出問題，是那個時間點使用者
的 RDP session 被登出了（常見成因：關 RDP 視窗時點了「登出/Sign out」而不是直接關
視窗或「中斷連線」；VM 上設了閒置 RDP 自動登出的原則；同一帳號從別處又開了一個新
連線把舊 session 踢掉登出）。中止期間，實盤策略完全沒有在運作、也沒有任何保護在
盯著既有持倉，直到有人發現並手動 `Start-ScheduledTask` 重啟。

**How to apply**：
- 結束 RDP 連線時一律用「直接關閉視窗」或「中斷連線」，不要點「登出/Sign out」。
- 查 `Get-ScheduledTask -TaskName "TMF-Trading-API" | Get-ScheduledTaskInfo` 的
  `LastTaskResult` 是不是 `267014`，是的話代表任務被中止而非自然結束，先
  `Start-ScheduledTask` 救回，再去查是不是 session 被登出這個根因，不是程式碼問題。
- 長期建議加一個用 SYSTEM 身分（不需要登入 SKCOM，不會踩 602）跑的看門狗排程，
  定期檢查這兩個任務是否還活著、死了就自動拉起來，不用等人發現。

## 7. Cloudflare quick tunnel 的連線壽命有限，不適合長期部署

`trycloudflare.com` 這種免帳號、免網域的 quick tunnel，連線本身不保證能撐多久——
2026-09-03 實測從 `tunnel.log` 完整重建過一次時間軸：一條連線從建立到被 Cloudflare
邊緣節點主動送出 `Initiating graceful shutdown due to signal terminated` 斷開，
剛好是 3 天整（起訖秒數幾乎對齊），跟 `TMF-Trading-Tunnel` 排程任務本身死沒死
（見上面第 6 點）完全無關——就算任務一直活著，連線本身到期一樣會斷、網址一樣會換。
Cloudflare 官方在每次啟動 quick tunnel 時印出的警語也講得很明白：這種
account-less tunnel 沒有 uptime 保證，正式環境應該用具名 tunnel。

**How to apply**：這個專案已經改用 **GCP 靜態外部 IP + VPC 防火牆規則**取代 quick
tunnel（見 `installer/tmf_trading_setup.iss`/`post_install.ps1` 2026-09-03 之後的
版本，已移除 `TMF-Trading-Tunnel` 排程任務與 `run_tunnel.ps1`/`cloudflared.exe`
打包）。往後不要再用 quick tunnel 做需要長期穩定的對外連線，除非明確知道對方只是
短期測試用途。

## 8. GCP 靜態 IP／防火牆設定：兩個實測踩過的小雷

1. **`gcloud compute firewall-rules create` 沒有 `--target-instances`/`--instance-zone`
   這兩個參數**（會被誤導去猜這兩個名字，但這是給別的資源用的）。firewall rule 只能用
   `--target-tags`（先用 `gcloud compute instances describe <vm> --format="get(tags.items)"`
   查這台機器本來掛的 tag，例如新建 VM 常見的 `http-server`/`https-server`，直接沿用；
   沒有的話用 `gcloud compute instances add-tags <vm> --zone=<zone> --tags=<tag>` 補一個），
   或完全不指定 target（套用到整個網路，適合只有一台 VM 的簡單場景）。
2. **刪掉 VM 之後，之前保留的靜態 IP 不會跟著消失，會變成「保留但沒有掛在任何運行中
   機器上」的孤兒狀態，這種狀態 GCP 是持續計費的**（掛在運行中的 VM 上才免費）。重建
   VM 後如果不再需要舊的靜態 IP，記得 `gcloud compute addresses delete <name>
   --region=<region>` 釋放掉，不要讓它一直躺著計費。
3. 同一個 Google 帳號底下常常橫跨多個 GCP 專案（例如免費試用建了不只一次），VM 也可能
   換過 zone——`gcloud config get-value project`/`gcloud compute instances list` 先
   查清楚實際的 project/zone，不要沿用之前記得的值，這個專案就因為猜錯專案/zone
   走過兩次冤枉路（`compute.addresses.create` 權限不足、`describe` 查不到 VM）。
