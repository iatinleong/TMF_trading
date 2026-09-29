# 死叉做空策略：前端 15分鐘/60分鐘 圖表切換 設計文件

## 背景與目標

Phase 2（`docs/superpowers/specs/2026-09-29-death-cross-live-trading-phase2-design.md`）把死叉做空策略接上即時交易後，發現一個真實的可觀測性落差：Dashboard 圖表（`frontend/index.html`/`app.js`)跟支援它的兩個端點（`/api/klines`、`/api/klines/signals`)都寫死只讀 60 分鐘 K 棒，死叉策略跑在後端獨立的 15 分鐘資料流上（Phase 2 Task 1 建的 `get_kline_store(product_code, interval_minutes=15)`），使用者完全看不到 15 分鐘K棒走勢，也看不到死叉訊號在圖上長什麼樣子——只能看到真正成交後的「DS 進場/出場」箭頭疊在 60 分鐘K棒背景上，看不出觸發當下的 15 分鐘 MA5/MA20 死叉形狀。

這份文件的目標：讓 Dashboard 圖表能切換顯示 15 分鐘或 60 分鐘K棒（含對應的 MA5/MA20/MA60 均線跟死叉訊號),使用者可以實際看到死叉策略在跑什麼，不用再盲操。

**這是 Phase 2 的延伸範圍（使用者已確認直接併入 Phase 2,不另外排隊)**，依賴 Phase 2 Task 1 的 `get_kline_store` 跟 Task 3 的 `generate_death_cross_signals`，兩者都已經完成。

## 一、後端：4 處改動，全部是既有介面的參數擴充

1. **`backend/live_service.py::get_klines()`** 加 `interval_minutes: int = 60` 參數：
   ```python
   def get_klines(product: str | None = None, limit: int = DEFAULT_KLINE_LIMIT, interval_minutes: int = 60) -> list[dict[str, Any]]:
       code = resolve_quote_product_code(product or current_product)
       return get_kline_store(code, interval_minutes=interval_minutes).get_klines(limit=limit)
   ```
   （原本呼叫 `get_store(code)`，改呼叫 Phase 2 Task 1 已經建好的 `get_kline_store`；預設值 60 保證所有既有呼叫端行為不變。）

2. **`backend/api.py`**：
   - `/api/klines` 加 `interval: int = 60` query 參數，往下傳給 `get_klines(prod, limit=..., interval_minutes=interval)`。
   - **`/api/klines/signals` 也要加 `interval: int = 60`**（這是設計審查時 Gemini 抓到、我原本遺漏的一步：只改 `klines_with_signals()` 函式本身沒有用，呼叫它的這個端點如果沒有接收並往下傳這個參數，函式永遠只會收到預設值 60，均線/死叉訊號不會真的跟著切換),往下傳給 `klines_with_signals(prod, limit=..., interval_minutes=interval)`。

3. **`backend/strategy_service.py::klines_with_signals()`** 加 `interval_minutes: int = 60`：
   ```python
   def klines_with_signals(product_code: str, limit: int = 500, interval_minutes: int = 60) -> list[dict[str, Any]]:
       contract = _contract_from_product(product_code)
       bars = _load_bars(
           contract, product_code=product_code,
           interval_minutes=interval_minutes,
           min_bars_required=(DEFAULT_STRATEGY.ma_mid + 2) if interval_minutes != 60 else None,
       )
       if bars.empty:
           return []
       bars = bars.tail(limit)

       breakout = _signaled_frame("breakout", bars)
       pullback = _signaled_frame("pullback", bars)
       death_cross = _signaled_frame("death_cross", bars) if interval_minutes != 60 else None
       ...
   ```
   `interval_minutes=60`（預設)時行為完全不變（只有 breakout/pullback,沒有 death_cross_signal 欄位);`interval_minutes=15` 時額外算 `death_cross_signal`，回傳的每列多一個 `death_cross_signal` 欄位（沿用 Phase 1 已經寫好的 `generate_death_cross_signals`)。

4. **`backend/live_service.py::poll_once()`** 的 WebSocket 推播封包，在現有 `"kline": kline`（60分）旁邊多加一個 `"kline_15m": kline_15m`：
   ```python
   klines = get_klines(quote_product, limit=1)
   kline = klines[-1] if klines else None
   klines_15m = get_klines(quote_product, limit=1, interval_minutes=15)
   kline_15m = klines_15m[-1] if klines_15m else None
   return {
       "type": "tick",
       ...
       "kline": kline,
       "kline_15m": kline_15m,
       ...
   }
   ```
   兩個都送，不依訂閱者目前在看哪個時間軸決定要不要算——`ws_clients` 本來就是 `product_code -> WebSocket set`，不是時間軸感知的，依訂閱狀態決定算哪個會增加複雜度；`get_kline_store(...).get_klines(limit=1)` 是本機字典查詢，不是外部呼叫，兩個都算成本可忽略。

## 二、前端：圖表切換按鈕

**`frontend/index.html`**：圖表面板頂部加兩個按鈕「15分」「60分」（簡單的 toggle,不需要更複雜的 UI 元件)。

**`frontend/js/app.js`**：
- 新增全域狀態 `let currentInterval = 60;`（預設 60，保證頁面剛載入時行為跟現在完全一樣)。
- `loadKlines()`/`loadSignals()` 呼叫 `/api/klines`/`/api/klines/signals` 時帶上 `&interval=${currentInterval}`。
- 切換按鈕的 click handler：設定 `currentInterval`、清空 `chartInitialFitted`（讓圖表重新 fit 一次可視範圍),重新呼叫 `loadKlines()` 完整重繪。
- `ws.onmessage` 現有的 `candleSeries.update({ ...msg.kline })`（第359行附近)改成依 `currentInterval` 決定套用 `msg.kline`（60)還是 `msg.kline_15m`（15)：
  ```js
  const liveKline = currentInterval === 15 ? msg.kline_15m : msg.kline;
  if (liveKline) candleSeries.update({ ...liveKline });
  ```
- MA5/MA20/MA60 線（`maFastSeries`/`maMidSeries`/`maSlowSeries`)本來就是從 `loadSignals()` 的回傳資料算的，切換時會跟著 `loadSignals()` 重新抓、重新畫，不用額外處理。

## 三、測試計畫

- `backend/live_service.py::get_klines(interval_minutes=15)` 讀的是 15 分鐘 store，`interval_minutes=60`（預設)行為不變。
- `backend/strategy_service.py::klines_with_signals(interval_minutes=15)` 回傳列多出 `death_cross_signal` 欄位；`interval_minutes=60`（預設)回傳結構跟改動前一致（不會多出這個欄位，避免影響既有前端邏輯的欄位假設)。
- `/api/klines`、`/api/klines/signals` 兩個端點接收 `interval` query 參數並正確往下傳。
- `poll_once()` 回傳的字典同時包含 `kline`（60分)跟 `kline_15m`（15分)兩個欄位。
- 全套現有 242 個測試持續全過（不動任何既有行為的預設值)。

## 四、明確不在範圍內

- 不新增比 15/60 分鐘更多的時間軸選項（例如 5分、30分)——這次只解決死叉策略需要被看見這個具體問題，不做成通用的任意週期選擇器。
- 不改動大台/小台圖表顯示（這幾個商品目前不支援死叉/15分鐘,同 Phase 1/2 一貫理由)。
