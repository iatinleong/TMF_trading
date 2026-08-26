# 部署到「沒寫過 code」的人的電腦 — 規劃

## 先講結論

**不要用 Docker image，也不要叫他裝 VSCode。**

- Docker：群益 API 靠的是 `SKCOM.dll` 這顆 Windows COM 元件，要 `regsvr32` 註冊到系統，且群益的雙因子登入 + 憑證是**綁在那台實體電腦**上的（見 `login_doc_utf8.txt`：CA 憑證要在該電腦申請安裝，SKCOMVerifyDJ 連線測試也要在該電腦跑）。包成 Linux container 完全繞不開這件事；Windows container 技術上可行但對非工程師來說安裝 Docker Desktop 本身就是一道坎，得不償失。
- VSCode：那是給寫 code 的人開發用的編輯器，不是給終端使用者「執行程式」用的東西。叫一個不會 code 的人開 VSCode 只會製造困惑（他要學 open folder、integrated terminal、怎麼跑 uvicorn…）。

**建議做法：包成一個雙擊安裝的 Windows 安裝程式（Inno Setup 產生單一 Setup.exe），裡面塞好 Python、程式碼、SKCOM 元件，安裝時自動做完 `vm-bootstrap.ps1` 現在做的事，只是 target 從 GCP VM 換成他的電腦。使用者體驗只有：雙擊安裝 → 填一個小表單（帳號密碼）→ 桌面多一個捷徑，點了就能看儀表板。**

這跟現有的 `scripts/gcp/vm-bootstrap.ps1` / `pack_deploy_zip.py` 邏輯高度重疊，可以直接沿用大部分程式碼，不用重寫。

---

## 有一段沒辦法自動化：帳號側的憑證申請

依 `login_doc_utf8.txt`，群益策略王 API 有強制的雙因子 + 下單憑證流程，**這一段只有帳號本人能做，不能由我方腳本代勞**：

1. 進群益金融網 → 憑證專區 → AP軟體憑證 → 下載「憑證精靈 RAWinApp」
2. 用他自己的身分證字號 + 密碼登入，申請憑證，簡訊驗證碼
3. 下載 `SKCOMVerifyDJ.exe`（API 下載專區），做雙因子登入 + 下單連線測試
4. 確認「此電腦已安裝您的有效憑證」

這步驟必須排在「安裝我們的程式」**之前**，而且必須在**同一台電腦**上做。交付物裡要附一頁圖文步驟（用他看得懂的白話文，不要用術語），這步驟做不完，後面裝了也連不上。

---

## 整體流程（分四階段）

### 階段 0：帳號端準備（使用者自己做，一次性）
- 上面那段 CA 憑證 + SKCOMVerifyDJ 連線測試
- 準備好群益帳號、密碼、期貨帳號（給我方安裝程式的表單填）
- 交付物：一頁 PDF/圖文步驟（含截圖），不涉及任何程式安裝

### 階段 1：打包安裝程式（我方 / 開發端做）
把現有 `scripts/gcp/` 底下的邏輯改包成 Inno Setup 專案：

| 現有腳本 | 對應到安裝程式的哪個環節 |
|---|---|
| `pack_deploy_zip.py` | 決定哪些檔案要打進安裝包（排除 `.env`、`__pycache__`、大型 raw data） |
| `vm-bootstrap.ps1` | 變成 Inno Setup 的 `[Run]` 區段：裝 Python（或改用內嵌的 embeddable/frozen exe，見下）、`pip install -r requirements.txt`、VC++ redist、`regsvr32` 註冊 SKCOM.dll |
| `.env.example` | 變成安裝精靈裡的「填寫帳號」頁面，安裝完寫出 `.env` |
| 排程任務 `TMF-Trading-API` | 保留這個模式：用 Task Scheduler 開機自動啟動 uvicorn，使用者完全不用碰終端機 |

兩個實作層級可選（建議 B）：

- **A（快，重用現有腳本）**：安裝程式其實就是把 `vm-bootstrap.ps1` 包進 Setup.exe 裡跑，一樣要求電腦上有網路去裝 Python。優點：改動最小、可以馬上用現有腳本驗證。缺點：使用者電腦沒網路/防毒擋下載 Python 安裝檔時會卡住。
- **B（更穩，建議）**：改用 [PyInstaller](https://pyinstaller.org/) 把 `backend/` 打包成單一 `tmf-backend.exe`（內含 Python runtime），安裝程式不再需要現場下載 Python。SKCOM 相關 DLL 隨安裝包附上並在安裝時 `regsvr32` 註冊。這樣「沒網路也能裝」，對非工程師的电脑更保險（防毒軟體、公司網路限制下載執行檔的情況很常見）。

安裝精靈（Inno Setup 的 `[Code]` 自訂頁面，或退而求其次用一個簡單的 Tkinter 小視窗）需要蒐集：
- `CAPITAL_USER_ID` / `CAPITAL_PASSWORD`
- `CAPITAL_ENVIRONMENT`（**預設先給測試環境 2，附一行說明「先測試, 沒問題再切正式」**，避免非工程師誤觸正式下單）
- `LIVE_PRODUCT_CODE`（可以先給合理預設 `TM2608`，並註明這要跟著月份換）

安裝完自動：
- 開防火牆規則（沿用 `New-NetFirewallRule` 那段）
- 註冊 Task Scheduler，開機自動跑
- 桌面/開始選單放一個捷徑「開啟 XX交易系統」→ 直接開瀏覽器連到 `http://127.0.0.1:8765`（儀表板頁面，不是叫他看 API JSON）

### 階段 2：使用者端安裝體驗
1. 雙擊 `TMF-Trading-Setup.exe`
2. Windows SmartScreen 可能跳警告「未知發行者」→ 這是本來就會發生的事，因為安裝程式沒有數位簽章。交付物裡要先講清楚「這是正常的，點『其他資訊』→『仍要執行』」，不然使用者看到警告會直接放棄。（若要徹底解決，需要買程式碼簽章憑證，是額外成本，先列為 open item，見下方「待決事項」。）
2. 一路「下一步」，中途填帳號密碼那頁
3. 安裝完成，桌面出現捷徑
4. 點捷徑 → 瀏覽器開啟儀表板，看得到「已連線 / 未連線」狀態

全程不出現終端機、不出現程式碼、不需要裝 VSCode。

### 階段 3：日常維運 / 更新
非工程師的電腦上，你（開發者）事後要怎麼推新版，有兩條路：
- **輕量**：沿用現有 GCS + `vm-pull-deploy.ps1` 的「拉更新」模式，改成他電腦上也跑一個背景排程，定期檢查 GCS 上的 `deploy-manifest.json`，有新版就自動抓、`robocopy` 覆蓋（保留 `.env`）、重啟排程任務。使用者完全無感。
- **保守**：不自動更新，你需要更新時遠端連線（TeamViewer / AnyDesk 之類）手動處理，或請他重新跑一次安裝程式（安裝程式設計成可重複執行、保留既有 `.env`）。

建議先用「保守」路線上線，跑穩了、有信心了再打開自動更新，畢竟這是會下單的系統，自動靜默更新出包的代價比較高。

---

## 已完成 / 已驗證

- `installer/tmf_trading_setup.iss` + `installer/post_install.ps1` + `run_server.py` +
  `docs/ca_cert_guide.md` 都已寫好並實測跑通（在這台開發機上完整跑過一次安裝 → 憑證檢查 → 連線成功）。
- **關鍵踩坑**：排程任務原本設計成開機時用 SYSTEM 身分啟動，實測會讓群益 SKCOM 登入固定回傳
  602。已修正為「登入時以目前使用者身分啟動」（`-LogonType Interactive -AtLogOn`），
  現有 `post_install.ps1` 已套用此修正。詳見記憶 `capital-skcom-system-account-602`。
- 還沒在**全新的另一台電腦**上測過完整安裝流程——目前的驗證都在同一台開發機上進行，
  這台機器的帳號/憑證/COM 註冊本來就有歷史殘留，不能完全排除換一台乾淨機器才會冒出來的問題
  （例如 VC++ Redistributable 缺失、防毒軟體攔截、SmartScreen 更嚴格等）。真正拿給別人裝之前，
  建議至少在一台乾淨的 Windows VM 上完整跑一次。

## 待決事項（需要你決定，會影響安裝程式怎麼做）

1. **要不要買程式碼簽章憑證**：不買 → SmartScreen 警告要在交付文件裡講清楚；買 → 使用者體驗更順，但有金錢/流程成本。
2. **正式/測試環境預設值**：建議安裝精靈預設鎖定 `CAPITAL_ENVIRONMENT=2`（測試），並且要求手動改 `.env` 才能切正式，避免對方裝好當下就不小心跑到真帳號下單。
3. **異常/斷線通知**：非工程師不會主動去看 log，要不要在儀表板上做一個很顯眼的「未連線/策略異常」紅色提示？還是額外做 Line/Email 通知？
4. **遠端支援管道**：出問題時你怎麼幫他 debug？建議先講好用 AnyDesk/TeamViewer，而不是叫他讀 log 貼給你。
5. **這台電腦的角色**：它是完全獨立跑（他自己的帳號、自己的策略），還是你仍然要盯著、只是借用他的電腦跑單？這會影響儀表板要不要做「操作」功能（暫停策略、手動平倉）還是只做「唯讀監控」。

---

## 下一步

如果方向沒問題，我可以先做：
1. 用 PyInstaller 把 `backend/` 打包測試一次，確認 comtypes + SKCOM 在凍結後的 exe 裡能不能正常運作（這是最大技術風險，需要先驗證，凍結 COM 相依套件偶爾會有坑）。
2. 寫一版 Inno Setup script 骨架，重用 `vm-bootstrap.ps1` 的註冊/防火牆/排程邏輯。
3. 寫階段 0 那頁「CA 憑證申請」圖文指南草稿。

先跟我確認「待決事項」那幾點，我再往下動工。
