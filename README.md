# 台指期量化回測系統

台指期（TAIEX Futures）60 分鐘 K 棒均線策略回測系統。

## 策略概述

### 1. 突破/跌破 (Breakout) 系統
- 60K 5MA 由下往上穿越 60K 20MA → 作多訊號
- 60K 5MA 由上往下穿越 60K 20MA → 作空訊號
- 停損：進場價反向 150 點
- 停利：進場價正向 500 點

### 2. 回測/回彈 (Pullback) 系統
- 多方：價格在 60K 60MA 與 60K 20MA 之上，回測 60K 20MA → 作多訊號
- 空方：價格在 60K 60MA 與 60K 20MA 之下，回彈 60K 20MA → 作空訊號
- 停損：進場價反向 150 點
- 停利：進場價正向 300 點
- 紅箭頭（多單）進場後若出現綠箭頭，立即停利並反手掛空單；反之亦然
- 若遇滑價或早/夜盤跳空，直接以市價停損/停利成交

## 專案結構

```
backend/
  config.py           # 契約規格、手續費、稅率等參數
  indicators.py        # 60 分鐘 K 棒轉換與均線計算
  signals.py           # 突破/跌破、回測/回彈訊號邏輯
  backtest_engine.py   # 事件驅動回測引擎
  main.py              # 執行進入點
data/                  # 歷史K線資料 (CSV)
tests/                 # pytest 單元測試
```

## 安裝

```powershell
pip install -r requirements.txt
```

## 執行回測

```powershell
python backend\main.py --data data\your_data.csv --strategy breakout
```

## 重要回測原則（避免與實盤不一致）

1. **訊號延遲一根執行**：訊號在 K 棒「收盤」時判定，實際下單於「下一根 K 棒開盤」成交，避免用未來資訊回測（look-ahead bias）。
2. **手續費與期交稅**：每筆交易都計入來回手續費與期貨交易稅，不可忽略。
3. **停損/停利以當根高低點判定**：同一根K棒內若停損與停利同時可能觸發，預設先判定停損（保守假設）。
4. **跳空缺口**：遇到停損/停利價位被跳空穿越時，以缺口開盤價（市價）成交，而非理論停損/停利價。
5. **測試盤 vs 回測邏輯必須共用同一套訊號/成交規則**，避免回測與實盤（paper/live）行為不一致。

（詳見 backend/backtest_engine.py 內註解）

## 實盤下單：群益證券（Capital Futures）API 整合

`backend/broker/capital_futures.py` 將群益證券 SKCOM API（`CapitalAPI_2.13.58_PythonExample/`）
包裝成 `CapitalFuturesBroker` 類別，支援台指期（大台 TX / 小台 MTX / 微台 TMF）
登入、下單、刪單、改價與即時報價訂閱，可與既有策略/回測邏輯整合為實盤交易。

### 環境設置（僅需一次，Windows only）

1. 若 `元件\x64` 內的 DLL 是從壓縮檔解壓縮而來，Windows 會標記為「網路來源」而阻擋載入：
   ```powershell
   Get-ChildItem "CapitalAPI_2.13.58_PythonExample\CapitalAPI_2.13.58_PythonExample\元件\x64" -Recurse | Unblock-File
   ```
2. 若系統缺少舊版 VC++ Runtime（`SKCOM.dll` 依賴 `CTSecuritiesATL.dll` → `mfc100.dll`/`msvcr100.dll`），
   需安裝 Microsoft **Visual C++ 2010 SP1 Redistributable (x64)**。
3. 以系統管理員身分執行 `元件\x64\install.bat`（內部即為 `regsvr32 SKCOM.dll`）完成 COM 元件註冊。
4. 繁體中文 Windows + Python 3.13 環境下，comtypes 產生 COM wrapper 時可能出現：
   ```
   SyntaxError: 'mbcs' codec can't decode bytes ...
   ```
   原因是 comtypes 原始碼寫入 `# -*- coding: mbcs -*-` 但實際寫檔編碼不一致。
   需修改本機 `site-packages/comtypes`：
   - `client/_generate.py`：寫入生成檔案時加上 `encoding="utf-8"`
   - `tools/codegenerator/codegenerator.py`：coding cookie 改為 `# -*- coding: utf-8 -*-`

   修正後清除快取重新產生：`site-packages/comtypes/gen/_<GUID>*.py` 可直接刪除，
   下次呼叫 `comtypes.client.GetModule()` 會自動重新產生。

### 設定帳密

複製 `.env.example` 為 `.env`，填入 `CAPITAL_USER_ID` / `CAPITAL_PASSWORD`，
並將 `CAPITAL_ENVIRONMENT` 設為 `2`（測試環境）先行驗證。

### 使用範例

```powershell
python -m backend.broker.example_place_order --product MXFR1 --side buy --qty 1 --fok
```

```python
from backend.broker import CapitalFuturesBroker, BuySell, Environment

broker = CapitalFuturesBroker(environment=Environment.TEST)
broker.login()  # 從 .env 讀取帳密
accounts = broker.initialize_order()
result = broker.send_future_order(
    account=accounts[0], product_code="MXFR1", side=BuySell.BUY, qty=1, price="0",
)
print(result)
```

⚠️ **注意**：實際下單前務必先以測試環境／測試帳號確認流程正確，
正式環境下單具有真實資金風險，請自行確認商品代碼、口數與風控設定無誤。

