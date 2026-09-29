# 死叉做空策略 Phase 2：接上即時交易 設計文件

## 背景與目標

Phase 1（`docs/superpowers/specs/2026-09-19-death-cross-short-backtest-design.md`）已經用歷史資料驗證過死叉做空策略邏輯（15分鐘K，MA5死叉MA20+MA20斜率向下,SL/TP各130點),`commit d6b6fae` 起最終參數確認為 130/130。這份文件把它接上即時交易，讓它變成 Dashboard 上可以手動開關的第 5 個策略，享有跟現有 4 個策略完全一樣的基礎設施（OCO智慧單/軟停損/風控保險/反向出場、斷線重連對帳）。

**這次只交付「能力」，不自動開始用真錢**（已與使用者確認）：程式碼、測試、Supabase 綁定、重新打包的 installer 都會完成，但策略預設不武裝——要嘛使用者自己在 Dashboard 手動開關，要嘛之後另外設定 `enabled=true`（但目前部署是共用帳號模式,不是 WORKER_MODE,`enabled` 欄位現階段不會觸發自動武裝，純粹是紀錄用）。**這次也不會自動更新正在跑的 VM**，部署時機由使用者自己決定。

**商品範圍**：只做微台（TMF），跟現有 4 策略一致。原因：OCO智慧單目前只驗證過 TMF 的具體月份碼（`TM+YYMM`）格式，大台/小台用的連續碼會跳過 OCO、退回較弱的軟停損保護——這個限制 Phase 1 就已經存在，這次不重新處理。

## 一、即時 15 分鐘K棒聚合管線

沿用 Phase 1 驗證過的「泛化 + 薄包裝」模式，這次應用在即時 tick 處理路徑上：

- `backend/kline_engine.py`：
  - `bar_close_time_from_ts(ts, minutes: int = 60)` — 加上參數，內部把寫死的 `60.0`/`* 60` 換成 `minutes`。預設值 60 保證所有既有呼叫（不傳這個參數）行為完全不變。
  - `LiveKlineStore.__init__(self, product_code, interval_minutes: int = 60)` — 存起來，`on_tick()` 內部呼叫 `bar_close_time_from_ts(ts, minutes=self.interval_minutes)`。快取檔名依 `interval_minutes` 分開命名（例如 `TM2609.json` vs `TM2609_15min.json`），避免兩條時間軸互相覆蓋落地檔。
  - 新增 `get_kline_store(product_code, interval_minutes: int = 60) -> LiveKlineStore`，內部維護以 `(product_code, interval_minutes)` 為 key 的 registry。既有的 `get_store(product_code)` 改成呼叫 `get_kline_store(product_code, interval_minutes=60)` 的薄包裝，**簽名完全不變**——`api.py`/`live_service.py`/`shioaji_backfill.py`/`strategy_service.py` 這些既有呼叫端一行都不用改。

**掛勾點**（`backend/broker/capital_futures.py::_on_tick_for_kline()`，這是這個 session 兩次重大事故發生過的同一個即時 tick 處理路徑，改動必須做到「新路徑掛掉絕對不能拖垮舊路徑」）：

```python
get_store(product).on_tick(price, volume, ts)  # 既有 60 分鐘路徑，不動，擺在前面
try:
    get_kline_store(product, interval_minutes=15).on_tick(price, volume, ts)
except Exception:  # noqa: BLE001
    logger.warning("15分鐘K棒聚合失敗（不影響既有60分鐘路徑）", exc_info=True)
```

**回補限制**：60 分鐘 K 棒有群益 `RequestKLineAMByDate` 官方回補 API，重開機時可以補洞；15 分鐘沒有對應的官方回補管道（群益不提供任意週期回補），15 分鐘 store 只能靠即時 tick 現場累積。

**K棒等待門檻要用 `ma_mid`，不是 `ma_slow`（設計審查時發現並修正）**：`strategy_service.py` 目前有 4 處寫死 `len(bars) < DEFAULT_STRATEGY.ma_slow + 2`（即 62 根)的判斷式（`_load_bars`/`_tick_one` 內的一般判斷與換約後的重判斷)，這是給 pullback 策略用的（`generate_pullback_signals` 真的需要 `ma_slow`)。死叉策略的訊號函式只驗證 `ma_fast`/`ma_mid` 兩欄（`_validate_columns(df, ["ma_fast", "ma_mid"])`)，完全不需要 `ma_slow`，62 根（15 分鐘×62 ≈ 15.5 小時,得跨過一整個日盤加半個夜盤)是不必要的等待。這 4 處判斷式都要改成依 `state.strategy` 決定門檻：死叉策略用 `ma_mid(20) + 2 = 22` 根（約 5.5 小時,跑完一個交易時段就緒),其他策略維持原本的 `ma_slow + 2 = 62` 根不變。

**`_load_bars()` 的 CSV 暖機合併邏輯必須整條跳過，不能只是「門檻不夠」**：`_load_bars()` 目前在即時 K 棒不足時，會退回讀取 `{contract}_60min_real.csv`/`{contract}_60min.csv` 並用 `resample_to_60min()` 合併——這是 60 分鐘專用的暖機備援。死叉策略沒有對應的 15 分鐘 CSV，如果只調低門檻、不特別處理這個分支，K棒不足時反而可能誤觸發這條合併邏輯，把 60 分鐘歷史 K 棒硬併進 15 分鐘即時序列，徹底弄亂 MA 計算（間距不一致）。因此 `_load_bars()` 需要一個 `interval_minutes` 參數：`interval_minutes=15` 時完全跳過 CSV 合併分支，直接回傳 `add_moving_averages(live_bars, ...)`（`live_bars` 為空或不足也直接回傳，讓 `_tick_one()` 既有的「K 棒資料不足，略過本輪」邏輯接手，不嘗試補資料）。

門檻調整後，死叉策略重開機只要跑完一個交易時段（約 5.5 小時）就會就緒，這段期間顯示「K 棒資料不足，略過本輪」——這是既有邏輯的正常等待狀態，不是新問題。

## 二、接進 `backend/strategy_service.py`

**`STRATEGY_DEFS` 新增一項：**
```python
"death_cross_short": {"strategy": "death_cross", "direction_limit": "short", "label": "死叉做空"},
```

**K棒/訊號讀取依策略種類切換時間軸**：`_bars_from_kline_store`/`_load_bars` 加上 `interval_minutes` 參數，`_tick_one()` 呼叫時依 `state.strategy == "death_cross"` 決定傳 15 還是 60。60 分鐘的 CSV 暖機備援（`{contract}_60min_real.csv`）是 60 分鐘專用的，死叉策略沒有對應的 15 分鐘 CSV 暖機檔——K棒不足時走既有的「K 棒資料不足，略過本輪」邏輯，不會硬套錯誤時間軸的資料。`_signaled_frame(strategy, bars)` 新增 `if strategy == "death_cross": return generate_death_cross_signals(bars)` 分支。

**`strategy_config_defaults()` 新增死叉專屬的預設值分支**（這是這次唯一會動到、且被多個既有策略共用的函式）：

```python
def strategy_config_defaults(strategy_id: str | None = None) -> dict[str, Any]:
    defs = STRATEGY_DEFS.get(strategy_id, {}) if strategy_id else {}
    strategy_kind = defs.get("strategy", "pullback")
    is_death_cross = strategy_kind == "death_cross"
    return {
        ...
        "stop_loss_points": 130.0 if is_death_cross else _env_float("STRATEGY_STOP_LOSS_POINTS", 100.0),
        "take_profit_points": 130.0 if is_death_cross else _env_float("STRATEGY_TAKE_PROFIT_POINTS", 250.0),
        ...
        "reverse_signal_exit_enabled": not is_death_cross,
    }
```

**為什麼這個 `if/else` 不會讓死叉的值污染既有策略**（使用者在設計討論中特別確認過這點，這裡記錄清楚，避免以後有人看代碼時同樣的疑慮）：`strategy_config_defaults()` 每次呼叫只針對一個 `strategy_id` 計算一次結果，`is_death_cross` 是從**該次呼叫的** `strategy_id` 算出來的區域變數，不是共用狀態。呼叫 `strategy_config_defaults("breakout_long")` 時 `is_death_cross=False`，三元運算式一律走 `else`，拿到跟現在一模一樣的值；呼叫 `strategy_config_defaults("death_cross_short")` 時 `is_death_cross=True`，才會走 `if` 那一側拿到 130.0。兩次呼叫互不影響，物理上不可能把 130 的值帶到其他策略身上——這點在測試計畫裡會有專門測試明確證明（同一個測試同時斷言兩種策略的輸出都符合各自預期）。

## 三、Dashboard / Supabase 綁定

**修正（設計過程中發現先前理解錯誤，特別記錄）**：`frontend/js/app.js::loadStrategyDefs()`（第541-550行）是**白名單過濾機制**：
```js
myStrategyConfigs = Object.fromEntries(mineRows.map((r) => [r.strategy_id, r]));
strategyDefs = allDefs.filter((d) => myStrategyConfigs[d.strategy_id]);
```
`mineRows` 來自 `/api/my-strategy-configs` → `list_user_strategy_configs(user_id)`，直接回傳 Supabase `user_strategy_configs` 表裡「這個帳號已綁定」的列，**沒有任何自動補預設值的機制**（`backend/api.py:872-879` 的 docstring 明講：「新帳號常態是空 list，不代表出錯」）。這代表：**只新增 `STRATEGY_DEFS` 項目、不綁定 Supabase 的話，這個策略在 `allDefs.filter(...)` 這一步就會被濾掉，Dashboard 上完全不會顯示，開關也打不開。**

（先前一版分析誤以為「不綁定 Supabase 也能顯示、開關送 undefined 會退回程式碼預設值」——這個結論本身在後端邏輯上沒錯，但完全忽略了前端在那之前就已經把沒有綁定的策略整個過濾掉、根本不會顯示成一張卡片，被 Gemini review 抓到並經程式碼核對確認為真。）

**因此 Supabase 綁定是這次 Phase 2 的必要交付項目，不是可選**：部署完成後執行
```
python scripts/bind_strategy.py --user-id 01e79d6d-a686-4588-bdd2-5329512b97ef --strategy-id death_cross_short --no-reverse-signal-exit-enabled
```
`stop_loss_points`/`take_profit_points` 等數值欄位留空即可——留空時前端（`app.js:726-729`）送出 `undefined`，後端 `strategy_config_defaults()` 會退回 130/130 這組死叉專屬預設值。**但 `reverse_signal_exit_enabled` 這個布林欄位不能留空**（設計審查時發現並修正）：前端組裝啟動請求的程式碼是 `reverse_signal_exit_enabled: cfg.reverse_signal_exit_enabled !== false`（`app.js:733`）——留空/`null` 時 `null !== false` 結果是 `true`，會送出明確的 `true` 蓋掉後端的 `False` 預設值，導致金叉還是會平倉，跟已確認的「金叉忽略、只靠130點TP/SL」設計不符。`oco_enabled`/`soft_stop_enabled`/`risk_insurance_enabled` 這三個布林欄位用同一種前端寫法，但這三個死叉策略本來就要跟其他策略一樣預設開啟，留空剛好符合預期，不用額外處理。**不加 `--enabled`**（維持「只交付能力,預設不啟動」）。

這一步做完後，Supabase `user_strategy_configs` 表會跟另外 4 個策略一樣有一筆對應紀錄，Dashboard 上正常顯示第 5 張策略卡片，且反向出場行為正確關閉。

**順手優化（低風險,非必要)**：`frontend/js/app.js:256-259` 的 `STRATEGY_SHORT_CODE` 對照表（圖表買賣箭頭標籤用)目前只有 4 個既有策略的縮寫，沒有 `death_cross_short` 也不會壞（`STRATEGY_SHORT_CODE[sid] || sid` 有 fallback,只是箭頭文字會顯示完整 `death_cross_short` 而不是簡短代碼),補上 `death_cross_short: 'DS'` 讓圖表顯示更簡潔。

## 四、測試計畫

- `kline_engine.py`：`bar_close_time_from_ts(ts, minutes=15)`、`LiveKlineStore(interval_minutes=15)` 的日/夜盤根數驗證（跟 Phase 1 `resample_to_nmin` 測試同款手法）；`get_kline_store`/`get_store` 薄包裝行為一致性（`get_store` 结果等同 `get_kline_store(..., interval_minutes=60)`）。
- `capital_futures.py::_on_tick_for_kline()`：**關鍵安全測試**——mock 15 分鐘 store 讓它 `on_tick()` 直接拋例外，驗證 60 分鐘那行呼叫完全不受影響（呼叫過、資料正確落地）、且整個 `_on_tick_for_kline()` 不會往外拋例外。
- `strategy_service.py`：
  - `strategy_config_defaults("death_cross_short")` 回傳 `stop_loss_points=130.0`、`take_profit_points=130.0`、`reverse_signal_exit_enabled=False`；同一個測試裡也斷言 `strategy_config_defaults("breakout_long")`（或其他既有策略）回傳值與這次修改前完全一致，明確證明兩者互不污染。
  - `_load_bars`/`_bars_from_kline_store` 依 `state.strategy` 正確切換 15/60 分鐘 store。
  - 死叉策略的K棒充足門檻用 `ma_mid+2`（22 根），其他策略維持 `ma_slow+2`（62 根）不變的驗證；死叉策略在即時K棒不足時**不會**觸發 60 分鐘 CSV 合併分支（避免不同時間軸資料混在一起）。
  - 端到端模擬（比照這個 session 稍早修 9/17 事故時寫的 `test_full_session_switch_simulation_end_to_end` 手法）：假造 15 分鐘 K 棒序列出現死叉訊號 → `_tick_one()` 正確進場、掛 OCO（`stop_loss_points=130`/`take_profit_points=130`）、金叉出現時不平倉、SL/TP 觸發時正確出場。
- Supabase 綁定：手動驗證步驟（非自動化測試）——執行 `bind_strategy.py` 後，用瀏覽器登入 Dashboard 確認第 5 張策略卡片正常顯示、開關可以正常打開/關閉。

## 五、部署

交付：程式碼 + 測試 + Supabase 綁定（第三節指令）+ 重新打包的 `TMF-Trading-Setup.exe`。**不自動更新正在跑的 VM**，使用者自行決定何時 RDP 上去重裝。

## 六、明確排除項目（這次不做）

- 大台/小台即時交易（同 Phase 1 理由，OCO 只驗證過 TMF）。
- 日夜盤進場時段過濾（使用者最初規格提過，但 Phase 1/2 都還沒實作，留給以後有需要再加）。
- 其他參數調整探索（MA週期組合、不對稱SL/TP、斜率過濾嚴格度等——Phase 1 結束時討論過的優化方向，使用者選擇先照原始 130/130 規格上線，不在這次調參）。
